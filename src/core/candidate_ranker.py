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
        strict: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """批量计算候选的多维得分并打上叙事角色与聚类标签。"""
        if strict is not None:
            is_strict_profile = bool(strict)
        else:
            is_strict_profile = (
                product_profile.get("quality_policy") == "strict" or
                product_profile.get("visual_verification") == "required" or
                product_profile.get("strict") is True
            )

        rel_map = {}
        if relation_decisions:
            for d in relation_decisions.get("decisions", []):
                rel_map[d["candidate_id"]] = d

        selling_points = product_profile.get("selling_points", [])
        sku_name = product_profile.get("name", "")
        scores_list = []

        # 重叠聚类辅助：按源和首个意群锚点（以及高相似口播台词）聚类，
        # 既防止同一发话点变体被重复入选，又避免链式重叠将整段视频全量锁死为一个聚类
        clusters: Dict[str, str] = {}
        for c in candidates:
            cid = c["candidate_id"]
            sid = str(c.get("source_id", "src_0"))
            uids = c.get("utterance_ids", [])
            first_uid = uids[0] if uids else str(c.get("source_start_ms", 0) // 2500)
            text_norm = re.sub(r"[^\w]", "", c.get("text", "")).strip()
            if text_norm and len(text_norm) >= 8:
                cl_key = f"cl_sem_{text_norm[:24]}"
            else:
                cl_key = f"cl_{sid}_{first_uid}"
            clusters[cid] = cl_key

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
                r".*抠衣.*", r".*扣[1一小].*", r".*加[鲜现]货.*", r".*一号[令练界链].*",
                r".*手批.*", r".*抢[毛大]衣.*", r".*先去抢.*", r".*不等了.*", r".*要.*的大宝.*",
            ]
            for pat in inventory_changeover_patterns:
                if re.search(pat, text):
                    eligible = False
                    rejection_reasons.append(f"命中叫卖/换款模式: '{pat}'")
                    break

            # 品类负向冲突硬过滤
            cat_name = str(product_profile.get("category", "") or product_profile.get("name", "")).lower()
            try:
                from src.tools.verify_clip_product_alignment import CATEGORY_NEGATIVE_TERMS, infer_product_category
                inferred_cat = infer_product_category(cat_name)
                # 排除本品自身标题中包含的词（如本品就是羽绒服时，"羽绒"不是负向词）
                neg_terms = [
                    t for t in CATEGORY_NEGATIVE_TERMS.get(inferred_cat, [])
                    if t not in cat_name
                ]
            except Exception:
                neg_terms = []

            # 搭配识别：如果是真正搭配语境（如"上衣搭西裤"），不作为负向冲突误杀，而是认定为合规搭配 (ACCESSORY_OR_STYLING)
            styling_patterns = (
                r"搭(配)?.*?(裤|裙|鞋|包|开衫|马甲|西裤|打底)",
                r"配.*?(裤|裙|鞋|包|开衫|马甲|西裤|打底)",
                r"脚上(穿|搭)", r"下半身.*?(搭|配)", r"里面(搭|穿)",
            )
            # 但若台词核心是在主推下装/换款（如"主推的裤子"、"裤子给大家安排"、"这条裤"），绝非合规搭配
            promotes_other_item = bool(re.search(r"(主推|拍|买|这款|这条|这件裤|给大家|安排|换下).*(裤|裙)", text))

            hits = [nt for nt in neg_terms if nt in text]
            has_styling_pattern = any(re.search(p, text) for p in styling_patterns)

            # CandidateRanker 只能降低或维持上游的身份可信度，严禁仅凭搭配话术将 UNCERTAIN 升级为已验证关系
            if promotes_other_item or relation == "OTHER_SKU":
                eligible = False
                relation = "OTHER_SKU"
                if promotes_other_item:
                    rejection_reasons.append("口播主推他款/换款，非合规搭配片段")
                else:
                    rejection_reasons.append("上游已判定为非本品款式 (OTHER_SKU)")
            elif relation in ("SAME_SKU", "SAME_SKU_VARIANT"):
                if hits:
                    if not promotes_other_item and has_styling_pattern:
                        relation = "ACCESSORY_OR_STYLING"
                    else:
                        eligible = False
                        relation = "OTHER_SKU"
                        rejection_reasons.append(f"品类负向冲突（非主推商品或属于换款/主推他款）: {hits}")
            elif relation == "ACCESSORY_OR_STYLING":
                # 上游已核实为合规搭配片段；只要不主动推销其他商品，即使文本包含下装词（如"西裤"）也不被品类负向词误杀
                # 严格模式下：搭配片段必须具备本品可见、主品身份已核实或其他明确关联证据；严禁缺失证据的搭配片段逃逸
                cand_gaps = (rel_info.get("evidence_gap") or []) + (cand.get("evidence_gap") or [])
                has_gap = any("UNVERIFIABLE" in str(g) or "视觉证据不足" in str(g) or "视觉冲突" in str(g) for g in cand_gaps)
                conf = float(rel_info.get("confidence") or cand.get("relation_confidence") or 0.85)
                if is_strict_profile and (has_gap or conf < 0.7):
                    eligible = False
                    relation = "UNCERTAIN"
                    rejection_reasons.append("严格模式下搭配片段缺失有效视觉证据核验")
            elif relation == "UNCERTAIN":
                # 严禁将 UNCERTAIN 升级为 ACCESSORY_OR_STYLING！
                if promotes_other_item:
                    eligible = False
                    relation = "OTHER_SKU"
                    rejection_reasons.append("口播主推他款/换款，非合规搭配片段")
                elif hits and not has_styling_pattern:
                    eligible = False
                    relation = "OTHER_SKU"
                    rejection_reasons.append(f"品类负向冲突且身份未核实: {hits}")
                else:
                    # 包含搭配话术但上游未核实身份，坚决维持 UNCERTAIN，严禁升级为已验证关系
                    relation = "UNCERTAIN"
                    if is_strict_profile:
                        eligible = False
                        rejection_reasons.append("严格模式下未通过身份核实 (UNCERTAIN)")

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
                "duplicate_cluster_id": clusters.get(cid, f"cluster_{cid}"),
                "evidence_refs": cand.get("selling_point_evidence_refs") or [],
                "rejection_reasons": rejection_reasons,
                "missing_evidence": missing_evidence,
            })

        return {
            "schema_version": "3.0",
            "scores": scores_list,
        }
