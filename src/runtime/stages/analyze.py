# -*- coding: utf-8 -*-
"""
analyze.py - 阶段 2：镜头切点、封面提取、ASR 对齐、候选挖掘、视觉评估与导演简报冻结
"""

import os
import json
import time
import hashlib
from typing import Dict, Any, List

from src.core.project_context import (
    compute_full_file_sha256,
    sanitize_id,
    normalize_project_path,
)
from src.core.path_resolver import resolve_path
from src.core.utterance_aligner import UtteranceAligner
from src.core.candidate_generator import CandidateGenerator
from src.core.frame_provider import VideoFrameProvider
from src.core.sku_tracker import SkuTracker
from src.core.candidate_ranker import CandidateRanker
from src.tools.extract_cover_candidates import extract_cover_candidates, _load_constraints
from src.tools.analyze_story_segments import analyze_story_segments
from src.tools.detect_scenes import detect_scene_cuts
from src.runtime.stages._compat import resolve_dep
from src.runtime.stages.common import _log_event, _write_run_state, resolve_run_dir, compute_candidate_pool_hash
from src.runtime.stages.security import resolve_trusted_paths


def stage_analyze(run_arg: str) -> Dict[str, Any]:
    work_dir = resolve_run_dir(run_arg)
    context_path = os.path.join(work_dir, "run_context.json")
    if not os.path.isfile(context_path):
        raise FileNotFoundError(f"未找到 run_context.json: {context_path}，请先执行 prepare 阶段")

    with open(context_path, "r", encoding="utf-8") as f:
        context = json.load(f)

    paths = resolve_trusted_paths(work_dir, context)
    output_dir = paths.output_dir

    deliv_manifest_path = os.path.join(output_dir, "delivery_manifest.json")
    if os.path.isfile(deliv_manifest_path):
        raise RuntimeError(
            f"当前运行 ({context.get('run_id')}) 已完成正式交付签署，计划与产物已被锁定为不可变！如需修改计划请创建新 run_id"
        )

    spec_path = os.path.join(work_dir, "spec_snapshot.json")
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    manifest_path = os.path.join(work_dir, "input_manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    profile_path = os.path.join(work_dir, "product_profile.json")
    with open(profile_path, "r", encoding="utf-8") as f:
        product_profile = json.load(f)

    raw_videos = [s["path"] for s in manifest.get("source_videos", [])]
    if not raw_videos:
        raise ValueError("输入素材清单中无可用源视频")

    analysis_dir = os.path.join(work_dir, "analysis")
    candidates_dir = os.path.join(work_dir, "candidates")
    os.makedirs(analysis_dir, exist_ok=True)
    os.makedirs(candidates_dir, exist_ok=True)

    _write_run_state(work_dir, "ANALYZING", "analyze", 0.2, "正在提取镜头切点与封面候选...")
    _log_event(work_dir, "ANALYZE_START", "启动媒体分析阶段")

    detect_scenes_fn = resolve_dep("detect_scene_cuts", detect_scene_cuts)
    extract_covers_fn = resolve_dep("extract_cover_candidates", extract_cover_candidates)
    analyze_story_fn = resolve_dep("analyze_story_segments", analyze_story_segments)

    # 1. 镜头边界检测 + 语音相对凹陷气口检测（可选增强，失败不阻塞）
    scene_cuts_by_source: Dict[str, Any] = {}
    pauses_by_source: Dict[str, Any] = {}
    for v in raw_videos:
        try:
            cuts = detect_scenes_fn(v)
            scene_cuts_by_source[v] = cuts
        except Exception as e:
            print(f"[STAGE-ANALYZE][WARN] 镜头切点检测跳过 ({os.path.basename(v)}): {e}")
            scene_cuts_by_source[v] = []
        try:
            from src.tools.detect_speech_pauses import detect_pauses
            pauses_by_source[v] = detect_pauses(v)
        except Exception:
            pauses_by_source[v] = {}

    # 2. 封面候选提取
    constraints = _load_constraints()
    cover_cand_dir = os.path.join(candidates_dir, "cover_candidates")
    os.makedirs(cover_cand_dir, exist_ok=True)
    cover_candidates = []
    seen_cover_ids = set()
    ck = context.get("spec_fingerprint")
    for v in raw_videos:
        v_stem = sanitize_id(os.path.splitext(os.path.basename(v))[0])
        v_dir = os.path.join(cover_cand_dir, v_stem[-10:])
        os.makedirs(v_dir, exist_ok=True)
        try:
            cands = extract_covers_fn(
                video_path=v,
                output_dir=v_dir,
                spec_data=spec,
                constraints=constraints,
                cache_key=(ck + "|" + v) if ck else None,
                cache_work_dir=work_dir,
            )
            for c in cands:
                cid = c["id"]
                new_id = f"{cid}_{v_stem[-6:]}"
                if new_id not in seen_cover_ids:
                    seen_cover_ids.add(new_id)
                    c = dict(c, id=new_id, source_file=v)
                    cover_candidates.append(c)
        except Exception as e:
            print(f"[STAGE-ANALYZE][WARN] 封面提取跳过 ({os.path.basename(v)}): {e}")

    opts = spec.get("options", {}) or {}
    cover_strat = str(opts.get("cover_strategy", "smart_frontal")).strip().lower()
    if cover_strat == "product_image":
        raw_imgs = [im["path"] for im in manifest.get("product_images", []) if os.path.isfile(im.get("path", ""))]
        if not raw_imgs:
            for im in (spec.get("product", {}) or {}).get("item_images", []):
                rp = resolve_path(im, work_dir)
                if rp and os.path.isfile(rp):
                    raw_imgs.append(rp)
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

    if not cover_candidates:
        placeholder_path = os.path.join(cover_cand_dir, "cover_placeholder.jpg")
        if not os.path.isfile(placeholder_path):
            from PIL import Image
            Image.new("RGB", (1080, 1920), color=(30, 30, 30)).save(placeholder_path)
        cover_candidates.append({
            "id": "cand_fallback_01",
            "timestamp": 0.0,
            "strategy": "fallback_placeholder",
            "frontal_score": 100.0,
            "image_path": placeholder_path,
            "description": "兜底占位封面帧",
            "framing": {"post_scale_sharpness": 500.0, "achieved_width_ratio": 0.5},
        })

    with open(os.path.join(analysis_dir, "cover_candidates.json"), "w", encoding="utf-8") as f:
        json.dump(cover_candidates, f, ensure_ascii=False, indent=2)

    # 3. 语音 ASR 分析与分段
    _write_run_state(work_dir, "ANALYZING", "analyze", 0.35, "正在进行语音识别与分句对齐...")
    story_cand_json = os.path.join(candidates_dir, "story_candidates.json")
    try:
        analyze_story_fn(
            video_path=raw_videos,
            spec_data=spec,
            cache_dir=work_dir,
            output_json_path=story_cand_json,
            cache_key=ck,
        )
    except Exception as e:
        print(f"[STAGE-ANALYZE][WARN] ASR 分段分析异常: {e}")

    verified_cache = os.path.join(work_dir, "whisper_segments_verified.json")
    if not os.path.exists(verified_cache):
        verified_cache = os.path.join(work_dir, "whisper_segments.json")
    segments = []
    if os.path.exists(verified_cache):
        try:
            with open(verified_cache, "r", encoding="utf-8") as f:
                segments = json.load(f)
        except Exception:
            segments = []

    # 4. 意群对齐与候选片段挖掘
    _write_run_state(work_dir, "ANALYZING", "analyze", 0.5, "正在挖掘带货分镜候选池与多模态证据...")
    aligner = UtteranceAligner(merge_gap_ms=600, min_utterance_ms=1200, max_utterance_ms=10000)
    raw_by_source: Dict[str, List[Any]] = {}
    for s in segments:
        src = s.get("source_file")
        if not src:
            if len(raw_videos) == 1:
                src = raw_videos[0]
            else:
                raise ValueError(f"SOURCE_UNRESOLVED: 多源素材模式下 ASR 片段未标注 source_file: {s.get('text', '')}")
        raw_by_source.setdefault(src, []).append(s)

    utterances_by_source = {}
    for src, src_segs in raw_by_source.items():
        utterances_by_source[src] = aligner.align_segments(src_segs, source_id=src)

    generator = CandidateGenerator(min_candidate_ms=1800, max_candidate_ms=12000, target_candidate_ms=5000)
    all_candidates = []

    source_videos_list = manifest.get("source_videos", [])
    src_map_by_id = {s.get("source_id"): s for s in source_videos_list if s.get("source_id")}
    src_map_by_path = {
        os.path.normcase(os.path.realpath(normalize_project_path(s["path"]))): s
        for s in source_videos_list if s.get("path")
    }
    src_map_by_name = {s.get("filename"): s for s in source_videos_list if s.get("filename")}

    def _resolve_source_meta(src_key: str) -> Dict[str, Any]:
        if not src_key:
            raise ValueError("SOURCE_UNRESOLVED: 素材来源标识为空，无法关联输入清单")
        if src_key in src_map_by_id:
            return src_map_by_id[src_key]
        norm_k = os.path.normcase(os.path.realpath(normalize_project_path(src_key)))
        if norm_k in src_map_by_path:
            return src_map_by_path[norm_k]
        fn = os.path.basename(src_key)
        if fn in src_map_by_name:
            matches = [s for s in source_videos_list if s.get("filename") == fn]
            if len(matches) == 1:
                return matches[0]
            elif len(matches) > 1:
                raise ValueError(f"SOURCE_UNRESOLVED: 存在多个同名素材文件 '{fn}'，无法仅凭文件名解析，禁止自动归属")
        raise ValueError(f"SOURCE_UNRESOLVED: 素材来源 '{src_key}' 未在 input_manifest.json 清单中登记，禁止自动归属第一段视频")

    for src, utts in utterances_by_source.items():
        shots_for_src = scene_cuts_by_source.get(src, [])
        s_meta = _resolve_source_meta(src)
        canonical_sid = s_meta.get("source_id") or src
        canonical_path = s_meta.get("path") or src
        canonical_sha = s_meta.get("sha256") or (
            compute_full_file_sha256(canonical_path) if os.path.isfile(canonical_path) else ""
        )
        cands = generator.generate_candidates(
            utts,
            product_profile,
            source_id=canonical_sid,
            source_file=canonical_path,
            source_sha256=canonical_sha,
            shots=shots_for_src,
        )
        # 标注附近相对凹陷气口裕量 (pause_margin_ms) 供 V3.3 导演微调切点参考
        p_info = pauses_by_source.get(src) or pauses_by_source.get(canonical_path) or {}
        dips = p_info.get("dips") or []
        if dips:
            dip_ms_list = [int(round(float(d[0]) * 1000)) for d in dips if isinstance(d, (list, tuple)) and d]
            for c in cands:
                en_ms = int(c.get("source_end_ms", 0))
                nearest = None
                for dm in dip_ms_list:
                    diff = dm - en_ms
                    if -300 <= diff <= 500:
                        if nearest is None or abs(diff) < abs(nearest):
                            nearest = diff
                if nearest is not None:
                    c["pause_margin_ms"] = nearest
        all_candidates.extend(cands)

    # 5. 帧提供器（V3.3 支持顺序 3 点批抽）与 SKU 跟踪打分
    provider = VideoFrameProvider(work_dir=work_dir, default_video_path=raw_videos[0] if raw_videos else None)
    provider.register_sources(manifest.get("source_videos", []))
    if hasattr(provider, "extract_frames_batch") and all_candidates:
        try:
            provider.extract_frames_batch(all_candidates)
        except Exception as e:
            print(f"[STAGE-ANALYZE][WARN] 批量顺序抽帧降级为按需单抽: {e}")

    sku_tracker = SkuTracker()
    ranker = CandidateRanker()
    warnings = []

    auto_cfg = spec.get("options", {}).get("automation", {}) or {}
    quality_policy = auto_cfg.get("quality_policy", "standard")
    visual_verification = auto_cfg.get("visual_verification", "optional")
    is_strict_policy = (
        quality_policy == "strict"
        or visual_verification == "required"
        or spec.get("options", {}).get("strict") is True
        or product_profile.get("quality_policy") == "strict"
        or product_profile.get("visual_verification") == "required"
        or product_profile.get("strict") is True
    )

    relation_decisions = sku_tracker.track_candidates(
        all_candidates,
        product_profile,
        frame_provider=provider,
        strict_vision=is_strict_policy,
    )

    rank_res = ranker.score_candidates(
        all_candidates,
        product_profile,
        relation_decisions=relation_decisions,
        strict=is_strict_policy,
    )
    final_candidates = rank_res.get("candidates", all_candidates)
    scores = rank_res.get("scores", [])

    if not product_profile.get("reference_image_ids") and not product_profile.get("item_images"):
        warnings.append("未提供商品参考图/白底图，视觉识别已进入保守降级模式")

    # 6. 生成 V3.3 导演简报 + 视觉联系表 (Contact Sheets)
    digest_artifacts: Dict[str, Any] = {}
    try:
        from src.core.director_digest import build_director_digest
        digest_artifacts = build_director_digest(
            work_dir=work_dir,
            candidates=final_candidates,
            scores=scores,
            cover_candidates=cover_candidates,
            product_profile=product_profile,
        )
    except Exception as e:
        print(f"[STAGE-ANALYZE][WARN] 视觉导演简报生成跳过: {e}")

    # 7. 计算候选池哈希快照 (不可篡改证据)
    candidate_pool_hash = compute_candidate_pool_hash(final_candidates)

    scores_bytes = json.dumps(scores, sort_keys=True, ensure_ascii=False).encode("utf-8")
    scores_hash = hashlib.sha256(scores_bytes).hexdigest()[:24]

    evidence_manifest = {
        "schema_version": "3.3",
        "run_id": context.get("run_id"),
        "product_relation_decisions": relation_decisions,
        "warnings": warnings,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    evidence_bytes = json.dumps(evidence_manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
    evidence_manifest_hash = hashlib.sha256(evidence_bytes).hexdigest()[:24]

    snapshot_data = {
        "schema_version": "3.3",
        "run_id": context.get("run_id"),
        "candidate_pool_hash": candidate_pool_hash,
        "source_whitelist": [
            {
                "source_id": s.get("source_id"),
                "path": s.get("path"),
                "sha256": s.get("sha256"),
            }
            for s in manifest.get("source_videos", [])
        ],
        "scores_hash": scores_hash,
        "evidence_manifest_hash": evidence_manifest_hash,
        "total_candidates": len(final_candidates),
        "eligible_candidates": len([c for c in final_candidates if c.get("eligible")]),
        "gray_zone_candidates": len([c for c in final_candidates if c.get("visual_gray_zone")]),
        "cover_candidates_count": len(cover_candidates),
        "candidate_ids": [c["candidate_id"] for c in final_candidates],
        "director_brief_path": digest_artifacts.get("director_brief_md"),
        "storyboard_sheets": digest_artifacts.get("storyboard_sheets", []),
        "cover_sheet_path": digest_artifacts.get("cover_sheet_path"),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    with open(os.path.join(analysis_dir, "candidates.json"), "w", encoding="utf-8") as f:
        json.dump(final_candidates, f, ensure_ascii=False, indent=2)
    with open(os.path.join(analysis_dir, "scores.json"), "w", encoding="utf-8") as f:
        json.dump(scores, f, ensure_ascii=False, indent=2)
    with open(os.path.join(analysis_dir, "candidate_pool_snapshot.json"), "w", encoding="utf-8") as f:
        json.dump(snapshot_data, f, ensure_ascii=False, indent=2)
    with open(os.path.join(analysis_dir, "evidence_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(evidence_manifest, f, ensure_ascii=False, indent=2)

    _log_event(work_dir, "ANALYZE_COMPLETED", f"分析完成，候选池共 {len(final_candidates)} 个片段，哈希: {candidate_pool_hash}", snapshot_data)
    _write_run_state(work_dir, "ANALYZED", "analyze", 0.5, "素材分析完成，候选池与视觉导演简报已冻结，等待外部 Agent 编排提案")

    return {
        "schema_version": "3.3",
        "status": "ANALYZED",
        "run_id": context.get("run_id"),
        "candidate_pool_hash": candidate_pool_hash,
        "candidates_count": len(final_candidates),
        "eligible_candidates_count": len([c for c in final_candidates if c.get("eligible")]),
        "gray_zone_candidates_count": len([c for c in final_candidates if c.get("visual_gray_zone")]),
        "cover_candidates_count": len(cover_candidates),
        "warnings": warnings,
        "candidates_path": os.path.join(analysis_dir, "candidates.json"),
        "snapshot_path": os.path.join(analysis_dir, "candidate_pool_snapshot.json"),
        "evidence_manifest_path": os.path.join(analysis_dir, "evidence_manifest.json"),
        "director_brief_path": digest_artifacts.get("director_brief_md"),
        "director_brief_json": digest_artifacts.get("director_brief_json"),
        "storyboard_sheets": digest_artifacts.get("storyboard_sheets", []),
        "cover_sheet_path": digest_artifacts.get("cover_sheet_path"),
        "next_step": f"python -m src.cli validate-plan --run {work_dir} --proposal <path_to_sequence_proposal.json>",
    }
