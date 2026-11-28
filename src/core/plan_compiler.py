#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
plan_compiler.py - 兼容性入口与 V3.3 导演提案编译器
===================================================
1. 历史 PlanCompiler 委托给 src/tools/build_plan.py 的泛化实现；
2. compile_v3_plan 将经过审计的 v3.2/v3.3 proposal 编译为 VideoAssembler 执行契约，
   支持外部 Agent 精细控制单段切点微调 (cut_adjust_start_ms/end_ms)、特效 (zoom/hflip)、
   经核实贴纸文案 (sticker_text)、Hook 卡与 CTA 覆写。
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from typing import Dict, Any, List, Optional
from src.tools.build_plan import build_plan, build_blocks, is_blacklisted, _derive_hook_text, _derive_cta_text  # noqa: F401
from src.core.category_profile import load_category_profile


def _derive_verified_sticker(
    cat: str,
    role: str,
    idx: int,
    total_clips: int,
    cand: Dict[str, Any],
    product: Dict[str, Any],
    cat_profile: Optional[Dict[str, Any]] = None,
) -> str:
    """根据品类档案、商品档案与候选实际证据生成经证实的贴纸文案，绝不杜撰未经证实的面料或款式词。"""
    c_text = cand.get("text", "")
    sp_refs = cand.get("selling_point_evidence_refs") or []
    prof_sps = product.get("selling_points", []) if isinstance(product, dict) else []
    lex = (cat_profile or load_category_profile()).get("verified_lexicon") or {}

    if cat == "fabric":
        known_fabrics = tuple(lex.get("fabric") or (
            "羊绒", "白鸭绒", "鹅绒", "蚕丝", "真丝", "全棉", "纯棉", "亚麻",
            "德绒", "发热", "保暖", "莫代尔", "三防", "防风", "防泼水", "羊毛", "蕾丝",
        ))
        for fab in known_fabrics:
            if fab in c_text or any(fab in sp for sp in prof_sps):
                return f"{fab}面料"
        if sp_refs:
            return f"{sp_refs[0][:4]}面料"
        return "面料细节"

    # 优先根据当前镜头真实口播语义精准匹配贴纸，杜绝“讲不掉毛却贴版型细节”的图文割裂
    topic_rules = (
        (("不掉毛", "不怎么掉毛", "不算有掉毛", "几乎是不掉"), "不易掉毛"),
        (("贴肤", "贴皮肤", "贴服", "不扎", "板很软", "很软"), "亲肤软糯"),
        (("保暖", "暖和", "鹅绒", "零下"), "锁温保暖"),
        (("显瘦", "不臃肿", "遮肉"), "显瘦版型"),
        (("尺码", "建议穿", "斤"), "尺码参考"),
        (("搭长裙", "配牛仔", "搭配", "百搭"), "百搭实穿"),
    )
    for triggers, label in topic_rules:
        if any(t in c_text for t in triggers):
            return label

    if idx == 1:
        if prof_sps:
            return f"{prof_sps[0][:4]}推荐" if len(prof_sps[0]) >= 2 else "开场精选"
        return "开场精选"

    if idx == total_clips:
        return "品质保障"

    if cat == "fit":
        known_fits = tuple(lex.get("fit") or ("显瘦", "修身", "垂坠", "正肩", "落肩", "宽松", "直筒", "包容", "遮肉"))
        for f in known_fits:
            if f in c_text or any(f in sp for sp in prof_sps):
                return f if "版" in f else f"{f}版型"
        return "版型细节"

    if cat == "styling":
        known_stylings = tuple(lex.get("styling") or ("百搭", "一衣多穿", "叠穿", "气质", "搭配"))
        for st in known_stylings:
            if st in c_text or any(st in sp for sp in prof_sps):
                return st
        return "穿搭展示"

    return "精选细节"


def compile_v3_plan(
    proposal: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    scores: Dict[str, Any],
    spec_data: Dict[str, Any],
    constraints: Optional[Dict[str, Any]] = None,
    raw_video_path: Optional[str] = None,
) -> Dict[str, Any]:
    """将通过审计的 v3.2/v3.3 提案编译为视频组装引擎所需的完整 plan.json 契约。"""
    constraints = constraints or {}
    cand_map = {c["candidate_id"]: c for c in candidates}
    score_map = {s["candidate_id"]: s for s in scores.get("scores", [])}
    product_dict = spec_data.get("product", {}) or {}
    cat_profile = load_category_profile(spec_data)

    zoom_c = (constraints.get("zoom_effect", {}) or {})
    z_target = float(zoom_c.get("zoom", 1.3) or 1.3)
    z_max = float(zoom_c.get("max_zoom", 1.35) or 1.35)
    if z_target > z_max:
        z_target = z_max
    y_anchor_default = float(zoom_c.get("y_anchor", 0.35) or 0.35)

    clip_overrides = proposal.get("clip_overrides") or {}
    has_explicit_effect_override = any(
        isinstance(v, dict) and "effect" in v for v in clip_overrides.values()
    )

    clips = []
    ordered_cids = proposal.get("ordered_candidate_ids", [])
    total_clips = len(ordered_cids)

    for idx, cid in enumerate(ordered_cids, 1):
        if cid not in cand_map:
            raise ValueError(f"提案包含未知候选 ID: {cid}")
        cand = cand_map[cid]
        sc = score_map.get(cid, {})
        c_ov = clip_overrides.get(cid) if isinstance(clip_overrides.get(cid), dict) else {}
        role = str(sc.get("narrative_role", "PROOF")).upper()

        if c_ov.get("category"):
            cat = str(c_ov["category"])
        elif idx == 1:
            cat = "hook"
        elif idx == total_clips:
            cat = "decision_closing"
        elif role == "FABRIC":
            cat = "fabric"
        elif role == "FIT":
            cat = "fit"
        elif role == "STYLING":
            cat = "styling"
        else:
            cat = "styling" if role in ("HOOK", "STYLING") else "fit"

        if "sticker_text" in c_ov and c_ov["sticker_text"] is not None:
            # 显式传入 "" 允许导演关闭该镜次贴纸（双人/拥挤画面防压脸）
            sticker = str(c_ov["sticker_text"]).strip()
        else:
            sticker = _derive_verified_sticker(cat, role, idx, total_clips, cand, product_dict, cat_profile)

        # 应用已通过门禁审计的 V3.3 单段切点微调
        adj_st_ms = int(c_ov.get("cut_adjust_start_ms", 0) or 0)
        adj_en_ms = int(c_ov.get("cut_adjust_end_ms", 0) or 0)
        eff_st_ms = max(0, int(cand["source_start_ms"]) + adj_st_ms)
        eff_en_ms = max(eff_st_ms + 200, int(cand["source_end_ms"]) + adj_en_ms)

        st_sec = round(eff_st_ms / 1000.0, 3)
        end_sec = round(eff_en_ms / 1000.0, 3)
        dur_sec = round(end_sec - st_sec, 3)

        if "zoom" in c_ov and c_ov["zoom"] is not None:
            zoom = min(z_max, max(1.0, float(c_ov["zoom"])))
        else:
            zoom = z_target if cat == "fabric" else 1.0

        y_anchor = float(c_ov.get("y_anchor", y_anchor_default) or y_anchor_default)
        effect = str(c_ov.get("effect") or "none")

        prod_rel = cand.get("product_relation") or "UNVERIFIABLE"
        rel_conf = cand.get("relation_confidence")
        if rel_conf is None:
            rel_conf = sc.get("composite", 0.8) if prod_rel in ("SAME_SKU", "SAME_SKU_VARIANT", "ACCESSORY_OR_STYLING") else 0.0

        clip_obj = {
            "id": f"c{idx:02d}_{cat}",
            "candidate_id": cid,
            "st": st_sec,
            "end": end_sec,
            "dur": dur_sec,
            "source_start_ms": eff_st_ms,
            "source_end_ms": eff_en_ms,
            "cut_adjust_start_ms": adj_st_ms,
            "cut_adjust_end_ms": adj_en_ms,
            "is_hook": (idx == 1),
            "zoom": zoom,
            "y_anchor": y_anchor,
            "effect": effect,
            "category": cat,
            "desc": (cand["text"][:24] + "…") if len(cand["text"]) > 24 else cand["text"],
            "full_text": cand["text"],
            "text": cand["text"],
            "source_file": cand.get("source_file") or cand.get("source_id") or raw_video_path or "",
            "source_id": cand.get("source_id") or "",
            "source_file_sha256": cand.get("source_file_sha256") or "",
            "source_tag": os.path.basename(str(cand.get("source_id") or "")),
            "duration_in_range": True,
            "sticker_text": sticker,
            "product_relation": prod_rel,
            "relation_confidence": round(float(rel_conf), 2),
            "evidence_refs": cand.get("selling_point_evidence_refs", []) + (cand.get("frame_ids") or []),
            "risks": cand.get("risks", []),
            "matched_selling_points": cand.get("selling_point_evidence_refs", []),
        }
        if cand.get("agent_visual_override"):
            clip_obj["agent_visual_override"] = cand["agent_visual_override"]
        clips.append(clip_obj)

    # 若 Agent 未显式指定 effect，保留默认规则：允许 1 处镜像打破站位单一感
    if not has_explicit_effect_override:
        for clip in clips[1:]:
            if clip.get("category") in ("styling", "fit") and clip.get("zoom", 1.0) <= 1.0 and clip.get("effect") == "none":
                clip["effect"] = "hflip"
                clip["effect_reason"] = "打破连续分镜的站位单一感（SKILL 2.1）"
                break

    total_dur = round(sum(c["dur"] for c in clips), 3)

    sources_in_plan = sorted({c["source_file"] for c in clips if c.get("source_file")})
    primary_video = raw_video_path or (sources_in_plan[0] if sources_in_plan else "")
    options = spec_data.get("options", {}) or {}

    hook_ov = proposal.get("hook_card_override") if isinstance(proposal.get("hook_card_override"), dict) else {}
    cta_ov = proposal.get("cta_override") if isinstance(proposal.get("cta_override"), dict) else {}

    hook_block = {
        "text": str(hook_ov.get("text") or options.get("hook_card_text") or _derive_hook_text(spec_data)),
        "dur": float(hook_ov.get("dur", 2.5) or 2.5),
        "y": int(hook_ov.get("y", 280) or 280),
    }
    cta_block = {
        "text": str(cta_ov.get("text") or options.get("cta_text") or _derive_cta_text(spec_data)),
        "dur": float(cta_ov.get("dur", 2.5) or 2.5),
    }

    meta = {
        "generator": "v33_visual_director",
        "proposal_id": proposal.get("proposal_id"),
        "style": proposal.get("style"),
        "total_duration": total_dur,
        "selling_points": proposal.get("selling_point_coverage", []),
        "domain_profile": cat_profile.get("profile_id", "womenswear"),
    }

    # V3.4: 支持经过审计的同音错别字字幕精校与口播高亮词扩展
    sub_replacements = proposal.get("subtitle_replacements")
    if not isinstance(sub_replacements, dict):
        sub_replacements = {}
    base_kws = list(options.get("highlight_keywords") or [])
    extra_kws = proposal.get("highlight_keywords_override")
    if isinstance(extra_kws, list):
        for kw in extra_kws:
            k_str = str(kw or "").strip()
            if k_str and k_str not in base_kws:
                base_kws.append(k_str)
    else:
        # 自动补充当前成片口播中真实出现的高频卖点词（避免只高亮标题复合词而漏高亮口播重点）
        joined_speech = " ".join(c.get("text", "") for c in clips)
        for auto_kw in ("真羊毛", "全羊毛", "羊毛", "显瘦", "保暖", "不掉毛", "贴肤", "很软", "洋气", "百搭"):
            if auto_kw in joined_speech and auto_kw not in base_kws:
                base_kws.append(auto_kw)

    plan = {
        "project_id": spec_data.get("project_id", "demo"),
        "raw_video_path": os.path.abspath(primary_video) if primary_video else "",
        "multi_source": len(sources_in_plan) > 1,
        "sources_used": sources_in_plan,
        "total_expected_duration": total_dur,
        "clips": clips,
        "brand": spec_data.get("brand", {}) or {},
        "hook_card": dict(hook_block),
        "hook": dict(hook_block),
        "cta": dict(cta_block),
        "cta_card": dict(cta_block),
        "boom": {"volume": 0.45, "file": "boom.wav"},
        "subtitles": {
            "keywords": base_kws,
            "replacements": dict(sub_replacements),
        },
        "selection_meta": meta,
        "meta": meta,
        "v3_proposal_meta": proposal,
    }
    return plan


class PlanCompiler:
    """兼容壳：旧接口签名保留，内部委托 build_plan。"""

    def __init__(self, target_duration_min: float = 45.0, target_duration_max: float = 55.0):
        self.target_min = target_duration_min
        self.target_max = target_duration_max

    def compile_plan(self, raw_video_path, segments, spec_data):
        constraints = {
            "video": {
                "duration_range": [self.target_min, self.target_max],
                "funnel": {
                    "hook": {"min": 3, "max": 8},
                    "fabric": {"min": 8, "max": 20},
                    "fit": {"min": 8, "max": 15},
                    "styling": {"min": 15, "max": 30},
                },
            }
        }
        return build_plan(raw_video_path, segments, spec_data, constraints)


if __name__ == "__main__":
    print("plan_compiler 已重构为 build_plan 兼容壳；请使用 src/tools/build_plan.py")
