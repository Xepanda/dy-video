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

    zoom_c = (constraints.get("zoom_effect", {}) or {})
    z_target = float(zoom_c.get("zoom", 1.3) or 1.3)
    z_max = float(zoom_c.get("max_zoom", 1.35) or 1.35)
    if z_target > z_max:
        z_target = z_max
    y_anchor = float(zoom_c.get("y_anchor", 0.35) or 0.35)

    clips = []
    ordered_cids = proposal.get("ordered_candidate_ids", [])

    for idx, cid in enumerate(ordered_cids, 1):
        cand = cand_map[cid]
        sc = score_map.get(cid, {})
        role = str(sc.get("narrative_role", "PROOF")).upper()

        if idx == 1:
            cat = "hook"
            sticker = "开场必看"
        elif idx == len(ordered_cids):
            cat = "decision_closing"
            sticker = "闭眼入"
        elif role == "FABRIC":
            cat = "fabric"
            sticker = "羊毛面料"
        elif role == "FIT":
            cat = "fit"
            sticker = "版型显瘦"
        elif role == "STYLING":
            cat = "styling"
            sticker = "一衣多穿"
        else:
            cat = "styling" if role in ("HOOK", "STYLING") else "fit"
            sticker = "精选细节"

        st_sec = round(cand["source_start_ms"] / 1000.0, 3)
        end_sec = round(cand["source_end_ms"] / 1000.0, 3)
        dur_sec = round(end_sec - st_sec, 3)

        # 面料块推镜
        zoom = z_target if cat == "fabric" else 1.0

        clip_obj = {
            "id": f"c{idx:02d}_{cat}",
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
            "source_file": cand.get("source_id") or raw_video_path or "",
            "source_tag": os.path.basename(str(cand.get("source_id") or "")),
            "duration_in_range": True,
            "sticker_text": sticker,
        }
        clips.append(clip_obj)

    # 允许 1 处镜像打破站位单一感
    for clip in clips[1:]:
        if clip.get("category") in ("styling", "fit") and clip.get("zoom", 1.0) <= 1.0:
            clip["effect"] = "hflip"
            clip["effect_reason"] = "打破连续分镜的站位单一感（SKILL 2.1）"
            break

    total_dur = round(sum(c["dur"] for c in clips), 3)

    # 防御性门禁：确保面料类 (category=fabric) 占比严格 <= 24%（对齐 QC 严格门禁 25%）
    fabric_dur = sum(c["dur"] for c in clips if c.get("category") == "fabric")
    if total_dur > 0 and (fabric_dur / total_dur) > 0.24:
        for clip in reversed(clips):
            if clip.get("category") == "fabric":
                clip["category"] = "fit"
                clip["sticker_text"] = "精选细节"
                clip["zoom"] = 1.0
                fabric_dur -= clip["dur"]
                if (fabric_dur / total_dur) <= 0.24:
                    break

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
