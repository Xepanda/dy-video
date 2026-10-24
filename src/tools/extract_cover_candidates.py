#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
extract_cover_candidates.py - 封面正面候选帧智能提取与标准化构图工具（v2 重构）
===================================================================================
依据 2026-10-20 需求决策重构：

1. 严格正面姿态断言 (Strict Frontal Check):
   - 人脸偏航角 <= 12°（detect_face, solvePnP）——纪要"主播表情正常"的量化门禁（主判据）
   - 双肩深度差 |ls.z - rs.z|：软罚分（> shoulder_dz_soft 扣分），
     仅极端侧身（> shoulder_dz_max）硬排除（2026-10-21 泛化验收修正：
     实测 6 号素材主播重心切换/侧身展示时 dz 中位数 0.26，原 0.055 硬门禁误杀全部候选；
     纪要正面要求的原文为"表情正常"，即 face_yaw/EAR/MAR，双肩 dz 仅为实现启发式）
   - 遮挡检查：手腕高过鼻梁（挡脸风险）才判定；抬手展示姿态不误判
   - 可见性检查（双踝可见）
2. 水平 1:6:1 标准化构图（决策 1，替代旧版垂直 74% 方案）:
   - 主约束：人体轮廓宽 = 画布宽 75%（810px），左右边距各 135px（1:6:1）
   - 上限约束：人体高 <= 画布高 88%（与"完整人体露膝"冲突时的退让）
   - 头顶预留 >= 13% 画布高（避让品牌区）
   - 宽高双约束取 min 缩放，按人体中心 x 平移居中
   - 放大后清晰度门禁 post_scale_sharpness >= 200（防上采样糊脸）
3. 人脸质量门禁（detect_face）: 偏航角/EAR/MAR/人脸清晰度/人脸面积占比
4. 衣着一致性（detect_garment_match）: 与商品图匹配分（WARN 级记录）
5. 多目标候选评分: 宽占比偏差 + 表情/偏航 + 清晰度 + 遮挡罚分
6. 输出: 规范化候选图片集 + cover_candidates.json（含全部 metrics 供 Agent 裁决与 QC 复测）
"""

import os
import sys
import json
import argparse
from typing import List, Dict, Any, Optional, Tuple

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python
from mediapipe.tasks.python import vision

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.tools.detect_face import FaceAnalyzer
from src.tools.detect_garment_match import (
    garment_match_score, check_constraints as garment_check)
from src.tools.detect_ocr import check_background_text

POSE_MODEL_PATH = os.path.join(PROJECT_ROOT, "data", "pose_landmarker_lite.task")
OUT_W, OUT_H = 1080, 1920

# 构图目标（决策 1：水平 1:6:1；版式复刻会议纪要示例图 2026-10-20）
# ⚠️ 这些常量只是**缺省值**。真实取值来自 contracts/constraints.yaml 的 cover 节
#    （由 normalize_subject_framing 的 framing_params 参数注入），
#    改 yaml 才会真正生效——2026-10-24 修复前它们是硬编码的死值。
PERSON_WIDTH_RATIO = 0.75      # 人体宽占画布 75%（810px）
PERSON_WIDTH_TOL = 0.03        # 允许 [0.72, 0.78]
PERSON_HEIGHT_MAX = 0.80       # 人体高上限（示例版式：为底部商品名区预留空间）
FEET_TARGET_RATIO = 0.78       # 脚底目标位置（名称区顶部 0.80 上方）
HEAD_FLOOR_RATIO = 0.155       # 头顶预留下限（避让品牌区）
HEAD_TOP_MIN_RATIO = 0.13      # 头顶占比下限（width_priority 策略用）
KNEE_Y_MAX = 0.92              # C5 膝盖纵坐标上限（与 constraints cover.knee_y_max 一致）
ANKLE_Y_MAX = 0.99             # C5 踝部纵坐标上限（几何判据：踝在画面内）
KNEE_VIS_MIN = 0.30            # C5 膝盖可见性下限（模型置信度卫生检查）
# 构图策略（见 normalize_subject_framing 的几何取舍说明）：
#   example        —— 示例版式优先：脚底 78% + 名称区独立（默认）
#   width_priority —— 决策 1 优先：人宽锁定 75%，身高上限 88%（腿部可进入名称区）
FRAMING_POLICY = "example"
SHOULDER_DZ_SOFT = 0.055       # 双肩深度差软阈值（超过则评分罚分；阈值见 constraints cover.shoulder_dz_soft）
SHOULDER_DZ_MAX = 0.45         # 双肩深度差硬上限（极端侧身排除；阈值见 constraints cover.shoulder_dz_max）


def _load_constraints(path: str = None) -> Dict[str, Any]:
    path = path or os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
    if not os.path.exists(path):
        return {}
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------- 构图引擎

def normalize_subject_framing(
    frame_bgr: np.ndarray,
    landmarks,
    seg_mask: Optional[np.ndarray] = None,
    policy: str = None,
    framing_params: Optional[Dict[str, Any]] = None,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """水平 1:6:1 主约束 + 身高上限的双约束标准化构图。

    seg_mask: MediaPipe PoseLandmarker 的分割掩码（float32, HxW, 0~1），
              用于精确测量人体轮廓宽度；为空时退化为 pose bbox 宽度。
    policy:   "example"（默认，脚底 78% + 名称区独立）| "width_priority"
              （决策 1 优先：人宽锁定 75%，身高上限 88%）。
    framing_params: 构图阈值（来自 constraints.cover），缺省用模块常量。
              **2026-10-24**：此前这些值硬编码在模块常量里，改 constraints.yaml
              无效（实测注入 person_width_ratio=0.20 代码仍用 0.75），
              违反"阈值唯一来源"原则。现改为从 constraints 注入。
    """
    policy = policy or FRAMING_POLICY
    p = dict(framing_params or {})
    person_width_ratio = float(p.get("person_width_ratio", PERSON_WIDTH_RATIO))
    feet_target_ratio = float(p.get("feet_target_ratio", FEET_TARGET_RATIO))
    head_floor_ratio = float(p.get("head_floor_ratio", HEAD_FLOOR_RATIO))
    head_top_min_ratio = float(p.get("head_top_min_ratio", HEAD_TOP_MIN_RATIO))
    person_height_max = float(p.get("person_height_max_ratio", PERSON_HEIGHT_MAX))
    h, w = frame_bgr.shape[:2]
    nose, ls, rs = landmarks[0], landmarks[11], landmarks[12]
    la, ra = landmarks[27], landmarks[28]
    lk, rk = landmarks[25], landmarks[26]

    # ---- 人体垂直范围（头顶估算 + 脚底），像素域 ----
    shoulder_y = (ls.y + rs.y) / 2.0
    head_top_norm = max(0.0, nose.y - (shoulder_y - nose.y) * 1.30)
    feet_bottom_norm = min(1.0, max(la.y, ra.y) + 0.04)
    person_h = max(100.0, (feet_bottom_norm - head_top_norm) * h)  # 像素，下限 100px
    center_x = (ls.x + rs.x) / 2.0 * w

    # ---- 人体宽度测量 ----
    if seg_mask is not None:
        # 掩码法：人体垂直范围内，逐行取最宽轮廓
        y0 = int(head_top_norm * h)
        y1 = int(feet_bottom_norm * h)
        y0, y1 = max(0, y0), min(h, y1)
        if y1 - y0 >= 10:
            region = seg_mask[y0:y1, :] > 0.5
            widths = region.sum(axis=1)
            person_w = float(widths.max()) if widths.max() > 0 else 0.0
        else:
            person_w = 0.0
    else:
        person_w = 0.0
    if person_w < 40:  # 掩码不可用 → pose bbox 宽（含脚踝跨步）
        xs = [p.x for p in [ls, rs, landmarks[23], landmarks[24], la, ra]]
        person_w = (max(xs) - min(xs)) * w

    # ---- 宽高双约束（示例版式：脚底贴 78%，为名称区预留 80% 以下空间）----
    # 几何取舍说明（2026-10-21）：
    #   决策 1"人宽 75%"与 v3 示例版式"脚底 78% + 名称区 80% 起"在数学上不可兼得——
    #   直播主播体型宽高比约 0.50，人宽 810px 需身高 ≈1600px，而"头顶 15.5%→脚底 78%"
    #   仅 1200px 可用。故提供两种可切换策略：
    #     example     （默认，示例优先）：保全完整人体 + 名称区独立，宽占比实测 0.38~0.56，
    #                   偏差由 QC 记 WARN（纪要"尽力居中"语义）
    #     width_priority（决策 1 优先）：人宽锁定 75%，身高放宽至 88%，允许腿部进入名称区
    #                   （名称区覆盖在人像之上），偏差由 QC 记 WARN
    target_w = OUT_W * person_width_ratio            # 默认 810px
    scale_w = target_w / max(1.0, person_w)
    avail_h = (feet_target_ratio - head_floor_ratio) * OUT_H  # 品牌区底部→脚底目标
    scale_h = min(avail_h / max(1.0, person_h),
                  OUT_H * person_height_max / max(1.0, person_h))
    if policy == "width_priority":
        # 决策 1 优先：宽度为主约束，身高上限放宽到 88%，头顶下压到 13%
        scale_h = OUT_H * 0.88 / max(1.0, person_h)
        scale = scale_w
    else:
        scale = min(scale_w, scale_h)

    new_w = int(w * scale)
    new_h = int(h * scale)
    resized = cv2.resize(frame_bgr, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)

    res_head_top_y = head_top_norm * h * scale
    res_center_x = center_x * scale
    res_person_h = person_h * scale

    # 水平：人体中心锁定 x = 540（1:6:1 → 左右各 135px）
    x_offset = int(OUT_W * 0.5 - res_center_x)
    # 垂直：脚底贴 feet_target（名称区上方），头顶不低于品牌区底部
    y_offset = int(feet_target_ratio * OUT_H - res_person_h)
    y_offset = max(y_offset, int(head_floor_ratio * OUT_H))

    canvas = np.zeros((OUT_H, OUT_W, 3), dtype=np.uint8)

    src_x1 = max(0, -x_offset)
    src_y1 = max(0, -y_offset)
    src_x2 = min(new_w, OUT_W - x_offset)
    src_y2 = min(new_h, OUT_H - y_offset)
    dst_x1 = max(0, x_offset)
    dst_y1 = max(0, y_offset)
    dst_x2 = dst_x1 + (src_x2 - src_x1)
    dst_y2 = dst_y1 + (src_y2 - src_y1)
    canvas[dst_y1:dst_y2, dst_x1:dst_x2] = resized[src_y1:src_y2, src_x1:src_x2]

    # 左右边缘自适应镜像填充（消除黑边；带退化几何保护）
    if dst_x1 > 0 and (dst_x2 - dst_x1) > 0:
        edge_w = min(dst_x1, dst_x2 - dst_x1)
        edge = canvas[dst_y1:dst_y2, dst_x1:dst_x1 + edge_w]
        if edge.shape[1] > 0:
            canvas[dst_y1:dst_y2, 0:dst_x1] = cv2.flip(edge, 1)[:, -dst_x1:]
    if dst_x2 < OUT_W and (dst_x2 - dst_x1) > 0:
        gap = OUT_W - dst_x2
        edge = canvas[dst_y1:dst_y2, max(dst_x1, dst_x2 - gap):dst_x2]
        if edge.shape[1] > 0:
            canvas[dst_y1:dst_y2, dst_x2:OUT_W] = cv2.flip(edge, 1)[:, :gap]

    # 视觉醒目度提升：反差与微锐化
    blur = cv2.GaussianBlur(canvas, (0, 0), 2.2)
    enhanced = cv2.addWeighted(canvas, 1.25, blur, -0.25, 0)

    # ---- 量化指标 ----
    # 放大后清晰度：在最终画布的人体区域测量 Laplacian
    px0 = max(0, dst_x1)
    px1 = min(OUT_W, dst_x2)
    py0 = max(0, dst_y1)
    py1 = min(OUT_H, dst_y2)
    person_region = enhanced[py0:py1, px0:px1] if px1 > px0 and py1 > py0 else enhanced
    gray = cv2.cvtColor(person_region, cv2.COLOR_BGR2GRAY)
    post_scale_sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    achieved_width = person_w * scale
    achieved_height = res_person_h
    knees_visible = bool(lk.y <= KNEE_Y_MAX and rk.y <= KNEE_Y_MAX
                         and getattr(lk, "visibility", 1.0) > KNEE_VIS_MIN
                         and getattr(rk, "visibility", 1.0) > KNEE_VIS_MIN)
    ankles_visible = bool(la.y <= ANKLE_Y_MAX and ra.y <= ANKLE_Y_MAX)

    framing_meta = {
        "scale": round(scale, 3),
        "framing_policy": policy,
        "person_width_px_src": round(person_w, 1),
        "achieved_width_px": round(achieved_width, 1),
        "achieved_width_ratio": round(achieved_width / OUT_W, 4),
        "achieved_height_ratio": round(achieved_height / OUT_H, 4),
        "width_constraint_active": scale_w <= scale_h,
        "head_top_y_px": int(max(0, dst_y1)),
        "center_x_px": int(OUT_W * 0.5),
        "knees_visible": knees_visible,
        "ankles_visible": ankles_visible,
        "post_scale_sharpness": round(post_scale_sharpness, 1),
    }
    # 几何张力记录：身高约束压制宽度目标时给出可解释 note（QC WARN 依据）
    if not framing_meta["width_constraint_active"]:
        framing_meta["width_note"] = (
            f"身高约束优先（保全完整人体），宽占比 {framing_meta['achieved_width_ratio']:.2f} "
            f"未达目标 0.75（纪要'尽力居中'语义）")
    return enhanced, framing_meta


# ---------------------------------------------------------------- 候选提取

def extract_cover_candidates(
    video_path: str,
    output_dir: str,
    spec_data: Dict[str, Any] = None,
    constraints: Dict[str, Any] = None,
    top_k: int = 5,
    sample_step_sec: float = 1.5,
    scan_limit_sec: float = 180.0,
    cache_key: str = None,
    cache_work_dir: str = None,
) -> List[Dict[str, Any]]:
    """扫描视频提取正面候选帧并统一比例输出（v2：水平 1:6:1 + 人脸门禁 + 衣着检测）。

    cache_key + cache_work_dir 提供时启用阶段缓存（B13）：清单与图片均命中才跳过重算。
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"视频不存在: {video_path}")

    if cache_key and cache_work_dir:
        from src.tools.stage_cache import load_stage, save_stage
        cached = load_stage(cache_work_dir, "cover_candidates", cache_key)
        if cached is not None and all(
                os.path.exists(c.get("image_path", "")) for c in cached):
            print(f"[EXTRACT-COVER v2] 命中阶段缓存（{len(cached)} 个候选）")
            # 同步缓存清单到当前 output_dir（不同 project 复用同一视频时）
            os.makedirs(output_dir, exist_ok=True)
            with open(os.path.join(output_dir, "cover_candidates.json"), "w",
                      encoding="utf-8") as f:
                json.dump(cached, f, ensure_ascii=False, indent=2)
            return cached

    constraints = constraints or _load_constraints()
    cover_c = constraints.get("cover", {}) or {}
    garment_c = constraints.get("garment_match", {}) or {}
    spec_data = spec_data or {}

    top_k = top_k or int(cover_c.get("top_k", 5))
    sample_step_sec = sample_step_sec or float(cover_c.get("scan_step_sec", 1.5))
    scan_limit_sec = scan_limit_sec or float(cover_c.get("scan_limit_sec", 180))
    sharp_min = float(cover_c.get("sharpness_min", 200))  # 放大后清晰度门禁（post_scale）
    sharp_floor = float(cover_c.get("sharp_admission_min", 80))       # 扫描绝对地板
    sharp_ratio = float(cover_c.get("sharp_relative_ratio", 0.8))     # 相对素材分位校准
    shoulder_dz_soft = float(cover_c.get("shoulder_dz_soft", SHOULDER_DZ_SOFT))
    shoulder_dz_max = float(cover_c.get("shoulder_dz_max", SHOULDER_DZ_MAX))
    knee_y_max = float(cover_c.get("knee_y_max", 0.92))
    framing_policy = str(cover_c.get("framing_policy", "example"))
    knee_vis_min = float(cover_c.get("knee_visibility_min", 0.30))
    ankle_y_max = float(cover_c.get("ankle_y_max", 0.99))

    os.makedirs(output_dir, exist_ok=True)

    # 商品图（衣着一致性用）
    # 注意（2026-10-24）：占位示例图**不可用于衣着匹配**——占位图是灰蓝斜纹底，
    # 与直播画面的暖色调躯干做直方图/CLIP 比对必然得 0 分，会把"缺商品图"
    # 误报成"衣着不一致"。因此这里识别 spec.product.placeholder_images 标记，
    # 命中则跳过衣着匹配（记为 not_evaluable），等真实商品图到位后再自动生效。
    product = spec_data.get("product", {}) or {}
    item_images = [p for p in product.get("item_images", []) if p and os.path.exists(p)]
    is_placeholder = bool(product.get("placeholder_images"))
    product_image = item_images[0] if (item_images and not is_placeholder) else None
    if is_placeholder:
        print("[EXTRACT-COVER v2] 商品图为占位示例图：跳过衣着一致性匹配"
              "（拿到真实商品图后自动生效）")

    pose_options = vision.PoseLandmarkerOptions(
        base_options=python.BaseOptions(model_asset_path=POSE_MODEL_PATH),
        output_segmentation_masks=True,   # v2：启用分割掩码测人体宽
        num_poses=1,
    )
    pose_detector = vision.PoseLandmarker.create_from_options(pose_options)
    face_analyzer = FaceAnalyzer()

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    print(f"[EXTRACT-COVER v2] 扫描前 {min(total_frames / fps, scan_limit_sec):.1f}s"
          f"（步长 {sample_step_sec}s）：严格正面 + 人脸质量 + 水平 1:6:1 构图")

    raw_candidates = []
    all_frame_sharps = []   # 全部采样帧清晰度（相对阈值校准用）
    curr_t = 1.0
    video_dur = total_frames / fps if fps > 0 else scan_limit_sec
    scan_end = min(video_dur, scan_limit_sec)
    extended = False
    hard_cap = float(cover_c.get("scan_hard_cap_sec", 600))
    while True:
      while curr_t < scan_end:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(curr_t * fps))
        ret, frame = cap.read()
        if not ret or frame is None:
            curr_t += sample_step_sec
            continue

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        res = pose_detector.detect(mp_img)

        if not res.pose_landmarks:
            curr_t += sample_step_sec
            continue

        lm = res.pose_landmarks[0]
        ls, rs = lm[11], lm[12]
        la, ra = lm[27], lm[28]
        lk, rk = lm[25], lm[26]

        # 1. 正面姿态：双肩深度差仅极端侧身硬排除（软罚分见评分段）；
        #    正面性主判据为人脸偏航角（solvePnP，见步骤 4）
        dz_shoulder = abs(ls.z - rs.z)
        if dz_shoulder > shoulder_dz_max:
            curr_t += sample_step_sec
            continue

        # 2. 遮挡与 C5 完整人体（几何判据）：
        #    腕高过鼻梁=挡脸风险（抬手展示姿态不误判）；
        #    踝部"可见性"是模型置信度而非遮挡度量，画面底部边缘 landmark
        #    置信度天然偏低（实测 6 号素材踝部 vis 中位 0.40 但鞋完整可见），
        #    v1.1 起踝部改用几何判据：踝在画面内 + 膝在踝上方
        knees_ok = (lk.y <= knee_y_max and rk.y <= knee_y_max
                    and getattr(lk, "visibility", 1.0) > knee_vis_min
                    and getattr(rk, "visibility", 1.0) > knee_vis_min)
        ankles_in_frame = (la.y <= ankle_y_max and ra.y <= ankle_y_max)
        nose_y = lm[0].y
        occluded = (lm[15].y < nose_y or lm[16].y < nose_y)
        if occluded or not ankles_in_frame or not knees_ok:
            curr_t += sample_step_sec
            continue

        # 3. 清晰度（两级：低绝对地板排除运动模糊垃圾帧 + 相对该素材分位校准）。
        #    依据：实测各素材全帧 Laplacian 中位数差异大（demo 244 / 9号 182 /
        #    8号 144 / 7号 96），绝对 200 会误杀软素材 97% 帧；真正的防糊门禁是
        #    归一化放大后的 post_scale_sharpness（≥200，BLOCK，见评分段与 B12 网关）。
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        all_frame_sharps.append(sharp)
        if sharp < sharp_floor:
            curr_t += sample_step_sec
            continue

        # 4. 人脸质量门禁（姿态引导 ROI）
        face_m = face_analyzer.analyze(frame, pose_landmarks=lm)
        face_passed, face_failed = face_analyzer.check_constraints(face_m, cover_c)
        if not face_passed:
            curr_t += sample_step_sec
            continue

        # 5. 衣着一致性（WARN 级，不拦截；但阈值必须真正生效）
        #    修复（2026-10-24）：此前 garment_check 被 import 却从未调用，
        #    导致 constraints 的 histogram_min / garment_match_min 阈值形同虚设。
        #    现在真正执行约束校验，并把 passed/failed_keys/level 写进候选元数据。
        garment_result = None
        if product_image:
            garment_result = garment_match_score(frame, product_image, lm,
                                                 engine=garment_c.get("engine", "auto"))
            g_passed, g_failed, g_level = garment_check(garment_result, garment_c)
            garment_result["passed"] = bool(g_passed)
            garment_result["failed_keys"] = g_failed
            garment_result["level"] = g_level

        # 6. 构图归一化（水平 1:6:1）
        seg_mask = None
        if res.segmentation_masks:
            m = res.segmentation_masks[0]
            seg_mask = m.numpy_view() if hasattr(m, "numpy_view") else np.asarray(m)
            if seg_mask.shape[:2] != frame.shape[:2]:
                seg_mask = cv2.resize(seg_mask, (frame.shape[1], frame.shape[0]))
        norm_frame, f_meta = normalize_subject_framing(
            frame, lm, seg_mask, policy=framing_policy, framing_params=cover_c)

        # 7. 多目标评分
        width_target = float(cover_c.get("person_width_ratio", PERSON_WIDTH_RATIO))
        width_tol = float(cover_c.get("person_width_tolerance", PERSON_WIDTH_TOL))
        width_dev = abs(f_meta["achieved_width_ratio"] - width_target)
        s_width = max(0.0, 1.0 - width_dev / width_tol) * 35.0
        s_sharp = min(30.0, sharp / 15.0)
        s_face = 20.0 if face_passed else 0.0
        s_yaw = max(0.0, 1.0 - face_m["yaw_deg"] / cover_c.get("face_yaw_max_deg", 12)) * 15.0
        # 身体正面度软罚分（双肩深度差越小越正面；不再硬门禁，见模块 docstring）
        s_frontal = max(0.0, 1.0 - dz_shoulder / shoulder_dz_max) * 10.0
        score = s_width + s_sharp + s_face + s_yaw + s_frontal
        if not f_meta["knees_visible"]:
            score -= 25.0
        if f_meta["post_scale_sharpness"] < sharp_min:
            score -= 20.0  # 放大糊脸罚分

        raw_candidates.append({
            "timestamp": round(curr_t, 2),
            "frame": frame.copy(),
            "landmarks": lm,
            "frontal_score": round(score, 1),
            "dz_shoulder": round(dz_shoulder, 3),
            "sharpness": round(sharp, 1),
            "face": face_m,
            "face_passed": face_passed,
            "garment": garment_result,
            "framing": f_meta,
            "norm_frame": norm_frame,
        })
        curr_t += sample_step_sec

      # 内层扫描窗结束：候选数够填满 Top-K 时才停止扩扫描
      # 2026-10-24 修复：原条件 `len(raw_candidates) >= 2` 一旦满足就停止扩扫，
      # 导致**首窗只有 2 个候选时，180s 之后的画面永不参与竞争**——
      # 而最终要选 Top-K(默认5) 个，候选不够只能硬凑，可能错过后段更好的正脸帧。
      # 现改为：候选数达到 top_k 才停（够填满才不必再扫），
      # 兼顾覆盖度与扫描成本；硬上限 hard_cap 兜底。
      if extended or len(raw_candidates) >= top_k or scan_end >= hard_cap \
              or scan_end >= video_dur - 1:
          break
      new_end = min(video_dur, max(scan_limit_sec * 2, scan_end + scan_limit_sec), hard_cap)
      if new_end <= scan_end:
          break
      scan_end = new_end
      extended = True
      print(f"[EXTRACT-COVER v2] 自适应扩扫描至 {scan_end:.0f}s"
            f"（视频全长 {video_dur:.0f}s，硬上限 {hard_cap:.0f}s，"
            f"当前候选 {len(raw_candidates)}/{top_k} 个）")

    cap.release()

    # 3.5 清晰度相对校准（素材分位自适应）：admission = max(地板, 中位数×ratio)。
    #      软素材（中位 96）admission≈80 放过清晰帧；硬素材（中位 244）admission≈195
    #      与原绝对 200 行为一致——demo 无回归。
    if all_frame_sharps:
        p50 = float(np.median(all_frame_sharps))
        admission = max(sharp_floor, p50 * sharp_ratio)
        before = len(raw_candidates)
        raw_candidates = [c for c in raw_candidates if c["sharpness"] >= admission]
        dropped = before - len(raw_candidates)
        if dropped:
            print(f"[EXTRACT-COVER v2] 清晰度相对门禁: 素材中位 {p50:.0f} → "
                  f"准入线 {admission:.0f}，淘汰 {dropped} 个偏糊候选")
        else:
            print(f"[EXTRACT-COVER v2] 清晰度相对门禁: 素材中位 {p50:.0f} → 准入线 {admission:.0f}")

    raw_candidates.sort(key=lambda x: x["frontal_score"], reverse=True)
    selected = raw_candidates[:top_k]

    # 7.5 封面背景文字纯净度门禁（B11，V7）：对 Top-K 归一化底图执行 OCR，
    #     背景文字（店招/背景板）面积占比超限的候选淘汰；全部超限时保留
    #     文字最少的候选并标记 ocr_fallback（QC 阶段独立复测，BLOCK 拦截）。
    #     注意：selected 为空（全部门禁无候选）时直接放行到上层，
    #     由决策网关给出明确的"候选清单为空"拦截信息。
    ocr_c = constraints.get("ocr", {}) or {}
    if selected and ocr_c.get("engine"):
        ocr_evaluated = []
        for item in selected:
            passed, metrics = check_background_text(item["norm_frame"], constraints)
            item["background_text"] = metrics
            ocr_evaluated.append((item, passed))
        clean = [it for it, ok in ocr_evaluated if ok]
        if clean:
            eliminated = len(selected) - len(clean)
            selected = clean
            print(f"[EXTRACT-COVER v2] 背景文字门禁: {len(clean)}/{len(clean)+eliminated} "
                  f"个候选通过（淘汰 {eliminated} 个含店招/背景文字候选）")
        else:
            selected = [min(ocr_evaluated, key=lambda t: t[0]["background_text"]["ratio"])[0]]
            selected[0]["ocr_fallback"] = True
            print(f"[EXTRACT-COVER v2] 背景文字门禁: 全部候选超限，保留文字最少候选 "
                  f"(ratio={selected[0]['background_text']['ratio']}) 并标记 ocr_fallback")

    result_manifest = []
    print(f"[EXTRACT-COVER v2] 筛选出 {len(selected)} 个高标准正面候选帧（水平 1:6:1 归一化）:")

    for i, item in enumerate(selected):
        cand_id = f"cand_{i+1:02d}"
        img_name = f"{cand_id}_t{item['timestamp']:.1f}s.jpg"
        img_path = os.path.join(output_dir, img_name)
        cv2.imwrite(img_path, item["norm_frame"])

        cand_data = {
            "id": cand_id,
            "timestamp": item["timestamp"],
            "image_path": img_path,
            "image_filename": img_name,
            "frontal_score": item["frontal_score"],
            "dz_shoulder": item["dz_shoulder"],
            "sharpness": item["sharpness"],
            "face": item["face"],
            "garment_match": (item["garment"] or {}).get("score"),
            "garment_engine": (item["garment"] or {}).get("engine"),
            "garment_passed": (item["garment"] or {}).get("passed"),
            "garment_level": (item["garment"] or {}).get("level"),
            "garment_failed_keys": (item["garment"] or {}).get("failed_keys", []),
            "garment_not_evaluable": item["garment"] is None and is_placeholder,
            "background_text_ratio": (item.get("background_text") or {}).get("ratio"),
            "background_text_passed": (item.get("background_text") or {}).get("passed"),
            "ocr_fallback": bool(item.get("ocr_fallback", False)),
            "framing": item["framing"],
            "description": (
                f"严格正面立姿 @{item['timestamp']:.1f}s, 人像宽占比 "
                f"{item['framing']['achieved_width_ratio']:.2f} (目标 0.75), "
                f"偏航 {item['face']['yaw_deg']}°, 放大后清晰度 "
                f"{item['framing']['post_scale_sharpness']}"
            ),
        }
        result_manifest.append(cand_data)
        print(f"  [{cand_id}] @{item['timestamp']:.1f}s | 分 {item['frontal_score']} | "
              f"宽占比 {item['framing']['achieved_width_ratio']:.3f} | "
              f"yaw {item['face']['yaw_deg']}° | {img_path}")

    manifest_path = os.path.join(output_dir, "cover_candidates.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(result_manifest, f, ensure_ascii=False, indent=2)
    print(f"[EXTRACT-COVER v2] 候选清单已生成: {manifest_path}")

    # 衣着一致性汇总（阈值已真正生效，此处只做可见性提示，不淘汰候选）
    if is_placeholder:
        print("[EXTRACT-COVER v2] 衣着一致性: 未评估（商品图为占位示例图）")
    elif selected and selected[0].get("garment"):
        gs = [(c.get("garment_match"), c.get("garment_passed")) for c in result_manifest]
        ok = sum(1 for _, p in gs if p)
        eng = result_manifest[0].get("garment_engine")
        thr = garment_c.get("garment_match_min", 0.75) if eng == "clip" \
            else garment_c.get("histogram_min", 0.15)
        print(f"[EXTRACT-COVER v2] 衣着一致性: {ok}/{len(gs)} 个候选达标"
              f"（引擎 {eng}，阈值 {thr}）")
        for c in result_manifest:
            if c.get("garment_passed") is False:
                print(f"    [WARN {c.get('garment_level')}] {c['id']} 衣着不匹配: "
                      f"score={c.get('garment_match')} {c.get('garment_failed_keys')}")
    if cache_key and cache_work_dir:
        from src.tools.stage_cache import save_stage
        save_stage(cache_work_dir, "cover_candidates", cache_key, result_manifest)
    return result_manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="正面封面底图候选帧提取与水平 1:6:1 标准化工具")
    parser.add_argument("--video", required=True, help="视频素材路径")
    parser.add_argument("--spec", default="job_spec.json", help="任务契约（衣着检测用）")
    parser.add_argument("--constraints", default="contracts/constraints.yaml")
    parser.add_argument("--out-dir", default="data/candidates/cover_candidates")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    spec = {}
    if os.path.exists(args.spec):
        with open(args.spec, "r", encoding="utf-8") as f:
            spec = json.load(f)
    extract_cover_candidates(
        args.video, args.out_dir, spec_data=spec,
        constraints=_load_constraints(args.constraints), top_k=args.top_k)
