"""剪辑风格规则回归测试 —— R7~R10（2026-11-02）。

覆盖：
  1. R7 转场风格：硬切/叠化/混合三种模式的判定 + 毒化反向验证
  2. R8 音频风格：Boom/BGM/响度/LRA/语音占比的判定 + 毒化反向验证
  3. R9 调色一致性：变异系数阈值判定 + 毒化反向验证
  4. R10 节奏曲线：钩子密度/句长/语速/收紧比的判定 + 毒化反向验证
  5. 规则装载：约束键缺失时不应崩溃

运行：python -u tests/test_cut_style_rules.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import yaml  # noqa: E402

from src.tools.cut_style_rules import (  # noqa: E402
    check_audio_style, check_color_consistency, check_pacing_curve,
    check_transition_style, evaluate_style_plan, measure_pacing,
    resolve_audio_style, resolve_transition_style)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONSTRAINTS_PATH = os.path.join(ROOT, "contracts", "constraints.yaml")


def _cons() -> dict:
    with open(CONSTRAINTS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def case_rules_resolve():
    """用例1：R7/R8 的参数解析必须反映配置。"""
    c = _cons()
    ts = resolve_transition_style(c)
    assert ts["mode"] == "hard_cut", f"参考片实测为硬切，应为 hard_cut：{ts}"
    assert ts["video_transition_sec"] == 0.0, "硬切模式下叠化时长应为 0"
    assert ts["audio_crossfade"] is False, "硬切模式下不应做音频交叉淡化"
    assert ts["enabled"] is True

    au = resolve_audio_style(c)
    assert au["boom_sfx_enabled"] is False, "参考片实测无音效，应关闭 Boom"
    assert au["bgm_enabled"] is False, "参考片实测无 BGM"
    assert au["target_lufs"] == -15.0, "参考片实测 -15.3 LUFS"
    assert au["max_lra"] == 4.0, "参考片实测 LRA 1.5 LU"
    print("[PASS] 用例1 R7/R8 参数解析与配置一致")


def case_r7_transition_style():
    """用例2：R7 转场方式判定。"""
    c = _cons()
    # 硬切模式 + 实测全硬切 → PASS
    r = check_transition_style(c, {"cut_count": 9, "dissolve_count": 0})
    assert r["passed"], f"硬切模式应通过：{r}"
    # 硬切模式但出现叠化 → FAIL（毒化）
    r_bad = check_transition_style(c, {"cut_count": 9, "dissolve_count": 3})
    assert not r_bad["passed"], "硬切模式下出现叠化必须被拦截"
    assert "叠化" in r_bad["detail"], f"失败详情应说明叠化数量：{r_bad['detail']}"

    # 叠化模式 + 实测全叠化 → PASS
    c2 = _cons()
    c2["slicing_rules"]["transition_style"]["mode"] = "dissolve"
    c2["slicing_rules"]["transition_style"]["dissolve_sec"] = 0.25
    r2 = check_transition_style(c2, {"cut_count": 6, "dissolve_count": 6})
    assert r2["passed"], f"叠化模式应通过：{r2}"
    # 叠化模式但全是硬切 → FAIL
    r2_bad = check_transition_style(c2, {"cut_count": 6, "dissolve_count": 0})
    assert not r2_bad["passed"], "叠化模式下的硬切应被拦截"
    print("[PASS] 用例2 R7 转场方式判定（含毒化反向验证）")


def case_r8_audio_style():
    """用例3：R8 音频风格判定。"""
    c = _cons()
    good = {"integrated_lufs": -15.3, "lra": 1.5, "true_peak": -1.5,
            "speech_ratio": 0.964, "boom_present": False, "bgm_present": False}
    r = check_audio_style(c, good)
    assert r["passed"], f"参考片实测参数应通过：{r}"

    # 毒化1：出现 Boom
    bad1 = dict(good, boom_present=True)
    assert not check_audio_style(c, bad1)["passed"], "Boom 必须被拦截"
    # 毒化2：出现 BGM
    bad2 = dict(good, bgm_present=True)
    assert not check_audio_style(c, bad2)["passed"], "BGM 必须被拦截"
    # 毒化3：响度偏离
    bad3 = dict(good, integrated_lufs=-9.0)
    assert not check_audio_style(c, bad3)["passed"], "响度偏离必须被拦截"
    # 毒化4：LRA 超限
    bad4 = dict(good, lra=9.0)
    assert not check_audio_style(c, bad4)["passed"], "LRA 超限必须被拦截"
    # 毒化5：语音占比过低
    bad5 = dict(good, speech_ratio=0.5)
    assert not check_audio_style(c, bad5)["passed"], "语音占比过低必须被拦截"
    print("[PASS] 用例3 R8 音频风格判定（含 5 项毒化反向验证）")


def case_r9_color_consistency():
    """用例4：R9 调色一致性判定。"""
    c = _cons()
    # 参考片实测变异系数
    ref_cv = {"white_balance_r": 0.9, "white_balance_b": 0.8,
              "saturation": 4.6, "brightness": 3.7, "contrast": 8.2}
    r = check_color_consistency(c, {"cv": ref_cv})
    assert r["passed"], f"参考片实测应通过：{r}"

    # 毒化：白平衡跳变（不同机位/灯光）
    bad = dict(ref_cv, white_balance_r=18.0)
    assert not check_color_consistency(c, {"cv": bad})["passed"], \
        "白平衡跳变必须被拦截"
    # 毒化：饱和度跳变（商品颜色失真）
    bad2 = dict(ref_cv, saturation=25.0)
    assert not check_color_consistency(c, {"cv": bad2})["passed"], \
        "饱和度跳变必须被拦截"
    # 无数据时跳过而非崩溃
    assert check_color_consistency(c, {})["passed"], "无采样数据应跳过"
    print("[PASS] 用例4 R9 调色一致性判定（含毒化反向验证）")


def case_r10_pacing_curve():
    """用例5：R10 节奏与情绪曲线判定。"""
    c = _cons()
    # 参考片实测：钩子 3 切/10s、句长中位 1.54s、语速 397、收紧比 0.63、σ=3.55s、极差比=8.58
    ref = {"hook_cuts": 3, "sentence_len_median": 1.54,
           "speech_rate_cpm": 397, "tightening_ratio": 0.63,
           "max_mid_clip_sec": 12.97,
           "duration_sigma": 3.55, "duration_ratio": 8.58}
    r = check_pacing_curve(c, ref)
    assert r["passed"], f"参考片实测应通过：{r}"

    # 毒化1：钩子区无切点（开场平铺直叙，抓不住人）
    assert not check_pacing_curve(c, dict(ref, hook_cuts=0))["passed"], \
        "钩子区无切点必须被拦截"
    # 毒化2：句长过碎
    assert not check_pacing_curve(c, dict(ref, sentence_len_median=0.4))["passed"], \
        "句长过碎必须被拦截"
    # 毒化3：语速异常
    assert not check_pacing_curve(c, dict(ref, speech_rate_cpm=700))["passed"], \
        "语速异常必须被拦截"
    # 毒化4：后段未收紧（节奏全程平铺）
    assert not check_pacing_curve(c, dict(ref, tightening_ratio=1.2))["passed"], \
        "后段未收紧必须被拦截"
    # 毒化5：中段长段超时（核心论证拖沓）
    assert not check_pacing_curve(c, dict(ref, max_mid_clip_sec=20.0))["passed"], \
        "中段超长必须被拦截"
    # 毒化6：时长标准差不足（切片均质化，节奏平铺）
    assert not check_pacing_curve(c, dict(ref, duration_sigma=1.0))["passed"], \
        "时长标准差不足必须被拦截"
    # 毒化7：时长极差比不足（缺乏快切与长论证对比）
    assert not check_pacing_curve(c, dict(ref, duration_ratio=2.0))["passed"], \
        "时长极差比不足必须被拦截"
    print("[PASS] 用例5 R10 节奏曲线判定（含 7 项毒化反向验证）")


def case_measure_pacing():
    """用例6：measure_pacing 的实测口径。"""
    segs = [
        {"start": 0.0, "end": 2.0, "text": "abcdefgh"},     # 前段长句
        {"start": 2.0, "end": 4.0, "text": "abcdefgh"},
        {"start": 4.0, "end": 5.0, "text": "abcd"},          # 后段短句
        {"start": 5.0, "end": 6.0, "text": "abcd"},
    ]
    m = measure_pacing([1.0, 3.0], segs, 10.0)
    assert m["hook_cuts"] == 2, f"钩子区切点应统计 10s 内的：{m}"
    assert m["sentence_len_early"] > m["sentence_len_late"], \
        f"前段句长应大于后段：{m}"
    assert m["tightening_ratio"] < 1.0, f"收紧比应 <1：{m}"
    assert m["speech_ratio"] > 0.4, f"语音占比应按有声时长算：{m}"
    assert m["duration_sigma"] >= 2.0, f"标准差应正确计算：{m}"
    assert m["duration_ratio"] >= 4.0, f"极差比应正确计算：{m}"
    print("[PASS] 用例6 measure_pacing 实测口径（含时长离散度）")


def case_evaluate_style_plan():
    """用例7：汇总评估必须能整体判定。"""
    c = _cons()
    measured = {
        "transition": {"cut_count": 9, "dissolve_count": 0},
        "audio": {"integrated_lufs": -15.3, "lra": 1.5,
                  "speech_ratio": 0.964, "boom_present": False},
        "color": {"cv": {"white_balance_r": 0.9, "saturation": 4.6,
                         "brightness": 3.7, "contrast": 8.2}},
        "pacing": {"hook_cuts": 3, "sentence_len_median": 1.54,
                   "speech_rate_cpm": 397, "tightening_ratio": 0.63,
                   "max_mid_clip_sec": 12.97,
                   "duration_sigma": 3.55, "duration_ratio": 8.58},
    }
    rep = evaluate_style_plan(c, measured)
    assert rep["passed"], f"参考片实测应整体通过：{rep}"
    assert len(rep["checks"]) == 4, f"应有 4 条判定：{rep}"

    # 毒化：注入 Boom → 整体应 FAIL
    measured["audio"]["boom_present"] = True
    rep_bad = evaluate_style_plan(c, measured)
    assert not rep_bad["passed"], "注入 Boom 后整体必须 FAIL"
    print("[PASS] 用例7 汇总评估（含毒化反向验证）")


def case_missing_keys_no_crash():
    """用例8：约束键缺失时不应崩溃（降级而非异常）。"""
    c = {"slicing_rules": {}}
    for fn, arg in ((check_transition_style, {"cut_count": 3, "dissolve_count": 0}),
                    (check_audio_style, {"integrated_lufs": -14.0}),
                    (check_color_consistency, {"cv": {}}),
                    (check_pacing_curve, {})):
        r = fn(c, arg)
        assert "id" in r and "passed" in r, f"{fn.__name__} 应返回标准结构"
    ts = resolve_transition_style(c)
    assert ts["mode"] == "hard_cut", "缺省应为硬切（参考片默认）"
    print("[PASS] 用例8 约束键缺失时降级不崩溃")


def main() -> int:
    failures = []
    cases = [
        case_rules_resolve,
        case_r7_transition_style,
        case_r8_audio_style,
        case_r9_color_consistency,
        case_r10_pacing_curve,
        case_measure_pacing,
        case_evaluate_style_plan,
        case_missing_keys_no_crash,
    ]
    for fn in cases:
        try:
            fn()
        except AssertionError as e:
            print(f"[FAIL] {fn.__name__}: {e}")
            failures.append(fn.__name__)
        except Exception as e:
            print(f"[ERROR] {fn.__name__}: {type(e).__name__}: {e}")
            failures.append(fn.__name__)
    if failures:
        print(f"\n[TEST-CUT-STYLE] {len(failures)} 个用例失败: {failures}")
        return 1
    print(f"\n[TEST-CUT-STYLE] 全部 {len(cases)} 个用例通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
