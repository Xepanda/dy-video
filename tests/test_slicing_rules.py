#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_slicing_rules.py - 切片规则（R1~R6）回归测试
=========================================================
规则来源：参考成品 SSY410163_蝴蝶结高腰A字半身裙_切片.mp4 的实测拆解
         （证据见 docs/reference_cut_analysis_SSY410163.md）

覆盖：
  1. R1 切点吸附语音气口 + 切点类型判定 + 句内切点占比上限
  2. R2 片段时长区间
  3. R3 景别判定与全身占比
  4. R4 展示角度多样性
  5. R5 语音密度
  6. R6 漏斗顺序按品类覆写
  7. 门禁有效性反向验证（毒化输入必须被拦住，§11.2 #8）

运行: python -u tests/test_slicing_rules.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml

from src.tools.slicing_rules import (
    load_slicing_rules, rules_enabled, find_speech_pauses, find_local_silence_dips,
    snap_cut_to_pause, classify_cut_type, check_intra_sentence_ratio,
    check_clip_duration, trim_to_duration_window, classify_framing,
    check_full_body_ratio, check_shot_variety, check_speech_density,
    resolve_funnel_order, evaluate_plan_slicing,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "contracts", "constraints.yaml"), "r", encoding="utf-8") as f:
    CONSTRAINTS = yaml.safe_load(f)
RULES = load_slicing_rules(CONSTRAINTS)


def _cons(rules: dict) -> dict:
    """把切片规则段包回 constraints 形态（与生产调用一致）。"""
    return {"slicing_rules": rules}


def case_rules_loaded():
    """规则段必须存在且启用（否则整条规则链是死键）。"""
    assert RULES, "constraints.yaml 缺少 slicing_rules 段"
    assert rules_enabled(_cons(RULES)), "slicing_rules.enabled 应为 true"
    for k in ("cut_on_speech_pause", "clip_duration", "shot_framing",
              "shot_variety", "speech_density"):
        assert k in RULES, f"slicing_rules 缺少 {k}"
    print("[PASS] 用例1 切片规则段已加载并启用")


def case_r1_snap_to_pause():
    """R1：切点应吸附到语音气口（瞬时静音凹陷）。"""
    # 构造包络：0.0 有声，1.00 处瞬时凹陷（-90dB），2.00 有声
    windows = []
    for i in range(0, 300):
        t = i / 100.0
        db = -20.0
        if abs(t - 1.00) <= 0.02:
            db = -90.0
        windows.append((t, db))
    dips = find_local_silence_dips(-60.0, windows)
    assert dips, "应检测到瞬时静音凹陷"
    assert abs(dips[0][0] - 1.00) < 0.03, f"凹陷位置应贴近 1.00s，实得 {dips[0][0]}"

    # 切点 1.05s（句末标点落点），应吸附到 1.00s 的气口
    snapped = snap_cut_to_pause(1.05, [], 0.40, 0.45, dips=dips)
    assert snapped is not None, "应吸附到气口"
    assert abs(snapped - 1.00) < 0.03, f"应吸附到 1.00s 气口，实得 {snapped}"

    # 偏移过大时不吸附（宁可不吸附也不挪远）
    far = snap_cut_to_pause(1.50, [], 0.40, 0.45, dips=dips)
    assert far is None, f"偏移超限不应吸附，实得 {far}"

    # 连续静音区间亦可作为回退锚点
    pauses = find_speech_pauses(-60.0, 0.03, windows)
    snapped2 = snap_cut_to_pause(1.05, pauses, 0.40, 0.45)
    assert snapped2 is not None, "连续静音回退路径应可用"
    print("[PASS] 用例2 R1 切点吸附语音气口")


def case_r1_cut_type_and_ratio():
    """R1：切点类型判定 + 句内视觉切点占比上限。"""
    dips = [(6.50, -90.0), (22.31, -85.0), (29.04, -120.0)]
    assert classify_cut_type(6.50, [], None, -60.0, dips=dips) == "speech_pause"
    assert classify_cut_type(4.72, [], None, -60.0, dips=dips) == "intra_sentence"

    # 句内切点占比上限（参考片 2/9 = 22%，上限 30%）
    cuts = [(6.5, "speech_pause"), (9.3, "intra_sentence"),
            (22.3, "speech_pause"), (29.0, "speech_pause"),
            (38.8, "speech_pause"), (47.7, "speech_pause"),
            (53.6, "speech_pause"), (59.4, "speech_pause"),
            (4.7, "intra_sentence")]
    ok, ratio = check_intra_sentence_ratio(cuts, 0.30)
    assert ok, f"22% 应通过 30% 上限（实测 {ratio:.0%}）"
    assert abs(ratio - 2 / 9) < 0.01, f"应复现参考片 2/9 比例，实测 {ratio:.3f}"
    ok2, ratio2 = check_intra_sentence_ratio(
        [("a", "intra_sentence"), ("b", "intra_sentence"),
         ("c", "intra_sentence"), ("d", "intra_sentence")], 0.30)
    assert not ok2, "全句内切点必须被上限拦截"
    print("[PASS] 用例3 R1 切点类型判定与占比上限")


def case_r2_clip_duration():
    """R2：片段时长区间（参考片 1.78~12.97s，主体 5~10s）。"""
    cd = RULES.get("clip_duration") or {}
    lo, hi = float(cd["min_sec"]), float(cd["max_sec"])
    ok, msg = check_clip_duration(6.3, _cons(RULES))
    assert ok, f"中位时长应合规: {msg}"
    ok2, _ = check_clip_duration(lo - 0.5, _cons(RULES))
    assert not ok2, "低于下限应判碎切"
    ok3, _ = check_clip_duration(hi + 1.0, _cons(RULES))
    assert not ok3, "超过上限应判拖沓"
    assert trim_to_duration_window(20.0, _cons(RULES)) <= hi, "修剪应夹到上限内"
    assert trim_to_duration_window(0.5, _cons(RULES)) >= lo, "修剪应夹到下限内"
    print("[PASS] 用例4 R2 片段时长区间")


def case_r3_framing():
    """R3：景别判定与全身占比（参考片 9/10 全身，body_h 中位 0.556）。"""
    sf = RULES.get("shot_framing") or {}
    min_bh = float(sf["min_body_height_ratio"])
    assert classify_framing(0.556, _cons(RULES)) == "full_body", "0.556 应判全身"
    assert classify_framing(0.30, _cons(RULES)) == "partial", "0.30 应判局部"
    assert classify_framing(0.0, _cons(RULES)) == "invalid", "无人体应判 invalid"
    # span 优先（实测 span 比 body_h 稳定）
    assert classify_framing(0.30, _cons(RULES), span=0.64) == "full_body", \
        "span 达标时应判全身"
    assert classify_framing(0.70, _cons(RULES), span=0.30) == "partial", \
        "span 不足时应判局部"
    clips = [{"framing": "full_body"}] * 9 + [{"framing": "partial"}]
    ok, ratio = check_full_body_ratio(clips, _cons(RULES))
    assert ok, f"90% 全身应通过（实测 {ratio:.0%}）"
    ok2, ratio2 = check_full_body_ratio([{"framing": "partial"}] * 5, _cons(RULES))
    assert not ok2, "0% 全身必须被拦截"
    print("[PASS] 用例5 R3 景别判定与全身占比")


def case_r4_shot_variety():
    """R4：展示角度多样性（参考片 10 段 10 种角度，相邻不重）。"""
    sv = RULES.get("shot_variety") or {}
    # 构造 4 段：3 种不同角度 + 1 段与第 1 段同角度但不连续
    # fixture 取自参考成片实测（seg01/02/05/09 —— 角度差异最大的四段）
    clips = [
        {"span": 0.473, "shoulder_w": 0.248, "face_area": 0.0106, "fabric_block": 0.429},
        {"span": 0.723, "shoulder_w": 0.383, "face_area": 0.0345, "fabric_block": 0.328},
        {"span": 0.996, "shoulder_w": 0.427, "face_area": 0.0450, "fabric_block": 0.277},
        {"span": 0.640, "shoulder_w": 0.369, "face_area": 0.0349, "fabric_block": 0.566},
    ]
    ok, detail = check_shot_variety(clips, _cons(RULES))
    assert ok, f"3 种角度应通过: {detail}"
    assert detail["distinct_angles"] >= int(sv["min_distinct_angles"]), \
        f"角度数应 >= {sv['min_distinct_angles']}: {detail}"

    # 毒化：全部同角度 → 必须被拦
    same = [dict(clips[0]) for _ in range(4)]
    ok2, detail2 = check_shot_variety(same, _cons(RULES))
    assert not ok2, f"全同角度必须被拦: {detail2}"
    # 毒化：连续 3 段同角度 → 必须被拦
    consec = [dict(clips[0]), dict(clips[0]), dict(clips[0]), dict(clips[1])]
    ok3, detail3 = check_shot_variety(consec, _cons(RULES))
    assert not ok3, f"连续同角度必须被拦: {detail3}"
    print("[PASS] 用例6 R4 展示角度多样性（含反向验证）")


def case_r5_speech_density():
    """R5：语音密度（参考片 96.4%，几乎无冷场）。"""
    ok, ratio = check_speech_density(66.56, 69.34, _cons(RULES))
    assert ok, f"96.4% 应通过（实测 {ratio:.0%}）"
    ok2, ratio2 = check_speech_density(40.0, 69.34, _cons(RULES))
    assert not ok2, f"58% 应被拦（实测 {ratio2:.0%}）"
    print("[PASS] 用例7 R5 语音密度")


def case_r6_funnel_order_override():
    """R6：漏斗顺序可按品类覆写（参考片 hook→fit→fabric→styling）。"""
    default = resolve_funnel_order({}, _cons(RULES))
    assert default == ["hook", "fabric", "fit", "styling"], f"默认顺序应保持兼容: {default}"
    spec = {"options": {"funnel_order": ["hook", "fit", "fabric", "styling"]}}
    got = resolve_funnel_order(spec, _cons(RULES))
    assert got == ["hook", "fit", "fabric", "styling"], f"应按品类覆写: {got}"
    # 未列出的类目必须补回，不得漏类目
    spec2 = {"options": {"funnel_order": ["hook", "fit"]}}
    got2 = resolve_funnel_order(spec2, _cons(RULES))
    assert len(got2) == 4 and got2[0] == "hook" and got2[1] == "fit", \
        f"覆写不得丢类目: {got2}"
    print("[PASS] 用例8 R6 漏斗顺序按品类覆写")


def case_evaluate_plan_report():
    """整体评估：合规计划应通过，毒化计划应被标出。"""
    clips = [
        {"id": "c01", "dur": 6.0, "framing": "full_body", "span": 0.473,
         "shoulder_w": 0.248, "face_area": 0.0106, "fabric_block": 0.429},
        {"id": "c02", "dur": 7.5, "framing": "full_body", "span": 0.723,
         "shoulder_w": 0.383, "face_area": 0.0345, "fabric_block": 0.328},
        {"id": "c03", "dur": 5.5, "framing": "full_body", "span": 0.996,
         "shoulder_w": 0.427, "face_area": 0.0450, "fabric_block": 0.277},
        {"id": "c04", "dur": 6.2, "framing": "full_body", "span": 0.640,
         "shoulder_w": 0.369, "face_area": 0.0349, "fabric_block": 0.566},
    ]
    rep = evaluate_plan_slicing(clips, {}, CONSTRAINTS, speech_sec=24.0)
    assert rep["enabled"], "规则应启用"
    assert rep["passed"], f"合规计划应通过: {rep['checks']}"
    ids = {c["id"] for c in rep["checks"]}
    for must in ("R2_clip_duration", "R3_shot_framing", "R4_shot_variety",
                 "R5_speech_density"):
        assert must in ids, f"报告缺少 {must}"

    # 毒化：全同角度 + 超长片段 + 低语音密度 → 必须不通过
    bad = [dict(clips[0], dur=20.0, framing="partial", span=0.20) for _ in range(4)]
    rep2 = evaluate_plan_slicing(bad, {}, CONSTRAINTS, speech_sec=5.0)
    assert not rep2["passed"], "毒化计划必须被标出不通过"
    failed = {c["id"] for c in rep2["checks"] if not c["passed"]}
    assert "R2_clip_duration" in failed, f"超长片段必须被标出: {failed}"
    assert "R4_shot_variety" in failed, f"同角度必须被标出: {failed}"
    assert "R5_speech_density" in failed, f"低语音密度必须被标出: {failed}"
    print("[PASS] 用例9 切片规则整体评估（含毒化反向验证）")


def case_r2_split_long_block():
    """R2：超长块必须按句末边界拆分，且拆分后时长守恒。"""
    from src.tools.build_plan import _split_long_block
    for label, nseg, slen in (("整句 3s x10", 10, 3.0),
                              ("长句 5s x8", 8, 5.0),
                              ("短句 1.5s x30", 30, 1.5)):
        segs = [{"start": i * slen, "end": i * slen + slen * 0.93,
                  "text": f"第{i}句内容。", "source_file": "a.mp4",
                  "source_tag": "a"} for i in range(nseg)]
        total = nseg * slen
        blk = {"start": 0.0, "end": total, "dur": total, "segments": segs,
               "text": "x", "categories": ["fit"], "scores": {"fit": 1},
               "source_file": "a.mp4", "source_tag": "a", "_quota_cat": "fit"}
        out = _split_long_block(blk, 13.0, 1.7)
        assert len(out) > 1, f"{label}: 超长块应被拆分（实际 {len(out)} 段）"
        mx = max(b["dur"] for b in out)
        assert mx <= 13.0 + 0.01, f"{label}: 拆分后仍有超长段 {mx:.2f}s"
        s = sum(b["dur"] for b in out)
        # 拆分消耗的是句间停顿，缺口应在 5% 以内
        assert abs(total - s) / total < 0.05, \
            f"{label}: 拆分时长缺口过大 {total - s:.2f}s（{(total - s) / total:.1%}）"
    # 短尾段：应把上一段的最后一句挪过来，而非留下 <min_sec 的碎段
    segs = [{"start": 0.0, "end": 12.8,
             "text": "一大段连续讲解内容，包含很多句子在里面。"},
            {"start": 12.8, "end": 13.8, "text": "一句话。"},
            {"start": 13.8, "end": 25.0, "text": "另一大段连续讲解内容。"}]
    blk = {"start": 0.0, "end": 25.0, "dur": 25.0, "segments": segs,
           "text": "x", "categories": ["fit"], "scores": {"fit": 1},
           "source_file": "a.mp4", "source_tag": "a", "_quota_cat": "fit"}
    out = _split_long_block(blk, 13.0, 1.7)
    assert all(1.7 <= b["dur"] <= 13.01 for b in out), \
        f"短尾段再平衡后应全部合规：{[b['dur'] for b in out]}"

    # 未超长的块不应被拆
    segs = [{"start": 0.0, "end": 5.0, "text": "一句。", "source_file": "a.mp4"}]
    blk = {"start": 0.0, "end": 5.0, "dur": 5.0, "segments": segs, "text": "一句。",
           "categories": ["fit"], "scores": {"fit": 1}, "source_file": "a.mp4",
           "source_tag": "a"}
    assert len(_split_long_block(blk, 13.0, 1.7)) == 1, "未超长块不应被拆分"
    print("[PASS] 用例10 R2 超长块按句末拆分（时长守恒）")


def case_r10_hook_split_creates_multiple_cuts():
    """R10：钩子区必须切成**多段**（产生多个切点），而非一个长段。

    背景（2026-11-02）：首轮端到端跑完后 R10 判定 FAIL——钩子区只有 1 个切点，
    开场 6.44s 一个长段不够抓人。根因有二：
      1. 漏斗配额 hook 上限 8s 只够装 1 个短段；
      2. build_plan 的钩子切分把**余料挂回块池**，而 cands 已快照，
         本轮不会再消费它 → 无法产生第二个切点。

    本用例锁定：钩子选段必须能从同一块**连续切出多段**。
    """
    import yaml
    from src.tools.build_plan import build_plan
    cons_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "contracts", "constraints.yaml")
    with open(cons_path, "r", encoding="utf-8") as f:
        cons = yaml.safe_load(f) or {}

    # 构造 6 段带钩子关键词的素材（连续、可切）
    segs = []
    t = 0.0
    for i in range(12):
        segs.append({"start": t, "end": t + 2.5,
                     "text": "这条裙子真的太好看了显瘦又好看姐妹们闭眼入。",
                     "source_file": "a.mp4", "source_tag": "a",
                     "duration": 2.5})
        t += 2.6
    spec = {"options": {}, "product": {"name": "x"}}
    plan = build_plan("a.mp4", segs, spec, cons, scene_cuts=None,
                      work_dir=None)
    hook = [c for c in plan["clips"] if c.get("category") == "hook"]
    assert len(hook) >= 2, (
        f"钩子区应切出多段（产生多个切点），实际只有 {len(hook)} 段："
        f"{[round(c['dur'], 2) for c in hook]}")
    # 每个钩子段都应不长于 hook_segment_sec 的一个合理倍数
    seg_max = float(((cons.get("slicing_rules") or {})
                     .get("pacing_curve") or {}).get("hook_segment_sec", 4.0))
    for c in hook:
        assert c["dur"] <= seg_max + 1.5, (
            f"钩子段 {c['dur']:.2f}s 过长（上限 {seg_max + 1.5:.1f}s）")
    # 钩子累计时长应达到目标（否则等于没切）
    target = float(((cons.get("slicing_rules") or {})
                    .get("pacing_curve") or {}).get("hook_target_sec", 10.0))
    acc = sum(c["dur"] for c in hook)
    assert acc >= min(target, seg_max * 2) - 0.5, (
        f"钩子累计 {acc:.2f}s，未达到目标 {target}s")
    print(f"[PASS] 用例11 R10 钩子区切成多段（{len(hook)} 段 / "
          f"{[round(c['dur'], 2) for c in hook]}）")


def main():
    failures = []
    for fn in (case_rules_loaded, case_r1_snap_to_pause,
               case_r1_cut_type_and_ratio, case_r2_clip_duration,
               case_r3_framing, case_r4_shot_variety, case_r5_speech_density,
               case_r6_funnel_order_override, case_evaluate_plan_report,
               case_r2_split_long_block, case_r10_hook_split_creates_multiple_cuts):
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"[FAIL] {e}")
        except Exception as e:
            failures.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"[ERROR] {type(e).__name__}: {e}")
    print(f"\n[TEST-SLICING] {'全部通过' if not failures else '失败: ' + str(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
