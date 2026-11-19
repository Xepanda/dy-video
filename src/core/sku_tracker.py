# -*- coding: utf-8 -*-
"""
sku_tracker.py - 商品关系追踪与视觉/文本多模态识别（V3 PR-04 核心）
===================================================================
负责：
  1. 结合文本指代、商品档案、换款事件与视觉特征，区分 5 类商品关系：
     - SAME_SKU：当前主推本品
     - SAME_SKU_VARIANT：本品允许的颜色/尺码变体
     - ACCESSORY_OR_STYLING：用于搭配的配件或非主推下装/饰品（如"上衣搭西裤"）
     - OTHER_SKU：主推其他款式或正在换款
     - UNCERTAIN：证据不足或存在严重冲突
  2. 划分连续的 sku_intervals（时间段级别 SKU 状态机），防止单帧噪点跳变；
  3. 输出符合 product_relation_decisions.schema.json 的决策字典；
  4. 支持 strict 模式：阻止 OTHER_SKU 与 UNCERTAIN 候选进入成片。
"""

import os
import sys
import re
import json
from typing import Dict, Any, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.vision_backend import VisionBackend, LocalCVVisionBackend

# 换款与离场关键词
CHANGEOVER_PATTERNS = [
    "换下一款", "看下一件", "下一套", "下一号", "换掉", "拿走", "去换衣服",
    "下一款衣服", "换件衣服", "过款", "换款式", "走开一下", "我们换", "下一件",
    "换下一个", "不能再讲这个了", "我们要开始上对头了", "下楼"
]

# 搭配词模式（修饰性指代，不应篡改主推品类）
STYLING_PATTERNS = [
    r"搭(配)?(一条|个|件)?(裤|裙|鞋|包|开衫|马甲|西裤|打底)",
    r"配(一条|个|件)?(裤|裙|鞋|包|开衫|马甲|西裤|打底)",
    r"脚上(穿|搭)", r"下半身(搭|配)", r"里面(搭|穿)",
]


class SkuTracker:
    """商品关系追踪器。"""

    def __init__(self, vision_backend: Optional[VisionBackend] = None):
        self.vision = vision_backend or LocalCVVisionBackend()

    def identify_relation(
        self,
        text: str,
        product_profile: Dict[str, Any],
        visual_decision: Optional[Dict[str, Any]] = None,
        strict_vision: bool = False,
    ) -> Tuple[str, float, List[str], List[str]]:
        """综合文本与视觉信号，判定与指定 SKU 的关系。

        返回: (relation, confidence, reasons, evidence_gap)
        """
        sku_name = product_profile.get("name", "")
        category = product_profile.get("category", "")
        allowed_variants = [str(v) for v in product_profile.get("allowed_variants", [])]
        reasons = []
        evidence_gap = []

        # 1. 换款硬排除检测
        for cp in CHANGEOVER_PATTERNS:
            if cp in text:
                reasons.append(f"命中换款关键词: '{cp}'")
                return "OTHER_SKU", 0.95, reasons, evidence_gap

        # 2. 搭配物识别
        is_styling = False
        for pat in STYLING_PATTERNS:
            if re.search(pat, text):
                is_styling = True
                reasons.append(f"命中搭配话术模式: '{pat}'")
                break

        # 3. 变体识别
        matched_variant = None
        for var in allowed_variants:
            if var and var in text:
                matched_variant = var
                reasons.append(f"命中允许展示的颜色/变体: '{var}'")
                break

        # 4. 主品词与核心卖点命中
        has_primary_token = False
        selling_points = [str(sp) for sp in product_profile.get("selling_points", [])]
        highlight_keywords = [str(kw) for kw in product_profile.get("highlight_keywords", [])]

        primary_tokens = set()
        for raw in [sku_name, category] + selling_points + highlight_keywords:
            if not raw:
                continue
            for part in re.split(r"[\s,，、/·\-_]+", str(raw)):
                if len(part) >= 2:
                    primary_tokens.add(part)
                if len(part) > 2:
                    for n in (2, 3):
                        for i in range(len(part) - n + 1):
                            primary_tokens.add(part[i:i+n])

        # 品类与行业核心词扩展（直播带货自然口播指代）
        cat_lower = (category or "").lower()
        if any(k in cat_lower for k in ("针织", "毛衣", "衫", "上衣")):
            primary_tokens.update(["毛衣", "针织", "大毛衫", "线衫", "羊毛衫", "开衫", "打底", "上衣", "纱线", "面料", "手感", "版型", "领形", "领口", "正肩", "落肩"])
        elif any(k in cat_lower for k in ("裤", "阔腿", "西裤")):
            primary_tokens.update(["裤子", "西裤", "裤腿", "阔腿裤", "垂坠", "腰头"])
        elif any(k in cat_lower for k in ("裙", "连衣裙")):
            primary_tokens.update(["裙子", "连衣裙", "裙摆", "下摆", "腰线"])

        primary_tokens.discard("这个")
        primary_tokens.discard("一个")

        for tok in primary_tokens:
            if tok in text:
                has_primary_token = True
                reasons.append(f"命中商品关键词: '{tok}'")
                break

        # 5. 结合视觉后端
        vis_relation = visual_decision.get("sku_relation") if visual_decision else None
        vis_available = visual_decision.get("vision_available", False) if visual_decision else False
        vis_conf = visual_decision.get("confidence", 0.5) if visual_decision else 0.5

        if visual_decision and visual_decision.get("uncertainties"):
            evidence_gap.extend(visual_decision["uncertainties"])

        is_strict = (
            strict_vision or
            product_profile.get("quality_policy") == "strict" or
            product_profile.get("visual_verification") == "required" or
            product_profile.get("strict") is True
        )

        # 6. 综合裁决状态机
        if is_styling:
            # 搭配镜头
            return "ACCESSORY_OR_STYLING", 0.85, reasons, evidence_gap

        if matched_variant:
            return "SAME_SKU_VARIANT", 0.90, reasons, evidence_gap

        if has_primary_token:
            if vis_available and vis_relation in ("SAME_SKU", "SAME_SKU_VARIANT"):
                reasons.append("文本主推词与视觉特征高度双重吻合")
                return "SAME_SKU", max(0.9, vis_conf), reasons, evidence_gap
            elif vis_available and vis_relation == "OTHER_SKU":
                # 画面与口播冲突
                reasons.append("口播提到本品但视觉判定为其他商品")
                evidence_gap.append("音画商品不一致")
                return "UNCERTAIN", 0.45, reasons, evidence_gap
            elif not vis_available:
                # 缺失候选帧或视觉识别失败：严格模式下绝不直接认定 SAME_SKU
                if is_strict:
                    reasons.append("文本命中关键词，但严格模式下缺失有效视觉证据")
                    evidence_gap.append("UNVERIFIABLE: 缺失候选视频帧或未通过视觉比对")
                    return "UNCERTAIN", 0.40, reasons, evidence_gap
                else:
                    reasons.append("文本命中主推商品（未接通视觉验证）")
                    evidence_gap.append("缺少已验证视觉证据")
                    return "SAME_SKU", 0.65, reasons, evidence_gap
            else:
                # vis_available is True but vis_relation is UNCERTAIN
                if is_strict:
                    reasons.append("文本命中商品词，但视觉相似度偏低")
                    evidence_gap.append("视觉证据不足以确认为同款")
                    return "UNCERTAIN", 0.45, reasons, evidence_gap
                else:
                    reasons.append("文本命中主推商品")
                    return "SAME_SKU", 0.70, reasons, evidence_gap

        # 无明显文本关键词时查看视觉
        if vis_available:
            if vis_relation == "SAME_SKU" and vis_conf >= 0.7:
                reasons.append("视觉匹配商品参考图")
                return "SAME_SKU", vis_conf, reasons, evidence_gap
            elif vis_relation == "SAME_SKU_VARIANT":
                reasons.append("视觉特征疑似本品变体")
                return "SAME_SKU_VARIANT", vis_conf, reasons, evidence_gap

        evidence_gap.append("缺少本品文本关键词或确切视觉证据")
        return "UNCERTAIN", 0.3, reasons, evidence_gap

    def track_candidates(
        self,
        candidates: List[Dict[str, Any]],
        product_profile: Dict[str, Any],
        frame_provider: Optional[Any] = None,
        utterances_map: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """批量对候选进行商品关系判定，返回结构符合 product_relation_decisions.schema.json 的字典。"""
        decisions = []
        for cand in candidates:
            cid = cand.get("candidate_id", "")
            text = cand.get("text") or cand.get("desc") or ""
            if not text and utterances_map and cand.get("utterance_ids"):
                text = "".join(utterances_map[uid].get("text", "") for uid in cand["utterance_ids"] if uid in utterances_map)
            if not text:
                text = " ".join(cand.get("selling_point_evidence_refs") or [])

            frame_paths = []
            if frame_provider and hasattr(frame_provider, "get_frames_for_candidate"):
                frame_paths = frame_provider.get_frames_for_candidate(cand)

            vis_dec = self.vision.analyze_candidate(cand, frame_paths, product_profile)
            rel, conf, reasons, gap = self.identify_relation(text, product_profile, visual_decision=vis_dec)
            cand["product_relation"] = rel
            cand["relation_confidence"] = round(conf, 2)
            if frame_paths:
                cand.setdefault("frame_ids", []).extend([os.path.basename(p) for p in frame_paths])

            decisions.append({
                "candidate_id": cid,
                "relation": rel,
                "confidence": round(conf, 2),
                "evidence_refs": cand.get("selling_point_evidence_refs", []) + (vis_dec.get("visual_evidence_refs") or []),
                "text_span_ids": cand.get("utterance_ids", []),
                "reasons": reasons or ["常规时段评估"],
                "evidence_gap": gap,
            })

        return {
            "schema_version": "3.0",
            "decisions": decisions,
        }

    def build_sku_intervals(
        self,
        decisions: List[Dict[str, Any]],
        candidates_map: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """将相邻具有相同 SKU 关系的候选片段合并为稳定的 sku_intervals。"""
        intervals = []
        cur_int = None

        for dec in decisions:
            cid = dec["candidate_id"]
            cand = candidates_map.get(cid)
            if not cand:
                continue

            rel = dec["relation"]
            sid = cand["source_id"]
            st = cand["source_start_ms"]
            en = cand["source_end_ms"]

            if cur_int and cur_int["source_id"] == sid and cur_int["relation"] == rel and st <= cur_int["end_ms"] + 1000:
                # 扩展当前区间
                cur_int["end_ms"] = max(cur_int["end_ms"], en)
                cur_int["candidate_ids"].append(cid)
            else:
                if cur_int:
                    intervals.append(cur_int)
                cur_int = {
                    "interval_id": f"int_{len(intervals)+1:03d}",
                    "source_id": sid,
                    "start_ms": st,
                    "end_ms": en,
                    "relation": rel,
                    "confidence": dec["confidence"],
                    "candidate_ids": [cid],
                }

        if cur_int:
            intervals.append(cur_int)

        return intervals
