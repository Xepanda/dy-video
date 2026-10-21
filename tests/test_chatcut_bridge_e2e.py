#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_chatcut_bridge_e2e.py - B14 端到端验收：人工精修回注 → 重新组装 → QC 通过
=========================================================================
流程：
  1. 读取某素材的 plan.json（默认 demo）
  2. 导出 OpenChatCut 风格时间线
  3. 模拟人工精修：把第一个分镜的源入点后移 2s（时长仍落在 45~65s 区间）
  4. 回注为 refined plan.json
  5. 用 refined plan 重新跑主流水线（--plan，禁用缓存强制重算）
  6. 断言 QC 通过（BLOCK 全绿）

运行: python -u tests/test_chatcut_bridge_e2e.py [project_id]
"""
import os
import sys
import json
import shutil
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.tools.chatcut_bridge import plan_to_timeline, timeline_to_plan, validate_timeline

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    project_id = sys.argv[1] if len(sys.argv) > 1 else "silk_knitwear_7270"
    work_dir = os.path.join(PROJECT_ROOT, "data", "work_dirs", project_id)
    plan_path = os.path.join(work_dir, "plan.json")
    spec_path = os.path.join(PROJECT_ROOT, "job_spec.json")
    if project_id != "silk_knitwear_7270":
        spec_path = os.path.join(PROJECT_ROOT, "specs", f"job_spec_{project_id}.json")
    if not os.path.exists(plan_path):
        print(f"[E2E] 跳过：{plan_path} 不存在（该素材尚未跑过）")
        return 0

    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    # 1-2. 导出时间线
    timeline = plan_to_timeline(plan, spec)
    ok, errors = validate_timeline(timeline)
    assert ok, f"时间线自检失败: {errors}"
    print(f"[E2E] 时间线导出 OK（{len(timeline['tracks'][0]['clips'])} 视频 clip，"
          f"总长 {timeline['project']['duration']}s）")

    # 3. 模拟人工精修：首个 clip 源入点后移 2s
    vtrack = timeline["tracks"][0]
    before = vtrack["clips"][0]["source_in"]
    vtrack["clips"][0]["source_in"] = round(before + 2.0, 3)
    vtrack["clips"][0]["source_out"] = round(vtrack["clips"][0]["source_out"] + 2.0, 3)
    # 时间线坐标同步缩短
    vtrack["clips"][0]["timeline_end"] = round(vtrack["clips"][0]["timeline_end"] - 2.0, 3)
    for i, c in enumerate(vtrack["clips"][1:], 1):
        c["timeline_start"] = round(c["timeline_start"] - 2.0, 3)
        c["timeline_end"] = round(c["timeline_end"] - 2.0, 3)
    ok, errors = validate_timeline(timeline)
    assert ok, f"精修后时间线非法: {errors}"

    # 4. 回注
    refined = timeline_to_plan(timeline)
    refined_path = os.path.join(work_dir, "plan_refined.json")
    with open(refined_path, "w", encoding="utf-8") as f:
        json.dump(refined, f, ensure_ascii=False, indent=2)
    print(f"[E2E] 精修回注 OK：首分镜源入点 {before} → {before + 2.0}s，"
          f"计划总长 {refined['total_expected_duration']}s → {refined_path}")

    # 5. 用精修计划重新组装 + QC（--plan 干预模式自动禁用缓存）
    r = subprocess.run(
        [sys.executable, "-u", os.path.join(PROJECT_ROOT, "src", "main_pipeline.py"),
         "--spec", spec_path, "--plan", refined_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=PROJECT_ROOT)
    tail = "\n".join((r.stdout or "").splitlines()[-25:])
    print(tail)

    # 6. 断言 QC 通过
    manifest_path = os.path.join(PROJECT_ROOT, "output", project_id, "agent_decision_manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    qc_path = os.path.join(PROJECT_ROOT, "output", project_id, "qc_report.json")
    with open(qc_path, "r", encoding="utf-8") as f:
        qc = json.load(f)
    failed = [c["name"] for c in qc["checks"]
              if c.get("level") == "BLOCK" and c.get("status") == "FAIL"]
    print(f"[E2E] 精修后成片时长 {manifest.get('total_duration')}s，"
          f"QC={'PASS' if manifest.get('qc_passed') else 'FAIL'}，"
          f"BLOCK 失败项: {failed or '无'}")
    assert manifest.get("qc_passed") is True, f"精修后 QC 未通过: {failed}"
    print("[E2E] ✅ B14 端到端验收通过：人工精修回注后重新组装且 QC 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
