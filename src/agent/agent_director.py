#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
agent_director.py - 抖音带货直播剪辑 Agent 总导演决策中枢（v2）
===============================================================
架构：确定性原子工具 + Agent 总导演（Agent-in-the-Loop）。

Phase 1 确定性工具扫描：
  - detect_scenes: 镜头边界（替代旧硬编码时间窗）
  - extract_cover_candidates v2: 严格正面 + 人脸质量 + 水平 1:6:1 构图
  - analyze_story_segments: ASR 语境纠错 + 漏斗打标候选池
Phase 2 Agent 决策（B12：每次选择先过 DecisionGateway 约束校验，不合格拒绝重选）:
  - 封面优选：候选按多目标分数降序，取首个过约束项（可外部干预 --cover-id，同样校验）
  - 分镜编排：build_plan 泛化编译（配额 + 时长约束 + 黑名单，零硬编码），计划过网关校验
  - Hook 卡避让：由 Hook 片段首帧人脸顶计算 hook_card.y，避让结果过网关校验
Phase 3 工具链渲染：render_cover_with_meta + assemble_video_tool
Phase 4 质检门禁：QcAuditor v2（constraints 驱动，含响度/黑帧/零价格/几何色相）
全程决策落盘 agent_decision_manifest.json。
"""

import os
import sys
import json
import time
import shutil
import argparse
from typing import Dict, Any, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.tools.extract_cover_candidates import extract_cover_candidates, _load_constraints
from src.tools.analyze_story_segments import analyze_story_segments
from src.tools.build_plan import build_plan
from src.tools.detect_scenes import detect_scene_cuts
from src.tools.render_cover_tool import render_cover_with_meta
from src.tools.assemble_video_tool import assemble_video_tool
from src.tools.make_placeholder_cards import ensure_placeholder_cards
from src.core.path_resolver import resolve_path, relativize
from src.qc.qc_auditor import QcAuditor
from src.agent.decision_gateway import DecisionGateway, DecisionViolation

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class AgentDirector:
    def __init__(self, spec_path: str, cache_key: str = None, use_cache: bool = True):
        self.spec_path = os.path.abspath(spec_path)
        with open(self.spec_path, "r", encoding="utf-8") as f:
            self.spec = json.load(f)

        self.constraints = _load_constraints()
        # 统一能力开关（2026-11-01）：把 spec.options.features 派生到 constraints
        # （时长档/面料配额/转场/放大镜/字幕动作），并注入 constraints["features"]
        # 供 assembler / qc / build_plan 直接读取。深拷贝，不污染 yaml 默认值。
        try:
            from src.tools.feature_flags import apply as _apply_features
            self.constraints = _apply_features(self.constraints, self.spec)
            _f = self.constraints.get("features") or {}
            _vals = _f.get("values") or {}
            print(f"[FEATURES] 开关 v{_f.get('version')} | 包装={_vals.get('render_profile')} "
                  f"时长={_vals.get('duration_preset')} 参数占比={_vals.get('fabric_ratio')} "
                  f"违禁词={_vals.get('violation_action')} "
                  f"转场={_vals.get('transition')} 放大镜={_vals.get('magnifier')}")
            for _fr in (_f.get("forced") or []):
                print(f"  [FEATURES] 组合规则收敛 {_fr['key']}: {_fr['from']} → {_fr['to']}"
                      f"（{_fr.get('note', '')}）")
            for _w in (_f.get("warnings") or []):
                print(f"  [FEATURES][WARN] {_w}")
        except Exception as _e:
            print(f"[FEATURES][WARN] 开关解析失败，使用默认值: {_e}")
        self.project_id = self.spec.get("project_id", "demo")
        # —— 多源支持（2026-10-21）：同目录同商品的多个直播视频均可作为候选素材 ——
        # spec.raw_video_paths 显式列出；未提供则扫描 raw_video_path 所在目录下的全部视频。
        self.raw_videos = self._collect_source_videos()
        self.raw_video = self.raw_videos[0]
        self.work_dir = os.path.join(PROJECT_ROOT, "data", "work_dirs", self.project_id)
        self.output_dir = os.path.join(PROJECT_ROOT, "output", self.project_id)
        self.candidates_dir = os.path.join(PROJECT_ROOT, "data", "candidates", self.project_id)

        self.brand_info = self.spec.get("brand", {})
        self.product_info = self.spec.get("product", {})
        self.options = self.spec.get("options", {})

        # —— 商品位占位兜底（需求 1：2026-10-21）——
        # 当前素材普遍缺真实商品图。若不补位，封面会缺失商品贴图位、版面不完整。
        # 这里在导演初始化阶段统一补位：优先真实商品图，缺失则生成"明确标注为占位"的
        # 示例图，保证封面布局与视觉完整性，后续拿到真图同名替换即可。
        self._ensure_product_cards()

        # B12：决策约束网关——Agent 每次选择先过约束校验，不合格拒绝重选
        self.gateway = DecisionGateway(self.constraints)

        # B13：阶段缓存键（sha256(视频+spec+constraints)）；干预模式由工作流禁用
        self.use_cache = bool(use_cache)
        self.cache_key = cache_key
        if self.use_cache and not self.cache_key:
            from src.tools.stage_cache import compute_pipeline_key
            self.cache_key = compute_pipeline_key(self.raw_videos, self.spec_path)

        os.makedirs(self.work_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.candidates_dir, exist_ok=True)

    # 视频扩展名（同一商品的多段直播素材）
    VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".flv", ".m4v", ".ts")

    def _ensure_product_cards(self) -> None:
        """商品位兜底：真实商品图缺失时，用占位示例图补齐版面（需求 1）。

        行为约束：
        - 已有真实商品图 → 不做任何改动；
        - 缺失 → 生成占位图并登记到 product.item_images，
          同时把 spec 落盘，使下游 cover_renderer 走标准贴图渲染路径
          （C11~C14 几何断言与 QC「封面贴图几何」得以真实验证）。
        - options.disable_placeholder_cards=true 时跳过（供单元测试/离线体检使用，
          避免测试往共享目录里写产物）。
        """
        if self.options.get("disable_placeholder_cards"):
            return
        product = self.spec.get("product", {}) or {}
        spec_dir = os.path.dirname(self.spec_path)
        existing = []
        for i in product.get("item_images", []):
            if not i:
                continue
            rp = resolve_path(i, spec_dir)
            if rp and os.path.exists(rp):
                existing.append(rp)
        if existing:
            # 已有真实商品图 → 不生成占位，但把路径归一化后写回 spec（可移植性）
            product["item_images"] = existing
            self._write_spec_back()
            return
        n_cards = int(self.options.get("placeholder_cards", 2) or 2)
        size = int((self.constraints.get("sticker", {}) or {}).get("size", 280) or 280)
        try:
            ensure_placeholder_cards(self.spec, self.project_id, n_cards=n_cards, size=size)
        except Exception as e:
            # 占位是"锦上添花"的版面兜底，不应阻断主流水线
            print(f"[WARN] 商品位占位补齐失败（不阻断，封面将无贴图）: {e}")
            return
        # 写回 spec，保证渲染与后续审计读到的是同一份商品位定义
        self._write_spec_back()
        self.product_info = self.spec.get("product", {})

    def _write_spec_back(self):
        """把 spec 写回磁盘，路径统一转成**相对仓库根**的相对路径（可移植性）。

        2026-10-24：spec 此前存的是 D:/Projects/... 这类机器专属绝对路径，
        换机器/换 runner 即失效。写回时用 relativize 归一化。
        """
        try:
            # 归一化素材路径为相对路径
            for key in ("raw_video_path",):
                v = self.spec.get(key)
                if v:
                    self.spec[key] = relativize(v, PROJECT_ROOT)
            if self.spec.get("raw_video_paths"):
                self.spec["raw_video_paths"] = [
                    relativize(p, PROJECT_ROOT) for p in self.spec["raw_video_paths"]]
            product = self.spec.get("product", {}) or {}
            if product.get("item_images"):
                product["item_images"] = [
                    relativize(p, PROJECT_ROOT) for p in product["item_images"]]
            with open(self.spec_path, "w", encoding="utf-8") as f:
                json.dump(self.spec, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[WARN] spec 写回失败（不阻断）: {e}")

    def _collect_source_videos(self) -> list:
        """收集本任务的全部候选源素材（同目录同商品的多段直播视频）。

        优先级：
          1. spec.raw_video_paths 显式列表（用户/填表工具指定）；
          2. 否则扫描 spec.raw_video_path 所在目录下的全部视频文件
             （2026-10-21 需求：同一目录下的多个视频均为同一商品的直播素材）；
          3. 目录内无其他视频时退化为单文件。
        返回绝对路径列表，顺序稳定（按文件名排序）保证缓存键可复现。

        路径可移植性（2026-10-24）：spec 里的路径统一经 path_resolver 解析，
        相对路径与"换机器后失效的旧绝对路径"都能自动定位到本机实际文件。

        排除自身产物（2026-11-01）：交付物写入素材目录（`{project_id}.mp4`
        / `.jpg` 等），与 raw_video_path 同目录。重跑时若不排除，
        上一次的**成片**会被当成第 N+1 段原始素材重新摄入 →
        素材清单污染（实测 6 段变 7 段，多出的正是上轮成片）。
        故扫描时按 basename 过滤掉本任务的交付物与历史成片。
        """
        spec_dir = os.path.dirname(self.spec_path)
        spec_paths = self.spec.get("raw_video_paths") or []
        paths = []
        for p in spec_paths:
            rp = resolve_path(p, spec_dir)
            if rp and os.path.exists(rp) and rp not in paths:
                # 显式清单同样排除自身产物：resolved spec 由流水线生成并可能被
                # 上一轮污染（含上轮成片），重跑时不应把它当原始素材。
                if self._is_own_artifact(os.path.basename(rp)):
                    continue
                paths.append(rp)
        if paths:
            return paths
        primary = self.spec.get("raw_video_path")
        if primary:
            primary_abs = resolve_path(primary, spec_dir) or os.path.abspath(primary)
            if os.path.isdir(primary_abs):
                for name in sorted(os.listdir(primary_abs)):
                    if not name.lower().endswith(self.VIDEO_EXTS):
                        continue
                    if self._is_own_artifact(name):
                        continue
                    ap = os.path.abspath(os.path.join(primary_abs, name))
                    if ap not in paths:
                        paths.append(ap)
            elif os.path.isfile(primary_abs):
                if primary_abs not in paths:
                    paths.insert(0, primary_abs)
                scan_dir = os.path.dirname(primary_abs)
                # 同目录其余视频一并纳入候选（跨素材选段）
                if os.path.isdir(scan_dir):
                    for name in sorted(os.listdir(scan_dir)):
                        if not name.lower().endswith(self.VIDEO_EXTS):
                            continue
                        if self._is_own_artifact(name):
                            continue
                        ap = os.path.abspath(os.path.join(scan_dir, name))
                        if ap not in paths:
                            paths.append(ap)
            elif not os.path.exists(primary_abs):
                # 保持既有初始化容错契约：占位路径原样保留，由后续阶段报错
                if primary_abs not in paths:
                    paths.insert(0, primary_abs)
        if not paths:
            raise FileNotFoundError(
                f"未找到任何可用的源视频素材（raw_video_path={self.spec.get('raw_video_path')}）")
        return paths

    def _assign_sticker_sides(self, plan: Dict[str, Any]) -> None:
        """为每个分镜挑选**不压脸**的挂角贴纸边（左/右）。

        背景（2026-11-01）：挂角贴纸此前是**全局固定**位置（right, y=300），
        而人脸位置逐镜不同——某镜模特偏右时贴纸正好压脸，
        被 QC「叠加层人脸安全」判 BLOCK（实测 project-1 的 c03_fit）。

        本方法与 Hook 卡避让（§11.2 #5 多帧采样取最高人脸顶）同源思路：
        在该分镜的贴纸活跃时段内抽帧测人脸水平位置，
        选离人脸更远的一侧；两侧都压则把贴纸整体下移到人脸下方。
        结果写入 clip["sticker_side"] / clip["sticker_y"]，
        装配器逐镜读取（原先只读全局配置）。
        """
        try:
            import cv2
            from src.tools.detect_face import FaceAnalyzer, _load_pose_landmarks
        except Exception as e:
            print(f"  [STICKER-SIDE] 人脸检测不可用，沿用默认边: {e}")
            return
        st_c = (self.constraints.get("video_sticker", {}) or {})
        size = int(st_c.get("size", 220) or 220)
        mx = int(st_c.get("margin_x", 40) or 40)
        my = int(st_c.get("margin_y", 300) or 300)
        default_side = str(st_c.get("side", "right") or "right")
        fa = FaceAnalyzer()
        out_w = 1080
        for c in plan.get("clips", []):
            src = c.get("source_file") or self.raw_video
            st = float(c.get("st", 0.0))
            dur = float(c.get("dur", 0.0))
            if dur <= 0.2:
                continue
            # 贴纸活跃时段（与装配器一致：delay_ratio + hold_ratio）
            delay_r = float(st_c.get("delay_ratio", 0.10) or 0.10)
            hold_r = float(st_c.get("hold_ratio", 0.72) or 0.72)
            t_in = st + dur * delay_r
            t_out = min(st + dur * (delay_r + hold_r), st + dur - 0.05)

            # 在活跃时段内多帧采样人脸 bbox（与 QC check_overlay_face_safety
            # 同一口径：活跃时段内取样、算"贴纸矩形 vs 人脸 bbox"的间距）。
            faces = []
            span = max(0.1, t_out - t_in)
            for k in range(3):
                t = t_in + span * (k + 0.5) / 3.0
                try:
                    cap = cv2.VideoCapture(src)
                    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000)
                    ok, fr = cap.read()
                    cap.release()
                    if not ok or fr is None:
                        continue
                    lm = _load_pose_landmarks(fr)
                    if lm is None:
                        continue
                    face = fa.analyze(fr, pose_landmarks=lm)
                    if not face or not face.get("face_detected"):
                        continue
                    bbox = face["face_bbox_norm"]
                    fh, fw = fr.shape[:2]
                    x_off = (fw - out_w) / 2.0 if fw > out_w else 0.0
                    fx0 = (bbox[0] * fw - x_off) * (out_w / float(fw))
                    fx1 = (bbox[2] * fw - x_off) * (out_w / float(fw))
                    fy0 = bbox[1] * fh
                    fy1 = bbox[3] * fh
                    faces.append((fx0, fy0, fx1, fy1))
                except Exception:
                    continue
            if not faces:
                c["sticker_side"] = default_side
                c["sticker_y"] = my
                continue

            # 镜像分镜（effect=hflip）：成片画面左右翻转，人脸水平位置随之互换。
            # 若仍按源帧人脸位置选贴纸边，选出的"安全侧"在成片上正好压脸
            # （2026-11-04 project-4 实测：c07_fit 源帧间距 357px 判 OK，
            #  成片 QC「叠加层人脸安全」却判 BLOCK 压脸）。
            # 采样人脸 bbox 按成片画幅左右镜像后再参与避让计算，保证判定对象
            # 与实际交付对象一致（§11.2 #4）。注意必须是「替换」而非「并集」：
            # 并集会把源帧里并不存在的人脸也算进去，两侧都被判为压脸，
            # 反而选不出安全位置。镜像若因遮挡覆盖率不足被装配器放弃，
            # QC 的成片级人脸安全复检会兜底拦截（当前任务 mirror_abandoned=0）。
            if str(c.get("effect") or "") == "hflip":
                faces = [
                    (out_w - fx1, fy0, out_w - fx0, fy1)
                    for (fx0, fy0, fx1, fy1) in faces
                ]

            # 推镜分镜（zoom>1）：装配器用 zoompan 以「水平居中 + y_anchor」裁剪后
            # 放大，源帧人脸 bbox 在成片里会被放大并平移。挂角贴纸是成片坐标系里的
            # 固定矩形，若按未推镜的源帧判定，选出的"安全侧"推镜后可能正好压脸
            # （2026-11-04 project-4 v2 实测：c08_styling zoom=1.3 源帧判 right OK，
            #  成片 QC「叠加层人脸安全」却判 BLOCK 压脸 38px）。
            # 按 zoompan 同款公式把人脸映射到成片坐标系；推镜是缓入的，因此同时
            # 保留「未推镜」与「推镜完成」两种真实形态，取最坏帧间距最大者。
            z = float(c.get("zoom", 1.0) or 1.0)
            if z > 1.0:
                zc = (self.constraints.get("zoom_effect", {}) or {})
                ya = float(c.get("y_anchor", zc.get("y_anchor", 0.35)) or 0.35)
                out_h = 1920
                x_c = out_w * (1.0 - 1.0 / z) / 2.0
                y_c = out_h * ya * (1.0 - 1.0 / z)
                faces = faces + [
                    ((fx0 - x_c) * z, (fy0 - y_c) * z,
                     (fx1 - x_c) * z, (fy1 - y_c) * z)
                    for (fx0, fy0, fx1, fy1) in faces
                ]

            def _gap(rect, face_rect):
                """矩形间距（与 QC 的 _rect_overlap_gap 同一符号约定）。"""
                ax0, ay0, ax1, ay1 = rect
                bx0, by0, bx1, by1 = face_rect
                dx = max(bx0 - ax1, ax0 - bx1)
                dy = max(by0 - ay1, ay0 - by1)
                if dx >= 0 and dy >= 0:
                    return (dx * dx + dy * dy) ** 0.5
                if dx < 0 and dy < 0:
                    return -((min(ax1, bx1) - max(ax0, bx0)) *
                             (min(ay1, by1) - max(ay0, by0))) ** 0.5
                if dx < 0:
                    return float(dy)
                return float(dx)

            # 先在默认挂角高度上择优；只有确实"紧/压脸"（<30px）才允许整体下移。
            # 不能一味取"离脸最远"——那会把所有贴纸推到画面中部，破坏挂角版式。
            # 下移时取满足安全间距且 y 最小者，尽量贴近原始挂角位置。
            def _best_at(sy):
                opts = []
                for side in ("right", "left"):
                    sx = (out_w - size - mx) if side == "right" else mx
                    rect = (sx, sy, sx + size, sy + size)
                    opts.append((min(_gap(rect, f) for f in faces), side, sx, sy))
                return max(opts, key=lambda x: x[0])

            best = _best_at(my)
            if best[0] < 30:
                lowered = [_best_at(sy) for sy in (my + 220, my + 440, my + 660)]
                safe = [x for x in lowered if x[0] >= 30]
                if safe:
                    best = min(safe, key=lambda x: x[3])
            worst, side, sx, sy = best
            c["sticker_side"] = side
            c["sticker_y"] = int(sy)
            _flag = "OK" if worst >= 30 else ("紧" if worst >= 0 else "压脸")
            print(f"  [STICKER-SIDE] {c['id']}: 边={side} y={sy} "
                  f"最坏帧人脸间距 {worst:.0f}px [{_flag}]")

    def _detect_speech_pauses(self) -> dict:
        """检测各源素材的语音气口（切片规则 R1 的数据源）。

        背景（2026-11-01 参考成片实测）：人工切片的切点 **78% 落在**
        −60dB 以下的近数字静音"气口"上（全片随机位置仅 0.52% → 富集 29 倍），
        切点与 ASR 句末偏移中位 0.03s。即：切的是"字与字之间那一瞬空隙"。
        本方法把每个源素材的气口列表算好缓存，供 build_plan 把裁剪点吸附到气口。

        返回：{源文件绝对路径: [(气口时刻, dB), ...]}；失败时返回空 dict
        （空 dict 时 build_plan 退回纯标点吸附，行为与改动前一致）。
        """
        rules = {}
        try:
            from src.tools.slicing_rules import load_slicing_rules
            rules = load_slicing_rules(self.constraints)
        except Exception as e:
            print(f"  [PAUSES] 切片规则不可用，跳过气口检测: {e}")
            return {}
        cp = (rules.get("cut_on_speech_pause") or {}) if rules else {}
        if not rules or not cp.get("enabled", True):
            return {}
        silence_db = float(cp.get("silence_db", -60.0))
        min_sec = float(cp.get("min_silence_sec", 0.03))

        from src.tools.detect_speech_pauses import detect_pauses
        out = {}
        for v in self.raw_videos:
            cache_p = os.path.join(self.work_dir, "pauses",
                                   os.path.splitext(os.path.basename(v))[0]
                                   + ".pauses.json")
            dips = None
            try:
                if os.path.exists(cache_p):
                    with open(cache_p, "r", encoding="utf-8") as f:
                        dips = [(float(a), float(b)) for a, b in
                                (json.load(f).get("dips") or [])]
                else:
                    res = detect_pauses(v, silence_db, min_sec)
                    dips = [(float(a), float(b)) for a, b in (res.get("dips") or [])]
                    os.makedirs(os.path.dirname(cache_p), exist_ok=True)
                    with open(cache_p, "w", encoding="utf-8") as f:
                        json.dump(res, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"  [PAUSES] 气口检测失败 {os.path.basename(v)}"
                      f"（不阻断，退回标点吸附）: {e}")
                dips = None
            if dips:
                out[os.path.abspath(v)] = dips
        print(f"  [PAUSES] 语音气口检测完成：{len(out)}/{len(self.raw_videos)} "
              f"个源，共 {sum(len(x) for x in out.values())} 个气口"
              f"（阈值 {silence_db} dB）")
        return out

    def _is_own_artifact(self, filename: str) -> bool:
        """判断文件名是否为**本任务自己的产物**（不得再当原始素材摄入）。

        覆盖：交付到素材目录的 `{pid}.mp4` / `{pid}.jpg`、
        仓库 output 目录的历史副本 `{pid}_final.mp4` / `{pid}_cover.jpg`。
        原始素材的文件名是「时间戳_时间戳.mp4」形态，与任务名无交集。
        """
        pid = str(self.project_id or "")
        if not pid:
            return False
        stem = os.path.splitext(filename)[0]
        return stem in {pid, f"{pid}_final", f"{pid}_cover"}

    def get_material_dir(self) -> str:
        """获取用户提供的素材所在目录（绝对路径）。

        规则：
        1. 若 raw_video_path 指向目录，直接作为素材目录；
        2. 若 raw_video_path 指向文件，取其所在目录；
        3. 若 raw_video_paths 提供了文件列表，取首个文件所在目录；
        4. 兜底回退到 self.output_dir。
        """
        spec_dir = os.path.dirname(self.spec_path)
        primary = self.spec.get("raw_video_path")
        if primary:
            p = resolve_path(primary, spec_dir) or os.path.abspath(primary)
            if os.path.isdir(p):
                return os.path.abspath(p)
            elif os.path.isfile(p):
                return os.path.abspath(os.path.dirname(p))
            elif os.path.isdir(os.path.dirname(p)):
                return os.path.abspath(os.path.dirname(p))

        raw_paths = self.spec.get("raw_video_paths") or []
        for p in raw_paths:
            rp = resolve_path(p, spec_dir) or os.path.abspath(p)
            if os.path.exists(rp):
                return os.path.abspath(os.path.dirname(rp))

        if self.raw_videos:
            return os.path.abspath(os.path.dirname(self.raw_videos[0]))

        return self.output_dir

    def _assign_magnifiers(self, plan: Dict[str, Any]) -> None:
        """为面料/工艺类分镜分配放大镜圆圈锚点（2026-10-27 反馈驱动）。

        规则（硬约束，确定化）：
          - 仅面料类（category=fabric）且未推镜（zoom<=1.0）的分镜可获得；
          - 每条成片最多 magnifier_effect.max_per_video 个；
          - 锚点 = 肩部中心与胯部中心的中点（胸腹区域，天然避让面部）；
          - Pose 不可用时退化为约束里的兜底位置。
        """
        import cv2
        magn_c = (self.constraints.get("magnifier_effect", {}) or {})
        if not magn_c.get("enabled", True):
            return
        from src.tools.detect_face import _load_pose_landmarks
        max_n = int(magn_c.get("max_per_video", 2) or 2)
        r = int(magn_c.get("radius", 130) or 130)
        z = float(magn_c.get("zoom", 1.8) or 1.8)
        hold = float(magn_c.get("hold_ratio", 0.80) or 0.80)
        delay = float(magn_c.get("delay_ratio", 0.12) or 0.12)
        assigned = 0
        for c in plan.get("clips", []):
            if assigned >= max_n:
                break
            if c.get("category") != "fabric":
                continue
            if float(c.get("zoom", 1.0) or 1.0) > 1.0:
                continue
            if c.get("effect") == "split":
                continue
            src = c.get("source_file") or self.raw_video
            cx = cy = None
            frame = None
            try:
                cap = cv2.VideoCapture(src)
                cap.set(cv2.CAP_PROP_POS_MSEC, (float(c.get("st", 0)) + 0.8) * 1000)
                ret, frame = cap.read()
                cap.release()
                if ret and frame is not None:
                    lm = _load_pose_landmarks(frame)
                    if lm is not None:
                        ls, rs, lh, rh = lm[11], lm[12], lm[23], lm[24]
                        h, w = frame.shape[:2]
                        nx = ((ls.x + rs.x) / 2 + (lh.x + rh.x) / 2) / 2
                        ny = ((ls.y + rs.y) / 2 + (lh.y + rh.y) / 2) / 2
                        x_off = (w - 1080) / 2.0 if w > 1080 else 0.0
                        cx = int((nx * w - x_off) * (1080.0 / w))
                        cy = int(ny * h)
            except Exception as e:
                print(f"  [MAGNIFIER] {c['id']} Pose 解析失败，用兜底位置: {e}")
            if cx is None:
                cx = 1080 - int(magn_c.get("margin_x", 60)) - r
                cy = int(1920 * float(magn_c.get("anchor_y_ratio", 0.42)))
            cx = max(r + 8, min(1080 - r - 8, int(cx)))
            cy = max(r + 100, min(1920 - r - 260, int(cy)))

            # 2026-10-28 接线 magnifier_effect.require_face_clear（此前为死键）：
            # 圆圈（含 overlay_face_safety 安全间距）不得覆盖模特面部；
            # 覆盖则把圆心上拉到面部以下，仍越界则放弃该分镜的放大镜。
            if (bool(magn_c.get("require_face_clear", True))
                    and frame is not None):
                try:
                    from src.tools.detect_face import FaceAnalyzer
                    fm = FaceAnalyzer().analyze(frame)
                    if fm and fm.get("face_detected"):
                        fh, fw = frame.shape[:2]
                        x_off = (fw - 1080) / 2.0 if fw > 1080 else 0.0
                        fx0, fy0, fx1, fy1 = fm["face_bbox_norm"]
                        fx0o = (fx0 * fw - x_off) * (1080.0 / fw)
                        fx1o = (fx1 * fw - x_off) * (1080.0 / fw)
                        fy0o, fy1o = fy0 * fh, fy1 * fh
                        m_px = int((self.constraints.get(
                            "overlay_face_safety", {}) or {}).get(
                            "margin_px", 30) or 30)

                        def _hits_face(cx_, cy_):
                            return not (cx_ + r < fx0o - m_px
                                        or cx_ - r > fx1o + m_px
                                        or cy_ + r < fy0o - m_px
                                        or cy_ - r > fy1o + m_px)

                        if _hits_face(cx, cy):
                            cy2 = max(r + 100, min(
                                1920 - r - 260, int(fy1o + r + m_px)))
                            if _hits_face(cx, cy2):
                                print(f"  [MAGNIFIER] {c['id']} 圆圈与面部"
                                      f"冲突且无法下移避让，跳过")
                                continue
                            cy = cy2
                            print(f"  [MAGNIFIER] {c['id']} 避让面部："
                                  f"锚点下移至 y={cy}")
                except Exception as e:
                    print(f"  [MAGNIFIER] {c['id']} 人脸安全校验异常"
                          f"（放行）: {e}")

            dur = float(c.get("dur", 0))
            c["magnifier"] = {
                "enabled": True, "cx": cx, "cy": cy, "r": r, "zoom": z,
                "t_in": round(dur * delay, 3),
                "t_out": round(dur * (delay + hold), 3),
            }
            assigned += 1
            print(f"  [MAGNIFIER] {c['id']} 放大镜锚点 ({cx},{cy}) "
                  f"r={r} z={z} 时段 {c['magnifier']['t_in']}~{c['magnifier']['t_out']}s")

    def run_director_workflow(
        self,
        chosen_cover_id: Optional[str] = None,
        custom_plan_path: Optional[str] = None,
        edited_timeline_path: Optional[str] = None
    ) -> Dict[str, Any]:
        start_time = time.time()
        # 外部干预模式（指定封面/自定义计划/人工精修时间线）禁用阶段缓存，保证每次重算
        if chosen_cover_id or custom_plan_path or edited_timeline_path:
            self.use_cache = False
            self.cache_key = None
        ck = self.cache_key if self.use_cache else None
        print(f"\n=======================================================")
        print(f"启动 Agent剪辑导演中枢 v2: {self.project_id}")
        if len(self.raw_videos) > 1:
            print(f"多源素材模式: {len(self.raw_videos)} 段直播素材（跨素材挑选最优片段）")
            for i, v in enumerate(self.raw_videos, 1):
                print(f"   [{i}] {os.path.basename(v)}")
        else:
            print(f"原始视频: {self.raw_video}")
        print(f"阶段缓存: {'启用 key=' + ck[:16] if ck else '禁用'}")
        print(f"=======================================================\n")

        # -------------------------------------------------------------
        # Phase 1: 确定性工具扫描与候选池生成
        # -------------------------------------------------------------
        print(">>> [PHASE 1] 确定性工具链提取候选池...")

        # 1.1 镜头边界检测（多源：逐源检测后合并；B13 阶段缓存）
        scene_cuts = []
        try:
            for v in self.raw_videos:
                cuts = detect_scene_cuts(v, cache_key=(ck + "|" + v) if ck else None,
                                          work_dir=self.work_dir)
                scene_cuts.extend(cuts)
            print(f"    镜头边界: {len(scene_cuts)} 个切换点（{len(self.raw_videos)} 个源）")
        except Exception as e:
            scene_cuts = []
            print(f"    镜头边界检测失败（不阻断）: {e}")

        # 1.2 封面候选（v2：人脸门禁 + 水平 1:6:1；多源：全部源参与候选池；B13 阶段缓存）
        #多源时每源写入独立子目录，避免 cover_candidates.json 互相覆盖；
        # 最终由 Agent 汇总为统一候选池写入 cover_cand_dir/cover_candidates.json。
        cover_cand_dir = os.path.join(self.candidates_dir, "cover_candidates")
        multi = len(self.raw_videos) > 1
        cover_candidates = []
        seen_cand_ids = set()
        for v in self.raw_videos:
            v_dir = cover_cand_dir
            if multi:
                v_dir = os.path.join(cover_cand_dir,
                                     os.path.splitext(os.path.basename(v))[0][-10:])
                os.makedirs(v_dir, exist_ok=True)
            try:
                cands = extract_cover_candidates(
                    video_path=v,
                    output_dir=v_dir,
                    spec_data=self.spec,
                    constraints=self.constraints,
                    cache_key=(ck + "|" + v) if ck else None,
                    cache_work_dir=self.work_dir,
                )
            except Exception as e:
                print(f"    [WARN] 源素材封面候选提取失败 {os.path.basename(v)}: {e}")
                continue
            for c in cands:
                # 跨源候选 id 去重：加源后缀，避免同名文件候选 id 碰撞
                cid = c["id"]
                if multi:
                    stem = os.path.splitext(os.path.basename(v))[0][-6:]
                    new_id = f"{cid}_{stem}"
                    if new_id in seen_cand_ids:
                        continue
                    seen_cand_ids.add(new_id)
                    c = dict(c, id=new_id, source_file=v)
                cover_candidates.append(c)
        # 硬门禁前置：清晰度不达标的候选不参与排序
        # （评分函数对"宽占比达标"加权很高，可能让sharpness<200 的糊脸候选排到第一，
        #   而网关虽会拒绝但需逐个试错；这里直接过滤，保证首选即合规）
        sharp_min = float((self.constraints.get("cover", {}) or {})
                          .get("sharpness_min", 200))
        eligible = [c for c in cover_candidates
                    if (c.get("framing", {}) or {}).get("post_scale_sharpness", 0) >= sharp_min]
        filtered = len(cover_candidates) - len(eligible)
        if filtered and eligible:
            print(f"    清晰度门禁前置过滤: 淘汰 {filtered} 个 sharpness<{sharp_min} 的候选")
            cover_candidates = eligible
        elif filtered:
            print(f"    [WARN] 全部候选清晰度 <{sharp_min}，保留原池交由网关裁决")

        # 统一候选池落盘（按多目标分数降序，[0] 即 Agent 选定项，与 QC 读取约定一致）
        # 注意：QC._audit_cover_width 按数组读取 candidates[0]；排序键为 frontal_score
        # （与 extract_cover_candidates 内部排序一致），跨源时才能选出全局最优帧。
        cover_candidates.sort(key=lambda c: c.get("frontal_score", 0), reverse=True)
        with open(os.path.join(cover_cand_dir, "cover_candidates.json"), "w",
                  encoding="utf-8") as f:
            json.dump(cover_candidates, f, ensure_ascii=False, indent=2)
        with open(os.path.join(cover_cand_dir, "cover_sources.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"sources": [os.path.basename(v) for v in self.raw_videos]},
                      f, ensure_ascii=False, indent=2)
        print(f"    封面候选池: {len(cover_candidates)} 个（来自 {len(self.raw_videos)} 个源）")

        # 1.3 叙事分镜候选池（ASR + 漏斗打标；多源逐源转录后合并；B13 阶段缓存）
        story_cand_json = os.path.join(self.candidates_dir, "story_candidates.json")
        story_candidates = analyze_story_segments(
            video_path=self.raw_videos,
            spec_data=self.spec,
            cache_dir=self.work_dir,
            output_json_path=story_cand_json,
            cache_key=ck,
        )

        # -------------------------------------------------------------
        # Phase 1.5: 素材-契约一致性预检 + 对话脚本（2026-10-27 反馈驱动）
        # -------------------------------------------------------------
        consistency_report = None
        dialogue_stats = None
        verified_cache = os.path.join(self.work_dir, "whisper_segments_verified.json")
        if not os.path.exists(verified_cache):
            verified_cache = os.path.join(self.work_dir, "whisper_segments.json")
        if os.path.exists(verified_cache):
            with open(verified_cache, "r", encoding="utf-8") as f:
                all_segments = json.load(f)
            # 1.5-0 字形归一（2026-10-30）：缓存命中也做幂等归一，
            # 防止历史缓存带繁体字；归一后写回缓存保证字幕/文档一致。
            self._norm_report = {"skipped": True}
            try:
                from src.tools.normalize_zh_script import normalize_segments
                nrm_c = (self.constraints.get("asr_script_normalization", {}) or {})
                if nrm_c.get("enabled", True):
                    all_segments, norm_report = normalize_segments(
                        all_segments,
                        protect_terms=nrm_c.get("protect_terms"),
                        prefer=str(nrm_c.get("converter", "opencc")))
                    self._norm_report = norm_report
                    if norm_report.get("n_changed_segments"):
                        with open(verified_cache, "w", encoding="utf-8") as f:
                            json.dump(all_segments, f, ensure_ascii=False, indent=2)
                        print(f"  [ASR-NORM] 缓存归一 {norm_report['n_changed_segments']} 段 / "
                              f"{norm_report['n_changed_chars']} 字"
                              f"（残留 {len(norm_report['residual_traditional_segments'])} 段）")
                    with open(os.path.join(self.work_dir,
                                           "script_normalization_report.json"),
                              "w", encoding="utf-8") as f:
                        json.dump(norm_report, f, ensure_ascii=False, indent=2)
            except Exception as e:
                self._norm_report = {"error": str(e)}
                print(f"  [ASR-NORM] 字形归一失败（跳过）: {e}")
            # 1.5a 一致性预检（防止素材与契约商品错配）
            try:
                from src.tools.check_spec_material_consistency import (
                    check_spec_material_consistency)
                cc_c = self.constraints.get("consistency_check", {}) or {}
                if cc_c.get("enabled", True):
                    consistency_report = check_spec_material_consistency(
                        self.spec, all_segments, constraints=self.constraints)
                    print(f"\n>>> [PHASE 1.5] 素材-契约一致性预检: "
                          f"{'PASS' if consistency_report['passed'] else 'BLOCK'}"
                          f"（{consistency_report['summary']['n_warnings']} 条 WARN）")
                    for w in consistency_report.get("warnings", []):
                        print(f"  [一致性 WARN] {w}")
                    cc_path = os.path.join(self.output_dir,
                                           str(cc_c.get("report_path",
                                                        "consistency_report.json")))
                    with open(cc_path, "w", encoding="utf-8") as f:
                        json.dump(consistency_report, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"  [WARN] 一致性预检失败（不阻断）: {e}")
            # 1.5b 完整对话脚本（ASR 驱动剩剩的唯一依据）
            try:
                from src.tools.build_dialogue_script import build_dialogue_script
                script = build_dialogue_script(all_segments, self.spec)
                dialogue_stats = script.get("stats")
                with open(os.path.join(self.work_dir, "dialogue_script.json"),
                          "w", encoding="utf-8") as f:
                    json.dump(script, f, ensure_ascii=False, indent=2)
                print(f"  [对话脚本] {dialogue_stats['n_utterances']} 个完整语句"
                      f"（完整率 {dialogue_stats['complete_ratio']:.0%}）"
                      f" | {dialogue_stats['n_violation_utterances']} 个语句含违规词")
            except Exception as e:
                print(f"  [WARN] 对话脚本生成失败（不阻断）: {e}")

        # -------------------------------------------------------------
        # Phase 2: Agent 决策干预
        # -------------------------------------------------------------
        print("\n>>> [PHASE 2] Agent 启动决策与审美干预...")

        # 决策 2.1: 封面底图（B12：先过约束网关，不合格拒绝重选；外部指定同样校验）
        try:
            selected_cover = self.gateway.select_valid_cover(
                cover_candidates, forced_id=chosen_cover_id)
        except DecisionViolation as e:
            raise RuntimeError(
                f"封面决策被约束网关拦截: {e.violations}"
                f"（人工复核 candidates/cover_candidates.json 或放宽 constraints.yaml 后重跑）")
        if chosen_cover_id:
            print(f"  [外部干预] 采用指定封面候选: {chosen_cover_id}（已过约束校验）")
        else:
            print(f"  [自主仲裁] 优选封面: {selected_cover['id']} "
                  f"(@{selected_cover['timestamp']:.1f}s) | {selected_cover['description']}")

        # 决策 2.2: 分镜编排（build_plan 泛化编译；B12：计划过约束网关）
        if custom_plan_path and os.path.exists(custom_plan_path):
            with open(custom_plan_path, "r", encoding="utf-8") as f:
                plan = json.load(f)
            print(f"  [外部干预] 加载自定义分镜计划: {custom_plan_path}")
            plan_source = "custom_plan"
            plan_forced = True
        else:
            verified_cache = os.path.join(self.work_dir, "whisper_segments_verified.json")
            if not os.path.exists(verified_cache):
                verified_cache = os.path.join(self.work_dir, "whisper_segments.json")
            with open(verified_cache, "r", encoding="utf-8") as f:
                segments = json.load(f)
            # R1：检测各源语音气口，供 build_plan 把裁剪点吸附到气口
            pauses_by_source = self._detect_speech_pauses()
            plan = build_plan(self.raw_video, segments, self.spec,
                              self.constraints, scene_cuts=scene_cuts,
                              cache_key=ck, work_dir=self.work_dir,
                              pauses_by_source=pauses_by_source)
            plan_source = "build_plan"
            plan_forced = False
            # 保留自动计划的分镜与包装配置（人工精修只改时间轴）
            built_clips = list(plan.get("clips", []))
            built_plan_extras = {
                "hook_card": plan.get("hook_card"),
                "cta": plan.get("cta"),
                "brand": plan.get("brand"),
                "subtitles": plan.get("subtitles"),
            }

            # 决策 2.2b: 人工精修时间线回注（2026-10-24 接入生产）
            # 此前 chatcut_bridge 只在测试里被调用，用户无法从成片反推修改。
            # 现提供 --timeline 入口：导出 → 人工在 OpenChatCut 精修 → 回注重总装。
            if edited_timeline_path and os.path.exists(edited_timeline_path):
                try:
                    from src.tools.chatcut_bridge import (timeline_to_plan,
                                                        validate_timeline)
                    with open(edited_timeline_path, "r", encoding="utf-8") as f:
                        edited = json.load(f)
                    ok, problems = validate_timeline(edited)
                    if not ok:
                        print(f"  [外部干预] 精修时间线校验未通过，回注被拒绝: {problems[:3]}")
                    else:
                        plan = timeline_to_plan(edited)
                        # 保留 build_plan 生成的特效决策（桥接只搬运时间轴）
                        for c in plan.get("clips", []):
                            src_clip = next((o for o in built_clips
                                             if o["id"] == c["id"]), None)
                            if src_clip:
                                for k in ("effect", "zoom", "y_anchor", "sticker_text",
                                          "category", "source_file", "source_tag",
                                          "split_pair_st", "split_label_left",
                                          "split_label_right"):
                                    if k in src_clip:
                                        c[k] = src_clip[k]
                        plan["hook_card"] = built_plan_extras.get("hook_card", plan.get("hook_card"))
                        plan["cta"] = built_plan_extras.get("cta", plan.get("cta"))
                        plan["brand"] = built_plan_extras.get("brand", plan.get("brand"))
                        plan_source = "edited_timeline"
                        plan_forced = True
                        print(f"  [外部干预] 已回注人工精修时间线: {edited_timeline_path}")
                except Exception as e:
                    print(f"  [外部干预] 精修时间线回注失败（沿用自动计划）: {e}")
        try:
            self.gateway.validate_and_record_plan(
                plan, forced=plan_forced, source=plan_source)
        except DecisionViolation as e:
            raise RuntimeError(
                f"分镜计划被约束网关拦截: {e.violations}"
                f"（人工复核 plan.json 或调整 constraints.yaml 漏斗配额后重跑）")
        print(f"  [编排校验] {len(plan['clips'])} 个分镜通过约束网关，"
              f"总时长 {plan['total_expected_duration']}s")
        for c in plan["clips"]:
            print(f"             - {c['id']}: {c['st']:.2f}~{c['end']:.2f}s "
                  f"({c['dur']:.2f}s) zoom={c['zoom']} | {c['desc']}")

        # 决策 2.3: Hook 卡避让人脸顶（B10 + B12：避让结果过网关校验）
        # 2026-10-27：主播在黄金 3 秒内可能前倾/晃动，单帧采样会压脸
        # （实测 t=1.875s 人脸顶比首帧高 20px）。改为前 2.5s 多帧采样，
        # 取最高人脸顶（最小 y）+ 额外 10px 缓冲，保证整段活跃期不压脸。
        try:
            import cv2
            from src.tools.detect_face import FaceAnalyzer, _load_pose_landmarks
            # 多源：Hook 卡避让需在首个分镜所属的素材文件上取人脸顶
            hook_src = plan["clips"][0].get("source_file") or self.raw_video
            hook_st = float(plan["clips"][0].get("st", 0))
            face_tops = []
            for dt in (0.1, 0.5, 0.9, 1.3, 1.7, 2.1, 2.4):
                cap = cv2.VideoCapture(hook_src)
                cap.set(cv2.CAP_PROP_POS_MSEC, (hook_st + dt) * 1000)
                ret, hook_frame = cap.read()
                cap.release()
                if ret and hook_frame is not None:
                    pose_lm = _load_pose_landmarks(hook_frame)
                    if pose_lm is not None:
                        face_m = FaceAnalyzer().analyze(
                            hook_frame, pose_landmarks=pose_lm)
                        if face_m and face_m.get("face_detected"):
                            face_tops.append(float(face_m["face_top_y"]))
            if face_tops:
                face_top = min(face_tops)   # 最高人脸顶（最小归一化 y）
                # Hook 卡底边距人脸顶 30px（卡高取 constraints 的全局统一值）
                hc_c = self.gateway.constraints.get("hook_card", {}) or {}
                card_h = int(hc_c.get("height", 168) or 168)
                gap = int(hc_c.get("face_top_gap_px", 30) or 30)
                y_min = int(hc_c.get("y_min", 150) or 150)
                # 额外 10px 缓冲：覆盖采样间隙的人脸位移与检测误差
                card_y = int(max(y_min, face_top * 1920 - card_h - gap - 10))
                plan.setdefault("hook_card", {})["y"] = card_y
                plan["hook_card"]["face_top_y"] = face_top
                print(f"  [Hook 卡避让] {len(face_tops)} 帧采样最高人脸顶 "
                      f"y={face_top:.3f} → Hook 卡 y={card_y}"
                      f"（卡底 {card_y + card_h}，间距 "
                      f"{face_top * 1920 - card_y - card_h:.0f}px）")
        except Exception as e:
            print(f"  [Hook 卡避让] 计算失败（用默认 y=280）: {e}")

        # B12：Hook 卡避让结果校验（压脸则回退默认值并记录）
        hc_ok, hc_violations = self.gateway.validate_hook_card(plan)
        if not hc_ok:
            plan["hook_card"] = {"y": 280}
            self.gateway.record_generic(
                "hook_card_avoidance", False,
                [f"避让结果不合格({hc_violations})，回退默认 y=280"],
                plan.get("hook_card", {}))
            print(f"  [Hook 卡校验] 避让结果不合格 {hc_violations}，回退默认 y=280")
        else:
            self.gateway.record_generic(
                "hook_card_avoidance", True,
                ["Hook 卡位置满足不压脸约束（B10）"],
                plan.get("hook_card", {}))

        # 决策 2.4: 放大镜锚点分配（2026-10-27 反馈驱动：放大镜圆圈细节展示）
        try:
            self._assign_magnifiers(plan)
        except Exception as e:
            print(f"  [MAGNIFIER] 锚点分配失败（不阻断，成片将无放大镜）: {e}")

        # 决策 2.5: 违规词处置（2026-10-30 简化：Agent 一轮复核 → 仅字幕删词）
        try:
            verified_cache = os.path.join(self.work_dir,
                                          "whisper_segments_verified.json")
            if not os.path.exists(verified_cache):
                verified_cache = os.path.join(self.work_dir, "whisper_segments.json")
            if os.path.exists(verified_cache):
                with open(verified_cache, "r", encoding="utf-8") as f:
                    all_segments = json.load(f)
                from src.tools.apply_violation_handling import (
                    prepare_violation_handling)
                vh = prepare_violation_handling(plan, all_segments, self.work_dir,
                                                self.constraints)
                # Agent 字幕复核归档：归一结果 + 违规裁决，供人审与 QC 追溯
                try:
                    review = {
                        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "normalization": getattr(self, "_norm_report", None),
                        "violation_summary": vh.get("summary"),
                        "decisions": vh.get("decisions"),
                    }
                    with open(os.path.join(self.work_dir,
                                           "asr_subtitle_review.json"),
                              "w", encoding="utf-8") as f:
                        json.dump(review, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    print(f"  [REVIEW] 复核报告落盘失败（不阻断）: {e}")
        except Exception as e:
            print(f"  [VIOLATION] 处置准备失败（不阻断）: {e}")

        # R3/R4（2026-11-01）：补齐分镜的画面特征（景别 / 展示角度签名）。
        # 参考成片实测：9/10 段为全身景别（body_h 中位 0.556），
        # 且 10 段呈现 10 种不同展示角度、相邻不重复——
        # 此前的选段完全不感知画面，必然出现连续同质镜头。
        try:
            from src.tools.extract_shot_features import annotate_clips
            annotate_clips(plan.get("clips", []), self.raw_video,
                           work_dir=self.work_dir)
            _fr = [c.get("framing") for c in plan.get("clips", [])]
            print(f"  [SHOT-FEATURES] 分镜画面特征已补齐 "
                  f"（全身景别 {_fr.count('full_body')}/{len(_fr)}）")
        except Exception as e:
            print(f"  [SHOT-FEATURES] 特征提取失败（不阻断）: {e}")

        # 挂角贴纸逐镜避脸（QC「叠加层人脸安全」BLOCK 的正解）
        try:
            self._assign_sticker_sides(plan)
        except Exception as e:
            print(f"  [STICKER-SIDE] 避脸计算失败（不阻断）: {e}")

        # 切片规则自检（R2~R6）：**必须在画面特征补齐之后**重算，
        # 否则 R3（景别）/R4（角度）拿到的是无特征的空数据（实测误报 0% 全身）。
        try:
            from src.tools.slicing_rules import evaluate_plan_slicing
            _sr = evaluate_plan_slicing(plan.get("clips", []), self.spec,
                                       self.constraints)
            plan["slicing_rules"] = _sr
            # 清掉 build_plan 阶段的**过期**提示（当时尚无画面特征，R3/R4 会误报）
            try:
                _notes = plan.get("selection_meta", {}).get("notes")
                if isinstance(_notes, list):
                    plan["selection_meta"]["notes"] = [
                        _n for _n in _notes
                        if not str(_n).startswith("切片规则 ")]
            except Exception:
                pass
            for _c in _sr.get("checks", []):
                _m = "PASS" if _c.get("passed") else "WARN"
                print(f"  [SLICING] [{_m}] {_c['id']}: {_c.get('detail')}")
        except Exception as e:
            print(f"  [SLICING][WARN] 规则自检失败（不阻断）: {e}")

        plan_path = os.path.join(self.work_dir, "plan.json")
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)

        # -------------------------------------------------------------
        # Phase 3: 工具链执行确定性渲染与混流
        # -------------------------------------------------------------
        print("\n>>> [PHASE 3] 工具链执行成片渲染与混流...")

        material_dir = self.get_material_dir()
        os.makedirs(material_dir, exist_ok=True)

        final_cover_path = os.path.join(self.output_dir, f"{self.project_id}.jpg")
        cover_meta_path = os.path.join(self.output_dir, f"{self.project_id}_cover_render_meta.json")
        render_cover_with_meta(
            base_frame_path=selected_cover["image_path"],
            spec_data=self.spec,
            output_path=final_cover_path,
            meta_output_path=cover_meta_path,
        )

        final_mp4_path = os.path.join(self.output_dir, f"{self.project_id}.mp4")
        out_mp4, actual_dur = assemble_video_tool(
            plan_path=plan_path,
            output_mp4_path=final_mp4_path,
            work_dir=self.work_dir,
            cache_key=ck,
            # 必须透传：含 features 派生（render_profile=plain 决定是否烧字幕）
            constraints=self.constraints,
        )

        # -------------------------------------------------------------
        # Phase 4: 自动化质检门禁与交付签署
        # -------------------------------------------------------------
        print("\n>>> [PHASE 4] 自动化质检门禁核验（constraints v{}）...".format(
            self.constraints.get("version", 1)))
        auditor = QcAuditor(self.constraints)
        ass_path = os.path.join(self.work_dir, "subtitles.ass")
        qc_report_path = os.path.join(self.output_dir, "qc_report.json")
        candidates_json = os.path.join(cover_cand_dir, "cover_candidates.json")
        # B13：QC 阶段缓存（报告含全部断言明细才命中）
        qc_cached = None
        if ck:
            from src.tools.stage_cache import load_stage
            qc_cached = load_stage(self.work_dir, "qc", ck)
        if qc_cached is not None:
            print("[QC] 命中阶段缓存，跳过复测（约束与输入未变更）")
            passed = qc_cached.get("all_passed", False)
            auditor.report = qc_cached
        else:
            overlay_meta_path = os.path.join(self.work_dir, "video_overlay_meta.json")
            passed = auditor.audit_final_deliverables(
                video_path=out_mp4,
                cover_path=final_cover_path,
                ass_path=ass_path,
                report_output_path=qc_report_path,
                cover_meta_path=cover_meta_path,
                candidates_path=candidates_json,
                overlay_meta_path=overlay_meta_path,
                # 决策 6：参数类内容占比断言需要分镜表（I0 使能项）
                plan_path=plan_path,
                # 封面复测必须针对**实际交付**的候选：决策网关会逐个校验并
                # 拒绝不合规项，实际交付项未必是 candidates[0]（2026-11-01 实测）。
                chosen_cover_id=selected_cover.get("id"),
            )
            if ck:
                from src.tools.stage_cache import save_stage
                save_stage(self.work_dir, "qc", ck, auditor.report)

        # 产物直接交付并保存到用户提供的素材目录下，文件名与任务名字 (project_id) 完全一致
        target_mp4 = os.path.join(material_dir, f"{self.project_id}.mp4")
        target_cover = os.path.join(material_dir, f"{self.project_id}.jpg")
        target_qc = os.path.join(material_dir, f"{self.project_id}_qc_report.json")

        if os.path.abspath(final_mp4_path) != os.path.abspath(target_mp4):
            shutil.copy2(out_mp4, target_mp4)
        if os.path.abspath(final_cover_path) != os.path.abspath(target_cover):
            shutil.copy2(final_cover_path, target_cover)
        if os.path.exists(qc_report_path) and os.path.abspath(qc_report_path) != os.path.abspath(target_qc):
            shutil.copy2(qc_report_path, target_qc)

        # 兼容旧版读取：在 output_dir 留存 _final.mp4 和 _cover.jpg 副本
        legacy_mp4 = os.path.join(self.output_dir, f"{self.project_id}_final.mp4")
        legacy_cover = os.path.join(self.output_dir, f"{self.project_id}_cover.jpg")
        if not os.path.exists(legacy_mp4) and os.path.exists(out_mp4):
            shutil.copy2(out_mp4, legacy_mp4)
        if not os.path.exists(legacy_cover) and os.path.exists(final_cover_path):
            shutil.copy2(final_cover_path, legacy_cover)

        decision_manifest = {
            "project_id": self.project_id,
            "decision_timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "constraints_version": self.constraints.get("version", 1),
            "cache_key": ck,
            "chosen_cover": selected_cover,
            "chosen_clips": plan["clips"],
            "selection_meta": plan.get("selection_meta"),
            "scene_cuts": scene_cuts,
            "gateway_decisions": self.gateway.decisions,
            "qc_passed": passed,
            "qc_warnings": auditor.report.get("warnings", []),
            "total_duration": actual_dur,
            # 2026-10-27 新增：一致性预检 / 对话脚本 / 违规处理登记
            "consistency_check": consistency_report,
            "dialogue_script_stats": dialogue_stats,
            "final_deliverables": {
                "video": target_mp4,
                "cover": target_cover,
                "cover_render_meta": cover_meta_path,
                "qc_report": target_qc,
                "output_dir_video": out_mp4,
                "output_dir_cover": final_cover_path,
            },
        }
        manifest_path = os.path.join(self.output_dir, "agent_decision_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(decision_manifest, f, ensure_ascii=False, indent=2)

        elapsed = time.time() - start_time
        print(f"\n=======================================================")
        print(f"Agent 导演总装与质检验收完成! 总耗时: {elapsed:.1f} 秒")
        print(f"最终成片（已保存至素材目录）: {target_mp4} ({actual_dur:.2f}s)")
        print(f"高定封面（已保存至素材目录）: {target_cover}")
        print(f"质检报告（已保存至素材目录）: {target_qc}")
        print(f"决策清单: {manifest_path}")
        print(f"质检结论: {'ALL PASS' if passed else 'BLOCK FAILED'}"
              f"（{len(auditor.report.get('warnings', []))} 条 WARN）")
        print(f"=======================================================\n")
        return decision_manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="抖音带货直播剪辑 Agent 总导演中枢 v2")
    parser.add_argument("--spec", default="job_spec.json")
    parser.add_argument("--cover-id", default=None)
    parser.add_argument("--plan", default=None)
    args = parser.parse_args()

    director = AgentDirector(args.spec)
    director.run_director_workflow(
        chosen_cover_id=args.cover_id,
        custom_plan_path=args.plan,
    )
