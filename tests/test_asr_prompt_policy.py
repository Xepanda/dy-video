#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_asr_prompt_policy.py - initial_prompt 策略回归测试（2026-10-24）
======================================================================
背景：给 faster-whisper 注入 initial_prompt 后，模型会把 prompt 文本当成
音频内容识别，挤占**开头约 30 秒**的真实语音。

实测（6 号素材 318s，small 模型）：
  有 prompt：段数 151 / 总字 1478 / 前 90s 仅 348 字 / 首段 [0-30s]="连帽衫·绵羊毛"
  无 prompt：段数 162 / 总字 1625 / 前 90s 达 495 字 / 首段 [0-1.8s]=真实内容
  → 有 prompt 反而少识别 147 字（-30%），8/11 素材首段都能观察到回声。

关键：内容是在**转录阶段**就被挤掉的，事后过滤回声段无法恢复 →
必须从源头不注入（AsrEngine.use_initial_prompt 默认 False）。

本测试不跑真实模型（太慢），而是验证**策略层**：
  1. AsrEngine 默认不注入 prompt；
  2. 显式传 prompt 时被拦截并降级为 None；
  3. use_initial_prompt=True 时才放行（可显式opt-in）；
  4. 审核门禁能检出回声段（与 test_asr_review_gate 的 A10 呼应）。

运行: python -u tests/test_asr_prompt_policy.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.core.asr_engine import AsrEngine
from src.core.asr_review_gate import AsrReviewGate


def case_default_no_prompt():
    """默认不注入 prompt。"""
    e = AsrEngine()
    assert e.use_initial_prompt is False, \
        "AsrEngine 默认不应注入 initial_prompt（会挤掉开头约 30s 真实语音）"
    print("[PASS] AsrEngine 默认 use_initial_prompt=False")


def case_prompt_downgraded():
    """显式传 prompt 但未 opt-in 时，应被拦截降级为 None。"""
    e = AsrEngine()
    captured = {}

    class _FakeFW:
        @staticmethod
        def WhisperModel(*a, **k):
            class _M:
                @staticmethod
                def transcribe(video_path, **kwargs):
                    captured.update(kwargs)
                    return iter([]), None
            return _M()

    orig = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = _FakeFW
    try:
        e.transcribe_video(__file__, initial_prompt="主播口语高频词：实物更漂亮")
    finally:
        if orig is not None:
            sys.modules["faster_whisper"] = orig
        else:
            sys.modules.pop("faster_whisper", None)
    assert captured.get("initial_prompt") is None, \
        f"未 opt-in 时 prompt 应被降级为 None，实际 {captured.get('initial_prompt')!r}"
    print("[PASS] 未 opt-in 时 prompt 被拦截降级为 None")


def case_opt_in_allows_prompt():
    """显式 opt-in 才放行 prompt。"""
    e = AsrEngine(use_initial_prompt=True)
    assert e.use_initial_prompt is True, "opt-in 应生效"
    captured = {}

    class _FakeFW:
        @staticmethod
        def WhisperModel(*a, **k):
            class _M:
                @staticmethod
                def transcribe(video_path, **kwargs):
                    captured.update(kwargs)
                    return iter([]), None
            return _M()

    orig = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = _FakeFW
    try:
        e.transcribe_video(__file__, initial_prompt="主播口语高频词：实物更漂亮")
    finally:
        if orig is not None:
            sys.modules["faster_whisper"] = orig
        else:
            sys.modules.pop("faster_whisper", None)
    assert captured.get("initial_prompt") == "主播口语高频词：实物更漂亮", \
        f"opt-in 时 prompt 应放行，实际 {captured.get('initial_prompt')!r}"
    assert captured.get("condition_on_previous_text") is False, \
        "condition_on_previous_text 必须保持 False（防复读循环）"
    print("[PASS] opt-in 时 prompt 放行，且 condition_on_previous_text 保持 False")


def case_gate_detects_echo():
    """审核门禁能检出回声段（与 A10 用例互补，用真实样本）。"""
    real_echo_samples = [
        {"start": 0.0, "end": 30.0, "text": "连帽衫·绵羊毛"},
        {"start": 0.0, "end": 30.0, "text": "蕉个朋友·环保毛绒，"},
        {"start": 0.0, "end": 8.0, "text": "主播口语高频词，羽绒服，羽绒，"},
    ]
    for seg in real_echo_samples:
        segs = [seg] + [
            {"start": seg["end"] + i * 1.5, "end": seg["end"] + 1.4 + i * 1.5,
             "text": f"这个面料很舒服第{i}句"} for i in range(6)]
        r = AsrReviewGate(media_duration=seg["end"] + 10).review(segs)
        codes = [f["code"] for f in r["findings"]]
        assert "A10_PROMPT_ECHO" in codes, \
            f"真实回声样本未被检出: {seg['text']!r} -> {codes}"
    print(f"[PASS] 审核门禁检出全部 {len(real_echo_samples)} 个真实回声样本")


def main():
    failures = []
    for fn in (case_default_no_prompt, case_prompt_downgraded,
               case_opt_in_allows_prompt, case_gate_detects_echo):
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"[FAIL] {e}")
        except Exception as e:
            failures.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"[ERROR] {type(e).__name__}: {e}")
    print(f"\n[TEST-ASR-PROMPT] {'全部通过' if not failures else '失败: ' + str(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
