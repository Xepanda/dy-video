#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
plan_compiler.py - 兼容性入口（已重构）
=======================================
历史版本在此文件中硬编码了 demo 视频的分镜时间戳，换素材即失效（技术评审 G1）。
现全面委托给 src/tools/build_plan.py 的泛化实现；本文件仅保留旧导入路径兼容。

新代码请直接使用：
    from src.tools.build_plan import build_plan
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from typing import Dict, Any, List, Optional
from src.tools.build_plan import build_plan, build_blocks, is_blacklisted, _derive_hook_text, _derive_cta_text  # noqa: F401


def _derive_verified_sticker(
    cat: str,
    role: str,
    idx: int,
    total_clips: int,
    cand: Dict[str, Any],
    product: Dict[str, Any],
) -> str:
    """根据商品档案与候选实际证据生成经证实的贴纸文案，绝不硬编码未经证实的面料或款式词。"""
    c_text = cand.get("text", "")
    sp_refs = cand.get("selling_point_evidence_refs") or []
    prof_sps = product.get("selling_points", []) if isinstance(product, dict) else []

    if idx == 1:
        if prof_sps:
            return f"{prof_sps[0][:4]}推荐" if len(prof_sps[0]) >= 2 else "开场精选"
        return "开场精选"

    if idx == total_clips:
        return "品质保障"

    if cat == "fabric":
        # 仅当文本或商品档案确实含有相应面料词时，才使用具体面料名；否则通用“面料细节”
        known_fabrics = (
            "羊绒", "白鸭绒", "鹅绒", "蚕丝", "真丝", "全棉", "纯棉", "亚麻",
            "德绒", "发热", "保暖", "莫代尔", "三防", "防风", "防泼水", "羊毛"
        )
        for fab in known_fabrics:
            if fab in c_text or any(fab in sp for sp in prof_sps):
                return f"{fab}面料"
        if sp_refs:
            return f"{sp_refs[0][:4]}面料"
        return "面料细节"

    if cat == "fit":
        known_fits = ("显瘦", "修身", "垂坠", "正肩", "落肩", "宽松", "直筒", "包容", "遮肉")
        for f in known_fits:
            if f in c_text or any(f in sp for sp in prof_sps):
                return f if "版" in f else f"{f}版型"
        return "版型细节"

    if cat == "styling":
        known_stylings = ("百搭", "一衣多穿", "叠穿", "气质", "搭配")
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
    """将 SequenceOptimizer 优选的提案编译为视频组装引擎所需的完整 plan.json 契约。"""
    constraints = constraints or {}
    cand_map = {c["candidate_id"]: c for c in candidates}
    score_map = {s["candidate_id"]: s for s in scores.get("scores", [])}
    product_dict = spec_data.get("product", {}) or {}

    zoom_c = (constraints.get("zoom_effect", {}) or {})
    z_target = float(zoom_c.get("zoom", 1.3) or 1.3)
    z_max = float(zoom_c.get("max_zoom", 1.35) or 1.35)
    if z_target > z_max:
        z_target = z_max
    y_anchor = float(zoom_c.get("y_anchor", 0.35) or 0.35)

    clips = []
    ordered_cids = proposal.get("ordered_candidate_ids", [])
    total_clips = len(ordered_cids)

    for idx, cid in enumerate(ordered_cids, 1):
        if cid not in cand_map:
            raise ValueError(f"提案包含未知候选 ID: {cid}")
        cand = cand_map[cid]
        sc = score_map.get(cid, {})
        role = str(sc.get("narrative_role", "PROOF")).upper()

        if idx == 1:
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

        sticker = _derive_verified_sticker(cat, role, idx, total_clips, cand, product_dict)

        st_sec = round(cand["source_start_ms"] / 1000.0, 3)
        end_sec = round(cand["source_end_ms"] / 1000.0, 3)
        dur_sec = round(end_sec - st_sec, 3)

        # 面料块推镜
        zoom = z_target if cat == "fabric" else 1.0

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
            "is_hook": (idx == 1),
            "zoom": zoom,
            "y_anchor": y_anchor,
            "effect": "none",
            "category": cat,
            "desc": (cand["text"][:24] + "…") if len(cand["text"]) > 24 else cand["text"],
            "full_text": cand["text"],
            "text": cand["text"],
            "source_file": cand.get("source_file") or cand.get("source_id") or raw_video_path or "",
            "source_id": cand.get("source_id") or "",
            "source_tag": os.path.basename(str(cand.get("source_id") or "")),
            "duration_in_range": True,
            "sticker_text": sticker,
            # 完整保留商品关系与证据链（PR-03 / PR-08 门禁硬依赖，字段缺失严禁默认视为 SAME_SKU）
            "product_relation": prod_rel,
            "relation_confidence": round(float(rel_conf), 2),
            "evidence_refs": cand.get("selling_point_evidence_refs", []) + (cand.get("frame_ids") or []),
            "risks": cand.get("risks", []),
            "matched_selling_points": cand.get("selling_point_evidence_refs", []),
        }
        clips.append(clip_obj)

    # 允许 1 处镜像打破站位单一感
    for clip in clips[1:]:
        if clip.get("category") in ("styling", "fit") and clip.get("zoom", 1.0) <= 1.0:
            clip["effect"] = "hflip"
            clip["effect_reason"] = "打破连续分镜的站位单一感（SKILL 2.1）"
            break

    total_dur = round(sum(c["dur"] for c in clips), 3)

    sources_in_plan = sorted({c["source_file"] for c in clips if c.get("source_file")})
    primary_video = raw_video_path or (sources_in_plan[0] if sources_in_plan else "")
    options = spec_data.get("options", {}) or {}

    meta = {
        "generator": "v3_agent_director",
        "proposal_id": proposal.get("proposal_id"),
        "style": proposal.get("style"),
        "total_duration": total_dur,
        "selling_points": proposal.get("selling_point_coverage", []),
    }

    plan = {
        "project_id": spec_data.get("project_id", "demo"),
        "raw_video_path": os.path.abspath(primary_video) if primary_video else "",
        "multi_source": len(sources_in_plan) > 1,
        "sources_used": sources_in_plan,
        "total_expected_duration": total_dur,
        "clips": clips,
        "brand": spec_data.get("brand", {}) or {},
        "hook_card": {
            "text": options.get("hook_card_text") or _derive_hook_text(spec_data),
            "dur": 2.5,
            "y": 280,
        },
        "cta": {
            "text": options.get("cta_text") or _derive_cta_text(spec_data),
            "dur": 2.5,
        },
        "boom": {"volume": 0.45, "file": "boom.wav"},
        "subtitles": {"keywords": options.get("highlight_keywords", [])},
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
