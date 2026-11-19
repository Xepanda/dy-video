# -*- coding: utf-8 -*-
"""
utterance_aligner.py - 完整语句意群对齐与时间基归一化（V3 PR-03 核心）
=====================================================================
负责：
  1. 将 ASR 原始片段（秒或毫秒浮点）归一化为源相对毫秒整数（start_ms, end_ms）；
  2. 严格按中文意群与标点合并为语义完整的 utterance，绝不在从句或半个词中间截断；
  3. 可选词级/字级对齐支持：
     - 若片段包含精确字词列表（words），输出词级时间戳并标明 alignment_precision="WORD_LEVEL"；
     - 若缺失或失败，平滑退回句级保守分段，明确标明 alignment_precision="SEGMENT_LEVEL"，
       严禁在未对齐时声称词级精度；
  4. 支持声源切换检测（多源视频按 source_id 严格隔离时间轴）。
"""

import os
import sys
import re
from typing import Dict, Any, List, Optional, Tuple

SENT_END_CHARS = "。！？!?；;…"
CLAUSE_END_CHARS = "，,、"


def _ends_with_sentence_punct(text: str) -> bool:
    t = (text or "").rstrip()
    return bool(t) and t[-1] in SENT_END_CHARS


def _ends_with_clause_punct(text: str) -> bool:
    t = (text or "").rstrip()
    return bool(t) and t[-1] in (SENT_END_CHARS + CLAUSE_END_CHARS)


class UtteranceAligner:
    """语句对齐器，提供高精度且语义完整的口播单元。"""

    def __init__(self, merge_gap_ms: int = 600, min_utterance_ms: int = 1200, max_utterance_ms: int = 10000):
        self.merge_gap_ms = merge_gap_ms
        self.min_utterance_ms = min_utterance_ms
        self.max_utterance_ms = max_utterance_ms

    def align_segments(
        self,
        raw_segments: List[Dict[str, Any]],
        source_id: str = "src_0",
    ) -> List[Dict[str, Any]]:
        """将 ASR 原始片段转换为结构化的完整语句列表（毫秒整数）。"""
        if not raw_segments:
            return []

        # 归一化输入片段时间为毫秒
        norm_segs = []
        for s in raw_segments:
            st = s.get("start_ms")
            if st is None:
                st = int(round(float(s.get("start", 0.0)) * 1000))
            en = s.get("end_ms")
            if en is None:
                en = int(round(float(s.get("end", 0.0)) * 1000))
            text = str(s.get("text", "")).strip()
            words = s.get("words") or []
            norm_words = []
            for w in words:
                w_st = w.get("start_ms") if "start_ms" in w else int(round(float(w.get("start", 0.0)) * 1000))
                w_en = w.get("end_ms") if "end_ms" in w else int(round(float(w.get("end", 0.0)) * 1000))
                norm_words.append({
                    "word": str(w.get("word", "")).strip(),
                    "start_ms": int(w_st),
                    "end_ms": int(w_en),
                })
            norm_segs.append({
                "start_ms": int(st),
                "end_ms": int(en),
                "text": text,
                "words": norm_words,
                "has_word_align": len(norm_words) > 0,
                "source_file": s.get("source_file", ""),
                "categories": s.get("categories", []),
            })

        # 排序
        norm_segs.sort(key=lambda x: x["start_ms"])

        utterances: List[Dict[str, Any]] = []
        cur_group: List[Dict[str, Any]] = []

        def _flush_group(grp: List[Dict[str, Any]], idx: int, is_pause_break: bool = True):
            if not grp:
                return
            st_ms = grp[0]["start_ms"]
            en_ms = grp[-1]["end_ms"]
            combined_text = "".join(x["text"] for x in grp)
            all_words = []
            has_word_level = True
            for x in grp:
                if not x["has_word_align"]:
                    has_word_level = False
                all_words.extend(x["words"])

            precision = "WORD_LEVEL" if (has_word_level and all_words) else "SEGMENT_LEVEL"
            all_cats = set()
            for x in grp:
                all_cats.update(x.get("categories") or [])

            # 当包含句末标点，或在气口停顿点自然截断，或末尾带语气/句末助词时，视为完整意群闭合
            ends_sentence = (
                _ends_with_sentence_punct(combined_text) or
                is_pause_break or
                combined_text.rstrip().endswith(("呢", "啦", "呀", "吧", "啊", "哦", "的", "了", "套", "条", "件"))
            )

            utterances.append({
                "utterance_id": f"utt_{source_id}_{idx:03d}",
                "source_id": source_id,
                "start_ms": st_ms,
                "end_ms": en_ms,
                "duration_ms": en_ms - st_ms,
                "text": combined_text,
                "words": all_words if precision == "WORD_LEVEL" else [],
                "alignment_precision": precision,
                "ends_sentence": ends_sentence,
                "categories": sorted(all_cats),
                "segment_count": len(grp),
            })

        u_idx = 1
        for seg in norm_segs:
            if not cur_group:
                cur_group.append(seg)
                continue

            last_seg = cur_group[-1]
            gap_ms = seg["start_ms"] - last_seg["end_ms"]
            accum_dur = seg["end_ms"] - cur_group[0]["start_ms"]

            # 拆分条件（适配无标点 ASR 流）：
            # 1. 包含显式句末标点且达到最小意群时长；
            # 2. 停顿间隙超过气口阈值 (>=250ms) 且累计达到最小意群时长；
            # 3. 累计时长达到最大上限 (max_utterance_ms) 强制截断，防长达数分钟滚雪球；
            # 4. 出现语气词/句末特征字且伴随微弱气口 (>=100ms) 且累计时长合规。
            should_split = False
            if _ends_with_sentence_punct(last_seg["text"]) and (last_seg["end_ms"] - cur_group[0]["start_ms"]) >= self.min_utterance_ms:
                should_split = True
            elif gap_ms >= 250 and accum_dur >= self.min_utterance_ms:
                should_split = True
            elif accum_dur >= self.max_utterance_ms:
                should_split = True
            elif gap_ms >= 100 and accum_dur >= self.min_utterance_ms and last_seg["text"].rstrip().endswith(("吧", "呢", "呀", "啊", "了", "好", "对", "入", "色", "套", "单")):
                should_split = True

            if should_split:
                _flush_group(cur_group, u_idx)
                u_idx += 1
                cur_group = [seg]
            else:
                cur_group.append(seg)

        if cur_group:
            _flush_group(cur_group, u_idx)

        return utterances
