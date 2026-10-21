#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_chatcut_bridge.py - B14 OpenChatCut 时间线桥接测试
======================================================
用例：
  1. 往返一致性：plan → timeline → plan' 语义保持（分镜边界/缩放/类目/Hook卡/CTA/Boom）
  2. 人工精修模拟：修剪某 clip 源区间 + 删除一个 clip → 回注计划反映编辑
  3. 精修计划过 B12 决策网关（合法编辑通过；非法编辑——如删到时长不足——被拦截）
  4. 时间线结构校验拦截（重叠/区间非法/缺视频轨）
  5. CLI 导出/导入往返

运行: python -u tests/test_chatcut_bridge.py
"""
import os
import sys
import json
import copy
import subprocess
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.tools.chatcut_bridge import (
    plan_to_timeline, timeline_to_plan, validate_timeline)
from src.agent.decision_gateway import DecisionGateway, DecisionViolation

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_PLAN = os.path.join(PROJECT_ROOT, "data", "work_dirs", "silk_knitwear_7270", "plan.json")
SPEC = {"project_id": "silk_knitwear_7270",
        "raw_video_path": "data/raw_samples/demo.mp4"}


def _load_demo_plan():
    if not os.path.exists(DEMO_PLAN):
        raise FileNotFoundError(f"demo plan 不存在: {DEMO_PLAN}")
    with open(DEMO_PLAN, "r", encoding="utf-8") as f:
        return json.load(f)


def case_roundtrip():
    plan = _load_demo_plan()
    timeline = plan_to_timeline(plan, SPEC)
    ok, errors = validate_timeline(timeline)
    assert ok, f"生成的时间线未通过自检: {errors}"
    plan2 = timeline_to_plan(timeline)
    assert len(plan2["clips"]) == len(plan["clips"]), "分镜数量不一致"
    for a, b in zip(plan["clips"], plan2["clips"]):
        assert abs(a["st"] - b["st"]) < 1e-6 and abs(a["end"] - b["end"]) < 1e-6, \
            f"分镜边界丢失: {a['id']}"
        assert a.get("zoom") == b.get("zoom"), f"缩放丢失: {a['id']}"
        assert a.get("category") == b.get("category"), f"类目丢失: {a['id']}"
    assert abs(plan["total_expected_duration"] - plan2["total_expected_duration"]) < 0.01
    print(f"[PASS] 用例1 往返一致（{len(plan['clips'])} 分镜边界/缩放/类目保持）")


def case_human_refine():
    """人工精修：trim 第一个 clip 的源入点 +3s，删除最后一个 clip"""
    plan = _load_demo_plan()
    timeline = plan_to_timeline(plan, SPEC)
    vtrack = timeline["tracks"][0]
    vtrack["clips"][0]["source_in"] = round(vtrack["clips"][0]["source_in"] + 3.0, 3)
    removed = vtrack["clips"].pop()
    # 重排时间线坐标（删除 clip 后后续 clip 前移）
    t = 0.0
    for c in vtrack["clips"]:
        dur = c["timeline_end"] - c["timeline_start"]
        c["timeline_start"] = round(t, 3)
        c["timeline_end"] = round(t + dur, 3)
        t += dur
    ok, errors = validate_timeline(timeline)
    assert ok, f"精修后时间线应合法: {errors}"
    refined = timeline_to_plan(timeline)
    assert len(refined["clips"]) == len(plan["clips"]) - 1, "删除未生效"
    assert abs(refined["clips"][0]["st"] - (plan["clips"][0]["st"] + 3.0)) < 1e-6, "trim 未生效"
    print(f"[PASS] 用例2 人工精修回注（trim+删除生效，剩 {len(refined['clips'])} 分镜）")
    return refined


def case_gateway_on_refined(refined):
    """精修计划过 B12 网关（合法编辑仅剩时长类违规；源时间非单调不误报）"""
    gw = DecisionGateway()
    ok, violations = gw.validate_plan(refined)
    assert not any("重叠" in v for v in violations), \
        f"源时间非单调被误报为重叠: {violations}"
    print(f"[PASS] 用例3 精修计划过 B12 网关（ok={ok}, 剩余违规={violations[:2]}）")
    # 非法编辑：把总时长删到 45s 以下 → 网关必须拦截
    broken = copy.deepcopy(refined)
    for c in broken["clips"]:
        c["dur"] = 1.0
        c["end"] = c["st"] + 1.0
    broken["total_expected_duration"] = float(len(broken["clips"]))
    ok2, v2 = gw.validate_plan(broken)
    assert not ok2, "非法编辑未被网关拦截"
    print(f"[PASS] 用例3b 非法编辑（时长不足）被 B12 网关拦截: {v2[0]}")


def case_validation_rejects_bad_timeline():
    plan = _load_demo_plan()
    timeline = plan_to_timeline(plan, SPEC)
    # 制造重叠
    vtrack = timeline["tracks"][0]
    vtrack["clips"][1]["timeline_start"] = vtrack["clips"][0]["timeline_start"]
    ok, errors = validate_timeline(timeline)
    assert not ok, "重叠未被拦截"
    # 制造源区间非法
    timeline2 = plan_to_timeline(plan, SPEC)
    timeline2["tracks"][0]["clips"][0]["source_out"] = \
        timeline2["tracks"][0]["clips"][0]["source_in"] - 1
    ok2, _ = validate_timeline(timeline2)
    assert not ok2, "源区间非法未被拦截"
    # 缺视频轨
    timeline3 = plan_to_timeline(plan, SPEC)
    timeline3["tracks"] = [t for t in timeline3["tracks"] if t["type"] != "video"]
    ok3, _ = validate_timeline(timeline3)
    assert not ok3, "缺视频轨未被拦截"
    print("[PASS] 用例4 时间线结构校验拦截（重叠/源区间非法/缺视频轨）")


def case_cli_roundtrip():
    plan = _load_demo_plan()
    with tempfile.TemporaryDirectory() as td:
        tl_path = os.path.join(td, "timeline.json")
        plan_path = os.path.join(td, "plan.json")
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False)
        r1 = subprocess.run(
            [sys.executable, os.path.join(PROJECT_ROOT, "src", "tools", "chatcut_bridge.py"),
             "--plan", plan_path, "--out", tl_path],
            capture_output=True, text=True)
        assert r1.returncode == 0, f"CLI 导出失败: {r1.stdout}{r1.stderr}"
        plan2_path = os.path.join(td, "plan2.json")
        r2 = subprocess.run(
            [sys.executable, os.path.join(PROJECT_ROOT, "src", "tools", "chatcut_bridge.py"),
             "--timeline", tl_path, "--plan-out", plan2_path],
            capture_output=True, text=True)
        assert r2.returncode == 0, f"CLI 导入失败: {r2.stdout}{r2.stderr}"
        with open(plan2_path, "r", encoding="utf-8") as f:
            plan2 = json.load(f)
        assert len(plan2["clips"]) == len(plan["clips"]), "CLI 往返分镜数不一致"
    print("[PASS] 用例5 CLI 导出/导入往返")


def case_multi_source_roundtrip():
    """用例6：多源时间线往返（跨素材选段的核心回归）。

    背景（2026-10-21）：多源计划里每个 clip 可能来自不同源文件，
    若时间线只挂单一 media，回注会丢失 source_file，
    重新组装时将从错误文件切片（画面与口型全错）。
    """
    plan = _load_demo_plan()
    multi = copy.deepcopy(plan)
    src_a = "D:/raw/A_001.mp4"
    src_b = "D:/raw/A_002.mp4"
    multi["raw_video_path"] = src_a
    multi["multi_source"] = True
    multi["sources_used"] = [src_a, src_b]
    for i, c in enumerate(multi["clips"]):
        c["source_file"] = src_a if i % 2 == 0 else src_b
        c["source_tag"] = os.path.basename(c["source_file"])
    spec = {"project_id": "multi_demo", "raw_video_path": src_a}

    timeline = plan_to_timeline(multi, spec)
    medias = {m["id"]: m for m in timeline["media"]}
    vids = [c for t in timeline["tracks"] if t["type"] == "video" for c in t["clips"]]
    assert len(vids) == len(multi["clips"]), "视频轨 clip 数与计划不一致"
    # 每个 clip 的 media_id 必须指向其真实来源
    for c, tc in zip(multi["clips"], vids):
        mid = tc["media_id"]
        assert mid in medias, f"clip {c['id']} 的 media_id={mid} 不在 media 列表"
        assert medias[mid]["path"] == c["source_file"], \
            f"clip {c['id']} 指向 {medias[mid]['path']}，应为 {c['source_file']}"
    assert len(medias) >= 2, f"多源应生成 >=2 个 media 实体，实得 {len(medias)}"

    back = timeline_to_plan(timeline)
    assert len(back["clips"]) == len(multi["clips"]), "回注后分镜数不一致"
    for c, bc in zip(multi["clips"], back["clips"]):
        assert bc.get("source_file") == c["source_file"], \
            f"回注丢失来源: {c['source_tag']} -> {bc.get('source_file')}"
    print("[PASS] 用例6 多源时间线往返（来源不丢失）")


def case_multi_source_single_backcompat():
    """用例7：单源计划仍走m_source 且不带 source_file（向后兼容）。"""
    plan = _load_demo_plan()
    spec = {"project_id": "silk_knitwear_7270",
            "raw_video_path": plan.get("raw_video_path", "data/raw_samples/demo.mp4")}
    timeline = plan_to_timeline(plan, spec)
    vids = [c for t in timeline["tracks"] if t["type"] == "video" for c in t["clips"]]
    assert all(c["media_id"] == "m_source" for c in vids), "单源时 media_id 应恒为 m_source"
    back = timeline_to_plan(timeline)
    assert all("source_file" not in c for c in back["clips"]), \
        "单源回注不应产生冗余 source_file"
    print("[PASS] 用例7 单源向后兼容")


def main():
    failures = []
    try:
        case_multi_source_roundtrip()
        case_multi_source_single_backcompat()
        case_roundtrip()
        refined = case_human_refine()
        case_gateway_on_refined(refined)
        case_validation_rejects_bad_timeline()
        case_cli_roundtrip()
    except AssertionError as e:
        failures.append(str(e))
        print(f"[FAIL] {e}")
    except Exception as e:
        failures.append(f"{type(e).__name__}: {e}")
        print(f"[ERROR] {type(e).__name__}: {e}")
    print(f"\n[TEST-B14] {'全部通过' if not failures else '失败: ' + str(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
