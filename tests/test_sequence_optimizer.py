# -*- coding: utf-8 -*-
"""
test_sequence_optimizer.py - PR-05 多模态精排与全局编排测试
"""

import os
import sys
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.core.candidate_ranker import CandidateRanker
from src.core.sequence_optimizer import SequenceOptimizer


def _get_samples():
    candidates = [
        {
            "candidate_id": "c1",
            "source_id": "src_0",
            "source_start_ms": 0,
            "source_end_ms": 4000,
            "product_relation": "SAME_SKU",
            "selling_point_evidence_refs": ["保暖"],
            "text": "今天这件羽绒服特别保暖。",
            "risks": []
        },
        # c2 与 c1 在同一源上存在严重重叠 [2000, 6000]
        {
            "candidate_id": "c2",
            "source_id": "src_0",
            "source_start_ms": 2000,
            "source_end_ms": 6000,
            "product_relation": "SAME_SKU",
            "selling_point_evidence_refs": ["保暖", "轻盈"],
            "text": "羽绒服特别保暖，穿在身上非常轻盈。",
            "risks": []
        },
        # c3 在跨源 src_1 上
        {
            "candidate_id": "c3",
            "source_id": "src_1",
            "source_start_ms": 1000,
            "source_end_ms": 6000,
            "product_relation": "SAME_SKU",
            "selling_point_evidence_refs": ["显瘦"],
            "text": "上身超级显瘦不挑人。",
            "risks": []
        },
        # c4 是 OTHER_SKU（换款）
        {
            "candidate_id": "c4",
            "source_id": "src_0",
            "source_start_ms": 10000,
            "source_end_ms": 14000,
            "product_relation": "OTHER_SKU",
            "selling_point_evidence_refs": [],
            "text": "换下一款毛衣看下。",
            "risks": []
        },
    ]
    product_profile = {
        "sku_id": "SKU-TEST",
        "name": "羽绒服",
        "selling_points": ["保暖", "轻盈", "显瘦"]
    }
    return candidates, product_profile


def test_candidate_ranker_hard_filter():
    """验证硬过滤：OTHER_SKU 候选其 eligible 必须为 False。"""
    sample_candidates, product_profile = _get_samples()
    ranker = CandidateRanker()
    scored = ranker.score_candidates(sample_candidates, product_profile)
    scores_map = {s["candidate_id"]: s for s in scored["scores"]}

    assert scores_map["c1"]["eligible"] is True
    assert scores_map["c4"]["eligible"] is False
    assert any("非主推商品" in r for r in scores_map["c4"]["rejection_reasons"])


def test_same_source_overlap_forbidden():
    """测试同源重叠禁入：c1 与 c2 在 src_0 重叠，两者绝不能同时出现在同一 proposal 中。"""
    sample_candidates, product_profile = _get_samples()
    ranker = CandidateRanker()
    scored = ranker.score_candidates(sample_candidates, product_profile)

    optimizer = SequenceOptimizer(target_min_ms=5000, target_max_ms=15000)
    res = optimizer.optimize_sequence(sample_candidates, scored, product_profile, target_min_ms=3000, target_max_ms=12000)

    assert res["status"] == "PROPOSED"
    assert len(res["proposals"]) > 0

    for prop in res["proposals"]:
        cids = prop["ordered_candidate_ids"]
        # c1 和 c2 绝对不能同时存在
        assert not ("c1" in cids and "c2" in cids), f"同源重叠片段同时出现在提案中: {cids}"


def test_insufficient_evidence_graceful_failure():
    """测试无有效候选或时长严重不足时，优雅返回 INSUFFICIENT_EVIDENCE 而非强行出片。"""
    _, product_profile = _get_samples()
    few_candidates = [
        {
            "candidate_id": "c_short",
            "source_id": "src_0",
            "source_start_ms": 0,
            "source_end_ms": 3000,
            "product_relation": "SAME_SKU",
            "selling_point_evidence_refs": [],
            "text": "只有3秒。",
            "risks": []
        }
    ]
    ranker = CandidateRanker()
    scored = ranker.score_candidates(few_candidates, product_profile)

    optimizer = SequenceOptimizer(target_min_ms=45000, target_max_ms=65000)
    res = optimizer.optimize_sequence(few_candidates, scored, product_profile)

    assert res["status"] == "INSUFFICIENT_EVIDENCE"
    assert len(res["proposals"]) == 0
    assert len(res["evidence_gap"]) > 0
    assert any("不足" in gap for gap in res["evidence_gap"])


def main():
    test_candidate_ranker_hard_filter()
    test_same_source_overlap_forbidden()
    test_insufficient_evidence_graceful_failure()
    print("[TEST-SEQUENCE-OPTIMIZER] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
