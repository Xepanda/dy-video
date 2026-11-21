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
from typing import Dict, Any, Optional, List, Tuple

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
from src.core.project_context import ProjectPaths, is_historical_output, InputManifest
from src.qc.qc_auditor import QcAuditor
from src.agent.decision_gateway import DecisionGateway, DecisionViolation

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def assign_sticker_sides(plan: Dict[str, Any], constraints: Optional[Dict[str, Any]] = None, default_raw_video: Optional[str] = None) -> None:
    """为每个分镜挑选**不压脸**的挂角贴纸边（左/右与高度）。

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
    constraints = constraints or {}
    st_c = (constraints.get("video_sticker", {}) or {})
    size = int(st_c.get("size", 220) or 220)
    mx = int(st_c.get("margin_x", 40) or 40)
    my = int(st_c.get("margin_y", 300) or 300)
    default_side = str(st_c.get("side", "right") or "right")
    fa = FaceAnalyzer()
    out_w = 1080
    for c in plan.get("clips", []):
        src = c.get("source_file") or default_raw_video
        st = float(c.get("st", 0.0))
        dur = float(c.get("dur", 0.0))
        if dur <= 0.2:
            continue
        delay_r = float(st_c.get("delay_ratio", 0.10) or 0.10)
        hold_r = float(st_c.get("hold_ratio", 0.72) or 0.72)
        t_in = st + dur * delay_r
        t_out = min(st + dur * (delay_r + hold_r), st + dur - 0.05)

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

        if str(c.get("effect") or "") == "hflip":
            faces = [
                (out_w - fx1, fy0, out_w - fx0, fy1)
                for (fx0, fy0, fx1, fy1) in faces
            ]

        z = float(c.get("zoom", 1.0) or 1.0)
        if z > 1.0:
            zc = (constraints.get("zoom_effect", {}) or {})
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


class AgentDirector:
    def __init__(
        self,
        spec_path: str,
        cache_key: str = None,
        use_cache: bool = True,
        agent_adapter: Optional[Any] = None,
        project_paths: Optional[Any] = None,
        run_id: Optional[str] = None,
    ):
        self.spec_path = os.path.abspath(spec_path)
        with open(self.spec_path, "r", encoding="utf-8") as f:
            self.spec = json.load(f)
        self.agent_adapter = agent_adapter
        self.run_id = run_id

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

        # 统一项目路径管理 (ProjectPaths)
        if project_paths:
            self.project_paths = project_paths
        else:
            spec_dir = os.path.dirname(self.spec_path)
            if self.spec.get("project_root"):
                proj_root = self.spec["project_root"]
            elif os.path.basename(spec_dir) in ("素材", "materials", "input"):
                proj_root = os.path.dirname(spec_dir)
            elif os.path.isdir(os.path.join(spec_dir, "素材")):
                proj_root = spec_dir
            else:
                norm_spec = os.path.normpath(spec_dir)
                norm_repo = os.path.normpath(PROJECT_ROOT)
                if norm_spec.startswith(norm_repo):
                    proj_root = os.path.join(PROJECT_ROOT, "data", "projects", self.project_id)
                else:
                    proj_root = spec_dir
            self.project_paths = ProjectPaths(proj_root, run_id=self.run_id)

        self.work_dir = self.project_paths.work_dir
        self.output_dir = self.project_paths.output_dir
        self.candidates_dir = os.path.join(self.work_dir, "candidates")
        self.project_paths.ensure_runtime_dirs()
        os.makedirs(self.candidates_dir, exist_ok=True)

        # —— 多源支持：通过统一 InputManifest 安全解析，排除历史成片 ——
        self.raw_videos = self._collect_source_videos()
        self.raw_video = self.raw_videos[0]

        self.brand_info = self.spec.get("brand", {})
        self.product_info = self.spec.get("product", {})
        self.options = self.spec.get("options", {})

        # —— 商品位占位兜底（需求 1：2026-10-21，绝不修改用户磁盘 spec）——
        self._ensure_product_cards()

        # B12：决策约束网关——Agent 每次选择先过约束校验，不合格拒绝重选
        self.gateway = DecisionGateway(self.constraints)

        # B13：阶段缓存键（sha256(视频+spec+constraints)）；干预模式由工作流禁用
        self.use_cache = bool(use_cache)
        self.cache_key = cache_key
        if self.use_cache and not self.cache_key:
            from src.tools.stage_cache import compute_pipeline_key
            self.cache_key = compute_pipeline_key(self.raw_videos, self.spec_path)

    # 视频扩展名（同一商品的多段直播素材）
    VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".flv", ".m4v", ".ts")

    def _ensure_product_cards(self) -> None:
        """商品位兜底：真实商品图缺失时，用占位示例图补齐版面（绝不修改用户磁盘 spec）。"""
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
            product["item_images"] = existing
            return
        n_cards = int(self.options.get("placeholder_cards", 2) or 2)
        size = int((self.constraints.get("sticker", {}) or {}).get("size", 280) or 280)
        ph_dir = os.path.join(self.work_dir, "placeholders")
        try:
            ensure_placeholder_cards(self.spec, self.project_id, out_dir=ph_dir, n_cards=n_cards, size=size)
        except Exception as e:
            print(f"[WARN] 商品位占位补齐失败（不阻断，封面将无贴图）: {e}")
            return
        self.product_info = self.spec.get("product", {})

    def _write_spec_back(self):
        """已废弃：禁止运行时修改或覆盖用户原始 job_spec.json。
        如需序列化运行时 spec，写入当前 run 的 work_dir。
        """
        try:
            runtime_spec_path = os.path.join(self.work_dir, "job_spec.runtime.json")
            with open(runtime_spec_path, "w", encoding="utf-8") as f:
                json.dump(self.spec, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _collect_source_videos(self) -> list:
        """收集本任务的全部有效源素材，严格排除历史成片。"""
        if not hasattr(self, "project_paths") or not self.project_paths:
            base_dir = os.path.dirname(os.path.abspath(getattr(self, "spec_path", "") or "."))
            self.project_paths = ProjectPaths(project_root=base_dir)

        manifest_mgr = InputManifest(self.project_paths)
        explicit_paths = self.spec.get("raw_video_paths")
        if explicit_paths is not None:
            manifest = manifest_mgr.scan_and_resolve(explicit_video_paths=explicit_paths)
        else:
            primary = self.spec.get("raw_video_path")
            if primary:
                primary_abs = resolve_path(primary, self.project_paths.materials_dir) or os.path.abspath(primary)
                if os.path.isdir(primary_abs):
                    manifest = manifest_mgr.scan_and_resolve()
                elif os.path.isfile(primary_abs):
                    manifest = manifest_mgr.scan_and_resolve(explicit_video_paths=[primary_abs])
                else:
                    manifest = manifest_mgr.scan_and_resolve()
            else:
                manifest = manifest_mgr.scan_and_resolve()

        self.manifest = manifest
        paths = [s["path"] for s in manifest.get("source_videos", [])]
        if not paths:
            # 兼容既有初始化容错契约（仅当未显式指定 raw_video_paths 时）
            if not explicit_paths:
                primary = self.spec.get("raw_video_path")
                if primary and not os.path.exists(primary):
                    return [primary]
            raise FileNotFoundError(
                f"未找到任何可用的源视频素材（raw_video_path={self.spec.get('raw_video_path')}）"
            )
        return paths

    def _assign_sticker_sides(self, plan: Dict[str, Any]) -> None:
        """委托模块级 assign_sticker_sides 处理。"""
        assign_sticker_sides(plan, self.constraints, self.raw_video)

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
        """获取用户提供的素材所在目录（绝对路径）。"""
        if hasattr(self, "project_paths") and self.project_paths:
            return self.project_paths.materials_dir
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
        edited_timeline_path: Optional[str] = None,
        custom_sequence_proposal: Optional[Dict[str, Any]] = None,
        agent_adapter: Optional[Any] = None,
        cancel_token: Optional[Any] = None,
        progress_callback: Optional[Any] = None,
    ) -> Dict[str, Any]:
        if agent_adapter is not None:
            self.agent_adapter = agent_adapter
        self.cancel_token = cancel_token
        start_time = time.time()

        def _check_cancel():
            if cancel_token:
                is_c = getattr(cancel_token, "is_canceled", False)
                if callable(is_c):
                    is_c = is_c()
                if is_c:
                    from src.runtime.job_runner import JobCanceledError
                    raise JobCanceledError("任务已被用户取消")

        def _report(state: str, pct: float, msg: str):
            if progress_callback:
                progress_callback(state, pct, msg)

        _check_cancel()
        _report("ANALYZING", 0.15, "正在初始化项目环境与候选池扫描...")
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
        _check_cancel()
        _report("ANALYZING", 0.20, "正在提取镜头切换点与高光候选帧...")
        print(">>> [PHASE 1] 确定性工具链提取候选池...")

        # 1.1 镜头边界检测（多源：逐源检测保存字典 + 合并；B13 阶段缓存）
        scene_cuts = []
        scene_cuts_by_source: Dict[str, List[float]] = {}
        try:
            for v in self.raw_videos:
                cuts = detect_scene_cuts(v, cache_key=(ck + "|" + v) if ck else None,
                                          work_dir=self.work_dir)
                scene_cuts_by_source[v] = cuts
                scene_cuts.extend(cuts)
            print(f"    镜头边界: {len(scene_cuts)} 个切换点（{len(self.raw_videos)} 个源）")
        except Exception as e:
            scene_cuts = []
            scene_cuts_by_source = {}
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

        # 消费 cover_strategy 策略卡
        cover_strat = str(self.options.get("cover_strategy", "smart_frontal")).strip().lower()
        if cover_strat == "product_image":
            raw_imgs = []
            for im in self.product_info.get("item_images", []):
                if im and os.path.isfile(im):
                    raw_imgs.append(os.path.abspath(im))
            if hasattr(self, "manifest") and self.manifest:
                for im in self.manifest.get("product_images", []):
                    if im.get("path") and os.path.isfile(im["path"]):
                        raw_imgs.append(os.path.abspath(im["path"]))
            if raw_imgs:
                top_img = raw_imgs[0]
                p_cand = {
                    "id": "cand_product_image_01",
                    "timestamp": 0.0,
                    "image_path": top_img,
                    "frontal_score": 9999.0,
                    "strategy": "product_image",
                    "framing": {
                        "post_scale_sharpness": 800.0,
                        "knees_visible": True,
                        "achieved_width_ratio": 0.75,
                    },
                    "description": f"商品主图优先封面: {os.path.basename(top_img)}",
                }
                cover_candidates.insert(0, p_cand)
                print(f"    [COVER-STRATEGY] 已根据商品主图优先策略注入封面候选: {top_img}")

        if not cover_candidates:
            placeholder_path = os.path.join(cover_cand_dir, "cover_placeholder.jpg")
            if not os.path.isfile(placeholder_path):
                from PIL import Image
                Image.new("RGB", (1080, 1920), color=(30, 30, 30)).save(placeholder_path)
            cover_candidates = [{
                "id": "cand_fallback_01",
                "timestamp": 0.0,
                "strategy": "fallback_placeholder",
                "frontal_score": 100.0,
                "image_path": placeholder_path,
                "description": "兜底占位封面帧",
            }]

        # 统一候选池落盘（按多目标分数降序，[0] 即 Agent 选定项，与 QC 读取约定一致）
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
        _check_cancel()
        _report("ANALYZING", 0.35, "正在提取语音转写并执行 ASR 审核与意群分析...")
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
            segments = []
            if os.path.exists(verified_cache):
                with open(verified_cache, "r", encoding="utf-8") as f:
                    segments = json.load(f)

            # —— V3 Agent 总导演决策流（PR-01~PR-08 全链路编排）——
            plan = None
            plan_source = "v3_agent_director"
            plan_forced = False
            try:
                from src.core.product_profile import build_product_profile
                from src.core.utterance_aligner import UtteranceAligner
                from src.core.candidate_generator import CandidateGenerator
                from src.core.sku_tracker import SkuTracker
                from src.core.candidate_ranker import CandidateRanker
                from src.core.sequence_optimizer import SequenceOptimizer
                from src.core.plan_compiler import compile_v3_plan

                # 1. 建立商品档案
                product_profile = build_product_profile(
                    self.spec, spec_dir=self.work_dir, strict_missing_images=False, validate=False
                )

                # 2. 意群对齐
                aligner = UtteranceAligner(merge_gap_ms=600, min_utterance_ms=1200, max_utterance_ms=10000)
                raw_by_source = {}
                for s in segments:
                    src = s.get("source_file") or self.raw_video
                    raw_by_source.setdefault(src, []).append(s)

                utterances_by_source = {}
                for src, src_segs in raw_by_source.items():
                    utterances_by_source[src] = aligner.align_segments(src_segs, source_id=src)

                # 3. 候选片段挖掘
                _check_cancel()
                _report("ANALYZING", 0.50, "正在挖掘候选片段并执行 SKU 视觉与卖点对齐...")
                generator = CandidateGenerator(min_candidate_ms=1800, max_candidate_ms=12000, target_candidate_ms=5000)
                all_candidates = []
                for src, utts in utterances_by_source.items():
                    shots_for_src = scene_cuts_by_source.get(src, [])
                    cands = generator.generate_candidates(
                        utts, product_profile, source_id=src, shots=shots_for_src, source_file=src
                    )
                    all_candidates.extend(cands)

                # 4. 商品关系追踪（真实接通候选视频帧与视觉比对）
                from src.core.frame_provider import VideoFrameProvider
                from src.core.sequence_optimizer import InsufficientEvidenceError
                from src.runtime.adapters.agent_protocol import AgentAuthRequiredError, AgentPermissionDeniedError, AgentStatus
                policy = (self.spec.get("options", {}).get("automation", {}).get("quality_policy") or "standard")
                is_strict_policy = (
                    policy == "strict" or
                    self.spec.get("options", {}).get("automation", {}).get("visual_verification") == "required" or
                    self.options.get("automation", {}).get("quality_policy") == "strict"
                )
                source_reg = {}
                if hasattr(self, "manifest") and self.manifest:
                    for s in self.manifest.get("source_videos", []):
                        source_reg[s["source_id"]] = s["path"]
                        if s.get("filename"):
                            source_reg[s["filename"]] = s["path"]
                if hasattr(self, "raw_videos"):
                    for idx, rv in enumerate(self.raw_videos, 1):
                        source_reg[f"src_{idx:02d}"] = rv
                        source_reg[os.path.basename(rv)] = rv
                frame_provider = VideoFrameProvider(
                    work_dir=self.work_dir,
                    default_video_path=self.raw_video,
                    source_registry=source_reg,
                )
                sku_tracker = SkuTracker()
                relation_decisions = sku_tracker.track_candidates(
                    all_candidates, product_profile, frame_provider=frame_provider, strict_vision=is_strict_policy
                )
                self._all_candidates_map = {c["candidate_id"]: c for c in all_candidates}

                # 5. 多模态候选精排
                ranker = CandidateRanker()
                candidate_scores = ranker.score_candidates(
                    all_candidates, product_profile, relation_decisions=relation_decisions
                )

                # 6. 全局序列优化与 Agent 编排
                _check_cancel()
                _report("DECIDING", 0.65, "正在执行排片序列优化与 Agent 镜头编排...")
                scores_map = {s["candidate_id"]: s for s in candidate_scores.get("scores", [])}
                eligible_candidates = [
                    c for c in all_candidates
                    if scores_map.get(c["candidate_id"], {}).get("eligible") is True
                    and scores_map.get(c["candidate_id"], {}).get("product_relation", c.get("product_relation")) in ("SAME_SKU", "SAME_SKU_VARIANT", "ACCESSORY_OR_STYLING")
                ]
                allowed_cids_set = {c["candidate_id"] for c in eligible_candidates}
                dur_range = (self.constraints.get("video", {}) or {}).get("duration_range", [45, 65])

                if custom_sequence_proposal:
                    for cid in custom_sequence_proposal.get("ordered_candidate_ids", []):
                        if cid not in allowed_cids_set:
                            raise ValueError(f"自定义提案包含未知或不合格候选 ID: {cid}")
                    opt_res = {"status": "PROPOSED", "proposals": [custom_sequence_proposal]}
                elif getattr(self, "agent_adapter", None):
                    is_avail, avail_msg = self.agent_adapter.check_availability()
                    if not is_avail:
                        raise AgentAuthRequiredError(f"AGY CLI 工具未就绪: {avail_msg}")
                    schema_p = os.path.join(PROJECT_ROOT, "contracts", "schemas", "sequence_proposals.schema.json")
                    eligible_summary = [
                        {
                            "candidate_id": c["candidate_id"],
                            "role": scores_map.get(c["candidate_id"], {}).get("narrative_role") or c.get("narrative_role") or "PROOF",
                            "duration_ms": c["source_end_ms"] - c["source_start_ms"],
                            "text": c.get("text", "")[:40],
                            "product_relation": scores_map.get(c["candidate_id"], {}).get("product_relation") or c.get("product_relation", "SAME_SKU"),
                            "selling_points": c.get("selling_point_evidence_refs", []),
                        }
                        for c in eligible_candidates
                    ]
                    style_preset = str(self.options.get("style_preset", "balanced")).strip().lower()
                    prompt = (
                        f"任务ID: {self.project_id}\n"
                        f"风格偏好: {style_preset}\n"
                        f"商品档案: {json.dumps(product_profile, ensure_ascii=False)}\n"
                        f"候选片段池: {json.dumps(eligible_summary, ensure_ascii=False)}\n"
                        f"目标成片时长区间: {dur_range[0]}s ~ {dur_range[1]}s\n"
                        "请依据 sequence_proposals.schema.json 契约，从候选片段池中选取片段并排序，生成优选镜头序列提案。"
                    )
                    try:
                        resp = self.agent_adapter.invoke(prompt, schema_p, self.work_dir, cancellation_token=self.cancel_token)
                    except TypeError:
                        resp = self.agent_adapter.invoke(prompt, schema_p, self.work_dir)

                    if resp.status == AgentStatus.ERROR and "取消" in (resp.error_message or ""):
                        from src.runtime.job_runner import JobCanceledError
                        raise JobCanceledError("AGY 调用已被用户取消")
                    elif resp.status == AgentStatus.AUTH_REQUIRED:
                        raise AgentAuthRequiredError(resp.error_message or "AGY 未登录或凭证过期")
                    elif resp.status == AgentStatus.PERMISSION_DENIED:
                        raise AgentPermissionDeniedError(resp.error_message or "AGY 权限被软拒绝")
                    elif resp.status == AgentStatus.SUCCESS:
                        s_out = resp.structured_output or {}
                        if s_out.get("status") == "INSUFFICIENT_EVIDENCE":
                            gaps = s_out.get("evidence_gap") or ["Agent 判定素材证据不足"]
                            raise InsufficientEvidenceError(f"本品素材证据不足: {'; '.join(gaps)}")
                        proposals = s_out.get("proposals", [])
                        if not proposals or not isinstance(proposals, list):
                            raise ValueError("Agent 未输出有效提案列表")
                        for p in proposals:
                            used_spans: Dict[str, List[Tuple[int, int]]] = {}
                            for cid in p.get("ordered_candidate_ids", []):
                                if cid not in allowed_cids_set:
                                    raise ValueError(f"Agent 决策包含未知幻觉候选 ID: {cid}")
                                cand_obj = self._all_candidates_map.get(cid)
                                if cand_obj:
                                    src_k = cand_obj.get("source_id") or cand_obj.get("source_file") or "src_0"
                                    st_ms = cand_obj.get("source_start_ms", 0)
                                    en_ms = cand_obj.get("source_end_ms", 0)
                                    for prev_st, prev_en in used_spans.get(src_k, []):
                                        if max(st_ms, prev_st) < min(en_ms, prev_en):
                                            raise ValueError(f"Agent 提案包含同源时间重叠片段: {cid} 与既选片段在 {src_k} 上重叠 [{st_ms}, {en_ms}]")
                                    used_spans.setdefault(src_k, []).append((st_ms, en_ms))
                        opt_res = s_out
                    else:
                        raise RuntimeError(f"Agent 决策调用失败: {resp.error_message}")
                else:
                    if len(eligible_candidates) == 0:
                        raise InsufficientEvidenceError("本品素材证据不足: 所有候选片段均未通过精排门禁 (eligible=True)")
                    style_preset = str(self.options.get("style_preset", "balanced")).strip().lower()
                    opt = SequenceOptimizer(
                        target_min_ms=int(dur_range[0] * 1000),
                        target_max_ms=int(dur_range[1] * 1000),
                        preferred_style=style_preset,
                    )
                    opt_res = opt.optimize_sequence(
                        all_candidates, candidate_scores, product_profile, preferred_style=style_preset
                    )

                if opt_res.get("status") == "PROPOSED" and opt_res.get("proposals"):
                    chosen_proposal = opt_res["proposals"][0]
                    plan = compile_v3_plan(
                        chosen_proposal,
                        all_candidates,
                        candidate_scores,
                        self.spec,
                        constraints=self.constraints,
                        raw_video_path=self.raw_video,
                    )
                    print(f"  [V3 DIRECT-PLAN] 成功生成 V3 全局导演排片方案: {chosen_proposal['proposal_id']} "
                          f"（风格={chosen_proposal.get('style')}，{len(plan['clips'])} 个分镜，预期时长 {plan['total_expected_duration']}s）")
                elif opt_res.get("status") == "INSUFFICIENT_EVIDENCE":
                    gaps = opt_res.get("evidence_gap") or ["可用本品素材累计时长不足"]
                    raise InsufficientEvidenceError(f"本品素材证据不足: {'; '.join(gaps)}")
            except (InsufficientEvidenceError, AgentAuthRequiredError, AgentPermissionDeniedError):
                # 内容证据不足与授权错误均为关键门禁，绝不允许静默降级到旧版 build_plan 强行凑片！
                raise
            except ValueError as ve:
                # 包含未知幻觉候选 ID 或契约违规，坚决阻断，严禁静默回退启发式凑片
                if getattr(self, "agent_adapter", None) or custom_sequence_proposal:
                    raise
                print(f"  [V3 PLAN][WARN] V3 编排参数异常，降级回退至启发式 build_plan: {ve}")
                plan = None
            except Exception as v3_err:
                if getattr(self, "agent_adapter", None) or custom_sequence_proposal:
                    raise
                print(f"  [V3 PLAN][WARN] V3 编排技术异常，降级回退至启发式 build_plan: {v3_err}")
                plan = None

            if not plan:
                # 降级回退
                pauses_by_source = self._detect_speech_pauses()
                plan = build_plan(self.raw_video, segments, self.spec,
                                  self.constraints, scene_cuts=scene_cuts,
                                  cache_key=ck, work_dir=self.work_dir,
                                  pauses_by_source=pauses_by_source,
                                  scene_cuts_by_source=scene_cuts_by_source)
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

        # 分镜品类与语义对齐门禁（S2 步骤 6 接线：2026-11 新增，BLOCK 门禁）
        try:
            from src.tools.verify_clip_product_alignment import verify_clip_alignment
            alignment_report = verify_clip_alignment(
                plan, self.spec, constraints=self.constraints, work_dir=self.work_dir
            )
            plan["alignment_report"] = alignment_report
            for _c in alignment_report.get("checks", []):
                _st = _c.get("status", "PASS")
                print(f"  [ALIGNMENT] [{_st}] {_c.get('name')}: {_c.get('detail')}")
            if not alignment_report.get("passed"):
                _v = alignment_report.get("violations", [])
                print(f"  [ALIGNMENT][BLOCK] 分镜对齐检出违规 (BLOCK): {_v}")
                raise RuntimeError(f"分镜品类与语义对齐门禁拦截 (BLOCK): {_v}")
        except RuntimeError:
            raise
        except Exception as e:
            print(f"  [ALIGNMENT][WARN] 对齐校验工具执行异常（降级放行）: {e}")

        # 决策 2.7: 视听接缝连续性审查与切点微调（V3 PR-07 ContinuityAuditor & CutOptimizer）
        continuity_review = None
        try:
            from src.qc.continuity_qc import ContinuityAuditor
            from src.core.cut_optimizer import CutOptimizer
            c_auditor = ContinuityAuditor()
            cand_map = getattr(self, "_all_candidates_map", None) or {}
            continuity_review = c_auditor.audit_sequence_seams(plan.get("clips", []), cand_map)
            cont_path = os.path.join(self.output_dir, "continuity_review.json")
            with open(cont_path, "w", encoding="utf-8") as f:
                json.dump(continuity_review, f, ensure_ascii=False, indent=2)
            plan["continuity_review"] = continuity_review
            cut_opt = CutOptimizer()
            fixed_clips, opt_actions = cut_opt.optimize_cuts(plan.get("clips", []), continuity_review, plan=plan)
            if opt_actions:
                plan["clips"] = fixed_clips
                print(f"  [CONTINUITY] 接缝微调完成 ({len(opt_actions)} 处)")
        except Exception as ce:
            print(f"  [CONTINUITY][WARN] 连续性审计异常（跳过）: {ce}")

        plan_path = os.path.join(self.work_dir, "plan.json")
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)

        # -------------------------------------------------------------
        # Phase 3: 工具链执行确定性渲染与混流
        # -------------------------------------------------------------
        _check_cancel()
        _report("RENDERING", 0.80, "正在渲染高定封面并执行音视频轨道合流...")
        print("\n>>> [PHASE 3] 工具链执行成片渲染与混流...")

        material_dir = self.get_material_dir()
        os.makedirs(material_dir, exist_ok=True)

        final_cover_path = os.path.join(self.output_dir, f"{self.project_id}.jpg")
        cover_meta_path = os.path.join(self.output_dir, f"{self.project_id}_cover_render_meta.json")
        base_fp = selected_cover.get("image_path")
        if not base_fp or not os.path.isfile(base_fp):
            base_fp = os.path.join(self.work_dir, "cover_candidates", "cover_placeholder.jpg")
            if not os.path.isfile(base_fp):
                from PIL import Image
                os.makedirs(os.path.dirname(base_fp), exist_ok=True)
                Image.new("RGB", (1080, 1920), color=(30, 30, 30)).save(base_fp)

        render_cover_with_meta(
            base_frame_path=base_fp,
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
            cancel_token=self.cancel_token,
        )

        # -------------------------------------------------------------
        # Phase 4: 自动化质检门禁与交付签署
        # -------------------------------------------------------------
        _check_cancel()
        _report("QC", 0.90, "正在执行技术参数与内容层双重质检门禁审核...")
        print("\n>>> [PHASE 4] 自动化质检门禁核验（constraints v{}）...".format(
            self.constraints.get("version", 1)))
        auditor = QcAuditor(self.constraints)
        ass_path = os.path.join(self.work_dir, "subtitles.ass")
        qc_report_path = os.path.join(self.output_dir, "qc_report.json")
        candidates_json = os.path.join(cover_cand_dir, "cover_candidates.json")
        # B13：QC 阶段缓存（包含 plan_hash，计划变更时必须重新质检）
        import hashlib
        with open(plan_path, "rb") as pf:
            plan_hash = hashlib.sha256(pf.read()).hexdigest()[:16]
        qc_ck = f"{ck}|{plan_hash}" if ck else None

        qc_cached = None
        if qc_ck:
            from src.tools.stage_cache import load_stage
            qc_cached = load_stage(self.work_dir, "qc", qc_ck)
        if qc_cached is not None and qc_cached.get("all_passed", False):
            print("[QC] 命中阶段缓存，跳过复测（全绿已通过）")
            passed = True
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
            if qc_ck and passed:
                from src.tools.stage_cache import save_stage
                save_stage(self.work_dir, "qc", qc_ck, auditor.report)

        # 4.2 电商内容层与业务质量门禁（V3 PR-08 EditorialQcAuditor）
        editorial_report = None
        try:
            from src.qc.editorial_qc import EditorialQcAuditor
            policy = (self.spec.get("options", {}).get("automation", {}).get("quality_policy") or "standard")
            ed_auditor = EditorialQcAuditor(policy=policy)
            product_profile = None
            if self.spec.get("product_profile_path") and os.path.isfile(self.spec["product_profile_path"]):
                try:
                    with open(self.spec["product_profile_path"], "r", encoding="utf-8") as pf:
                        product_profile = json.load(pf)
                except Exception:
                    pass
            if not product_profile:
                from src.core.product_profile import build_product_profile
                product_profile = build_product_profile(self.spec, spec_dir=self.work_dir, strict_missing_images=False, validate=False)

            editorial_report = ed_auditor.audit_editorial_quality(plan, product_profile, continuity_review)
            ed_report_path = os.path.join(self.output_dir, "editorial_qc_report.json")
            with open(ed_report_path, "w", encoding="utf-8") as f:
                json.dump(editorial_report, f, ensure_ascii=False, indent=2)
            print(f"  [EDITORIAL-QC] {editorial_report.get('summary')}")
            if not editorial_report.get("passed", True) or editorial_report.get("block_count", 0) > 0:
                print(f"  [EDITORIAL-QC][BLOCK] 内容质检未通过，触发阻断拦截 (block_count={editorial_report.get('block_count', 0)})")
                passed = False
        except Exception as ee:
            print(f"  [EDITORIAL-QC][FAIL-CLOSED] 内容质检异常，触发安全阻断: {ee}")
            passed = False

        if not editorial_report or not editorial_report.get("passed", False) or editorial_report.get("block_count", 0) > 0:
            print(f"  [EDITORIAL-QC][BLOCK] 内容质检未完全通过，触发阻断拦截")
            passed = False

        # 产物交付：严格写入 output_dir (output/<run_id>/)
        # 禁止向素材目录复制最终成片，禁止删除素材目录中的已有文件
        legacy_mp4 = os.path.join(self.output_dir, f"{self.project_id}_final.mp4")
        legacy_cover = os.path.join(self.output_dir, f"{self.project_id}_cover.jpg")
        if not os.path.exists(legacy_mp4) and os.path.exists(out_mp4):
            shutil.copy2(out_mp4, legacy_mp4)
        if not os.path.exists(legacy_cover) and os.path.exists(final_cover_path):
            shutil.copy2(final_cover_path, legacy_cover)

        # 更新最新产物指针 (output/latest.json)
        try:
            latest_info = {
                "project_id": self.project_id,
                "run_id": getattr(self.project_paths, "run_id", "default"),
                "output_dir": self.output_dir,
                "video_path": out_mp4 if passed else "",
                "cover_path": final_cover_path if passed else "",
                "qc_passed": passed,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(self.project_paths.latest_pointer, "w", encoding="utf-8") as lf:
                json.dump(latest_info, lf, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"  [DIRECTOR][WARN] 更新 latest.json 失败: {e}")

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
            "continuity_review": continuity_review,
            "editorial_qc": editorial_report,
            "final_deliverables": {
                "video": out_mp4 if passed else "",
                "cover": final_cover_path if passed else "",
                "cover_render_meta": cover_meta_path,
                "qc_report": qc_report_path if passed else "",
                "editorial_qc_report": ed_report_path if (editorial_report and passed) else "",
                "output_dir_video": out_mp4,
                "output_dir_cover": final_cover_path,
                "delivery_status": "DELIVERED" if passed else "QC_FAILED",
            },
        }
        manifest_path = os.path.join(self.output_dir, "agent_decision_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(decision_manifest, f, ensure_ascii=False, indent=2)

        elapsed = time.time() - start_time
        print(f"\n=======================================================")
        print(f"Agent 导演总装与质检验收完成! 总耗时: {elapsed:.1f} 秒")
        if passed:
            print(f"最终成片（输出交付目录）: {out_mp4} ({actual_dur:.2f}s)")
            print(f"高定封面（输出交付目录）: {final_cover_path}")
            print(f"质检报告（输出交付目录）: {qc_report_path}")
        else:
            print(f"成片质检未通过，已隔离至输出目录: {out_mp4}")
        print(f"决策清单: {manifest_path}")
        print(f"质检结论: {'ALL PASS' if passed else 'BLOCK FAILED'}"
              f"（{len(auditor.report.get('warnings', []))} 条 WARN）")
        print(f"=======================================================\n")
        _report("DELIVERED", 1.0, "任务已圆满完成交付")
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
