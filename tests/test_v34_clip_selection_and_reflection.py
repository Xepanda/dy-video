# -*- coding: utf-8 -*-
"""
tests/test_v34_clip_selection_and_reflection.py
V3.4 选片叙事质量、字幕同音精校、贴纸显式关闭与成品叠加反思图单元测试。
"""

import os
import sys
import json
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.core.candidate_generator import CandidateGenerator
from src.core.candidate_ranker import CandidateRanker
from src.core.plan_compiler import compile_v3_plan


def test_candidate_generator_extends_dangling_clause_and_tags_risks() -> None:
    """遇到悬空从句结尾自动延伸合并完整语义，并对半截话/口水起头/直播杂音打软风险标签。"""
    utts = [
        {"utterance_id": "u1", "start_ms": 0, "end_ms": 4800, "text": "像100多克的鹅绒还没有这个保暖所以我们在新疆直播的时候", "ends_sentence": True, "categories": ["fabric"]},
        {"utterance_id": "u2", "start_ms": 4800, "end_ms": 8200, "text": "户外雪到腰那么高穿它都很暖和", "ends_sentence": True, "categories": ["fabric"]},
        {"utterance_id": "u3", "start_ms": 9000, "end_ms": 13500, "text": "好吧我们家这个新款耳环也不做了", "ends_sentence": True, "categories": ["hook"]},
    ]
    profile = {"name": "100%绵羊毛大衣", "category": "女装/大衣外套", "selling_points": ["羊毛", "保暖"]}
    gen = CandidateGenerator()
    cands = gen.generate_candidates(utts, profile, source_id="src_01")

    # 1. 短的悬空单句会被标记 DANGLING_CLAUSE_END
    dangling_cands = [c for c in cands if c["text"].endswith("的时候")]
    assert dangling_cands, "应当生成对应候选"
    assert "DANGLING_CLAUSE_END" in dangling_cands[0]["risks"]

    # 2. 应当延伸生成包含 u1 + u2 的完整 8.2s 叙事候选，且无 DANGLING_CLAUSE_END
    full_cands = [c for c in cands if "雪到腰那么高" in c["text"] and "保暖" in c["text"]]
    assert full_cands, "悬空从句后应延伸合并生成完整两句叙事候选"
    assert "DANGLING_CLAUSE_END" not in full_cands[0]["risks"]

    # 3. 口水起头 + 直播杂音被精准标出
    noisy_cands = [c for c in cands if c["text"].startswith("好吧")]
    assert noisy_cands
    assert "WEAK_FILLER_START" in noisy_cands[0]["risks"]
    assert "NOISE_OR_CHAT_MENTION" in noisy_cands[0]["risks"]
    print("  [PASS] CandidateGenerator：悬空从句延伸合并与叙事软风险标记正确")


def test_candidate_ranker_breaks_flat_tie_and_ranks_complete_clips_first() -> None:
    """精排器打破 80.62 集体平分：完整高卖点表达远高于半截话与口水杂音片段。"""
    profile = {"name": "100%绵羊毛大衣", "category": "女装/大衣外套", "selling_points": ["羊毛"]}
    cands = [
        {
            "candidate_id": "c_complete",
            "source_id": "src_01",
            "source_start_ms": 0,
            "source_end_ms": 7800,
            "product_relation": "SAME_SKU",
            "relation_confidence": 0.9,
            "vision_similarity": 0.45,
            "text": "这件100%全羊毛外套贴肤穿特别柔软不掉毛而且宽松版型非常显瘦保暖",
            "utterance_ids": ["u1", "u2"],
            "frame_ids": ["f1"],
            "selling_point_evidence_refs": ["羊毛"],
            "shot_ids": ["s1"],
            "risks": [],
        },
        {
            "candidate_id": "c_dangling",
            "source_id": "src_01",
            "source_start_ms": 10000,
            "source_end_ms": 15000,
            "product_relation": "SAME_SKU",
            "relation_confidence": 0.7,
            "vision_similarity": 0.05,
            "text": "好吧我们家这个毛毛外套在直播的时候",
            "utterance_ids": ["u3"],
            "frame_ids": ["f2"],
            "selling_point_evidence_refs": [],
            "shot_ids": [],
            "risks": ["DANGLING_CLAUSE_END", "WEAK_FILLER_START"],
        },
        {
            "candidate_id": "c_noisy",
            "source_id": "src_01",
            "source_start_ms": 20000,
            "source_end_ms": 25000,
            "product_relation": "SAME_SKU",
            "relation_confidence": 0.7,
            "vision_similarity": 0.05,
            "text": "宝宝们感谢大家这个白色也没有了过两天预售",
            "utterance_ids": ["u4"],
            "frame_ids": ["f3"],
            "selling_point_evidence_refs": [],
            "shot_ids": [],
            "risks": ["NOISE_OR_CHAT_MENTION"],
        },
    ]
    res = CandidateRanker().score_candidates(cands, profile)
    score_by_id = {c["candidate_id"]: c["composite_score"] for c in res["candidates"]}
    assert score_by_id["c_complete"] > score_by_id["c_dangling"] + 12.0, f"完整卖点片段应显著高于半截话: {score_by_id}"
    assert score_by_id["c_complete"] > score_by_id["c_noisy"] + 12.0, f"完整卖点片段应显著高于杂音片段: {score_by_id}"
    print(f"  [PASS] CandidateRanker：有效打破平分 ({score_by_id})")


def test_plan_compiler_supports_empty_sticker_topic_match_and_subtitle_replacements() -> None:
    """支持显式关闭贴纸、按口播精准匹配贴纸、以及传递 subtitle_replacements 与动态高亮词。"""
    cands = [
        {
            "candidate_id": "c1",
            "source_id": "src_01",
            "source_file": "a.mp4",
            "source_start_ms": 0,
            "source_end_ms": 5000,
            "product_relation": "SAME_SKU",
            "text": "这个外套几乎是不掉毛的而且很保暖",
            "selling_point_evidence_refs": [],
            "frame_ids": ["f1"],
            "risks": [],
        },
        {
            "candidate_id": "c2",
            "source_id": "src_01",
            "source_file": "a.mp4",
            "source_start_ms": 6000,
            "source_end_ms": 11000,
            "product_relation": "SAME_SKU",
            "text": "贴服船特别柔软显瘦",
            "selling_point_evidence_refs": [],
            "frame_ids": ["f2"],
            "risks": [],
        },
    ]
    scores = {"scores": [
        {"candidate_id": "c1", "narrative_role": "PROOF", "product_relation": "SAME_SKU"},
        {"candidate_id": "c2", "narrative_role": "FIT", "product_relation": "SAME_SKU"},
    ]}
    proposal = {
        "proposal_id": "p1",
        "ordered_candidate_ids": ["c1", "c2"],
        "clip_overrides": {
            "c2": {"sticker_text": ""},  # 显式关闭贴纸
        },
        "subtitle_replacements": {
            "贴服船": "贴肤穿",
        },
    }
    spec = {
        "project_id": "demo",
        "product": {"name": "100%绵羊毛外套", "selling_points": ["羊毛"]},
        "options": {"highlight_keywords": ["羊毛"]},
    }
    plan = compile_v3_plan(proposal, cands, scores, spec)
    assert plan["clips"][0]["sticker_text"] == "不易掉毛", f"应按口播匹配'不易掉毛'，实际={plan['clips'][0]['sticker_text']}"
    assert plan["clips"][1]["sticker_text"] == "", "显式传空字符串应关闭贴纸而非回退默认文案"
    assert plan["subtitles"]["replacements"] == {"贴服船": "贴肤穿"}
    assert "保暖" in plan["subtitles"]["keywords"] and "显瘦" in plan["subtitles"]["keywords"]
    print("  [PASS] compile_v3_plan：贴纸主题匹配、显式关闭贴纸、字幕精校与口播高亮均正确")


def main() -> int:
    tests = [
        test_candidate_generator_extends_dangling_clause_and_tags_risks,
        test_candidate_ranker_breaks_flat_tie_and_ranks_complete_clips_first,
        test_plan_compiler_supports_empty_sticker_topic_match_and_subtitle_replacements,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  [ERROR] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n[TEST-V34-CLIP] {len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
