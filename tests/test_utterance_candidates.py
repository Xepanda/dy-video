# -*- coding: utf-8 -*-
"""
test_utterance_candidates.py - PR-03 完整语句与证据候选层测试
"""

import os
import sys
import json
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.core.evidence_store import EvidenceStore
from src.core.utterance_aligner import UtteranceAligner
from src.core.candidate_generator import CandidateGenerator


def test_chinese_utterance_grouping_no_truncation():
    """中文意群不截断：从句标点（，）在未达时长时不打断，句末标点（。）才安全结束。"""
    raw_segs = [
        {"start": 0.0, "end": 1.5, "text": "这件羽绒服面料，"},
        {"start": 1.6, "end": 3.2, "text": "采用的是高品质白鸭绒。"},
        {"start": 3.8, "end": 5.5, "text": "穿在身上非常轻盈保暖。"}
    ]
    aligner = UtteranceAligner(merge_gap_ms=800, min_utterance_ms=1500)
    utts = aligner.align_segments(raw_segs, source_id="src_0")

    # 第一段以从句逗号结束，不应单独成句，应与第二段合并为完整意群
    assert len(utts) >= 1
    assert "这件羽绒服面料" in utts[0]["text"]
    assert "白鸭绒" in utts[0]["text"]
    assert utts[0]["ends_sentence"] is True
    assert utts[0]["start_ms"] == 0
    assert utts[0]["end_ms"] == 3200


def test_word_level_alignment_and_fallback():
    """字词对齐模式：有词级数据时标 WORD_LEVEL，缺失时安全退回 SEGMENT_LEVEL。"""
    aligner = UtteranceAligner()

    # 带词级对齐的输入
    segs_with_words = [
        {
            "start": 0.0, "end": 2.0, "text": "闭眼入。",
            "words": [
                {"word": "闭", "start": 0.0, "end": 0.5},
                {"word": "眼", "start": 0.5, "end": 1.0},
                {"word": "入", "start": 1.0, "end": 2.0},
            ]
        }
    ]
    utts_word = aligner.align_segments(segs_with_words, source_id="src_0")
    assert utts_word[0]["alignment_precision"] == "WORD_LEVEL"
    assert len(utts_word[0]["words"]) == 3
    assert utts_word[0]["words"][0]["start_ms"] == 0

    # 无词级对齐的输入
    segs_no_words = [
        {"start": 0.0, "end": 2.0, "text": "闭眼入。"}
    ]
    utts_seg = aligner.align_segments(segs_no_words, source_id="src_0")
    assert utts_seg[0]["alignment_precision"] == "SEGMENT_LEVEL"
    assert utts_seg[0]["words"] == []


def test_evidence_store_and_source_isolation():
    """证据中心与多源时间轴隔离。"""
    store = EvidenceStore(project_id="test_proj")
    sid_a = store.register_source(__file__, source_id="src_a", duration_sec=10.0)
    sid_b = store.register_source(__file__, source_id="src_b", duration_sec=20.0)

    assert sid_a == "src_a"
    assert sid_b == "src_b"
    assert store.sources["src_a"]["sha256"] != ""

    store.add_utterance("utt_1", "src_a", start_ms=0, end_ms=3000, text="A源台词。")
    store.add_utterance("utt_2", "src_b", start_ms=0, end_ms=4000, text="B源台词。")

    assert len(store.utterances) == 2
    assert store.utterances[0]["source_id"] == "src_a"
    assert store.utterances[1]["source_id"] == "src_b"


def test_candidate_generation_schema_conformity():
    """候选生成严格符合 candidate_v3.schema.json。"""
    raw_segs = [
        {"start": 0.0, "end": 2.5, "text": "这件大衣不仅显瘦，"},
        {"start": 2.6, "end": 5.0, "text": "而且版型非常挺括舒适。"},
        {"start": 5.5, "end": 8.0, "text": "姐妹们看一下走线细节。"}
    ]
    aligner = UtteranceAligner()
    utts = aligner.align_segments(raw_segs, source_id="src_demo")

    prod_profile = {
        "sku_id": "SKU-COAT",
        "name": "羊毛大衣",
        "category": "大衣",
        "selling_points": ["显瘦", "挺括", "保暖"]
    }

    generator = CandidateGenerator(min_candidate_ms=2000, max_candidate_ms=9000)
    cands = generator.generate_candidates(utts, prod_profile, source_id="src_demo", source_sha256="abc12345")

    assert len(cands) > 0
    c0 = cands[0]
    assert c0["schema_version"] == "3.0"
    assert c0["source_id"] == "src_demo"
    assert c0["source_file_sha256"] == "abc12345"
    assert "显瘦" in c0["selling_point_evidence_refs"]
    assert c0["source_start_ms"] >= 0
    assert c0["source_end_ms"] > c0["source_start_ms"]

    # 结构完整性验证（对标 candidate_v3.schema.json）
    required_fields = [
        "schema_version", "candidate_id", "source_id", "source_start_ms",
        "source_end_ms", "product_relation", "utterance_ids", "frame_ids",
        "selling_point_evidence_refs", "eligible", "risks"
    ]
    allowed_relations = {"SAME_SKU", "SAME_SKU_VARIANT", "ACCESSORY_OR_STYLING", "OTHER_SKU", "UNCERTAIN"}
    for c in cands:
        for rf in required_fields:
            assert rf in c, f"Candidate 缺少必填字段: {rf}"
        assert c["schema_version"] == "3.0"
        assert c["product_relation"] in allowed_relations
        assert isinstance(c["source_start_ms"], int)
        assert isinstance(c["source_end_ms"], int)
        assert isinstance(c["utterance_ids"], list)
        assert isinstance(c["frame_ids"], list)
        assert isinstance(c["selling_point_evidence_refs"], list)
        assert isinstance(c["eligible"], bool)
        assert isinstance(c["risks"], list)


def main():
    test_chinese_utterance_grouping_no_truncation()
    test_word_level_alignment_and_fallback()
    test_evidence_store_and_source_isolation()
    test_candidate_generation_schema_conformity()
    print("[TEST-UTTERANCE-CANDIDATES] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
