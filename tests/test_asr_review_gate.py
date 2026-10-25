#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_asr_review_gate.py - ASR 执行者审核门禁测试（需求 2/4）
=============================================================
用例：
  1. 各校验点 BLOCK 行为：空结果/空文本/纯标点/噪声串/时间戳倒置/时间戳缺失/低密度退化
  2. 正常 ASR 结果放行，且返回结构含完整校验点清单
  3. WARN 分级与自动升级（WARN 占比超限→ BLOCK）
  4. 门禁已挂载到 AsrContextVerifier.verify_and_correct（机制性保证，不是靠调用方自觉）
  5. 审核报告落盘 + 失败时阻断（不静默降级）
  6. 阈值可由 constraints.asr_review 覆盖

运行: python -u tests/test_asr_review_gate.py
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.core.asr_review_gate import (
    AsrReviewGate, AsrReviewError, enforce_review_gate)

GOOD = [
    {"start": 0.0, "end": 2.8, "text": "这个半岛棉的面料非常柔软亲肤"},
    {"start": 3.0, "end": 5.9, "text": "上身超级显瘦整个人都瘦没了"},
    {"start": 6.1, "end": 9.0, "text": "搭配西裤是老钱风松弛感"},
    {"start": 9.2, "end": 12.0, "text": "针织的做工很精致又透气"},
]


def _expect_block(name, segs, duration=None, code=None):
    try:
        AsrReviewGate(media_duration=duration).review(segs)
    except AsrReviewError as e:
        if code and code not in str(e):
            raise AssertionError(f"{name}: 期望错误码含 {code}，实际 {e}")
        print(f"[PASS] {name} → BLOCK 阻断")
        return
    raise AssertionError(f"{name}: 期望 BLOCK 但被放行（审核失效）")


def case_block_points():
    _expect_block("空结果", [], code="A5")
    _expect_block("空文本", [{"start": 0, "end": 2, "text": "   "}], code="A1_EMPTY")
    _expect_block("纯标点", [{"start": 0, "end": 2, "text": "。。。"}], code="A1_PUNCT_ONLY")
    _expect_block("噪声串", [{"start": 0, "end": 2, "text": "啊啊啊啊啊啊啊啊"}],
                  code="A2_REPEAT_RUN")
    _expect_block("时间戳倒置", [{"start": 5, "end": 3, "text": "半岛棉面料"}],
                  code="A3_TS_INVERTED")
    _expect_block("时间戳缺失", [{"text": "半岛棉面料"}], code="A3_TS_MISSING")
    _expect_block("时间戳为负", [{"start": -1, "end": 3, "text": "半岛棉面料"}],
                  code="A3_TS_NEGATIVE")
    _expect_block("低密度退化(转录复读)",
                  [{"start": 0, "end": 2, "text": "主播口语高频语。"}], duration=300.0,
                  code="A5_LOW_DENSITY")


def case_pass_ok():
    r = AsrReviewGate(media_duration=12.0).review(GOOD)
    assert r["passed"] is True, "正常 ASR 应通过"
    assert r["valid_segments"] == len(GOOD)
    # 校验点清单必须完整落盘（便于审计"审核了什么"）
    for code in ("A1_non_empty", "A2_noise", "A3_timestamp_validity",
                 "A4_timestamp_monotonic", "A5_coverage", "A6_segment_length",
                 "A7_meta_hallucination", "A8_semantic_density", "A9_garbled_ratio"):
        assert code in r["checks"], f"审核报告缺少校验点 {code}"
    print(f"[PASS] 正常放行且报告含 {len(r['checks'])} 个校验点")


def case_warn_escalation():
    # 大量幻觉元话语 → WARN 占比超限 → 自动升级 BLOCK
    segs = [{"start": i * 2.0, "end": i * 2.0 + 1.8,
             "text": "我怎么跑到姜桑达说错了"} for i in range(6)]
    _expect_block("WARN 占比超限自动升级", segs, duration=12.0,
                  code="WARN_RATIO_ESCALATED")
    # 少量 WARN 应放行
    segs2 = list(GOOD) + [{"start": 12.2, "end": 14.0, "text": "我怎么跑到那说错了"}]
    r = AsrReviewGate(media_duration=14.0).review(segs2)
    assert r["passed"] is True, "少量 WARN 应放行"
    assert r["warn_count"] >= 1, "应记录 WARN"
    print(f"[PASS] WARN 分级正确（放行时 warn={r['warn_count']}）")


def case_prompt_echo():
    """A10：initial_prompt 回声污染检测（2026-10-24 新增）。

    背景：给 faster-whisper 注入 initial_prompt 后，模型把 prompt 文本当成
    音频内容识别，挤占开头约 30s 真实语音。真实样本：
      "连帽衫·绵羊毛"(7字/30s)、"蕉个朋友·环保毛绒，"(10字/30s)、
      "主播口语高频词，羽绒服，羽绒，"(15字/8s)

    注：样本需满足 A5 识别密度（句/秒 >= min_density），否则会先被 A5 阻断。
    """
    # 1) 固定措辞命中（任意位置）
    segs = [{"start": 0.0, "end": 8.0, "text": "主播口语高频词，羽绒服，羽绒，"}] + [
        {"start": 8.0 + i * 1.5, "end": 9.4 + i * 1.5, "text": f"这个面料很舒服第{i}句"}
        for i in range(6)]
    r = AsrReviewGate(media_duration=20.0).review(segs)
    codes = [f["code"] for f in r["findings"]]
    assert "A10_PROMPT_ECHO" in codes, f"应检出 prompt 回声，实际 {codes}"
    assert r["passed"] is True, "回声是 WARN 级，不阻断（根治靠不注入 prompt）"

    # 2) 首段异常长 + 文字极少（商品名回声，未命中固定措辞）
    segs2 = [{"start": 0.0, "end": 30.0, "text": "连帽衫·绵羊毛"}] + [
        {"start": 30.0 + i * 1.5, "end": 31.4 + i * 1.5, "text": f"这个版型很修身第{i}句"}
        for i in range(6)]
    r2 = AsrReviewGate(media_duration=40.0).review(segs2)
    codes2 = [f["code"] for f in r2["findings"]]
    assert "A10_PROMPT_ECHO" in codes2, f"应检出首段回声，实际 {codes2}"

    # 3) 正常首段不得误报（真实语音，短且密）
    segs3 = [{"start": 0.0, "end": 1.8, "text": "厚度也不是那种超级厚的"}] + [
        {"start": 1.8 + i * 1.5, "end": 3.2 + i * 1.5, "text": f"上身很显瘦第{i}句"}
        for i in range(6)]
    r3 = AsrReviewGate(media_duration=12.0).review(segs3)
    codes3 = [f["code"] for f in r3["findings"]]
    assert "A10_PROMPT_ECHO" not in codes3, f"正常首段被误报回声: {codes3}"
    assert "A10_LOW_DENSITY_LONG" not in codes3, f"正常首段被误报低密度: {codes3}"

    # 4) 校验点清单含A10
    assert "A10_prompt_echo" in r3["checks"], "报告校验点清单缺少 A10"
    print("[PASS] A10 提示词回声检测（命中 2 类样本，正常首段零误报）")


def case_wired_into_verifier():
    """机制性保证：门禁自动挂载，不依赖调用方自觉。"""
    from src.core.asr_agent_verifier import AsrContextVerifier
    import src.core.asr_agent_verifier as mod
    # 仅 import asr_agent_verifier 就应自动带上门禁（无需调用方显式 import）
    assert getattr(mod, "_asr_review_gate", None) is not None, \
        "asr_agent_verifier 应自动导入审核门禁模块"
    assert getattr(AsrContextVerifier, "_review_gate_wired", False), \
        "审核门禁未挂载到 verify_and_correct"
    # 挂载后 verify_and_correct 内部会跑审核：空 segments 应抛审核错误
    v = AsrContextVerifier({"product": {}, "brand": {}, "options": {}})
    try:
        v.verify_and_correct([])
    except AsrReviewError:
        print("[PASS] verify_and_correct 已强制执行审核（空结果被阻断）")
        return
    except Exception as e:
        raise AssertionError(f"verify_and_correct 抛出了非审核异常: {type(e).__name__}: {e}")
    raise AssertionError("verify_and_correct 未阻断空 ASR 结果（门禁未生效）")


def case_report_persisted():
    with tempfile.TemporaryDirectory() as td:
        rp = os.path.join(td, "review.json")
        enforce_review_gate(GOOD, media_duration=12.0, report_path=rp)
        assert os.path.exists(rp), "通过时也应落盘报告（审计需要）"
        with open(rp, "r", encoding="utf-8") as f:
            rep = json.load(f)
        assert rep["passed"] is True and "checks" in rep
        # 失败时也要落盘，且携带 findings
        rp2 = os.path.join(td, "review_fail.json")
        try:
            enforce_review_gate([], media_duration=12.0, report_path=rp2)
        except AsrReviewError:
            pass
        assert os.path.exists(rp2), "失败时未落盘报告"
        with open(rp2, "r", encoding="utf-8") as f:
            rep2 = json.load(f)
        assert rep2["passed"] is False and rep2["findings"], "失败报告缺少 findings"
    print("[PASS] 审核报告落盘（通过/失败双路径）")


def case_constraints_override():
    c = {"asr_review": {"min_density": 0.001, "warn_ratio_limit": 0.99}}
    r = AsrReviewGate(media_duration=300.0, constraints=c).review(
        [{"start": 0, "end": 2, "text": "半岛棉面料"}])
    assert r["passed"] is True, "阈值覆盖后应放行（放宽 min_density 生效）"
    print("[PASS] constraints.asr_review 阈值可覆盖")


def main():
    failures = []
    for fn in (case_block_points, case_pass_ok, case_warn_escalation,
               case_prompt_echo,
               case_wired_into_verifier, case_report_persisted, case_constraints_override):
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"[FAIL] {e}")
        except Exception as e:
            failures.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"[ERROR] {type(e).__name__}: {e}")
    print(f"\n[TEST-ASR-REVIEW] {'全部通过' if not failures else '失败: ' + str(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())