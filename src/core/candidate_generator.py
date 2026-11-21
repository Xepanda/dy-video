# -*- coding: utf-8 -*-
"""
candidate_generator.py - 高召回候选片段与多模态证据生成（V3 PR-03 核心）
=====================================================================
负责：
  1. 结合完整语义单元 (Utterances) 与镜头边界 (Shots) 召回 2~12s 候选窗口；
  2. 输出符合 candidate_v3.schema.json 的结构化候选对象；
  3. 保留高召回，显式记录 risks（如 INCOMPLETE_SENTENCE, SHORT_DURATION 等）
     与 rejection_reasons，不强行为了凑数硬过；
  4. 关联卖点证据引用 (selling_point_evidence_refs) 与帧引用 (frame_ids)。
"""

import os
import sys
import json
import re
from typing import Dict, Any, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.evidence_store import EvidenceStore


class CandidateGenerator:
    """从语义单元与镜头边界中挖掘高质量带货候选段。"""

    def __init__(
        self,
        min_candidate_ms: int = 1800,
        max_candidate_ms: int = 12000,
        target_candidate_ms: int = 5000,
    ):
        self.min_candidate_ms = min_candidate_ms
        self.max_candidate_ms = max_candidate_ms
        self.target_candidate_ms = target_candidate_ms

    def generate_candidates(
        self,
        utterances: List[Dict[str, Any]],
        product_profile: Dict[str, Any],
        source_id: str = "src_0",
        source_sha256: str = "",
        shots: Optional[List[Dict[str, Any]]] = None,
        source_file: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """基于 utterances 和可选的 shots 生成 candidates 列表。"""
        candidates: List[Dict[str, Any]] = []
        if not utterances:
            return []

        # 确保 source_id 不含路径分隔符或非法文件名字符
        clean_source_id = str(source_id).strip()
        if "\\" in clean_source_id or "/" in clean_source_id or ":" in clean_source_id:
            stem = os.path.splitext(os.path.basename(clean_source_id))[0]
            clean_source_id = re.sub(r"[^\w\-]", "_", stem).strip("_") or "src_0"

        selling_points = product_profile.get("selling_points", [])
        sku_name = product_profile.get("name", "")

        # 1. 单语句候选与多语句滑动组合
        n_utt = len(utterances)
        cand_idx = 1

        for i in range(n_utt):
            cur_utts = []
            cur_text = ""
            start_ms = utterances[i]["start_ms"]
            
            for j in range(i, n_utt):
                u = utterances[j]
                dur_ms = u["end_ms"] - start_ms
                if dur_ms > self.max_candidate_ms and cur_utts:
                    break

                cur_utts.append(u)
                cur_text += u["text"]
                end_ms = u["end_ms"]
                candidate_dur = end_ms - start_ms

                # 判断该区间是否构成一个合理候选
                if candidate_dur >= self.min_candidate_ms:
                    # 匹配卖点证据
                    sp_refs = []
                    for sp in selling_points:
                        if sp and sp in cur_text:
                            sp_refs.append(sp)

                    risks = []
                    # 语言完整性检查
                    if not cur_utts[-1].get("ends_sentence", False):
                        risks.append("INCOMPLETE_SENTENCE")
                    if candidate_dur < 2500:
                        risks.append("SHORT_DURATION")

                    # 初步商品相关性
                    has_product_signal = (
                        (sku_name and any(token in cur_text for token in sku_name.split() if len(token) >= 2)) or
                        len(sp_refs) > 0 or
                        any(c in cur_utts[0].get("categories", []) for c in ("fabric", "hook", "craft_detail", "fit", "styling"))
                    )
                    product_relation = "SAME_SKU" if has_product_signal else "UNCERTAIN"

                    # 镜头边界对齐与关联 (PR-01 & PR-03)
                    matched_shot_ids = []
                    eff_end_ms = end_ms
                    eff_start_ms = start_ms
                    if shots:
                        for s in shots:
                            if isinstance(s, dict):
                                s_st = s.get("start_ms") if "start_ms" in s else int(round(float(s.get("start", 0.0)) * 1000))
                                s_en = s.get("end_ms") if "end_ms" in s else int(round(float(s.get("end", 0.0)) * 1000))
                                sid = s.get("shot_id") or f"shot_{s_st}_{s_en}"
                                if max(start_ms, s_st) < min(end_ms, s_en):
                                    matched_shot_ids.append(sid)
                                # 若镜头切换点与句末接近且不会截断语句，对齐到镜头边界
                                if s_en >= end_ms and (s_en - end_ms) <= 250 and cur_utts[-1].get("ends_sentence", False):
                                    eff_end_ms = s_en
                            elif isinstance(s, (int, float)):
                                cut_ms = int(round(float(s) * 1000)) if s < 10000 else int(s)
                                if cut_ms >= end_ms and (cut_ms - end_ms) <= 250 and cur_utts[-1].get("ends_sentence", False):
                                    eff_end_ms = cut_ms

                    # 关联帧索引（中点帧）
                    mid_ms = (eff_start_ms + eff_end_ms) // 2
                    frame_id = f"f_{clean_source_id}_{mid_ms}"

                    cid = f"c_{clean_source_id}_{cand_idx:03d}"
                    cand = {
                        "schema_version": "3.0",
                        "candidate_id": cid,
                        "source_id": clean_source_id,
                        "source_file": str(source_file or source_id),
                        "source_start_ms": eff_start_ms,
                        "source_end_ms": eff_end_ms,
                        "product_relation": product_relation,
                        "text": cur_text,
                        "utterance_ids": [x["utterance_id"] for x in cur_utts],
                        "frame_ids": [frame_id],
                        "selling_point_evidence_refs": sp_refs,
                        "shot_ids": matched_shot_ids,
                        "eligible": len(risks) == 0 or (len(risks) == 1 and risks[0] == "SHORT_DURATION"),
                        "risks": risks,
                        "source_file_sha256": source_sha256,
                    }
                    candidates.append(cand)
                    cand_idx += 1

                # 超过目标时长后如果已是句末，停止向后扩展
                if candidate_dur >= self.target_candidate_ms and u.get("ends_sentence"):
                    break

        return candidates
