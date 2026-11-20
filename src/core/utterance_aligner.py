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

        def _flush_group(grp: List[Dict[str, Any]], idx: int, is_pause_break: bool = False):
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

            # 句子完整性判定（绝非默认 True）：
            # 1. 显式从句标点（逗号/顿号）：必然是半句未闭合
            if _ends_with_clause_punct(combined_text) and not _ends_with_sentence_punct(combined_text):
                ends_sentence = False
            # 2. 显式句末标点（句号/问号/感叹号/分号/省略号）：完整闭合
            elif _ends_with_sentence_punct(combined_text):
                ends_sentence = True
            # 3. 伴随真实气口停顿且非未完从句连接词：判定为自然完整句
            elif is_pause_break:
                clean_t = combined_text.rstrip()
                incomplete_tails = (
                    "因为", "所以", "但是", "如果", "虽然", "而且", "不仅",
                    "并且", "以及", "采用", "采用的是", "比如", "这个", "那个", "这件",
                    "这是", "它是", "就是", "然后", "还有", "和", "跟", "与", "或", "或者",
                    "稍", "更", "越", "比较", "非常", "十分", "极其", "特别", "一件",
                )
                if any(clean_t.endswith(tail) for tail in incomplete_tails) or _ends_with_clause_punct(clean_t):
                    ends_sentence = False
                else:
                    ends_sentence = True
            else:
                clean_t = combined_text.rstrip()
                if clean_t.endswith(("呢", "啦", "呀", "吧", "啊", "哦", "的", "了", "套", "条", "件", "好", "对", "入", "色", "单")):
                    ends_sentence = True
                else:
                    ends_sentence = False

            risks = [] if ends_sentence else ["INCOMPLETE_SENTENCE"]

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
                "risks": risks,
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
            # 3. 达到最小意群时长且当前段末尾无从句标点/挂起连词（自然停顿分界）；
            # 4. 累计时长达到最大上限 (max_utterance_ms) 强制截断，防长达数分钟滚雪球；
            # 5. 出现语气词/句末特征字且伴随微弱气口 (>=100ms) 且累计时长合规。
            should_split = False
            split_pause = False
            last_text = last_seg["text"].rstrip()
            is_dangling = any(last_text.endswith(t) for t in (
                "因为", "所以", "但是", "如果", "虽然", "而且", "不仅",
                "并且", "以及", "采用", "采用的是", "比如", "这个", "那个", "这件",
                "这是", "它是", "就是", "然后", "还有", "和", "跟", "与", "或", "或者", "稍"
            )) or _ends_with_clause_punct(last_text)

            if _ends_with_sentence_punct(last_text) and (last_seg["end_ms"] - cur_group[0]["start_ms"]) >= self.min_utterance_ms:
                should_split = True
                split_pause = True
            elif gap_ms >= 250 and accum_dur >= self.min_utterance_ms:
                should_split = True
                split_pause = True
            elif accum_dur >= self.min_utterance_ms and not is_dangling:
                should_split = True
                split_pause = True
            elif accum_dur >= self.max_utterance_ms:
                should_split = True
                split_pause = not is_dangling
            elif gap_ms >= 100 and accum_dur >= self.min_utterance_ms and last_text.endswith(("吧", "呢", "呀", "啊", "了", "好", "对", "入", "色", "套", "单")):
                should_split = True
                split_pause = True

            if should_split:
                _flush_group(cur_group, u_idx, is_pause_break=split_pause)
                u_idx += 1
                cur_group = [seg]
            else:
                cur_group.append(seg)

        if cur_group:
            # 尾段检查是否以句末标点或自然停顿闭合
            last_text = cur_group[-1].get("text", "")
            tail_dangling = any(last_text.rstrip().endswith(t) for t in (
                "因为", "所以", "但是", "如果", "虽然", "而且", "不仅",
                "并且", "以及", "采用", "采用的是", "比如", "这个", "那个", "这件",
                "这是", "它是", "就是", "然后", "还有", "和", "跟", "与", "或", "或者", "稍"
            )) or _ends_with_clause_punct(last_text)
            end_pause = _ends_with_sentence_punct(last_text) or not tail_dangling
            _flush_group(cur_group, u_idx, is_pause_break=end_pause)

        return utterances
