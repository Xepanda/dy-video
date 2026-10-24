#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
run_regression.py - 素材回归测试体系（B16）
==========================================
机制：
  1. 单元测试层：tests/ 下全部测试脚本（价格检测/OCR 纯净度/决策网关/
     时间线桥接/LLM 审核/管道单测）逐个子进程执行；
  2. 素材回归层：fixtures/materials.json 登记的全部素材（4 条新素材 + demo）
     经 batch_runner（B13 缓存，中断续跑不重算）跑端到端；
  3. 区间锁定层：fixtures/baselines.json 锁定每条素材的关键指标区间
     （时长/宽占比/响度/候选数/QC BLOCK 数等），实测越界即 FAIL；
     基线更新只能通过 --update-baselines 显式进行（阈值变更纪律：
     改阈值必须附回归结果，CI 全绿才允许合入）。

CLI:
  python scripts/run_regression.py                 # 全量回归
  python scripts/run_regression.py --quick         # 只跑单元测试层
  python scripts/run_regression.py --update-baselines  # 用当前结果更新基线
"""

import os
import sys
import json
import time
import argparse
import subprocess

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_DIR = os.path.join(PROJECT_ROOT, "fixtures")
MATERIALS_JSON = os.path.join(FIXTURES_DIR, "materials.json")
BASELINES_JSON = os.path.join(FIXTURES_DIR, "baselines.json")

# 单元测试层（相对 PROJECT_ROOT）
UNIT_TESTS = [
    "tests/test_improvements.py",
    "tests/test_pipeline_units.py",
    "tests/test_ocr_purity.py",
    "tests/test_llm_verifier.py",
    "tests/test_chatcut_bridge.py",
    "tests/test_blacklist_precision.py",
    "tests/test_multi_source.py",
    "tests/test_asr_review_gate.py",
    "tests/test_path_resolver.py",
    "tests/test_asr_prompt_policy.py",
    "tests/test_stage_cache_fingerprint.py",
]

PYTHON = sys.executable


def run_unit_tests() -> dict:
    results = {}
    for t in UNIT_TESTS:
        path = os.path.join(PROJECT_ROOT, t)
        if not os.path.exists(path):
            results[t] = {"status": "MISSING"}
            continue
        r = subprocess.run([PYTHON, "-u", path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", cwd=PROJECT_ROOT)
        results[t] = {"status": "PASS" if r.returncode == 0 else "FAIL",
                      "returncode": r.returncode,
                      "tail": (r.stdout or "")[-400:]}
    return results


def load_materials() -> list:
    with open(MATERIALS_JSON, "r", encoding="utf-8") as f:
        return json.load(f)["materials"]


def material_metrics(project_id: str) -> dict:
    """从输出产物提取回归指标（manifest + qc_report）。"""
    out_dir = os.path.join(PROJECT_ROOT, "output", project_id)
    manifest_path = os.path.join(out_dir, "agent_decision_manifest.json")
    qc_path = os.path.join(out_dir, "qc_report.json")
    metrics = {"project_id": project_id, "artifacts_present": os.path.exists(manifest_path)}
    if not os.path.exists(manifest_path):
        return metrics
    with open(manifest_path, "r", encoding="utf-8") as f:
        m = json.load(f)
    metrics["qc_passed"] = m.get("qc_passed")
    metrics["duration"] = m.get("total_duration")
    metrics["warnings"] = len(m.get("qc_warnings", []))
    cover = m.get("chosen_cover") or {}
    framing = cover.get("framing") or {}
    metrics["cover_width_ratio"] = framing.get("achieved_width_ratio")
    metrics["cover_post_scale_sharpness"] = framing.get("post_scale_sharpness")
    metrics["cover_yaw_deg"] = (cover.get("face") or {}).get("yaw_deg")
    metrics["clip_count"] = len(m.get("chosen_clips") or [])
    metrics["background_text_ratio"] = cover.get("background_text_ratio")
    if os.path.exists(qc_path):
        with open(qc_path, "r", encoding="utf-8") as f:
            qc = json.load(f)
        checks = qc.get("checks", [])
        metrics["qc_block_total"] = sum(1 for c in checks if c.get("level") == "BLOCK")
        metrics["qc_block_failed"] = sum(1 for c in checks
                                         if c.get("level") == "BLOCK" and c.get("status") == "FAIL")
        loud = next((c for c in checks if c.get("metric_key") == "audio.loudness_lufs"), {})
        measured = loud.get("measured") or {}
        if isinstance(measured, dict):
            metrics["loudness_lufs"] = measured.get("integrated_lufs")
            metrics["true_peak_dbtp"] = measured.get("true_peak_dbtp")
    return metrics


def compare_baseline(metrics: dict, baseline: dict) -> list:
    """区间比对，返回越界项列表（空=全部在区间内）。"""
    violations = []
    for key, rule in (baseline or {}).items():
        if key not in metrics or metrics[key] is None:
            continue  # 指标缺失不判越界（由 artifacts_present/单独断言处理）
        val = metrics[key]
        if isinstance(rule, dict) and ("min" in rule or "max" in rule):
            if "min" in rule and val < rule["min"]:
                violations.append(f"{key}={val} < min {rule['min']}")
            if "max" in rule and val > rule["max"]:
                violations.append(f"{key}={val} > max {rule['max']}")
        elif isinstance(rule, (int, float)) and isinstance(val, (int, float)):
            # 数值型单值规则：允许 ±10% 漂移
            if abs(val - rule) > max(0.1, abs(rule) * 0.1):
                violations.append(f"{key}={val} 偏离基线 {rule} 超 10%")
    return violations


def main():
    parser = argparse.ArgumentParser(description="素材回归测试体系（B16）")
    parser.add_argument("--quick", action="store_true", help="只跑单元测试层")
    parser.add_argument("--update-baselines", action="store_true",
                        help="用当前实测结果更新基线（阈值变更须附回归结果）")
    args = parser.parse_args()

    report = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "layers": {}}

    # ---- 层 1：单元测试 ----
    print("=" * 66)
    print("[回归层 1/3] 单元测试（价格/OCR/网关/桥接/LLM/管道）")
    print("=" * 66)
    unit = run_unit_tests()
    unit_fail = [k for k, v in unit.items() if v["status"] != "PASS"]
    for k, v in unit.items():
        print(f"  [{v['status']}] {k}")
    report["layers"]["unit_tests"] = unit

    if args.quick:
        print(f"\n[回归] quick 模式结束：单元测试 {len(unit) - len(unit_fail)}/{len(unit)} 通过")
        return 0 if not unit_fail else 1

    # ---- 层 2：素材端到端（batch_runner，B13 缓存续跑）----
    print("\n" + "=" * 66)
    print("[回归层 2/3] 素材端到端（batch_runner + 阶段缓存）")
    print("=" * 66)
    materials = load_materials()
    spec_paths = [os.path.join(PROJECT_ROOT, m["spec"]) for m in materials]
    r = subprocess.run(
        [PYTHON, "-u", os.path.join(PROJECT_ROOT, "src", "batch_runner.py"),
         "--specs"] + spec_paths,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=PROJECT_ROOT)
    batch_report_path = os.path.join(PROJECT_ROOT, "logs", "batch_report.json")
    batch = {}
    if os.path.exists(batch_report_path):
        with open(batch_report_path, "r", encoding="utf-8") as f:
            batch = json.load(f)
    report["layers"]["batch"] = {
        "returncode": r.returncode,
        "done": batch.get("done"), "qc_failed": batch.get("qc_failed"),
        "error": batch.get("error"), "skipped_cached": batch.get("skipped_cached"),
    }
    print(f"  batch: done={batch.get('done')} qc_failed={batch.get('qc_failed')} "
          f"error={batch.get('error')} skipped={batch.get('skipped_cached')}")

    # ---- 层 3：指标区间锁定 ----
    print("\n" + "=" * 66)
    print("[回归层 3/3] 指标区间锁定比对（fixtures/baselines.json）")
    print("=" * 66)
    baselines = {}
    if os.path.exists(BASELINES_JSON):
        with open(BASELINES_JSON, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # 兼容两种结构：{"materials": {pid: baseline}} 与扁平 {pid: baseline}
        if isinstance(raw, dict) and "materials" in raw and isinstance(raw["materials"], dict):
            baselines = raw["materials"]
        elif isinstance(raw, dict):
            baselines = raw

    all_metrics = {}
    interval_fail = []
    for m in materials:
        pid = m["project_id"]
        metrics = material_metrics(pid)
        all_metrics[pid] = metrics
        baseline = baselines.get(pid)
        if not metrics.get("artifacts_present"):
            interval_fail.append(f"{pid}: 缺少产物（manifest 不存在）")
            print(f"  [FAIL] {pid}: 缺少产物")
            continue
        if metrics.get("qc_passed") is not True:
            interval_fail.append(f"{pid}: QC 未通过")
            print(f"  [FAIL] {pid}: QC 未通过")
            continue
        if args.update_baselines or not baseline:
            print(f"  [BASELINE] {pid}: {'更新' if baseline else '建立'}基线")
            continue
        violations = compare_baseline(metrics, baseline)
        if violations:
            interval_fail.append(f"{pid}: {violations}")
            print(f"  [FAIL] {pid}: 指标越界 {violations}")
        else:
            print(f"  [PASS] {pid}: 指标全部在锁定区间内 "
                  f"(时长 {metrics.get('duration')}s, 宽占比 {metrics.get('cover_width_ratio')})")

    if args.update_baselines:
        os.makedirs(FIXTURES_DIR, exist_ok=True)
        new_baselines = {}
        for pid, metrics in all_metrics.items():
            if not metrics.get("artifacts_present"):
                continue
            b = {}
            for key in ("duration", "cover_width_ratio", "cover_post_scale_sharpness",
                        "cover_yaw_deg", "clip_count", "loudness_lufs",
                        "true_peak_dbtp", "background_text_ratio", "qc_block_total"):
                if metrics.get(key) is not None:
                    if key == "duration":
                        b[key] = {"min": max(45, metrics[key] - 6),
                                  "max": min(65, metrics[key] + 6)}
                    elif key == "cover_width_ratio":
                        b[key] = {"min": round(metrics[key] - 0.06, 3),
                                  "max": round(metrics[key] + 0.06, 3)}
                    elif key == "cover_yaw_deg":
                        b[key] = {"min": 0, "max": round(metrics[key] + 3, 1)}
                    elif key == "loudness_lufs":
                        b[key] = {"min": round(metrics[key] - 1.0, 1),
                                  "max": round(metrics[key] + 1.0, 1)}
                    elif key == "true_peak_dbtp":
                        b[key] = {"max": -0.5}
                    elif key == "background_text_ratio":
                        b[key] = {"max": 0.02}
                    elif key == "clip_count":
                        b[key] = {"min": max(1, metrics[key] - 2),
                                  "max": metrics[key] + 3}
                    elif key == "cover_post_scale_sharpness":
                        b[key] = {"min": 200}
                    elif key == "qc_block_total":
                        b[key] = {"min": metrics[key]}  # BLOCK 断言只增不减
            new_baselines[pid] = b
        with open(BASELINES_JSON, "w", encoding="utf-8") as f:
            json.dump({"updated": report["timestamp"], "materials": new_baselines},
                      f, ensure_ascii=False, indent=2)
        print(f"\n[回归] 基线已更新: {BASELINES_JSON}（{len(new_baselines)} 条素材）")
        report["baselines_updated"] = True

    # ---- 汇总 ----
    report["interval_failures"] = interval_fail
    report["unit_failures"] = unit_fail
    report["all_passed"] = (not unit_fail) and (not interval_fail) \
        and batch.get("error", 1) == 0 and batch.get("qc_failed", 1) == 0
    log_dir = os.path.join(PROJECT_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "regression_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 66)
    verdict = "CI 全绿 ✅" if report["all_passed"] else "CI 未通过 ❌"
    print(f"[回归总结] {verdict}")
    if unit_fail:
        print(f"  单元测试失败: {unit_fail}")
    if interval_fail:
        print(f"  指标越界: {interval_fail}")
    print("=" * 66)
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
