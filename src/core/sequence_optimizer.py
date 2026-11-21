# -*- coding: utf-8 -*-
"""
sequence_optimizer.py - 全片全局序列优化与多方案草案生成（V3 PR-05 核心）
========================================================================
负责：
  1. 基于 Beam Search 与确定性约束优化全片镜头序列；
  2. 严格硬约束：
     - 同源时间重叠严格禁入（同一素材禁止时间倒流或重叠回放）；
     - 严格过滤 OTHER_SKU，只选 eligible=True 的候选；
     - 时长必须满足目标范围 [min_dur_ms, max_dur_ms]；
     - 重复惩罚：同一聚类簇 duplicate_cluster_id 仅允许至多 1 个入选；
  3. 产出 balanced / fast_paced / showcase 三类内部草案，由评估器择优；
  4. 当素材不足或无可用本品片段时，安全返回 INSUFFICIENT_EVIDENCE 与诊断报告，
     绝不为了凑满时长强行塞入他品；
  5. 输出符合 sequence_proposals.schema.json。
"""

import os
import sys
from typing import Dict, Any, List, Optional, Tuple, Set

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class InsufficientEvidenceError(RuntimeError):
    """当素材证据不足以构建合规成片时抛出的硬阻断异常。"""
    pass


class SequenceOptimizer:
    """全片序列编排器。

    支持依据不同带货风格（balanced, fast, detailed）进行多路全局 Beam Search 求解，
    并在满足时长范围、卖点覆盖与同源时间无重叠的前提下输出优选提案。
    """

    STYLE_ALIASES = {
        "fast": "fast",
        "fast_paced": "fast",
        "detailed": "detailed",
        "showcase": "detailed",
        "balanced": "balanced",
    }

    def __init__(
        self,
        target_min_ms: int = 45000,
        target_max_ms: int = 65000,
        beam_width: int = 5,
        preferred_style: Optional[str] = None,
    ):
        self.target_min_ms = target_min_ms
        self.target_max_ms = target_max_ms
        self.beam_width = beam_width
        self.preferred_style = self.STYLE_ALIASES.get((preferred_style or "balanced").strip().lower(), "balanced")

    def _is_overlapping(self, cand_a: Dict[str, Any], cand_b: Dict[str, Any]) -> bool:
        """检查两个候选是否在同一素材源上存在时间重叠。"""
        if cand_a.get("source_id") != cand_b.get("source_id"):
            return False
        s1, e1 = cand_a["source_start_ms"], cand_a["source_end_ms"]
        s2, e2 = cand_b["source_start_ms"], cand_b["source_end_ms"]
        # 允许微小容差 100ms
        return max(s1, s2) < min(e1, e2) - 100

    def optimize_sequence(
        self,
        candidates: List[Dict[str, Any]],
        candidate_scores: Dict[str, Any],
        product_profile: Dict[str, Any],
        target_min_ms: Optional[int] = None,
        target_max_ms: Optional[int] = None,
        preferred_style: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """执行全局序列优化，生成三套合法提案。"""
        min_ms = target_min_ms or self.target_min_ms
        max_ms = target_max_ms or self.target_max_ms

        score_map = {s["candidate_id"]: s for s in candidate_scores.get("scores", [])}
        cand_map = {c["candidate_id"]: c for c in candidates}

        # 过滤出合格候选
        eligible_cands = []
        for c in candidates:
            cid = c["candidate_id"]
            sc = score_map.get(cid)
            if not sc or not sc.get("eligible", False):
                continue
            if sc.get("product_relation") not in ("SAME_SKU", "SAME_SKU_VARIANT", "ACCESSORY_OR_STYLING"):
                continue
            dur = c["source_end_ms"] - c["source_start_ms"]
            eligible_cands.append({
                "cand": c,
                "score_info": sc,
                "dur": dur,
                "composite": sum(sc["subscores"].values()) / len(sc["subscores"]),
                "role": sc.get("narrative_role", "PROOF"),
                "cluster": sc.get("duplicate_cluster_id", cid),
            })

        # 按综合分降序排序
        eligible_cands.sort(key=lambda x: x["composite"], reverse=True)

        # 检查是否有足够素材
        total_eligible_dur = sum(x["dur"] for x in eligible_cands)
        if not eligible_cands or total_eligible_dur < min_ms:
            return {
                "schema_version": "3.0",
                "status": "INSUFFICIENT_EVIDENCE",
                "proposals": [],
                "evidence_gap": [
                    f"可用本品素材累计时长不足 ({total_eligible_dur/1000:.1f}s < 目标下限 {min_ms/1000:.1f}s)",
                    "缺少足够通过商品门禁的合格候选片段"
                ],
            }

        # 区分风格生成提案：balanced, fast, detailed
        proposals = []

        # 参数类内容（面料/做工）占比上限约束（决策 6：QC 严格门禁 <= 25%，此处收敛为 23%）
        max_fabric_ms = int(max_ms * 0.23)

        effective_style = preferred_style or kwargs.get("preferred_style") or self.preferred_style or "balanced"
        pref = self.STYLE_ALIASES.get(str(effective_style).strip().lower(), "balanced")
        if pref == "fast":
            style_order = ("fast", "balanced", "detailed")
        elif pref == "detailed":
            style_order = ("detailed", "balanced", "fast")
        else:
            style_order = ("balanced", "fast", "detailed")

        for style in style_order:
            # 偏好排序与候选池配置
            pool = list(eligible_cands)
            if style == "fast":
                pool.sort(key=lambda x: (x["dur"] > 5000, -x["composite"]))
            elif style == "detailed":
                pool.sort(key=lambda x: (x["role"] not in ("FABRIC", "PROOF", "STYLING"), -x["composite"]))
            else:
                pool.sort(key=lambda x: -x["composite"])

            # Beam Search 全局多路搜索（Beam Width = 5）
            # 状态元组: (score, chosen_cands, chosen_clusters, cur_dur, cur_fabric_dur, covered_sp)
            initial_beams = []
            hooks = [x for x in pool if x["role"] == "HOOK"]
            start_candidates = hooks[:3] if hooks else pool[:3]
            for start_cand in start_candidates:
                fab_d = start_cand["dur"] if start_cand["role"] == "FABRIC" else 0
                sp_cov = set(start_cand["cand"].get("selling_point_evidence_refs") or [])
                initial_beams.append((
                    start_cand["composite"],
                    [start_cand],
                    {start_cand["cluster"]},
                    start_cand["dur"],
                    fab_d,
                    sp_cov
                ))

            if not initial_beams:
                initial_beams = [(0.0, [], set(), 0, 0, set())]

            beams = initial_beams
            completed_beams = []

            for _ in range(len(pool)):
                next_beams = []
                for b_score, b_cands, b_clusters, b_dur, b_fab_dur, b_sp in beams:
                    if b_dur >= min_ms:
                        completed_beams.append((b_score, b_cands, b_clusters, b_dur, b_fab_dur, b_sp))
                        if b_dur >= max_ms:
                            continue

                    expanded = False
                    for item in pool:
                        item_cid = item["cand"]["candidate_id"]
                        if any(chosen["cand"]["candidate_id"] == item_cid for chosen in b_cands):
                            continue
                        if item["cluster"] in b_clusters:
                            continue
                        # 同源重叠检查
                        if any(self._is_overlapping(item["cand"], chosen["cand"]) for chosen in b_cands):
                            continue

                        # 面料占比严格门禁：加入后整体面料占比不得超过 24%
                        new_fab_dur = b_fab_dur + (item["dur"] if item["role"] == "FABRIC" else 0)
                        new_tot_dur = b_dur + item["dur"]
                        if new_tot_dur > 0 and (new_fab_dur / new_tot_dur) > 0.24:
                            continue
                        if new_fab_dur > max_fabric_ms:
                            continue
                        if new_tot_dur > max_ms and b_cands:
                            continue

                        # 叙事与卖点增益评分
                        new_sp = b_sp | set(item["cand"].get("selling_point_evidence_refs") or [])
                        sp_gain = (len(new_sp) - len(b_sp)) * 12.0
                        narrative_bonus = 5.0
                        if b_cands:
                            prev_role = b_cands[-1]["role"]
                            # 期望的叙事跃迁流转: HOOK -> PROOF/FABRIC -> FIT/STYLING
                            if prev_role == "HOOK" and item["role"] in ("PROOF", "FABRIC"):
                                narrative_bonus = 10.0
                            elif prev_role in ("PROOF", "FABRIC") and item["role"] in ("FIT", "STYLING"):
                                narrative_bonus = 8.0

                        new_score = b_score + item["composite"] + sp_gain + narrative_bonus
                        next_beams.append((
                            new_score,
                            b_cands + [item],
                            b_clusters | {item["cluster"]},
                            new_tot_dur,
                            new_fab_dur,
                            new_sp
                        ))
                        expanded = True

                    if not expanded and b_cands:
                        completed_beams.append((b_score, b_cands, b_clusters, b_dur, b_fab_dur, b_sp))

                if not next_beams:
                    break

                # 保留 Top-K 路径
                next_beams.sort(key=lambda x: x[0], reverse=True)
                beams = next_beams[:5]

            # 汇总并过滤达到最低时长要求的方案
            all_viable = [b for b in (completed_beams + beams) if b[3] >= min_ms]
            if not all_viable:
                # 没有任何候选组合能够满足 min_ms，丢弃该 style
                continue

            all_viable.sort(key=lambda x: x[0], reverse=True)
            best_beam = all_viable[0]
            chosen_cands = best_beam[1]
            cur_dur = best_beam[3]
            all_sp = best_beam[5]

            pid = f"p_{style}"
            rationale = [
                f"风格配置: {style}",
                f"优选片段数: {len(chosen_cands)}",
                f"叙事结构: {' → '.join(x['role'] for x in chosen_cands[:5])}",
            ]

            proposals.append({
                "proposal_id": pid,
                "style": style,
                "ordered_candidate_ids": [x["cand"]["candidate_id"] for x in chosen_cands],
                "expected_duration_ms": cur_dur,
                "selling_point_coverage": sorted(all_sp),
                "narrative_rationale": rationale,
                "risk_notes": [],
                "confidence": 0.92,
            })

        status = "PROPOSED" if proposals else "INSUFFICIENT_EVIDENCE"
        return {
            "schema_version": "3.0",
            "status": status,
            "proposals": proposals,
            "evidence_gap": [] if proposals else [
                f"所有合格候选组合均未能达到最低时长要求 ({min_ms/1000:.1f}s)",
                "可用本品素材不足或存在严重重叠"
            ],
        }
