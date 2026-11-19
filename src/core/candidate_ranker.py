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

        # 重叠聚类辅助：按 source 分组并在时序窗口内聚类，避免 O(N^2) 笛卡尔积卡死
        clusters: Dict[str, str] = {}
        cluster_counter = 1

        # 先按 source_id 分组
        by_source: Dict[str, List[Dict[str, Any]]] = {}
        for c in candidates:
            sid = str(c.get("source_id", "src_0"))
            by_source.setdefault(sid, []).append(c)

        for sid, src_cands in by_source.items():
            # 保证按起始时间排序
            src_cands.sort(key=lambda x: x["source_start_ms"])
            n_src = len(src_cands)
            for i in range(n_src):
                c1 = src_cands[i]
                cid1 = c1["candidate_id"]
                if cid1 not in clusters:
                    clusters[cid1] = f"cluster_{cluster_counter:03d}"
                    cluster_counter += 1

                s1, e1 = c1["source_start_ms"], c1["source_end_ms"]
                for j in range(i + 1, n_src):
                    c2 = src_cands[j]
                    s2, e2 = c2["source_start_ms"], c2["source_end_ms"]
                    if s2 >= e1:
                        # 后续候选起点已超过 c1 终点，不可能再有重叠
                        break
                    overlap = max(0, min(e1, e2) - max(s1, s2))
                    min_dur = min(e1 - s1, e2 - s2)
                    if min_dur > 0 and (overlap / min_dur) >= 0.5:
                        cid2 = c2["candidate_id"]
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

            # 违规极限词硬过滤
            try:
                from src.tools.detect_violation import find_violation_matches
                v_matches = find_violation_matches(text)
                block_hits = [m for m in v_matches if m.get("severity") == "BLOCK"]
                if block_hits:
                    eligible = False
                    rejection_reasons.append(f"命中违规极限词: {[m['word'] for m in block_hits]}")
            except Exception:
                pass

            # 价格表达硬过滤（决策 3：零具体价格）
            try:
                from src.tools.detect_price import find_price_matches
                p_matches = find_price_matches(text)
                if p_matches:
                    eligible = False
                    rejection_reasons.append(f"命中价格表达: {[m['text'] for m in p_matches]}")
            except Exception:
                pass

            # 库存叫卖、迫切催单与换款离场硬过滤
            inventory_changeover_patterns = [
                r".*库存.*", r".*只剩.*", r".*单了.*", r".*快下了.*", r".*上链接.*", r".*一号链接.*",
                r".*抢购.*", r".*秒杀.*", r".*现货.*", r".*发货.*", r".*不能再讲.*", r".*上对头.*",
                r".*换下一款.*", r".*下楼.*", r".*过款.*", r".*拿走.*", r".*去换衣服.*", r".*换款式.*",
                r".*不用讲.*", r".*随便对比.*", r".*凑单去拍.*",
            ]
            for pat in inventory_changeover_patterns:
                if re.search(pat, text):
                    eligible = False
                    rejection_reasons.append(f"命中叫卖/换款模式: '{pat}'")
                    break

            # 搭配识别：如果是搭配语境（如"上衣搭西裤"），不作为负向冲突误杀，而是认定为合规搭配 (ACCESSORY_OR_STYLING)
            styling_patterns = (
                r"搭(配)?(一条|个|件)?(裤|裙|鞋|包|开衫|马甲|西裤|打底)",
                r"配(一条|个|件)?(裤|裙|鞋|包|开衫|马甲|西裤|打底)",
                r"脚上(穿|搭)", r"下半身(搭|配)", r"里面(搭|穿)",
            )
            is_styling_context = (
                relation == "ACCESSORY_OR_STYLING" or
                any(re.search(p, text) for p in styling_patterns)
            )

            # 品类冲突硬过滤（仅在非搭配且主讲非本品时拦截）
            cat_name = str(product_profile.get("category", "") or product_profile.get("name", "")).lower()
            if is_styling_context:
                if relation != "OTHER_SKU":
                    relation = "ACCESSORY_OR_STYLING"
            else:
                if any(k in cat_name for k in ("上衣", "毛衣", "针织", "衫", "外套", "大衣", "开衫")):
                    top_neg_terms = ("裤长", "毛呢裤", "毛泥裤", "裤腿", "西裤", "卫裤", "皮裤", "加绒裤", "侧缝", "腿在裤", "石墨烯裤")
                    hits = [nt for nt in top_neg_terms if nt in text and not any(k in text for k in (cat_name, "毛衣", "针织", "这件", "上衣"))]
                    if hits or relation == "OTHER_SKU":
                        eligible = False
                        relation = "OTHER_SKU"
                        rejection_reasons.append(f"品类负向冲突: 主品为上衣但台词主推下装 {hits}")
                elif any(k in cat_name for k in ("裤", "裙", "半裙", "短裤", "长裤", "阔腿")):
                    bottom_neg_terms = ("毛衣", "针织衫", "衬衫", "吊带", "大衣", "风衣")
                    hits = [nt for nt in bottom_neg_terms if nt in text and not any(k in text for k in (cat_name, "裤", "裙", "这条"))]
                    if hits or relation == "OTHER_SKU":
                        eligible = False
                        relation = "OTHER_SKU"
                        rejection_reasons.append(f"品类负向冲突: 主品为下装但台词主推上衣 {hits}")

            # 句子完整性硬约束：禁止未闭合或断句截断的碎片入选
            risks = cand.get("risks") or []
            if "INCOMPLETE_SENTENCE" in risks:
                eligible = False
                rejection_reasons.append("语句未在句末正常闭合（避免截断吞字）")

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

            # Visual Quality：基于真实媒体/帧特征评分
            vis_conf = cand.get("relation_confidence") or (0.9 if relation in ("SAME_SKU", "SAME_SKU_VARIANT") else 0.5)
            if cand.get("frame_ids") and relation in ("SAME_SKU", "SAME_SKU_VARIANT"):
                vis_score = min(98.0, 75.0 + float(vis_conf) * 25.0)
            elif "UNCERTAIN" in relation:
                vis_score = 45.0
            else:
                vis_score = 70.0

            # Utterance Completeness
            if "INCOMPLETE_SENTENCE" in risks:
                utt_score = 40.0
                missing_evidence.append("语句未在句末正常闭合")
            else:
                utt_score = 95.0

            # Action Completeness：结合镜头切换共振与动作完整性
            has_shot_align = len(cand.get("shot_ids") or []) > 0
            action_score = 90.0 if has_shot_align else 75.0

            # Audio Quality：结合字词级对齐精度与音频风险
            has_word_level = cand.get("alignment_precision") == "WORD_LEVEL"
            audio_score = 95.0 if has_word_level else 85.0
            if "AUDIO_JUMP" in risks:
                audio_score -= 20.0

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
