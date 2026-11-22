# -*- coding: utf-8 -*-
"""
stage_services.py - V3.2 Agent-First 阶段化媒体与门禁服务
=========================================================
为通用 CLI (python -m src.cli) 提供无外部 Agent 进程依赖的确定性阶段服务：
  1. prepare: 项目初始化、输入白名单审计、不可变 spec 快照与任务指纹
  2. analyze: ASR 语境分析、多源分镜抽帧、候选片段挖掘与候选池哈希冻结
  3. validate_plan: 外部 Agent 提案审计（白名单、时序防重叠、SKU 关系门禁、封面核验）
  4. render: 基于通过验证的 plan 渲染成片与封面（临时文件原子发布）
  5. qc: 工程技术 QC 与内容 Editorial QC 双层质检审计
  6. deliver: 严格双通过 + 指纹匹配签署正式 delivery_manifest
  7. status: 查询与报告任务进度及运行时上下文
"""

import os
import sys
import json
import time
import uuid
import shutil
import hashlib
from typing import Dict, Any, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.project_context import (
    ProjectPaths,
    InputManifest,
    compute_job_fingerprint,
    compute_file_sha256,
    is_historical_output,
    sanitize_id,
)
from src.core.product_profile import build_product_profile, save_product_profile, ProductProfileError
from src.core.path_resolver import resolve_path
from src.core.utterance_aligner import UtteranceAligner
from src.core.candidate_generator import CandidateGenerator
from src.core.frame_provider import VideoFrameProvider
from src.core.sku_tracker import SkuTracker
from src.core.candidate_ranker import CandidateRanker
from src.core.plan_compiler import compile_v3_plan
from src.core.video_assembler import VideoAssembler
from src.tools.extract_cover_candidates import extract_cover_candidates, _load_constraints
from src.tools.analyze_story_segments import analyze_story_segments
from src.tools.detect_scenes import detect_scene_cuts
from src.tools.render_cover_tool import render_cover_with_meta
from src.tools.detect_price import find_price_matches
from src.tools.detect_violation import find_violation_matches
from src.qc.qc_auditor import QcAuditor
from src.qc.editorial_qc import EditorialQcAuditor
from src.qc.continuity_qc import ContinuityAuditor
from src.agent.decision_gateway import DecisionGateway, DecisionViolation
from src.tools.desktop_spec_creator import SpecManager


def _log_event(work_dir: str, event_type: str, message: str, payload: Optional[Dict[str, Any]] = None) -> None:
    events_file = os.path.join(work_dir, "events.jsonl")
    event = {
        "timestamp": time.time(),
        "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "event": event_type,
        "message": message,
        "payload": payload or {},
    }
    try:
        with open(events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _write_run_state(work_dir: str, status: str, step: str, progress: float, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
    state_file = os.path.join(work_dir, "run_state.json")
    state = {
        "status": status,
        "current_step": step,
        "progress": round(progress, 2),
        "message": message,
        "updated_at": time.time(),
        "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if extra:
        state.update(extra)
    tmp_file = state_file + ".tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        if os.path.exists(state_file):
            os.remove(state_file)
        os.rename(tmp_file, state_file)
    except Exception:
        pass


def resolve_run_dir(run_arg: str) -> str:
    """从给定的路径参数解析出具体的 work/<run_id> 目录。"""
    p = os.path.abspath(run_arg)
    if os.path.isfile(p):
        if os.path.basename(p) in ("run_context.json", "run_state.json", "spec_snapshot.json"):
            return os.path.dirname(p)
        p = os.path.dirname(p)
    if os.path.isfile(os.path.join(p, "run_context.json")):
        return p
    # 如果指定的是项目根目录，寻找最新的 work/<run_id>
    work_base = os.path.join(p, "work")
    if os.path.isdir(work_base):
        subdirs = [os.path.join(work_base, d) for d in os.listdir(work_base) if os.path.isdir(os.path.join(work_base, d))]
        if subdirs:
            subdirs.sort(key=lambda x: os.path.getmtime(x), reverse=True)
            return subdirs[0]
    return p


# -----------------------------------------------------------------------------
# 1. PREPARE
# -----------------------------------------------------------------------------
def stage_prepare(spec_path: str, run_id: Optional[str] = None, resume: bool = False) -> Dict[str, Any]:
    spec_abs = os.path.abspath(spec_path)
    if not os.path.isfile(spec_abs):
        raise FileNotFoundError(f"任务契约文件不存在: {spec_abs}")

    with open(spec_abs, "r", encoding="utf-8") as f:
        spec = json.load(f)

    # 校验契约基本格式
    mgr = SpecManager()
    errors = mgr.validate(spec)
    if errors:
        raise ValueError("job_spec.json 校验未通过:\n" + "\n".join(f"- {e}" for e in errors))

    # 初始化项目路径，确保 project_root 绝不指向 素材/
    paths = ProjectPaths(project_root=spec_abs, run_id=run_id)

    # 防覆盖与恢复运行隔离机制
    if os.path.exists(paths.work_dir) or os.path.exists(paths.output_dir):
        if not resume:
            raise FileExistsError(f"运行目录已存在: {paths.work_dir}。为保护历史交付物不被覆盖，禁止直接创建同名运行。如需恢复请指定 --resume 或更换 run_id")
        ctx_file = os.path.join(paths.work_dir, "run_context.json")
        if not os.path.isfile(ctx_file):
            raise FileNotFoundError(f"无法恢复运行: 在 {paths.work_dir} 未找到历史 run_context.json")
        with open(ctx_file, "r", encoding="utf-8") as f:
            old_ctx = json.load(f)
        manifest_mgr = InputManifest(paths)
        manifest = manifest_mgr.scan_and_resolve(explicit_video_paths=spec.get("raw_video_paths"))
        spec_fingerprint = compute_job_fingerprint(spec, manifest)
        if old_ctx.get("spec_fingerprint") != spec_fingerprint:
            raise ValueError(f"恢复运行校验失败: 当前配置指纹 ({spec_fingerprint}) 与原运行指纹 ({old_ctx.get('spec_fingerprint')}) 不匹配，严禁跨配置恢复")
        return {
            "schema_version": "3.2",
            "status": "RESUMED",
            "run_id": paths.run_id,
            "project_id": paths.project_id,
            "work_dir": paths.work_dir,
            "output_dir": paths.output_dir,
            "spec_fingerprint": spec_fingerprint,
            "source_videos": [s["path"] for s in manifest.get("source_videos", [])],
            "quarantined_files": manifest.get("quarantined_files", []),
            "next_step": f"python -m src.cli analyze --run {paths.work_dir}",
        }

    paths.ensure_runtime_dirs()

    # 扫描与核验输入素材清单
    manifest_mgr = InputManifest(paths)
    explicit_vids = spec.get("raw_video_paths")
    if explicit_vids is not None:
        manifest = manifest_mgr.scan_and_resolve(explicit_video_paths=explicit_vids)
    elif spec.get("raw_video_path"):
        rvp = spec["raw_video_path"]
        resolved_rvp = resolve_path(rvp, paths.materials_dir) or os.path.abspath(rvp)
        if os.path.isdir(resolved_rvp):
            manifest = manifest_mgr.scan_and_resolve()
        elif os.path.isfile(resolved_rvp):
            manifest = manifest_mgr.scan_and_resolve(explicit_video_paths=[resolved_rvp])
        else:
            manifest = manifest_mgr.scan_and_resolve()
    else:
        manifest = manifest_mgr.scan_and_resolve()

    source_videos = manifest.get("source_videos", [])
    if not source_videos:
        fail_msg = "缺少有效原始视频素材（用户确认素材列表为空或所有候选均被确认为旧成片被隔离）"
        _write_run_state(paths.work_dir, "NEEDS_INPUT", "prepare", 0.0, fail_msg)
        raise ValueError(fail_msg)

    # 计算全局任务输入指纹
    spec_fingerprint = compute_job_fingerprint(spec, manifest)

    # 保存 input_manifest.json
    manifest_path = manifest_mgr.save_manifest(manifest)

    # 冻结并保存 spec_snapshot.json
    spec_snapshot_path = os.path.join(paths.work_dir, "spec_snapshot.json")
    with open(spec_snapshot_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)

    # 构建并保存商品档案
    try:
        profile = build_product_profile(spec, spec_dir=paths.work_dir)
        profile_path = os.path.join(paths.work_dir, "product_profile.json")
        save_product_profile(profile, profile_path)
    except ProductProfileError as pe:
        _write_run_state(paths.work_dir, "NEEDS_INPUT", "prepare", 0.05, f"商品档案校验阻断: {pe}")
        raise ValueError(f"商品档案校验阻断: {pe}")

    # 保存 run_context.json
    context_data = {
        "schema_version": "3.2",
        "tool_version": "3.2",
        "run_id": paths.run_id,
        "project_id": paths.project_id,
        "project_root": paths.project_root,
        "materials_dir": paths.materials_dir,
        "work_dir": paths.work_dir,
        "output_dir": paths.output_dir,
        "spec_path": spec_abs,
        "spec_fingerprint": spec_fingerprint,
        "source_count": len(source_videos),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "PREPARED",
    }
    context_path = os.path.join(paths.work_dir, "run_context.json")
    with open(context_path, "w", encoding="utf-8") as f:
        json.dump(context_data, f, ensure_ascii=False, indent=2)

    _log_event(paths.work_dir, "PREPARE_COMPLETED", f"项目初始化完成，登记 {len(source_videos)} 段有效源视频", context_data)
    _write_run_state(paths.work_dir, "PREPARED", "prepare", 0.1, "项目环境与素材白名单就绪，等待执行分析")

    return {
        "schema_version": "3.2",
        "status": "PREPARED",
        "run_id": paths.run_id,
        "project_id": paths.project_id,
        "work_dir": paths.work_dir,
        "output_dir": paths.output_dir,
        "spec_fingerprint": spec_fingerprint,
        "source_videos": [s["path"] for s in source_videos],
        "quarantined_files": manifest.get("quarantined_files", []),
        "product_profile": profile["name"],
        "next_step": f"python -m src.cli analyze --run {paths.work_dir}",
    }


# -----------------------------------------------------------------------------
# 2. ANALYZE
# -----------------------------------------------------------------------------
def stage_analyze(run_arg: str) -> Dict[str, Any]:
    work_dir = resolve_run_dir(run_arg)
    context_path = os.path.join(work_dir, "run_context.json")
    if not os.path.isfile(context_path):
        raise FileNotFoundError(f"未找到 run_context.json: {context_path}，请先执行 prepare 阶段")

    with open(context_path, "r", encoding="utf-8") as f:
        context = json.load(f)

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

    # 1. 镜头边界检测
    scene_cuts_by_source: Dict[str, Any] = {}
    for v in raw_videos:
        try:
            cuts = detect_scene_cuts(v)
            scene_cuts_by_source[v] = cuts
        except Exception as e:
            print(f"[STAGE-ANALYZE][WARN] 镜头切点检测跳过 ({os.path.basename(v)}): {e}")
            scene_cuts_by_source[v] = []

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
            cands = extract_cover_candidates(
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

    # 封面主图策略支持
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

    # 兜底封面保障
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
        analyze_story_segments(
            video_path=raw_videos,
            spec_data=spec,
            cache_dir=work_dir,
            output_json_path=story_cand_json,
            cache_key=ck,
        )
    except Exception as e:
        print(f"[STAGE-ANALYZE][WARN] ASR 分段分析异常: {e}")

    # 读取转写 segments
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
        src = s.get("source_file") or (raw_videos[0] if raw_videos else "")
        raw_by_source.setdefault(src, []).append(s)

    utterances_by_source = {}
    for src, src_segs in raw_by_source.items():
        utterances_by_source[src] = aligner.align_segments(src_segs, source_id=src)

    generator = CandidateGenerator(min_candidate_ms=1800, max_candidate_ms=12000, target_candidate_ms=5000)
    all_candidates = []
    for src, utts in utterances_by_source.items():
        shots_for_src = scene_cuts_by_source.get(src, [])
        cands = generator.generate_candidates(utts, product_profile, source_id=src, shots=shots_for_src, source_file=src)
        all_candidates.extend(cands)

    # 5. 帧提供器与 SKU 跟踪打分
    provider = VideoFrameProvider(work_dir=work_dir, default_video_path=raw_videos[0] if raw_videos else None)
    provider.register_sources(manifest.get("source_videos", []))

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

    # 经由 CandidateRanker 精排与门禁
    rank_res = ranker.score_candidates(
        all_candidates,
        product_profile,
        relation_decisions=relation_decisions,
        strict=is_strict_policy,
    )
    final_candidates = rank_res.get("candidates", all_candidates)
    scores = rank_res.get("scores", [])

    # 检查是否有视觉参考图
    if not product_profile.get("reference_image_ids") and not product_profile.get("item_images"):
        warnings.append("未提供商品参考图/白底图，视觉识别已进入保守降级模式")

    # 6. 计算候选池哈希快照 (不可篡改证据)
    hash_payload = [
        {
            "id": c.get("candidate_id"),
            "source_id": c.get("source_id"),
            "st": c.get("source_start_ms"),
            "en": c.get("source_end_ms"),
            "rel": c.get("product_relation"),
            "eligible": c.get("eligible", False),
            "text": c.get("text", ""),
        }
        for c in final_candidates
    ]
    candidate_pool_hash = hashlib.sha256(json.dumps(hash_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]

    snapshot_data = {
        "schema_version": "3.2",
        "run_id": context.get("run_id"),
        "candidate_pool_hash": candidate_pool_hash,
        "total_candidates": len(final_candidates),
        "eligible_candidates": len([c for c in final_candidates if c.get("eligible")]),
        "cover_candidates_count": len(cover_candidates),
        "candidate_ids": [c["candidate_id"] for c in final_candidates],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    evidence_manifest = {
        "schema_version": "3.2",
        "run_id": context.get("run_id"),
        "product_relation_decisions": relation_decisions,
        "warnings": warnings,
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

    # 停靠点：写入 run_state.json 并停止，等待外部 Agent 提交 proposal
    _log_event(work_dir, "ANALYZE_COMPLETED", f"分析完成，候选池共 {len(final_candidates)} 个片段，哈希: {candidate_pool_hash}", snapshot_data)
    _write_run_state(work_dir, "ANALYZED", "analyze", 0.5, "素材分析完成，候选池已冻结，等待外部 Agent 编排提案 (sequence_proposal.json)")

    return {
        "schema_version": "3.2",
        "status": "ANALYZED",
        "run_id": context.get("run_id"),
        "candidate_pool_hash": candidate_pool_hash,
        "candidates_count": len(final_candidates),
        "eligible_candidates_count": len([c for c in final_candidates if c.get("eligible")]),
        "cover_candidates_count": len(cover_candidates),
        "warnings": warnings,
        "candidates_path": os.path.join(analysis_dir, "candidates.json"),
        "snapshot_path": os.path.join(analysis_dir, "candidate_pool_snapshot.json"),
        "evidence_manifest_path": os.path.join(analysis_dir, "evidence_manifest.json"),
        "next_step": f"python -m src.cli validate-plan --run {work_dir} --proposal <path_to_sequence_proposal.json>",
    }


# -----------------------------------------------------------------------------
# 3. VALIDATE-PLAN
# -----------------------------------------------------------------------------
def stage_validate_plan(run_arg: str, proposal_path: str) -> Dict[str, Any]:
    work_dir = resolve_run_dir(run_arg)
    context_path = os.path.join(work_dir, "run_context.json")
    if not os.path.isfile(context_path):
        raise FileNotFoundError(f"未找到 run_context.json: {context_path}")

    with open(context_path, "r", encoding="utf-8") as f:
        context = json.load(f)

    snapshot_path = os.path.join(work_dir, "analysis", "candidate_pool_snapshot.json")
    if not os.path.isfile(snapshot_path):
        raise FileNotFoundError(f"未找到 candidate_pool_snapshot.json: {snapshot_path}，请先执行 analyze 阶段")

    with open(snapshot_path, "r", encoding="utf-8") as f:
        snapshot = json.load(f)

    candidates_path = os.path.join(work_dir, "analysis", "candidates.json")
    with open(candidates_path, "r", encoding="utf-8") as f:
        all_candidates = json.load(f)
    cand_by_id = {c["candidate_id"]: c for c in all_candidates}

    violations: List[str] = []
    warnings: List[str] = []

    # 重新从实际 candidates.json 计算哈希，杜绝篡改数据却保留快照漏洞
    hash_payload = [
        {
            "id": c.get("candidate_id"),
            "source_id": c.get("source_id"),
            "st": c.get("source_start_ms"),
            "en": c.get("source_end_ms"),
            "rel": c.get("product_relation"),
            "eligible": c.get("eligible", False),
            "text": c.get("text", ""),
        }
        for c in all_candidates
    ]
    computed_pool_hash = hashlib.sha256(json.dumps(hash_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]

    covers_path = os.path.join(work_dir, "analysis", "cover_candidates.json")
    cover_candidates = []
    if os.path.isfile(covers_path):
        with open(covers_path, "r", encoding="utf-8") as f:
            cover_candidates = json.load(f)

    # 预加载 scores.json 结构
    scores_data = {}
    scores_path = os.path.join(work_dir, "analysis", "scores.json")
    score_by_id: Dict[str, Dict[str, Any]] = {}
    if os.path.isfile(scores_path):
        try:
            with open(scores_path, "r", encoding="utf-8") as sf:
                raw_s = json.load(sf)
                if isinstance(raw_s, dict):
                    scores_data = raw_s
                    s_list = raw_s.get("scores", [])
                elif isinstance(raw_s, list):
                    scores_data = {"scores": raw_s}
                    s_list = raw_s
                else:
                    s_list = []
                score_by_id = {s["candidate_id"]: s for s in s_list if isinstance(s, dict) and "candidate_id" in s}
        except Exception as e:
            warnings.append(f"读取 scores.json 异常: {e}")
            scores_data = {"scores": all_candidates}
    else:
        scores_data = {"scores": all_candidates}

    spec_path = os.path.join(work_dir, "spec_snapshot.json")
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    auto_cfg = spec.get("options", {}).get("automation", {}) or {}
    quality_policy = auto_cfg.get("quality_policy", "standard")
    visual_verification = auto_cfg.get("visual_verification", "optional")
    is_strict_policy = (
        quality_policy == "strict"
        or visual_verification == "required"
        or spec.get("options", {}).get("strict") is True
    )

    prop_abs = os.path.abspath(proposal_path)
    if not os.path.isfile(prop_abs):
        raise FileNotFoundError(f"提案文件不存在: {prop_abs}")

    with open(prop_abs, "r", encoding="utf-8") as f:
        prop_data = json.load(f)

    # 兼容单个 proposal 对象与 { "proposals": [...] } 以及显式 INSUFFICIENT_EVIDENCE
    proposal: Dict[str, Any] = {}
    if "proposals" in prop_data and isinstance(prop_data["proposals"], list) and prop_data["proposals"]:
        proposal = prop_data["proposals"][0]
    elif "ordered_candidate_ids" in prop_data:
        proposal = prop_data
    elif prop_data.get("status") == "INSUFFICIENT_EVIDENCE":
        proposal = prop_data
    else:
        raise ValueError("提案格式非法: 必须包含 ordered_candidate_ids 字段")

    # 1. 验证 candidate_pool_hash 强锁与完整性（不可伪造或绕过）
    prop_hash = proposal.get("candidate_pool_hash") or prop_data.get("candidate_pool_hash")
    frozen_hash = snapshot.get("candidate_pool_hash")
    if frozen_hash and len(frozen_hash) == 24 and all(ch in "0123456789abcdefABCDEF" for ch in frozen_hash):
        if computed_pool_hash != frozen_hash:
            violations.append(f"候选池文件完整性校验失败: 实际计算哈希 ({computed_pool_hash}) 与分析快照哈希 ({frozen_hash}) 不一致，候选池已被篡改")

    if not prop_hash:
        violations.append("提案必须包含 candidate_pool_hash 字段以锁定候选池快照")
    elif prop_hash != frozen_hash and prop_hash != computed_pool_hash:
        violations.append(f"候选池哈希不匹配: 提案哈希 ({prop_hash}) != 当前冻结候选池哈希 ({frozen_hash})")

    # 2. 验证 run_id（若提供）
    prop_run_id = proposal.get("run_id") or prop_data.get("run_id")
    if prop_run_id and prop_run_id != context.get("run_id"):
        violations.append(f"运行编号不匹配: 提案 run_id ({prop_run_id}) != 当前工作区 run_id ({context.get('run_id')})")

    # 3. 显式声明证据不足直接阻断
    if prop_data.get("status") == "INSUFFICIENT_EVIDENCE" or proposal.get("status") == "INSUFFICIENT_EVIDENCE":
        violations.append("提案显式声明 INSUFFICIENT_EVIDENCE（证据不足），已按门禁规范安全阻断，不予生成执行计划")

    ordered_ids = proposal.get("ordered_candidate_ids")
    if not isinstance(ordered_ids, list):
        violations.append("提案中选定候选列表 (ordered_candidate_ids) 必须为数组列表")
        ordered_ids = []
    elif not ordered_ids:
        violations.append("提案中选定候选列表 (ordered_candidate_ids) 不能为空")

    # 2. 验证候选 ID 白名单与有效性
    selected_cands = []
    seen_ids = set()
    total_dur_ms = 0
    clips_by_source: Dict[str, List[Tuple[int, int, str]]] = {}

    for cid in ordered_ids:
        if not isinstance(cid, str):
            violations.append(f"候选 ID 必须为字符串: {cid}")
            continue
        if cid in seen_ids:
            violations.append(f"候选片段重复使用: {cid}")
            continue
        seen_ids.add(cid)

        if cid not in cand_by_id:
            violations.append(f"未知候选 ID（不属于当前冻结候选池）: {cid}")
            continue

        c = cand_by_id[cid]
        sc = score_by_id.get(cid, {})

        # 初筛合格性硬门禁 (eligible is True)
        is_eligible = sc.get("eligible") if "eligible" in sc else c.get("eligible")
        if is_eligible is False:
            violations.append(f"选定候选未通过初筛合格性检验 (eligible=False): {cid}")

        # 检查硬性阻断原因 (rejection_reasons)
        rej_reasons = sc.get("rejection_reasons") or c.get("rejection_reasons") or []
        if rej_reasons:
            violations.append(f"选定候选存在硬性阻断原因: {cid} -> {', '.join(str(r) for r in rej_reasons)}")

        # 商品关系硬门禁
        rel = sc.get("product_relation") or c.get("product_relation", "UNCERTAIN")
        if rel == "OTHER_SKU":
            violations.append(f"选定片段命中非本品商品 (OTHER_SKU 阻断): {cid}")
        elif rel == "UNCERTAIN" and is_strict_policy:
            violations.append(f"严格模式下选定片段未通过商品身份核实 (UNCERTAIN): {cid}")

        # 违规价格表达与极限违规词复检
        c_text = c.get("text", "")
        p_matches = find_price_matches(c_text)
        if p_matches:
            violations.append(f"选定候选包含违规价格表达: {cid} -> {p_matches}")
        v_matches = find_violation_matches(c_text)
        if v_matches:
            violations.append(f"选定候选命中极限违规词: {cid} -> {v_matches}")

        # 检查时间区间合法性与同源时间重叠
        s_file = c.get("source_file") or c.get("source_id", "default")
        st = c.get("source_start_ms", 0)
        en = c.get("source_end_ms", 0)
        if en <= st:
            violations.append(f"候选片段时序区间异常 (时长<=0): {cid} [{st}~{en}]")
        if s_file not in clips_by_source:
            clips_by_source[s_file] = []
        for prev_st, prev_en, prev_id in clips_by_source[s_file]:
            if max(st, prev_st) < min(en, prev_en):
                violations.append(f"同源视频时间轴严重重叠: {cid} [{st}~{en}] 与 {prev_id} [{prev_st}~{prev_en}]")
        clips_by_source[s_file].append((st, en, cid))

        total_dur_ms += max(0, en - st)
        selected_cands.append(c)

    # 3. 成片时长约束检查
    dur_range_str = str(spec.get("options", {}).get("duration_range", "45-65"))
    try:
        min_s, max_s = [float(x.strip()) for x in dur_range_str.split("-")]
    except Exception:
        min_s, max_s = 45.0, 65.0
    total_dur_s = total_dur_ms / 1000.0

    # 硬性时长上限门禁（超出硬性上限拒绝提案，轻微偏差产生警告）
    hard_max_s = max_s + 5.0
    if total_dur_s > hard_max_s:
        violations.append(f"成片预计总时长 ({total_dur_s:.1f}s) 严重超过目标硬性上限 ({hard_max_s:.1f}s)")
    elif total_dur_s < min_s - 5.0 or total_dur_s > max_s:
        warnings.append(f"成片预计总时长 ({total_dur_s:.1f}s) 偏离目标区间 [{min_s}, {max_s}]s")

    # 4. 封面有效性校验
    constraints = _load_constraints()
    gateway = DecisionGateway(constraints=constraints)
    chosen_cover_id = proposal.get("chosen_cover_id")
    selected_cover = None
    if cover_candidates:
        try:
            selected_cover = gateway.select_valid_cover(cover_candidates, forced_id=chosen_cover_id)
        except DecisionViolation as dv:
            violations.append(f"封面候选未通过门禁校验: {dv.violations}")
        except Exception as e:
            warnings.append(f"封面网关校验异常: {e}")
            selected_cover = cover_candidates[0]

    # 计算提案签名
    prop_bytes = json.dumps(proposal, sort_keys=True, ensure_ascii=False).encode("utf-8")
    proposal_hash = hashlib.sha256(prop_bytes).hexdigest()

    # 审计判定
    audit_report = {
        "schema_version": "3.2",
        "proposal_id": proposal.get("proposal_id", "agent_proposal"),
        "proposal_hash": proposal_hash,
        "candidate_pool_hash": prop_hash,
        "audit_status": "REJECTED" if violations else "APPROVED",
        "passed": (len(violations) == 0),
        "violations": violations,
        "warnings": warnings,
        "total_selected_clips": len(selected_cands),
        "total_duration_s": round(total_dur_s, 2),
        "chosen_cover_id": selected_cover.get("id") if selected_cover else None,
        "audited_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    audit_path = os.path.join(work_dir, "plan_audit.json")
    with open(audit_path, "w", encoding="utf-8") as f:
        json.dump(audit_report, f, ensure_ascii=False, indent=2)

    if violations:
        _write_run_state(work_dir, "PLAN_REJECTED", "validate_plan", 0.55, f"提案未通过审计: {'; '.join(violations)}")
        _log_event(work_dir, "PLAN_VALIDATION_FAILED", "Agent 提案被约束网关拦截", audit_report)
        raise ValueError(f"提案未通过质量与安全门禁:\n" + "\n".join(f"- {v}" for v in violations))

    # 5. 编译为执行计划 validated_plan.json
    compiled_plan = compile_v3_plan(
        proposal=proposal,
        candidates=all_candidates,
        scores=scores_data,
        spec_data=spec,
        constraints=constraints,
    )
    if selected_cover:
        compiled_plan["selected_cover"] = selected_cover

    compiled_plan["proposal_hash"] = proposal_hash
    compiled_plan["candidate_pool_hash"] = prop_hash
    compiled_plan["run_id"] = context.get("run_id")

    # 动态避脸：计算各分镜贴纸安全侧与位置，防止叠加层压脸
    try:
        from src.agent.agent_director import assign_sticker_sides
        assign_sticker_sides(compiled_plan, constraints)
    except Exception as e:
        print(f"  [WARN] 自动避脸计算跳过: {e}", file=sys.stderr)

    plan_bytes = json.dumps(compiled_plan, sort_keys=True, ensure_ascii=False).encode("utf-8")
    validated_plan_hash = hashlib.sha256(plan_bytes).hexdigest()
    compiled_plan["validated_plan_hash"] = validated_plan_hash

    plan_path = os.path.join(work_dir, "validated_plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(compiled_plan, f, ensure_ascii=False, indent=2)

    # 回写最终经过编译签署的 plan_hash 到 plan_audit.json
    audit_report["validated_plan_hash"] = validated_plan_hash
    with open(audit_path, "w", encoding="utf-8") as f:
        json.dump(audit_report, f, ensure_ascii=False, indent=2)

    _write_run_state(work_dir, "PLAN_VALIDATED", "validate_plan", 0.65, f"提案审计通过 ({len(selected_cands)} 分镜, {total_dur_s:.1f}s)，计划已冻结")
    _log_event(work_dir, "PLAN_VALIDATED", f"分镜计划编译成功: {len(selected_cands)} 个分镜", audit_report)

    return {
        "schema_version": "3.2",
        "status": "PLAN_VALIDATED",
        "run_id": context.get("run_id"),
        "plan_path": plan_path,
        "audit_path": audit_path,
        "clips_count": len(selected_cands),
        "total_duration_s": round(total_dur_s, 2),
        "chosen_cover_id": selected_cover.get("id") if selected_cover else None,
        "next_step": f"python -m src.cli render --run {work_dir}",
    }


# -----------------------------------------------------------------------------
# 4. RENDER
# -----------------------------------------------------------------------------
def stage_render(run_arg: str) -> Dict[str, Any]:
    work_dir = resolve_run_dir(run_arg)
    context_path = os.path.join(work_dir, "run_context.json")
    if not os.path.isfile(context_path):
        raise FileNotFoundError(f"未找到 run_context.json: {context_path}")

    with open(context_path, "r", encoding="utf-8") as f:
        context = json.load(f)

    plan_path = os.path.join(work_dir, "validated_plan.json")
    if not os.path.isfile(plan_path):
        raise FileNotFoundError(f"未找到已验证的计划文件: {plan_path}，必须先通过 validate-plan 校验")

    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)

    # 校验 plan_audit.json 是否存在且内容与 validated_plan.json 匹配
    audit_path = os.path.join(work_dir, "plan_audit.json")
    if not os.path.isfile(audit_path):
        raise ValueError(f"未找到计划审计报告: {audit_path}，必须先通过 validate-plan 校验")
    with open(audit_path, "r", encoding="utf-8") as af:
        audit_rep = json.load(af)
    if not (audit_rep.get("passed") or audit_rep.get("audit_status") == "APPROVED"):
        raise ValueError("分镜计划未通过质量门禁审计 (处于 REJECTED 状态)")

    # 验证 validated_plan_hash 绑定
    plan_bytes = json.dumps(plan, sort_keys=True, ensure_ascii=False).encode("utf-8")
    actual_plan_hash = hashlib.sha256(plan_bytes).hexdigest()
    if audit_rep.get("validated_plan_hash") and audit_rep.get("validated_plan_hash") != actual_plan_hash and audit_rep.get("validated_plan_hash") != plan.get("validated_plan_hash"):
        raise ValueError("分镜计划完整性校验失败: 当前计划哈希与审计签名不一致，禁止渲染未经审计的修改计划")

    spec_path = os.path.join(work_dir, "spec_snapshot.json")
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    output_dir = context.get("output_dir")
    os.makedirs(output_dir, exist_ok=True)

    # 渲染新成片必须使下游旧验收产物失效
    for old_artifact in ("technical_qc.json", "editorial_qc.json", "delivery_manifest.json"):
        old_art_p = os.path.join(output_dir, old_artifact)
        if os.path.isfile(old_art_p):
            try:
                os.remove(old_art_p)
            except Exception:
                pass

    _write_run_state(work_dir, "RENDERING", "render", 0.70, "启动 FFmpeg 音画物理强锁渲染...")
    _log_event(work_dir, "RENDER_START", "开始视频物理合成与封面生成")

    # 1. 封面渲染
    final_cover_path = os.path.join(output_dir, "cover.jpg")
    cover_meta_path = os.path.join(output_dir, "cover_meta.json")
    cover_info = plan.get("cover") or {}
    cover_source_img = cover_info.get("image_path")
    if not cover_source_img or not os.path.isfile(cover_source_img):
        # 尝试使用分析中的候选图
        cov_cand_file = os.path.join(work_dir, "analysis", "cover_candidates.json")
        if os.path.isfile(cov_cand_file):
            try:
                with open(cov_cand_file, "r", encoding="utf-8") as cf:
                    cc_list = json.load(cf)
                if cc_list and os.path.isfile(cc_list[0].get("image_path", "")):
                    cover_source_img = cc_list[0]["image_path"]
            except Exception:
                pass

    if cover_source_img and os.path.isfile(cover_source_img):
        render_cover_with_meta(cover_source_img, spec, final_cover_path, cover_meta_path)
    else:
        # 生成标准安全纯色底图
        from PIL import Image
        Image.new("RGB", (1080, 1920), color=(20, 20, 20)).save(final_cover_path)

    # 2. 视频物理合成 (临时文件原子写入保护)
    tmp_final_video = os.path.join(output_dir, f"temp_final_{uuid.uuid4().hex[:6]}.mp4")
    final_video = os.path.join(output_dir, "final.mp4")

    # 确保旧临时文件不污染
    if os.path.exists(tmp_final_video):
        try:
            os.remove(tmp_final_video)
        except Exception:
            pass

    assembler = VideoAssembler(work_dir=work_dir)
    try:
        out_mp4, duration = assembler.assemble_final_video(plan, tmp_final_video)
    except Exception as e:
        if os.path.exists(tmp_final_video):
            try:
                os.remove(tmp_final_video)
            except Exception:
                pass
        _write_run_state(work_dir, "FAILED", "render", 0.75, f"视频渲染失败: {e}")
        raise RuntimeError(f"FFmpeg 渲染合成失败: {e}")

    if not os.path.isfile(tmp_final_video) or os.path.getsize(tmp_final_video) == 0:
        raise RuntimeError("成片文件渲染异常（产物文件缺失或大小为 0）")

    # 原子重命名发布成片
    if os.path.exists(final_video):
        try:
            os.remove(final_video)
        except Exception:
            pass
    os.replace(tmp_final_video, final_video)

    # 3. 生成带货文案 copywriting.md
    copywriting_path = os.path.join(output_dir, "copywriting.md")
    hook_text = (plan.get("hook_card") or {}).get("text", "")
    cta_text = (plan.get("cta_card") or {}).get("text", "")
    prod_name = (spec.get("product") or {}).get("name", "本品")
    md_content = f"""# 带货视频文案与分镜说明

- **项目编号**: `{context.get("project_id")}`
- **运行编号**: `{context.get("run_id")}`
- **推荐商品**: {prod_name}
- **开场 Hook**: {hook_text}
- **结尾 CTA**: {cta_text}

## 核心分镜列表
"""
    for i, clip in enumerate(plan.get("clips", []), 1):
        md_content += f"{i}. [{clip.get('start', 0):.1f}s ~ {clip.get('end', 0):.1f}s] {clip.get('text', '')}\n"

    with open(copywriting_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    _write_run_state(work_dir, "RENDERED", "render", 0.85, f"成片与封面渲染完成 ({os.path.basename(final_video)})")
    _log_event(work_dir, "RENDER_COMPLETED", f"渲染成片已就绪: {final_video}")

    return {
        "schema_version": "3.2",
        "status": "RENDERED",
        "run_id": context.get("run_id"),
        "final_video": final_video,
        "cover_image": final_cover_path,
        "copywriting": copywriting_path,
        "next_step": f"python -m src.cli qc --run {work_dir}",
    }


# -----------------------------------------------------------------------------
# 5. QC
# -----------------------------------------------------------------------------
def stage_qc(run_arg: str) -> Dict[str, Any]:
    work_dir = resolve_run_dir(run_arg)
    context_path = os.path.join(work_dir, "run_context.json")
    if not os.path.isfile(context_path):
        raise FileNotFoundError(f"未找到 run_context.json: {context_path}")

    with open(context_path, "r", encoding="utf-8") as f:
        context = json.load(f)

    output_dir = context.get("output_dir")
    final_video = os.path.join(output_dir, "final.mp4")
    if not os.path.isfile(final_video):
        raise FileNotFoundError(f"未找到待检成片: {final_video}，请先执行 render 阶段")

    plan_path = os.path.join(work_dir, "validated_plan.json")
    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)

    spec_path = os.path.join(work_dir, "spec_snapshot.json")
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    profile_path = os.path.join(work_dir, "product_profile.json")
    with open(profile_path, "r", encoding="utf-8") as f:
        product_profile = json.load(f)

    _write_run_state(work_dir, "QC_EVALUATING", "qc", 0.90, "执行工程技术 QC 与内容 Editorial QC 双层门禁审计...")
    _log_event(work_dir, "QC_START", "开始双层质检")

    constraints = _load_constraints()

    # 1. 工程技术 QC (QcAuditor)
    tech_qc_path = os.path.join(output_dir, "technical_qc.json")
    tech_auditor = QcAuditor(constraints=constraints, spec=spec)
    cover_path = os.path.join(output_dir, "cover.jpg")
    cover_meta_path = os.path.join(output_dir, "cover_meta.json")
    overlay_meta_path = os.path.join(work_dir, "video_overlay_meta.json")
    ass_path = os.path.join(work_dir, "subtitles.ass")

    try:
        tech_passed = tech_auditor.audit_final_deliverables(
            video_path=final_video,
            cover_path=cover_path,
            ass_path=ass_path,
            report_output_path=tech_qc_path,
            cover_meta_path=cover_meta_path,
            overlay_meta_path=overlay_meta_path,
            plan_path=plan_path,
            spec=spec,
        )
        if os.path.isfile(tech_qc_path):
            with open(tech_qc_path, "r", encoding="utf-8") as f:
                tech_report = json.load(f)
        else:
            tech_report = {"passed": tech_passed}
    except Exception as e:
        tech_passed = False
        tech_report = {
            "passed": False,
            "fatal_error": str(e),
            "summary": {"n_passed": 0, "n_failed": 1, "n_warn": 0},
            "checks": [{"name": "tech_audit_execution", "status": "FAIL", "reason": str(e)}],
        }
        with open(tech_qc_path, "w", encoding="utf-8") as f:
            json.dump(tech_report, f, ensure_ascii=False, indent=2)

    current_video_sha256 = compute_file_sha256(final_video)

    # 2. 内容审核门禁 (EditorialQcAuditor)
    editorial_qc_path = os.path.join(output_dir, "editorial_qc.json")
    auto_cfg = spec.get("options", {}).get("automation", {}) or {}
    ed_policy = (
        "strict"
        if (
            auto_cfg.get("quality_policy") == "strict"
            or auto_cfg.get("visual_verification") == "required"
            or spec.get("options", {}).get("strict") is True
            or product_profile.get("quality_policy") == "strict"
        )
        else "standard"
    )
    ed_auditor = EditorialQcAuditor(policy=ed_policy)
    try:
        ed_report = ed_auditor.audit_editorial_quality(final_plan=plan, product_profile=product_profile)
    except Exception as e:
        ed_report = {
            "passed": False,
            "block_count": 1,
            "summary": {"status": "BLOCK", "block_count": 1, "warn_count": 0},
            "checks": [{"gate": "editorial_audit_execution", "status": "BLOCK", "detail": str(e)}],
        }

    ed_report["target_video_sha256"] = current_video_sha256
    ed_report["video_sha256"] = current_video_sha256
    with open(editorial_qc_path, "w", encoding="utf-8") as f:
        json.dump(ed_report, f, ensure_ascii=False, indent=2)

    tech_passed = bool(tech_report.get("all_passed", tech_report.get("passed", False)))
    tech_report["passed"] = tech_passed
    tech_report["target_video_sha256"] = current_video_sha256
    tech_report["video_sha256"] = current_video_sha256
    with open(tech_qc_path, "w", encoding="utf-8") as f:
        json.dump(tech_report, f, ensure_ascii=False, indent=2)

    ed_passed = bool(ed_report.get("passed", False) and ed_report.get("block_count", 0) == 0)
    dual_pass = tech_passed and ed_passed

    qc_status = "QC_PASSED" if dual_pass else "QC_BLOCKED"
    msg = "双层质检全部通过，可签署正式交付" if dual_pass else f"质检未通过 (技术通过={tech_passed}, 内容通过={ed_passed}, BLOCK={ed_report.get('block_count', 0)})"
    _write_run_state(work_dir, qc_status, "qc", 0.95, msg, {"technical_qc_passed": tech_passed, "editorial_qc_passed": ed_passed})
    _log_event(work_dir, "QC_COMPLETED", msg, {"tech_passed": tech_passed, "ed_passed": ed_passed})

    return {
        "schema_version": "3.2",
        "status": qc_status,
        "run_id": context.get("run_id"),
        "dual_qc_passed": dual_pass,
        "technical_qc": {
            "passed": tech_passed,
            "report_path": tech_qc_path,
        },
        "editorial_qc": {
            "passed": ed_passed,
            "block_count": ed_report.get("block_count", 0),
            "warn_count": ed_report.get("warn_count", 0),
            "report_path": editorial_qc_path,
        },
        "next_step": f"python -m src.cli deliver --run {work_dir}",
    }


# -----------------------------------------------------------------------------
# 6. DELIVER
# -----------------------------------------------------------------------------
def stage_deliver(run_arg: str) -> Dict[str, Any]:
    work_dir = resolve_run_dir(run_arg)
    context_path = os.path.join(work_dir, "run_context.json")
    if not os.path.isfile(context_path):
        raise FileNotFoundError(f"未找到 run_context.json: {context_path}")

    with open(context_path, "r", encoding="utf-8") as f:
        context = json.load(f)

    output_dir = context.get("output_dir")
    final_video = os.path.join(output_dir, "final.mp4")
    tech_qc_path = os.path.join(output_dir, "technical_qc.json")
    editorial_qc_path = os.path.join(output_dir, "editorial_qc.json")

    rejections = []
    current_video_sha256 = compute_file_sha256(final_video) if os.path.isfile(final_video) else ""
    if not os.path.isfile(final_video) or os.path.getsize(final_video) == 0:
        rejections.append("成片视频文件不存在或为空")

    if not os.path.isfile(tech_qc_path):
        rejections.append("未找到技术质检报告 technical_qc.json")
    else:
        with open(tech_qc_path, "r", encoding="utf-8") as f:
            t_data = json.load(f)
        if not (t_data.get("all_passed", False) or t_data.get("passed", False)):
            rejections.append("工程技术质检未通过")
        t_target_sha = t_data.get("target_video_sha256") or t_data.get("video_sha256")
        if t_target_sha and current_video_sha256 and t_target_sha != current_video_sha256:
            rejections.append(f"技术质检报告审核的目标视频哈希 ({t_target_sha}) 与当前成片哈希 ({current_video_sha256}) 不匹配，重新渲染后必须重新执行 qc 阶段")

    if not os.path.isfile(editorial_qc_path):
        rejections.append("未找到内容质检报告 editorial_qc.json")
    else:
        with open(editorial_qc_path, "r", encoding="utf-8") as f:
            e_data = json.load(f)
        if not e_data.get("passed", False) or e_data.get("block_count", 0) > 0:
            rejections.append(f"内容质检门禁阻断 (BLOCK={e_data.get('block_count', 0)})")
        e_target_sha = e_data.get("target_video_sha256") or e_data.get("video_sha256")
        if e_target_sha and current_video_sha256 and e_target_sha != current_video_sha256:
            rejections.append(f"内容质检报告审核的目标视频哈希 ({e_target_sha}) 与当前成片哈希 ({current_video_sha256}) 不匹配，重新渲染后必须重新执行 qc 阶段")

    plan_audit_path = os.path.join(work_dir, "plan_audit.json")
    plan_path = os.path.join(work_dir, "validated_plan.json")
    actual_plan_hash = None
    pa_data = {}
    if os.path.isfile(plan_audit_path):
        with open(plan_audit_path, "r", encoding="utf-8") as paf:
            pa_data = json.load(paf)
    if os.path.isfile(plan_path):
        with open(plan_path, "r", encoding="utf-8") as pf:
            plan_obj = json.load(pf)
        plan_bytes = json.dumps(plan_obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
        actual_plan_hash = hashlib.sha256(plan_bytes).hexdigest()
        if pa_data.get("validated_plan_hash") and pa_data.get("validated_plan_hash") != actual_plan_hash and pa_data.get("validated_plan_hash") != plan_obj.get("validated_plan_hash"):
            rejections.append("执行计划哈希与审计签名不一致，禁止跨计划交付")

    spec_path = os.path.join(work_dir, "spec_snapshot.json")
    manifest_path = os.path.join(work_dir, "input_manifest.json")
    if os.path.isfile(spec_path) and os.path.isfile(manifest_path):
        with open(spec_path, "r", encoding="utf-8") as f:
            spec = json.load(f)
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        curr_fp = compute_job_fingerprint(spec, manifest)
        if curr_fp != context.get("spec_fingerprint"):
            rejections.append("任务输入指纹发生变更，严禁跨配置交付")

    live_spec_path = context.get("spec_path")
    if live_spec_path and os.path.isfile(live_spec_path) and os.path.isfile(manifest_path):
        try:
            with open(live_spec_path, "r", encoding="utf-8") as f:
                live_spec = json.load(f)
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            live_fp = compute_job_fingerprint(live_spec, manifest)
            if live_fp != context.get("spec_fingerprint"):
                rejections.append("活任务配置已被外部修改导致指纹不匹配，严禁跨配置交付")
        except Exception as e:
            rejections.append(f"活任务配置指纹校验异常: {e}")

    if rejections:
        msg = f"交付硬门禁拒绝签署: {'; '.join(rejections)}"
        _write_run_state(work_dir, "DELIVERY_BLOCKED", "deliver", 0.95, msg)
        _log_event(work_dir, "DELIVERY_BLOCKED", msg)
        raise ValueError(msg)

    # 签署交付清单 delivery_manifest.json
    deliv_manifest = {
        "schema_version": "3.2",
        "project_id": context.get("project_id"),
        "run_id": context.get("run_id"),
        "status": "DELIVERED",
        "job_fingerprint": context.get("spec_fingerprint"),
        "proposal_hash": pa_data.get("proposal_hash") if "pa_data" in locals() else None,
        "validated_plan_hash": actual_plan_hash if "actual_plan_hash" in locals() else None,
        "video_path": final_video,
        "video_sha256": current_video_sha256 or compute_file_sha256(final_video),
        "cover_path": os.path.join(output_dir, "cover.jpg"),
        "copywriting_path": os.path.join(output_dir, "copywriting.md"),
        "technical_qc_path": tech_qc_path,
        "technical_qc_sha256": compute_file_sha256(tech_qc_path),
        "editorial_qc_path": editorial_qc_path,
        "editorial_qc_sha256": compute_file_sha256(editorial_qc_path),
        "qc_passed": True,
        "completed_at": time.time(),
        "completed_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    deliv_path = os.path.join(output_dir, "delivery_manifest.json")
    with open(deliv_path, "w", encoding="utf-8") as f:
        json.dump(deliv_manifest, f, ensure_ascii=False, indent=2)

    # 更新 output/latest.json 软指针
    output_base = os.path.dirname(output_dir)
    latest_path = os.path.join(output_base, "latest.json")
    latest_pointer = {
        "latest_run_id": context.get("run_id"),
        "status": "DELIVERED",
        "video_path": final_video,
        "delivery_manifest": deliv_path,
        "delivered_at": deliv_manifest["completed_at_str"],
    }
    try:
        with open(latest_path, "w", encoding="utf-8") as f:
            json.dump(latest_pointer, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    _write_run_state(work_dir, "DELIVERED", "deliver", 1.0, f"成片及全部物料正式交付: {final_video}")
    _log_event(work_dir, "DELIVERED", f"成片正式交付完成: {final_video}", deliv_manifest)

    return {
        "schema_version": "3.2",
        "status": "DELIVERED",
        "run_id": context.get("run_id"),
        "final_video": final_video,
        "cover_path": deliv_manifest["cover_path"],
        "delivery_manifest": deliv_path,
        "latest_pointer": latest_path,
    }


# -----------------------------------------------------------------------------
# 7. STATUS
# -----------------------------------------------------------------------------
def stage_status(run_arg: str) -> Dict[str, Any]:
    work_dir = resolve_run_dir(run_arg)
    context_path = os.path.join(work_dir, "run_context.json")
    if not os.path.isdir(work_dir) or not os.path.isfile(context_path):
        raise FileNotFoundError(f"未找到有效运行目录或 run_context.json: {work_dir}")
    state_path = os.path.join(work_dir, "run_state.json")

    context = {}
    if os.path.isfile(context_path):
        with open(context_path, "r", encoding="utf-8") as f:
            context = json.load(f)

    state = {}
    if os.path.isfile(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

    events = []
    events_path = os.path.join(work_dir, "events.jsonl")
    if os.path.isfile(events_path):
        try:
            with open(events_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        events.append(json.loads(line))
        except Exception:
            pass

    return {
        "schema_version": "3.2",
        "work_dir": work_dir,
        "run_id": context.get("run_id"),
        "project_id": context.get("project_id"),
        "status": state.get("status", "UNKNOWN"),
        "current_step": state.get("current_step", "unknown"),
        "progress": state.get("progress", 0.0),
        "message": state.get("message", ""),
        "last_updated": state.get("updated_at_str", ""),
        "recent_events": events[-5:],
    }
