# -*- coding: utf-8 -*-
"""
test_cut_and_continuity.py - PR-07 切点、动作与声音衔接测试
"""

import os
import sys
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.qc.continuity_qc import ContinuityAuditor
from src.core.cut_optimizer import CutOptimizer


def test_truncated_sentence_detected_and_shifted():
    """测试前段未闭合被识别为 INCOMPLETE_SENTENCE 并建议 SHIFT_CUT。"""
    clips = [
        {"candidate_id": "c1", "start": 0.0, "end": 3.0, "dur": 3.0, "source_id": "s1"},
        {"candidate_id": "c2", "start": 0.0, "end": 4.0, "dur": 4.0, "source_id": "s1"}
    ]
    candidates_map = {
        "c1": {
            "candidate_id": "c1", "text": "这件衣服不仅，",
            "risks": ["INCOMPLETE_SENTENCE"], "product_relation": "SAME_SKU", "source_id": "s1"
        },
        "c2": {
            "candidate_id": "c2", "text": "而且特别好穿。",
            "risks": [], "product_relation": "SAME_SKU", "source_id": "s1"
        }
    }
    auditor = ContinuityAuditor()
    report = auditor.audit_sequence_seams(clips, candidates_map)

    assert len(report["seams"]) == 1
    seam = report["seams"][0]
    assert seam["severity"] == "WARN"
    assert "INCOMPLETE_SENTENCE" in seam["issue_codes"]
    assert seam["suggested_action"] == "SHIFT_CUT"
    assert seam["suggested_time_shift_ms"] > 0

    # 验证 CutOptimizer 执行切点自修复
    optimizer = CutOptimizer()
    fixed_clips, actions = optimizer.optimize_cuts(clips, report)
    assert len(actions) == 1
    assert fixed_clips[0]["end"] > 3.0, "切点应向后微调以闭合语句"


def test_action_break_detected():
    """测试动作中途截断被检测到。"""
    clips = [
        {"candidate_id": "c1", "start": 0.0, "end": 3.0, "dur": 3.0, "source_id": "s1"},
        {"candidate_id": "c2", "start": 0.0, "end": 4.0, "dur": 4.0, "source_id": "s1"}
    ]
    candidates_map = {
        "c1": {"candidate_id": "c1", "motion_phase": "INCOMPLETE", "risks": ["ACTION_BREAK"], "source_id": "s1"},
        "c2": {"candidate_id": "c2", "motion_phase": "CORE", "risks": [], "source_id": "s1"}
    }
    auditor = ContinuityAuditor()
    report = auditor.audit_sequence_seams(clips, candidates_map)
    seam = report["seams"][0]
    assert "ACTION_BREAK" in seam["issue_codes"]


def test_cross_source_audio_jump():
    """测试跨源素材连接检测 AUDIO_JUMP 并建议 GAIN_MATCH。"""
    clips = [
        {"candidate_id": "c1", "start": 0.0, "end": 3.0, "dur": 3.0, "source_id": "s1"},
        {"candidate_id": "c2", "start": 0.0, "end": 4.0, "dur": 4.0, "source_id": "s2"}
    ]
    candidates_map = {
        "c1": {"candidate_id": "c1", "source_id": "s1"},
        "c2": {"candidate_id": "c2", "source_id": "s2"}
    }
    auditor = ContinuityAuditor()
    report = auditor.audit_sequence_seams(clips, candidates_map)
    seam = report["seams"][0]
    assert "AUDIO_JUMP" in seam["issue_codes"]
    assert seam["suggested_action"] == "GAIN_MATCH"


def main():
    test_truncated_sentence_detected_and_shifted()
    test_action_break_detected()
    test_cross_source_audio_jump()
    print("[TEST-CUT-AND-CONTINUITY] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
