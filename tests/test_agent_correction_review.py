#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_agent_correction_review.py — ASR 纠错复核契约（2026-10-30 重构）

背景：流水线不再内置 LLM 调用（不读 LLM_API_KEY、不发 HTTP）。
职责分离：`AsrContextVerifier` 产出 `asr_correction_review_request.json`，
agent 审阅后回写 `asr_correction_decisions.json`，流水线消费并保留护栏。

用例：
  1. 无 agent 决策 → 使用本地确定性引擎（并产出复核请求）
  2. agent 纠错生效 + 时间戳绝对守恒
  3. 价格护栏：修正引入价格 → 拦截
  4. 长度护栏：长度比超限 → 拦截
  5. index 越界 → 拦截

运行: python -u tests/test_agent_correction_review.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.core.asr_agent_verifier import AsrContextVerifier

SPEC = {
    "product": {"title_line1": "半岛棉镂空钩花", "title_line2": "气质显瘦针织衫"},
    "brand": {"title_en": "MIXCITY"},
    "options": {"highlight_keywords": ["半岛棉", "镂空", "显瘦", "针织"]},
}

SEGMENTS = [
    {"start": 0.0, "end": 2.5, "duration": 2.5, "text": "今天我们来看这个半导棉的开衫"},
    {"start": 2.5, "end": 5.0, "duration": 2.5, "text": "穿上去超选瘦的"},
    {"start": 5.0, "end": 7.5, "duration": 2.5, "text": "这个价格只要299块钱"},
]


def _run(corrections=None):
    """在临时目录跑一次 verify_and_correct，可选写入 agent 决策文件。"""
    wd = tempfile.mkdtemp()
    report_path = os.path.join(wd, "asr_report.json")
    if corrections is not None:
        with open(os.path.join(wd, "asr_correction_decisions.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"corrections": corrections}, f, ensure_ascii=False)
    v = AsrContextVerifier(SPEC)
    segs, report = v.verify_and_correct([dict(s) for s in SEGMENTS], report_path)
    return wd, segs, report


def case_no_agent_decisions():
    """用例 1：无 agent 回写 → 本地引擎 + 产出复核请求。"""
    wd, segs, report = _run(None)
    try:
        assert "Deterministic" in report["engine_used"], report["engine_used"]
        req = os.path.join(wd, "asr_correction_review_request.json")
        assert os.path.exists(req), "未产出复核请求"
        with open(req, "r", encoding="utf-8") as f:
            payload = json.load(f)
        assert len(payload["segments"]) == len(SEGMENTS)
        assert payload["response_schema"]["corrections"]
        assert payload["respond_to"].endswith("asr_correction_decisions.json")
        print("[PASS] 用例1 无 agent 决策 → 本地引擎 + 请求落盘")
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def case_agent_correction():
    """用例 2：agent 回写纠错生效 + 时间戳守恒。"""
    wd, segs, report = _run([
        {"index": 0, "corrected": "今天我们来看这个半岛棉的开衫", "reason": "面料名纠错"},
        {"index": 1, "corrected": "穿上去超显瘦的", "reason": "卖点词纠错"},
    ])
    try:
        assert report["engine_used"].startswith("Agent review"), report["engine_used"]
        assert segs[0]["text"] == "今天我们来看这个半岛棉的开衫", segs[0]["text"]
        assert segs[1]["text"] == "穿上去超显瘦的", segs[1]["text"]
        for orig, new in zip(SEGMENTS, segs):
            assert orig["start"] == new["start"] and orig["end"] == new["end"], \
                "时间戳被篡改!"
        assert "299" in segs[2]["text"]
        print("[PASS] 用例2 agent 纠错生效 + 时间戳守恒")
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def case_price_guardrail():
    """用例 3：修正引入价格 → 拦截。"""
    wd, segs, report = _run([
        {"index": 2, "corrected": "这个到手价299", "reason": "顺手补全"},
    ])
    try:
        assert segs[2]["text"] == "这个价格只要299块钱", "价格护栏未生效"
        rejected = report["agent_rejected_corrections"]
        assert len(rejected) == 1, rejected
        assert "价格" in rejected[0]["reason"]
        print("[PASS] 用例3 零价格护栏拦截")
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def case_length_guardrail():
    """用例 4：长度比超限（疑似整句改写）→ 拦截。"""
    wd, segs, report = _run([
        {"index": 1, "corrected": "穿上去超显瘦的这个版型特别适合小个子女生而且面料很软",
         "reason": "扩写"},
    ])
    try:
        assert segs[1]["text"] == "穿上去超选瘦的", "长度护栏未生效"
        assert len(report["agent_rejected_corrections"]) == 1
        assert "长度比" in report["agent_rejected_corrections"][0]["reason"]
        print("[PASS] 用例4 长度护栏拦截")
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def case_index_guardrail():
    """用例 5：index 越界 → 拦截（不崩溃）。"""
    wd, segs, report = _run([
        {"index": 99, "corrected": "任意", "reason": "越界"},
    ])
    try:
        assert len(report["agent_rejected_corrections"]) == 1
        assert "越界" in report["agent_rejected_corrections"][0]["reason"]
        print("[PASS] 用例5 index 越界拦截")
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def main():
    failures = []
    for fn in (case_no_agent_decisions, case_agent_correction,
               case_price_guardrail, case_length_guardrail,
               case_index_guardrail):
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"[FAIL] {fn.__name__}: {e}")
        except Exception as e:
            failures.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"[ERROR] {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n[TEST-AGENT-REVIEW] {'全部通过' if not failures else '失败: ' + str(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
