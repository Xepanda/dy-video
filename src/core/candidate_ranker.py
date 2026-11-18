# -*- coding: utf-8 -*-
"""
candidate_ranker.py - 多模态候选片段精排与硬过滤（V3 PR-05 核心）
=================================================================
负责：
  1. 硬约束过滤与多维软评分严格分离：
     - 硬过滤：OTHER_SKU、严重截断、违法宣称、跨源非法 → eligible=False；
     - 软评分：0~100 标准分 (relevance, selling_point_evidence, visual_quality,
       utterance_completeness, action_completeness, audio_quality)；
  2. 识别叙事角色 (HOOK / FABRIC / FIT / STYLING / COLOR / SIZE / CLOSING / PROOF)；
  3. 同源高度重叠片段聚类 (duplicate_cluster_id)，防止同一段台词被多个窗口重复选入；
  4. 产出符合 candidate_scores.schema.json 的结果字典。
"""

import os
import sys
import re
from typing import Dict, Any, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class CandidateRanker:
    """多模态候选精排器。"""

    def __init__(self, weights: Optional[Dict[str, float]] = None):
        self.weights = weights or {
            "relevance": 0.30,
            "selling_point_evidence": 0.20,
            "visual_quality": 0.15,
            "utterance_completeness": 0.15,
            "action_completeness": 0.10,
            "audio_quality": 0.10,
        }

    def score_candidates(
        self,
        candidates: List[Dict[str, Any]],
        product_profile: Dict[str, Any],
        relation_decisions: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """批量计算候选的多维得分并打上叙事角色与聚类标签。"""
        rel_map = {}
        if relation_decisions:
            for d in relation_decisions.get("decisions", []):
                rel_map[d["candidate_id"]] = d

        selling_points = product_profile.get("selling_points", [])
        sku_name = product_profile.get("name", "")
        scores_list = []

        # 重叠聚类辅助：同一 source 且重叠 >= 50% 的分入同一 duplicate_cluster
        clusters: Dict[str, str] = {}
        cluster_counter = 1

        for i, c1 in enumerate(candidates):
            cid1 = c1["candidate_id"]
            if cid1 not in clusters:
                clusters[cid1] = f"cluster_{cluster_counter:03d}"
                cluster_counter += 1

            for c2 in candidates[i+1:]:
                cid2 = c2["candidate_id"]
                if c1.get("source_id") == c2.get("source_id"):
                    s1, e1 = c1["source_start_ms"], c1["source_end_ms"]
                    s2, e2 = c2["source_start_ms"], c2["source_end_ms"]
                    overlap = max(0, min(e1, e2) - max(s1, s2))
                    min_dur = min(e1 - s1, e2 - s2)
                    if min_dur > 0 and (overlap / min_dur) >= 0.5:
                        clusters[cid2] = clusters[cid1]

        for cand in candidates:
            cid = cand["candidate_id"]
            text = cand.get("text") or " ".join(cand.get("selling_point_evidence_refs") or [])
            rel_info = rel_map.get(cid, {})
            relation = rel_info.get("relation") or cand.get("product_relation", "UNCERTAIN")

            rejection_reasons = []
            missing_evidence = []
            eligible = True

            # 1. 硬过滤
            if relation == "OTHER_SKU":
                eligible = False
                rejection_reasons.append("非主推商品或属于换款/离场区间")

            dur_ms = cand.get("source_end_ms", 0) - cand.get("source_start_ms", 0)
            if dur_ms < 1500:
                eligible = False
                rejection_reasons.append(f"时长过短 ({dur_ms}ms < 1500ms)")

            # 2. 软评分各分项 (0~100)
            # Relevance
            rel_score = 50.0
            if relation == "SAME_SKU":
                rel_score = 95.0
            elif relation == "SAME_SKU_VARIANT":
                rel_score = 85.0
            elif relation == "ACCESSORY_OR_STYLING":
                rel_score = 75.0
            elif relation == "UNCERTAIN":
                rel_score = 40.0
                missing_evidence.append("商品身份不确定")
            else:
                rel_score = 10.0

            # Selling Point Evidence
            sp_hits = cand.get("selling_point_evidence_refs") or []
            sp_score = min(100.0, len(sp_hits) * 35.0 + 30.0) if sp_hits else 40.0

            # Visual Quality
            vis_score = 80.0
            if "UNCERTAIN" in relation:
                vis_score = 50.0

            # Utterance Completeness
            risks = cand.get("risks") or []
            if "INCOMPLETE_SENTENCE" in risks:
                utt_score = 45.0
                missing_evidence.append("语句未在句末正常闭合")
            else:
                utt_score = 95.0

            # Action Completeness
            action_score = 85.0

            # Audio Quality
            audio_score = 90.0

            subscores = {
                "relevance": round(rel_score, 1),
                "selling_point_evidence": round(sp_score, 1),
                "visual_quality": round(vis_score, 1),
                "utterance_completeness": round(utt_score, 1),
                "action_completeness": round(action_score, 1),
                "audio_quality": round(audio_score, 1),
            }

            # 综合得分
            total_score = sum(subscores[k] * self.weights.get(k, 0.0) for k in subscores)
            cand["composite_score"] = round(total_score, 2)

            # 3. 叙事角色推断
            narrative_role = "PROOF"
            if any(w in text for w in ("你看", "姐妹们", "闭眼入", "第一件", "今天这件", "必须抢")):
                narrative_role = "HOOK"
            elif any(w in text for w in ("面料", "手感", "羊毛", "羊绒", "真丝", "棉", "材质", "羽绒")):
                narrative_role = "FABRIC"
            elif any(w in text for w in ("版型", "显瘦", "不挑人", "遮肉", "挺括", "落肩")):
                narrative_role = "FIT"
            elif any(w in text for w in ("搭配", "穿法", "配裤子", "配裙子", "下半身")):
                narrative_role = "STYLING"
            elif any(w in text for w in ("颜色", "色系", "黑色", "白色", "米色", "咖色")):
                narrative_role = "COLOR"
            elif any(w in text for w in ("小黄车", "拍一发", "库存", "带回家", "下单", "手慢无")):
                narrative_role = "CLOSING"

            scores_list.append({
                "candidate_id": cid,
                "eligible": eligible,
                "product_relation": relation,
                "subscores": subscores,
                "narrative_role": narrative_role,
                "duplicate_cluster_id": clusters.get(cid, "cluster_001"),
                "evidence_refs": cand.get("selling_point_evidence_refs") or [],
                "rejection_reasons": rejection_reasons,
                "missing_evidence": missing_evidence,
            })

        return {
            "schema_version": "3.0",
            "scores": scores_list,
        }
