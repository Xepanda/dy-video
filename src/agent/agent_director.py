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
        """
        spec_dir = os.path.dirname(self.spec_path)
        spec_paths = self.spec.get("raw_video_paths") or []
        paths = []
        for p in spec_paths:
            rp = resolve_path(p, spec_dir)
            if rp and os.path.exists(rp) and rp not in paths:
                paths.append(rp)
        primary = self.spec.get("raw_video_path")
        if primary:
            primary_abs = resolve_path(primary, spec_dir) or os.path.abspath(primary)
            exists = os.path.exists(primary_abs)
            if exists and primary_abs not in paths:
                paths.insert(0, primary_abs)
            scan_dir = os.path.dirname(primary_abs)
            # 同目录其余视频一并纳入候选（跨素材选段）
            # 仅在主文件真实存在时扫描，避免占位路径把无关目录视频误当素材。
            if exists and os.path.isdir(scan_dir):
                for name in sorted(os.listdir(scan_dir)):
                    if not name.lower().endswith(self.VIDEO_EXTS):
                        continue
                    ap = os.path.abspath(os.path.join(scan_dir, name))
                    if ap not in paths:
                        paths.append(ap)
            if not exists and primary_abs not in paths:
                # 保持既有初始化容错契约：占位路径原样保留，由后续阶段报错
                paths.insert(0, primary_abs)
        if not paths:
            raise FileNotFoundError(
                f"未找到任何可用的源视频素材（raw_video_path={self.spec.get('raw_video_path')}）")
        return paths

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
            plan = build_plan(self.raw_video, segments, self.spec,
                              self.constraints, scene_cuts=scene_cuts,
                              cache_key=ck, work_dir=self.work_dir)
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
        try:
            import cv2
            from src.tools.detect_face import FaceAnalyzer, _load_pose_landmarks
            # 多源：Hook 卡避让需在首个分镜所属的素材文件上取人脸顶
            hook_src = plan["clips"][0].get("source_file") or self.raw_video
            cap = cv2.VideoCapture(hook_src)
            cap.set(cv2.CAP_PROP_POS_MSEC, plan["clips"][0]["st"] * 1000)
            ret, hook_frame = cap.read()
            cap.release()
            if ret and hook_frame is not None:
                pose_lm = _load_pose_landmarks(hook_frame)
                if pose_lm is not None:
                    face_m = FaceAnalyzer().analyze(hook_frame, pose_landmarks=pose_lm)
                    if face_m:
                        # Hook 卡高 130px，底边距人脸顶 30px
                        card_y = int(max(150, face_m["face_top_y"] * 1920 - 160))
                        plan.setdefault("hook_card", {})["y"] = card_y
                        plan["hook_card"]["face_top_y"] = face_m["face_top_y"]
                        print(f"  [Hook 卡避让] 人脸顶 y={face_m['face_top_y']:.3f} → "
                              f"Hook 卡 y={card_y}")
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

        plan_path = os.path.join(self.work_dir, "plan.json")
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)

        # -------------------------------------------------------------
        # Phase 3: 工具链执行确定性渲染与混流
        # -------------------------------------------------------------
        print("\n>>> [PHASE 3] 工具链执行成片渲染与混流...")

        final_cover_path = os.path.join(self.output_dir, f"{self.project_id}_cover.jpg")
        cover_meta_path = os.path.join(self.output_dir, f"{self.project_id}_cover_render_meta.json")
        render_cover_with_meta(
            base_frame_path=selected_cover["image_path"],
            spec_data=self.spec,
            output_path=final_cover_path,
            meta_output_path=cover_meta_path,
        )

        final_mp4_path = os.path.join(self.output_dir, f"{self.project_id}_final.mp4")
        out_mp4, actual_dur = assemble_video_tool(
            plan_path=plan_path,
            output_mp4_path=final_mp4_path,
            work_dir=self.work_dir,
            cache_key=ck,
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
            )
            if ck:
                from src.tools.stage_cache import save_stage
                save_stage(self.work_dir, "qc", ck, auditor.report)

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
            "final_deliverables": {
                "video": out_mp4,
                "cover": final_cover_path,
                "cover_render_meta": cover_meta_path,
                "qc_report": qc_report_path,
            },
        }
        manifest_path = os.path.join(self.output_dir, "agent_decision_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(decision_manifest, f, ensure_ascii=False, indent=2)

        elapsed = time.time() - start_time
        print(f"\n=======================================================")
        print(f"Agent 导演总装与质检验收完成! 总耗时: {elapsed:.1f} 秒")
        print(f"最终成片: {out_mp4} ({actual_dur:.2f}s)")
        print(f"高定封面: {final_cover_path}")
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
