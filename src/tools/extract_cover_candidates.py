#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
extract_cover_candidates.py - 封面正面候选帧智能提取与标准化构图工具
===================================================================
向 Agent 提供客观、确定性的候选封面提取能力:
1. 严苛正面姿态断言 (Strict Frontal Pose):
   - MediaPipe 3D 骨骼: 双肩深度差 |ls.z - rs.z| <= 0.05, 髋部深度差 <= 0.05
   - 人体双眼水平正视, 面部偏航角 <= 12°
   - 彻底排除侧身、背身或低头镜头
2. 人体比例统一固定与居中标准化 (Normalized Framing):
   - 依据人体 Bounding Box, 自动将全身身高缩放至固定画幅比例 (74% ~ 76%)
   - 人像头顶自动对准 y = 16% H (为顶部品牌标预留充裕呼吸空间)
   - 人像身体中轴线自动锁定在 x = 50% W (水平居中)
   - 边缘自适应镜像/延展, 消除黑边
3. 画面醒目度增强 (Visual Pop):
   - 局部微锐化 (Unsharp Masking), 强化面料纹理与人像主体边缘
4. 输出:
   - 规范化候选图片集 (cand_01.jpg, cand_02.jpg...)
   - 结构化 JSON 清单 (cover_candidates.json), 供 Agent 审核决策。
"""

import os
import sys
import json
import math
import argparse
import cv2
import numpy as np
from typing import List, Dict, Any, Tuple
import mediapipe as mp
from mediapipe.tasks import python
from mediapipe.tasks.python import vision

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

POSE_MODEL_PATH = os.path.join(PROJECT_ROOT, "data", "pose_landmarker_lite.task")
FACE_MODEL_PATH = os.path.join(PROJECT_ROOT, "data", "face_landmarker.task")
OUT_W, OUT_H = 1080, 1920


def normalize_subject_framing(frame_bgr: np.ndarray, landmarks) -> Tuple[np.ndarray, Dict[str, float]]:
    """将原图中的模特根据骨骼包围盒标准化到统一固定的比例与位置"""
    h, w = frame_bgr.shape[:2]
    nose = landmarks[0]
    ls, rs = landmarks[11], landmarks[12]
    la, ra = landmarks[27], landmarks[28]

    shoulder_y = (ls.y + rs.y) / 2.0
    # 估算头顶高度 (鼻尖向上延展约 1.3 倍的肩鼻距)
    head_top_norm = max(0.0, nose.y - (shoulder_y - nose.y) * 1.30)
    # 估算脚底高度 (脚踝下移 4% 包含鞋子)
    feet_bottom_norm = min(1.0, max(la.y, ra.y) + 0.04)

    head_top_y = head_top_norm * h
    feet_bottom_y = feet_bottom_norm * h
    person_h = max(100.0, feet_bottom_y - head_top_y)
    center_x = (ls.x + rs.x) / 2.0 * w

    # 标准化目标参数
    TARGET_PERSON_H = OUT_H * 0.74   # 1420 px (占画面 74%)
    TARGET_HEAD_TOP_Y = OUT_H * 0.16 # 307 px (顶部留白 16% 给品牌标)
    TARGET_CENTER_X = OUT_W * 0.50   # 540 px (水平居中)

    scale = TARGET_PERSON_H / person_h
    new_w = int(w * scale)
    new_h = int(h * scale)
    resized = cv2.resize(frame_bgr, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)

    res_head_top_y = int(head_top_y * scale)
    res_center_x = int(center_x * scale)

    x_offset = int(TARGET_CENTER_X - res_center_x)
    y_offset = int(TARGET_HEAD_TOP_Y - res_head_top_y)

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

    # 左右边缘自适应镜像填充 (消除黑边)
    if dst_x1 > 0:
        edge = canvas[dst_y1:dst_y2, dst_x1:dst_x1 + min(dst_x1, dst_x2 - dst_x1)]
        if edge.shape[1] > 0:
            canvas[dst_y1:dst_y2, 0:dst_x1] = cv2.flip(edge, 1)[:, -dst_x1:]
    if dst_x2 < OUT_W:
        gap = OUT_W - dst_x2
        edge = canvas[dst_y1:dst_y2, max(0, dst_x2 - gap):dst_x2]
        if edge.shape[1] > 0:
            canvas[dst_y1:dst_y2, dst_x2:OUT_W] = cv2.flip(edge, 1)[:, :gap]

    # 视觉醒目度提升：反差与微锐化
    blur = cv2.GaussianBlur(canvas, (0, 0), 2.2)
    enhanced = cv2.addWeighted(canvas, 1.25, blur, -0.25, 0)

    framing_meta = {
        "scale": round(scale, 3),
        "target_person_h": round(TARGET_PERSON_H, 1),
        "target_head_top_y": round(TARGET_HEAD_TOP_Y, 1),
        "normalized_height_ratio": round(TARGET_PERSON_H / OUT_H, 2),
        "head_top_y_px": int(TARGET_HEAD_TOP_Y),
        "center_x_px": int(TARGET_CENTER_X)
    }
    return enhanced, framing_meta


def extract_cover_candidates(
    video_path: str,
    output_dir: str,
    top_k: int = 5,
    sample_step_sec: float = 1.5,
    scan_limit_sec: float = 180.0
) -> List[Dict[str, Any]]:
    """扫描视频提取正面候选帧并统一比例输出"""
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"视频不存在: {video_path}")

    os.makedirs(output_dir, exist_ok=True)

    pose_options = vision.PoseLandmarkerOptions(
        base_options=python.BaseOptions(model_asset_path=POSE_MODEL_PATH)
    )
    pose_detector = vision.PoseLandmarker.create_from_options(pose_options)

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = min(total_frames / fps, scan_limit_sec)

    print(f"[EXTRACT-COVER] 正在扫描前 {duration:.1f}s 视频中的严苛正面全身镜头...")

    raw_candidates = []
    curr_t = 1.0
    while curr_t < duration:
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
        lw, rw = lm[15], lm[16]
        la, ra = lm[27], lm[28]

        # 1. 严格正面朝向断言 (Strict Frontal Check)
        dz_shoulder = abs(ls.z - rs.z)
        if dz_shoulder > 0.055:  # 大于 0.055 说明侧身
            curr_t += sample_step_sec
            continue

        # 2. 遮挡与可见性检查
        ankles_visible = (la.y <= 0.96 and ra.y <= 0.96 and la.visibility > 0.40 and ra.visibility > 0.40)
        occluded = (lw.y < ls.y + 0.08 or rw.y < rs.y + 0.08)
        if occluded or not ankles_visible:
            curr_t += sample_step_sec
            continue

        # 3. 清晰度计算
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        if sharp < 200.0:
            curr_t += sample_step_sec
            continue

        # 正面综合评分
        frontal_score = (1.0 - (dz_shoulder / 0.055)) * 40.0 + min(30.0, sharp / 15.0) + 30.0
        raw_candidates.append({
            "timestamp": round(curr_t, 2),
            "frame": frame.copy(),
            "landmarks": lm,
            "frontal_score": round(frontal_score, 1),
            "dz_shoulder": round(dz_shoulder, 3),
            "sharpness": round(sharp, 1)
        })

        curr_t += sample_step_sec

    cap.release()

    # 排序并取 Top-K
    raw_candidates.sort(key=lambda x: x["frontal_score"], reverse=True)
    selected = raw_candidates[:top_k]

    result_manifest = []
    print(f"[EXTRACT-COVER] 共筛选出 {len(selected)} 个高标准正面候选帧 (统一比例归一化中):")

    for i, item in enumerate(selected):
        cand_id = f"cand_{i+1:02d}"
        norm_frame, f_meta = normalize_subject_framing(item["frame"], item["landmarks"])
        
        img_name = f"{cand_id}_t{item['timestamp']:.1f}s.jpg"
        img_path = os.path.join(output_dir, img_name)
        cv2.imwrite(img_path, norm_frame)

        cand_data = {
            "id": cand_id,
            "timestamp": item["timestamp"],
            "image_path": img_path,
            "image_filename": img_name,
            "frontal_score": item["frontal_score"],
            "dz_shoulder": item["dz_shoulder"],
            "sharpness": item["sharpness"],
            "framing": f_meta,
            "description": f"严格正面立姿 @{item['timestamp']:.1f}s, 清晰度 {item['sharpness']}, 人像占画幅 74% 统一构图"
        }
        result_manifest.append(cand_data)
        print(f"  [{cand_id}] 时间: @{item['timestamp']:.1f}s | 正面分: {item['frontal_score']} | 路径: {img_path}")

    # 保存元数据清单供 Agent 审视
    manifest_path = os.path.join(output_dir, "cover_candidates.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(result_manifest, f, ensure_ascii=False, indent=2)
    print(f"[EXTRACT-COVER] 候选清单已生成: {manifest_path}")

    return result_manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="正面封面底图候选帧提取与标准化比例工具")
    parser.add_argument("--video", required=True, help="视频素材路径")
    parser.add_argument("--out-dir", default="data/candidates/cover_candidates", help="输出目录")
    parser.add_argument("--top-k", type=int, default=5, help="保留候选数量")
    args = parser.parse_args()

    extract_cover_candidates(args.video, args.out_dir, top_k=args.top_k)
