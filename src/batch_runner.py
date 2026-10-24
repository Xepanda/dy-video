#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
batch_runner.py - 多素材批量剪辑运行器（B13）
=============================================
能力：
  1. 批量：--specs a.json b.json 或 --specs-dir specs/（目录下全部 job_spec*.json）；
  2. 断点续跑：阶段产物 sha256 缓存（stage_cache），中断后重跑命中的阶段跳过重算；
     已完整跑过且 cache_key 一致的素材默认跳过（--force 可强制重跑）；
  3. 汇总：每条素材的 QC 结论/时长/WARN 数落盘 logs/batch_report.json。

设计约束：
  - 顺序执行（CPU 密集型：MediaPipe + Whisper + FFmpeg，并行会互相抢核）；
  - 外部干预（--cover-id/--plan）由单条 main_pipeline 承担，不走批量；
  - 任一素材失败不阻断其余素材，最终以退出码汇报（0=全部 QC 通过或缓存跳过）。

CLI:
  python src/batch_runner.py --specs-dir specs
  python src/batch_runner.py --specs specs/a.json specs/b.json --force
"""

import os
import sys
import json
import time
import argparse
import glob

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.agent.agent_director import AgentDirector
from src.tools.stage_cache import compute_pipeline_key


def _existing_manifest(project_id: str) -> dict:
    path = os.path.join(PROJECT_ROOT, "output", project_id, "agent_decision_manifest.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def run_batch(spec_paths, force: bool = False, no_cache: bool = False) -> dict:
    results = []
    t_all = time.time()
    for i, spec_path in enumerate(spec_paths, 1):
        spec_path = os.path.abspath(spec_path)
        with open(spec_path, "r", encoding="utf-8") as f:
            spec = json.load(f)
        project_id = spec.get("project_id", os.path.basename(spec_path))
        print(f"\n{'='*66}\n[{i}/{len(spec_paths)}] 素材: {project_id}  ({spec_path})\n{'='*66}")

        # 断点续跑：完整跑过且 key 一致 → 跳过（除非 --force）
        cache_key = None
        if not no_cache:
            cache_key = compute_pipeline_key(
                os.path.abspath(spec["raw_video_path"]), spec_path)
        if not force and not no_cache:
            manifest = _existing_manifest(project_id)
            if manifest.get("cache_key") == cache_key and manifest.get("qc_passed") is not None:
                print(f"[BATCH] 已完整跑过且输入未变更，跳过（--force 可强制重跑）")
                results.append({
                    "project_id": project_id, "status": "skipped_cached",
                    "qc_passed": manifest.get("qc_passed"),
                    "warnings": len(manifest.get("qc_warnings", [])),
                    "duration": manifest.get("total_duration"),
                })
                continue

        entry = {"project_id": project_id, "status": "failed"}
        try:
            director = AgentDirector(spec_path, cache_key=cache_key, use_cache=not no_cache)
            manifest = director.run_director_workflow()
            entry = {
                "project_id": project_id,
                "status": "done" if manifest.get("qc_passed") else "qc_failed",
                "qc_passed": manifest.get("qc_passed"),
                "warnings": len(manifest.get("qc_warnings", [])),
                "duration": manifest.get("total_duration"),
                "video": manifest.get("final_deliverables", {}).get("video"),
            }
        except Exception as e:
            entry = {"project_id": project_id, "status": "error", "error": str(e)}
            print(f"[BATCH] 素材 {project_id} 执行异常: {e}")
        results.append(entry)

    elapsed = time.time() - t_all
    report = {
        "batch_timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(results),
        "done": sum(1 for r in results if r["status"] == "done"),
        "qc_failed": sum(1 for r in results if r["status"] == "qc_failed"),
        "skipped_cached": sum(1 for r in results if r["status"] == "skipped_cached"),
        "error": sum(1 for r in results if r["status"] == "error"),
        "elapsed_sec": round(elapsed, 1),
        "results": results,
    }
    log_dir = os.path.join(PROJECT_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    report_path = os.path.join(log_dir, "batch_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*66}\n批量运行汇总（总耗时 {elapsed:.0f}s）\n{'='*66}")
    for r in results:
        mark = {"done": "✅", "qc_failed": "❌", "skipped_cached": "⏭️",
                "error": "💥"}.get(r["status"], "?")
        extra = (f"QC{'PASS' if r.get('qc_passed') else 'FAIL'} "
                 f"{r.get('duration') or '?'}s WARN×{r.get('warnings', '?')}"
                 if r["status"] != "error" else r.get("error", "")[:80])
        print(f"  {mark} {r['project_id']:<24} {r['status']:<14} {extra}")
    print(f"\n[BATCH] 汇总报告: {report_path}")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="多素材批量剪辑运行器（B13）")
    parser.add_argument("--specs", nargs="+", default=None, help="job_spec JSON 路径列表")
    parser.add_argument("--specs-dir", default=None, help="job_spec*.json 所在目录")
    parser.add_argument("--force", action="store_true", help="忽略已完成跳过，强制重跑")
    parser.add_argument("--no-cache", action="store_true", help="禁用阶段缓存")
    args = parser.parse_args(argv)

    spec_paths = list(args.specs or [])
    if args.specs_dir:
        spec_paths += sorted(glob.glob(os.path.join(args.specs_dir, "job_spec*.json")))
    if not spec_paths:
        print("未提供任何 spec（--specs 或 --specs-dir）")
        return 2

    report = run_batch(spec_paths, force=args.force, no_cache=args.no_cache)
    ok = report["error"] == 0 and report["qc_failed"] == 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
