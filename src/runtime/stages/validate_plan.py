# -*- coding: utf-8 -*-
"""
validate_plan.py - 阶段 3：外部 Agent 提案审计（v3.2/v3.3 兼容 + 灰区 visual_overrides + 切点微调 + 违规三通道）
"""

import os
import sys
import json
import time
import hashlib
from typing import Dict, Any, List, Optional, Tuple

from src.core.project_context import is_historical_output, normalize_project_path
from src.core.plan_compiler import compile_v3_plan
from src.core.category_profile import load_category_profile
from src.tools.extract_cover_candidates import _load_constraints
from src.tools.detect_price import find_price_matches
from src.tools.detect_violation import find_violation_matches
from src.agent.decision_gateway import DecisionGateway, DecisionViolation
from src.runtime.stages.common import _log_event, _write_run_state, resolve_run_dir, compute_candidate_pool_hash
from src.runtime.stages.security import resolve_trusted_paths, resolve_and_verify_work_ref

DEFAULT_MAX_CUT_ADJUST_MS = 500
VALID_OVERRIDE_RELATIONS = ("SAME_SKU", "SAME_SKU_VARIANT", "ACCESSORY_OR_STYLING")


def _validate_factual_text(
    label: str,
    text: str,
    evidence_corpus: str,
    factual_tokens: List[str],
    violations: List[str],
) -> None:
    """校验贴纸/Hook/CTA 文本：零价格、零高危违规词、禁止无依据杜撰高危材质/成分词。"""
    if not text:
        return
    p_hits = find_price_matches(text)
    if p_hits:
        violations.append(f"{label} 包含违规价格表达: '{text}' -> {p_hits}")
    v_hits = find_violation_matches(text)
    block_v = [m for m in v_hits if m.get("severity") == "BLOCK"]
    if block_v:
        violations.append(f"{label} 命中违规极限词: '{text}' -> {[m.get('word') for m in block_v]}")
    for tok in factual_tokens:
        if tok and tok in text and tok not in evidence_corpus:
            violations.append(f"{label} 包含未经商品档案或台词证实的成分/材质宣称 '{tok}': '{text}'")


def stage_validate_plan(run_arg: str, proposal_path: str) -> Dict[str, Any]:
    work_dir = resolve_run_dir(run_arg)
    context_path = os.path.join(work_dir, "run_context.json")
    if not os.path.isfile(context_path):
        raise FileNotFoundError(f"未找到 run_context.json: {context_path}")

    with open(context_path, "r", encoding="utf-8") as f:
        context = json.load(f)

    paths = resolve_trusted_paths(work_dir, context)
    output_dir = paths.output_dir

    deliv_manifest_path = os.path.join(output_dir, "delivery_manifest.json")
    if os.path.isfile(deliv_manifest_path):
        raise RuntimeError(
            f"当前运行 ({context.get('run_id')}) 已完成正式交付签署，计划与产物已被锁定为不可变！如需修改计划请创建新 run_id"
        )

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

    computed_pool_hash = compute_candidate_pool_hash(all_candidates)
    legacy_payload = [
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
    legacy_pool_hash = hashlib.sha256(json.dumps(legacy_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]

    manifest_path = os.path.join(work_dir, "input_manifest.json")
    valid_source_ids = set()
    valid_source_paths = set()
    manifest_sources_by_id = {}
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as mf:
                manifest_data = json.load(mf)
            for s in manifest_data.get("source_videos", []):
                sid = s.get("source_id")
                sp = s.get("path")
                if sid:
                    valid_source_ids.add(sid)
                    manifest_sources_by_id[sid] = s
                if sp:
                    valid_source_paths.add(os.path.normcase(os.path.realpath(normalize_project_path(sp))))
        except Exception:
            pass
    elif snapshot.get("source_whitelist"):
        for s in snapshot.get("source_whitelist", []):
            sid = s.get("source_id")
            sp = s.get("path")
            if sid:
                valid_source_ids.add(sid)
                manifest_sources_by_id[sid] = s
            if sp:
                valid_source_paths.add(os.path.normcase(os.path.realpath(normalize_project_path(sp))))

    for c in all_candidates:
        cid = c.get("candidate_id", "")
        c_sid = c.get("source_id")
        c_src = c.get("source_file")

        if c_src:
            is_hist, hist_reason = is_historical_output(c_src, context.get("project_id", ""))
            if is_hist:
                violations.append(f"候选 {cid} 引用了历史成片作为剪辑素材源 ({c_src}): {hist_reason}")

        if valid_source_ids:
            if not c_sid or c_sid not in valid_source_ids:
                violations.append(f"候选 {cid} 的 source_id ({c_sid}) 不在 input_manifest.json 白名单中")
            if not c_src:
                violations.append(f"候选 {cid} 缺失 source_file")
            elif valid_source_paths:
                norm_c_src = os.path.normcase(os.path.realpath(normalize_project_path(c_src)))
                if norm_c_src not in valid_source_paths:
                    violations.append(f"候选 {cid} 的 source_file ({c_src}) 不在 input_manifest.json 白名单中")
                if c_sid in manifest_sources_by_id:
                    expected_p = os.path.normcase(os.path.realpath(normalize_project_path(manifest_sources_by_id[c_sid].get("path", ""))))
                    if norm_c_src != expected_p:
                        violations.append(f"候选 {cid} 的 source_file 与 source_id ({c_sid}) 绑定的清单路径不一致")
                    expected_sha = manifest_sources_by_id[c_sid].get("sha256")
                    c_sha = c.get("source_file_sha256")
                    if expected_sha and c_sha and c_sha != expected_sha:
                        violations.append(f"候选 {cid} 的 source_file_sha256 与清单完整哈希不匹配")

    covers_path = os.path.join(work_dir, "analysis", "cover_candidates.json")
    cover_candidates = []
    if os.path.isfile(covers_path):
        with open(covers_path, "r", encoding="utf-8") as f:
            cover_candidates = json.load(f)

    scores_data = {}
    scores_path = os.path.join(work_dir, "analysis", "scores.json")
    score_by_id: Dict[str, Dict[str, Any]] = {}
    scores_raw_data = None
    if os.path.isfile(scores_path):
        try:
            with open(scores_path, "r", encoding="utf-8") as sf:
                raw_s = json.load(sf)
                scores_raw_data = raw_s
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
            violations.append(f"读取 scores.json 异常: {e}")
            scores_data = {"scores": all_candidates}
    else:
        scores_data = {"scores": all_candidates}
        if snapshot.get("scores_hash"):
            violations.append("缺失必要打分文件: scores.json 不存在")

    spec_path = os.path.join(work_dir, "spec_snapshot.json")
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    cat_profile = load_category_profile(spec)
    factual_tokens = list(cat_profile.get("factual_claim_tokens") or [])
    prod_cfg = spec.get("product", {}) or {}
    profile_evidence_corpus = " ".join([
        str(prod_cfg.get("name", "")),
        str(prod_cfg.get("title_line1", "")),
        str(prod_cfg.get("title_line2", "")),
        " ".join(str(x) for x in (prod_cfg.get("selling_points") or [])),
        " ".join(str(x) for x in ((spec.get("options") or {}).get("highlight_keywords") or [])),
    ])

    auto_cfg = spec.get("options", {}).get("automation", {}) or {}
    quality_policy = auto_cfg.get("quality_policy", "standard")
    visual_verification = auto_cfg.get("visual_verification", "optional")
    is_strict_policy = (
        quality_policy == "strict"
        or visual_verification == "required"
        or spec.get("options", {}).get("strict") is True
        or spec.get("product", {}).get("quality_policy") == "strict"
        or spec.get("product", {}).get("strict") is True
    )

    prop_abs = os.path.abspath(proposal_path)
    if not os.path.isfile(prop_abs):
        raise FileNotFoundError(f"提案文件不存在: {prop_abs}")

    with open(prop_abs, "r", encoding="utf-8") as f:
        prop_data = json.load(f)

    proposal: Dict[str, Any] = {}
    if "proposals" in prop_data and isinstance(prop_data["proposals"], list) and prop_data["proposals"]:
        proposal = prop_data["proposals"][0]
    elif "ordered_candidate_ids" in prop_data:
        proposal = prop_data
    elif prop_data.get("status") == "INSUFFICIENT_EVIDENCE":
        proposal = prop_data
    else:
        raise ValueError("提案格式非法: 必须包含 ordered_candidate_ids 字段")

    # 1. 验证 candidate_pool_hash 强锁与完整性
    prop_hash = proposal.get("candidate_pool_hash") or prop_data.get("candidate_pool_hash")
    frozen_hash = snapshot.get("candidate_pool_hash")
    if frozen_hash and len(frozen_hash) == 24 and all(ch in "0123456789abcdefABCDEF" for ch in frozen_hash):
        if computed_pool_hash != frozen_hash and legacy_pool_hash != frozen_hash:
            violations.append(
                f"候选池文件完整性校验失败: 实际计算哈希 ({computed_pool_hash}) 与分析快照哈希 ({frozen_hash}) 不一致，候选池已被篡改"
            )

    if snapshot.get("scores_hash") and scores_raw_data is not None:
        current_scores_bytes = json.dumps(scores_raw_data, sort_keys=True, ensure_ascii=False).encode("utf-8")
        current_scores_hash = hashlib.sha256(current_scores_bytes).hexdigest()[:24]
        if current_scores_hash != snapshot.get("scores_hash"):
            violations.append(
                f"打分数据完整性校验失败: scores.json 实际计算哈希 ({current_scores_hash}) 与分析快照哈希 ({snapshot.get('scores_hash')}) 不一致，打分数据已被篡改"
            )

    evidence_path = os.path.join(work_dir, "analysis", "evidence_manifest.json")
    if snapshot.get("evidence_manifest_hash"):
        if not os.path.isfile(evidence_path):
            violations.append("缺失必要证据清单文件: evidence_manifest.json 不存在")
        else:
            try:
                with open(evidence_path, "r", encoding="utf-8") as ef:
                    ev_data = json.load(ef)
                current_ev_bytes = json.dumps(ev_data, sort_keys=True, ensure_ascii=False).encode("utf-8")
                current_ev_hash = hashlib.sha256(current_ev_bytes).hexdigest()[:24]
                if current_ev_hash != snapshot.get("evidence_manifest_hash"):
                    violations.append(
                        f"证据清单完整性校验失败: evidence_manifest.json 实际计算哈希 ({current_ev_hash}) 与分析快照哈希 ({snapshot.get('evidence_manifest_hash')}) 不一致，证据清单已被篡改"
                    )
            except Exception as e:
                violations.append(f"读取 evidence_manifest.json 异常: {e}")

    if not prop_hash:
        violations.append("提案必须包含 candidate_pool_hash 字段以锁定候选池快照")
    elif prop_hash != frozen_hash and prop_hash != computed_pool_hash and prop_hash != legacy_pool_hash:
        violations.append(f"候选池哈希不匹配: 提案哈希 ({prop_hash}) != 当前冻结候选池哈希 ({frozen_hash})")

    prop_run_id = proposal.get("run_id") or prop_data.get("run_id")
    if prop_run_id and prop_run_id != context.get("run_id"):
        violations.append(f"运行编号不匹配: 提案 run_id ({prop_run_id}) != 当前工作区 run_id ({context.get('run_id')})")

    if prop_data.get("status") == "INSUFFICIENT_EVIDENCE" or proposal.get("status") == "INSUFFICIENT_EVIDENCE":
        violations.append("提案显式声明 INSUFFICIENT_EVIDENCE（证据不足），已按门禁规范安全阻断，不予生成执行计划")

    ordered_ids = proposal.get("ordered_candidate_ids")
    if not isinstance(ordered_ids, list):
        violations.append("提案中选定候选列表 (ordered_candidate_ids) 必须为数组列表")
        ordered_ids = []
    elif not ordered_ids:
        violations.append("提案中选定候选列表 (ordered_candidate_ids) 不能为空")

    # V3.3: 解析并审计灰区 visual_overrides（ADR-002 D3：严禁覆盖 OTHER_SKU，仅限 UNCERTAIN 且须带真实帧证据留痕）
    raw_visual_overrides = proposal.get("visual_overrides") or []
    approved_visual_overrides: Dict[str, Dict[str, Any]] = {}
    if not isinstance(raw_visual_overrides, list):
        violations.append("visual_overrides 必须为数组列表")
    else:
        for ov in raw_visual_overrides:
            if not isinstance(ov, dict):
                violations.append("visual_overrides 条目必须为对象")
                continue
            ov_cid = str(ov.get("candidate_id") or "").strip()
            ov_rel = str(ov.get("override_relation") or "").strip()
            ov_refs = ov.get("inspected_frame_refs") or []
            ov_note = str(ov.get("visual_evidence_note") or "").strip()
            ov_agent = str(ov.get("agent_name") or proposal.get("agent_name") or "external-agent").strip()

            if not ov_cid or ov_cid not in cand_by_id:
                violations.append(f"visual_override 引用未知候选 ID: {ov_cid}")
                continue
            if ov_rel not in VALID_OVERRIDE_RELATIONS:
                violations.append(f"visual_override 非法目标关系 ({ov_rel})，仅允许 {VALID_OVERRIDE_RELATIONS}: {ov_cid}")
                continue

            c_ref = cand_by_id[ov_cid]
            sc_ref = score_by_id.get(ov_cid, {})
            orig_rel = sc_ref.get("product_relation") or c_ref.get("product_relation", "UNCERTAIN")

            # 红线：绝不允许覆盖 OTHER_SKU
            if orig_rel == "OTHER_SKU":
                violations.append(f"安全红线阻断: visual_override 严禁覆盖已判定为他款/换款 (OTHER_SKU) 的片段: {ov_cid}")
                continue
            if orig_rel != "UNCERTAIN":
                warnings.append(f"visual_override 目标 {ov_cid} 原关系已为 {orig_rel}，无需覆写")
                continue

            # 校验帧/拼图证据真实存在且严格落在当前 work_dir 内
            if not isinstance(ov_refs, list) or not ov_refs:
                violations.append(f"visual_override 缺少 inspected_frame_refs 视觉证据引用: {ov_cid}")
                continue
            verified_refs = []
            for r in ov_refs:
                real_r = resolve_and_verify_work_ref(work_dir, str(r))
                if real_r is None:
                    violations.append(f"visual_override 视觉证据路径不存在或越界脱离当前工作区: {ov_cid} -> {r}")
                else:
                    verified_refs.append(str(r))
            if not verified_refs:
                continue

            if len(ov_note) < 10:
                violations.append(f"visual_override 的 visual_evidence_note 过短（须≥10字具体描述视觉特征）: {ov_cid}")
                continue

            approved_visual_overrides[ov_cid] = {
                "candidate_id": ov_cid,
                "original_relation": orig_rel,
                "override_relation": ov_rel,
                "inspected_frame_refs": verified_refs,
                "visual_evidence_note": ov_note,
                "agent_name": ov_agent,
                "audited_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

    # 解析 V3.3 clip_overrides 与违规缓解策略
    clip_overrides = proposal.get("clip_overrides") or {}
    if not isinstance(clip_overrides, dict):
        violations.append("clip_overrides 必须为以 candidate_id 为键的对象")
        clip_overrides = {}

    mitigation_mode = str(proposal.get("violation_mitigation_mode") or "strict_block").strip().lower()
    constraints = _load_constraints()
    max_cut_adjust_ms = int((constraints.get("vision_v33") or {}).get("max_cut_adjust_ms", DEFAULT_MAX_CUT_ADJUST_MS))

    # 2. 验证候选 ID 白名单、合规性、时间边界与切点微调
    selected_cands = []
    seen_ids = set()
    total_dur_ms = 0
    clips_by_source: Dict[str, List[Tuple[int, int, str]]] = {}
    need_violation_handling = False

    # 预加载源视频物理时长（仅当有 cut_adjust_end_ms > 0 时校验不越界）
    source_dur_cache_ms: Dict[str, Optional[int]] = {}

    def _get_source_duration_ms(src_path: str) -> Optional[int]:
        if src_path in source_dur_cache_ms:
            return source_dur_cache_ms[src_path]
        dur_ms_val = None
        if src_path and os.path.isfile(src_path):
            try:
                import cv2
                cap = cv2.VideoCapture(src_path)
                if cap.isOpened():
                    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
                    fc = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
                    if fps > 0 and fc > 0:
                        dur_ms_val = int(round((fc / fps) * 1000.0))
                cap.release()
            except Exception:
                dur_ms_val = None
        source_dur_cache_ms[src_path] = dur_ms_val
        return dur_ms_val

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
        ov_entry = approved_visual_overrides.get(cid)

        # 初筛合格性硬门禁 (eligible is True；若仅因严格模式 UNCERTAIN 被拒且已获合法 visual_override，则放行)
        is_eligible = sc.get("eligible") if "eligible" in sc else c.get("eligible")
        rej_reasons = list(sc.get("rejection_reasons") or c.get("rejection_reasons") or [])

        if ov_entry is not None:
            # 移除仅因 UNCERTAIN 严格模式身份未核实产生的拒绝理由；其他硬阻断（断句/价格/违规词/换款）绝不豁免
            non_uncertain_rejs = [
                r for r in rej_reasons
                if not ("UNCERTAIN" in str(r) or "未通过身份核实" in str(r) or "缺失有效视觉证据" in str(r))
            ]
            if not non_uncertain_rejs:
                is_eligible = True
                rej_reasons = []
            else:
                rej_reasons = non_uncertain_rejs

        # 违规词三通道缓解：若模式为 auto_three_channel，仅豁免非高危 WARN 级违规拒因，BLOCK 仍拦截
        c_text = c.get("text", "")
        v_matches = find_violation_matches(c_text)
        block_v_matches = [m for m in v_matches if m.get("severity") == "BLOCK"]
        warn_v_matches = [m for m in v_matches if m.get("severity") != "BLOCK"]

        if is_eligible is not True:
            violations.append(f"选定候选未通过初筛合格性检验 (eligible!=True, eligible={is_eligible}): {cid}")
        if rej_reasons:
            violations.append(f"选定候选存在硬性阻断原因: {cid} -> {', '.join(str(r) for r in rej_reasons)}")

        rel = ov_entry["override_relation"] if ov_entry else (sc.get("product_relation") or c.get("product_relation", "UNCERTAIN"))
        if rel == "OTHER_SKU":
            violations.append(f"选定片段命中非本品商品 (OTHER_SKU 阻断): {cid}")
        elif rel == "UNCERTAIN" and is_strict_policy:
            violations.append(f"严格模式下选定片段未通过商品身份核实 (UNCERTAIN): {cid}")

        p_matches = find_price_matches(c_text)
        if p_matches:
            violations.append(f"选定候选包含违规价格表达: {cid} -> {p_matches}")

        if block_v_matches:
            violations.append(f"选定候选命中高危极限违规词: {cid} -> {block_v_matches}")
        elif warn_v_matches:
            if mitigation_mode == "auto_three_channel":
                need_violation_handling = True
                warnings.append(f"候选 {cid} 含轻微敏感词 {[m.get('word') for m in warn_v_matches]}，已自动开启违规三通道处理")
            else:
                violations.append(f"选定候选命中极限违规词: {cid} -> {warn_v_matches}")

        # V3.3 切点微调边界与单段覆写校验
        s_file = c.get("source_file") or c.get("source_id", "default")
        st = int(c.get("source_start_ms", 0))
        en = int(c.get("source_end_ms", 0))
        c_ov = clip_overrides.get(cid) or {}
        if c_ov:
            adj_st = int(c_ov.get("cut_adjust_start_ms", 0) or 0)
            adj_en = int(c_ov.get("cut_adjust_end_ms", 0) or 0)
            if abs(adj_st) > max_cut_adjust_ms:
                violations.append(f"候选 {cid} 的 cut_adjust_start_ms ({adj_st}ms) 超过硬上限 ±{max_cut_adjust_ms}ms")
            if abs(adj_en) > max_cut_adjust_ms:
                violations.append(f"候选 {cid} 的 cut_adjust_end_ms ({adj_en}ms) 超过硬上限 ±{max_cut_adjust_ms}ms")
            # 严禁向内倒切吞字（默认禁止 adj_en < -150ms 或 adj_st > 150ms，避免切掉首尾音节）
            if adj_en < -150:
                violations.append(f"候选 {cid} 的 cut_adjust_end_ms ({adj_en}ms) 向内回缩过深，存在吞字截断风险")
            if adj_st > 150:
                violations.append(f"候选 {cid} 的 cut_adjust_start_ms ({adj_st}ms) 向内切入过深，存在切头吞字风险")

            new_st = st + adj_st
            new_en = en + adj_en
            if new_st < 0:
                violations.append(f"候选 {cid} 微调后起点小于 0 ({new_st}ms)")
            src_dur_ms = _get_source_duration_ms(s_file) if adj_en > 0 else None
            if src_dur_ms is not None and new_en > src_dur_ms + 50:
                violations.append(f"候选 {cid} 微调后终点 ({new_en}ms) 超出源视频时长 ({src_dur_ms}ms)")
            st, en = new_st, new_en

            # 校验单段贴纸防造假与合规
            ov_sticker = str(c_ov.get("sticker_text") or "").strip()
            if ov_sticker:
                _validate_factual_text(
                    f"候选 {cid} 贴纸文案",
                    ov_sticker,
                    profile_evidence_corpus + " " + c_text,
                    factual_tokens,
                    violations,
                )

            # 校验 zoom 上限
            if "zoom" in c_ov and c_ov["zoom"] is not None:
                z_max = float((constraints.get("zoom_effect") or {}).get("max_zoom", 1.35) or 1.35)
                if float(c_ov["zoom"]) > z_max + 1e-6 or float(c_ov["zoom"]) < 1.0:
                    violations.append(f"候选 {cid} 的 zoom ({c_ov['zoom']}) 超出约束范围 [1.0, {z_max}]")

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

    # 校验 Hook 卡 / CTA 覆写文案防造假与合规
    all_selected_text = " ".join(c.get("text", "") for c in selected_cands)
    hook_ov = proposal.get("hook_card_override")
    if isinstance(hook_ov, dict) and hook_ov.get("text"):
        _validate_factual_text(
            "hook_card_override",
            str(hook_ov["text"]),
            profile_evidence_corpus + " " + all_selected_text,
            factual_tokens,
            violations,
        )
    cta_ov = proposal.get("cta_override")
    if isinstance(cta_ov, dict) and cta_ov.get("text"):
        _validate_factual_text(
            "cta_override",
            str(cta_ov["text"]),
            profile_evidence_corpus + " " + all_selected_text,
            factual_tokens,
            violations,
        )

    # V3.4: 校验字幕同音错别字精校表（严禁改写事实、引入价格/违规词或大幅扩写）
    sub_repls = proposal.get("subtitle_replacements")
    if sub_repls is not None:
        if not isinstance(sub_repls, dict):
            violations.append("subtitle_replacements 必须为 {原ASR错词: 修正词} 字典对象")
        else:
            for wrong_k, fixed_v in sub_repls.items():
                wk = str(wrong_k or "").strip()
                fv = str(fixed_v or "").strip()
                if not wk or not fv:
                    violations.append(f"subtitle_replacements 不允许空键或空替换值: {wk!r} -> {fv!r}")
                    continue
                if abs(len(fv) - len(wk)) > 4:
                    violations.append(f"subtitle_replacements 仅允许同音/错别字精校（字数差须≤4）: '{wk}' -> '{fv}'")
                    continue
                _validate_factual_text(
                    f"subtitle_replacements[{wk}]",
                    fv,
                    profile_evidence_corpus + " " + all_selected_text,
                    factual_tokens,
                    violations,
                )

    kw_ov = proposal.get("highlight_keywords_override")
    if kw_ov is not None:
        if not isinstance(kw_ov, list):
            violations.append("highlight_keywords_override 必须为字符串数组")
        else:
            for kw in kw_ov:
                ks = str(kw or "").strip()
                if not ks:
                    continue
                if len(ks) > 10:
                    violations.append(f"highlight_keywords_override 单条高亮词过长 (>10字): '{ks}'")
                    continue
                if find_price_matches(ks):
                    violations.append(f"highlight_keywords_override 含价格表达: '{ks}'")
                if any(m.get("severity") == "BLOCK" for m in find_violation_matches(ks)):
                    violations.append(f"highlight_keywords_override 含违规极限词: '{ks}'")

    # 3. 成片时长约束检查
    dur_range_str = str(spec.get("options", {}).get("duration_range", "45-65"))
    try:
        min_s, max_s = [float(x.strip()) for x in dur_range_str.split("-")]
    except Exception:
        min_s, max_s = 45.0, 65.0
    total_dur_s = total_dur_ms / 1000.0

    hard_max_s = float(auto_cfg.get("hard_max_duration") or spec.get("options", {}).get("hard_max_duration") or (max_s + 5.0))
    if total_dur_s > hard_max_s:
        violations.append(f"成片预计总时长 ({total_dur_s:.1f}s) 严重超过目标硬性上限 ({hard_max_s:.1f}s)")
    elif total_dur_s < min_s - 5.0 or total_dur_s > max_s:
        warnings.append(f"成片预计总时长 ({total_dur_s:.1f}s) 偏离目标区间 [{min_s}, {max_s}]s")

    # 4. 封面有效性校验
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

    prop_bytes = json.dumps(proposal, sort_keys=True, ensure_ascii=False).encode("utf-8")
    proposal_hash = hashlib.sha256(prop_bytes).hexdigest()

    audit_report = {
        "schema_version": "3.3",
        "run_id": context.get("run_id"),
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
        "agent_visual_overrides": list(approved_visual_overrides.values()),
        "has_agent_visual_overrides": bool(approved_visual_overrides),
        "violation_mitigation_mode": mitigation_mode,
        "audited_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    audit_path = os.path.join(work_dir, "plan_audit.json")
    with open(audit_path, "w", encoding="utf-8") as f:
        json.dump(audit_report, f, ensure_ascii=False, indent=2)

    if violations:
        _write_run_state(work_dir, "PLAN_REJECTED", "validate_plan", 0.55, f"提案未通过审计: {'; '.join(violations)}")
        _log_event(work_dir, "PLAN_VALIDATION_FAILED", "Agent 提案被约束网关拦截", audit_report)
        raise ValueError("提案未通过质量与安全门禁:\n" + "\n".join(f"- {v}" for v in violations))

    # 5. 编译为执行计划 validated_plan.json（将审核通过的 visual_overrides 生效于副本候选）
    effective_candidates = []
    for c in all_candidates:
        cid = c.get("candidate_id")
        if cid in approved_visual_overrides:
            c_copy = dict(c)
            c_copy["product_relation"] = approved_visual_overrides[cid]["override_relation"]
            c_copy["relation_confidence"] = max(float(c.get("relation_confidence") or 0.0), 0.85)
            c_copy["agent_visual_override"] = approved_visual_overrides[cid]
            effective_candidates.append(c_copy)
        else:
            effective_candidates.append(c)

    compiled_plan = compile_v3_plan(
        proposal=proposal,
        candidates=effective_candidates,
        scores=scores_data,
        spec_data=spec,
        constraints=constraints,
    )
    if selected_cover:
        compiled_plan["selected_cover"] = selected_cover
        compiled_plan["cover"] = selected_cover
    compiled_plan["chosen_cover_id"] = chosen_cover_id or (selected_cover.get("id") if selected_cover else None)

    compiled_plan["proposal_hash"] = proposal_hash
    compiled_plan["candidate_pool_hash"] = prop_hash
    compiled_plan["run_id"] = context.get("run_id")
    compiled_plan["agent_visual_overrides"] = list(approved_visual_overrides.values())

    try:
        from src.agent.agent_director import assign_sticker_sides
        assign_sticker_sides(compiled_plan, constraints)
    except Exception as e:
        print(f"  [WARN] 自动避脸计算跳过: {e}", file=sys.stderr)

    # 若启用违规三通道缓解且命中轻度敏感词，自动产出 violation_handling.json 供渲染与 QC 消费
    if need_violation_handling:
        try:
            seg_path = os.path.join(work_dir, "whisper_segments_verified.json")
            if not os.path.isfile(seg_path):
                seg_path = os.path.join(work_dir, "whisper_segments.json")
            segs_for_v = []
            if os.path.isfile(seg_path):
                with open(seg_path, "r", encoding="utf-8") as sf:
                    segs_for_v = json.load(sf)
            from src.tools.apply_violation_handling import prepare_violation_handling
            prepare_violation_handling(compiled_plan, segs_for_v, work_dir)
        except Exception as e:
            print(f"  [WARN] 违规词三通道准备跳过: {e}", file=sys.stderr)

    plan_to_hash = {k: v for k, v in compiled_plan.items() if k != "validated_plan_hash"}
    plan_bytes = json.dumps(plan_to_hash, sort_keys=True, ensure_ascii=False).encode("utf-8")
    validated_plan_hash = hashlib.sha256(plan_bytes).hexdigest()
    compiled_plan["validated_plan_hash"] = validated_plan_hash

    plan_path = os.path.join(work_dir, "validated_plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(compiled_plan, f, ensure_ascii=False, indent=2)

    audit_report["validated_plan_hash"] = validated_plan_hash
    with open(audit_path, "w", encoding="utf-8") as f:
        json.dump(audit_report, f, ensure_ascii=False, indent=2)

    downstream_files = [
        "final.mp4",
        "cover.jpg",
        "cover_meta.json",
        "copywriting.md",
        "render_manifest.json",
        "technical_qc.json",
        "editorial_qc.json",
        "continuity_review.json",
        "seams_review.jpg",
    ]
    for df in downstream_files:
        df_p = os.path.join(output_dir, df)
        if os.path.isfile(df_p):
            try:
                os.remove(df_p)
            except Exception:
                pass
    work_rm = os.path.join(work_dir, "render_manifest.json")
    if os.path.isfile(work_rm):
        try:
            os.remove(work_rm)
        except Exception:
            pass

    _write_run_state(work_dir, "PLAN_VALIDATED", "validate_plan", 0.65, f"提案审计通过 ({len(selected_cands)} 分镜, {total_dur_s:.1f}s)，计划已冻结")
    _log_event(work_dir, "PLAN_VALIDATED", f"分镜计划编译成功: {len(selected_cands)} 个分镜", audit_report)

    return {
        "schema_version": "3.3",
        "status": "PLAN_VALIDATED",
        "run_id": context.get("run_id"),
        "plan_path": plan_path,
        "audit_path": audit_path,
        "clips_count": len(selected_cands),
        "total_duration_s": round(total_dur_s, 2),
        "chosen_cover_id": selected_cover.get("id") if selected_cover else None,
        "agent_visual_overrides_count": len(approved_visual_overrides),
        "warnings": warnings,
        "next_step": f"python -m src.cli render --run {work_dir}",
    }
