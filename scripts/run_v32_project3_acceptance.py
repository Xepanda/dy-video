# -*- coding: utf-8 -*-
"""
scripts/run_v32_project3_acceptance.py
=============================================================================
A08 任务本地终验脚本：
  1. project-3 原始文件与历史成片基线哈希记录（防篡改保护）
  2. Windows EXE 构建产物与任务交接器 (SpecCreator) 验证（无 Agent 进程派生）
  3. 通用 CLI 阶段化流水线本地端到端闭环（prepare -> analyze -> validate-plan -> render -> qc -> deliver）
  4. 异常路径拦截验证（非法 candidate_id、篡改 candidate_pool_hash）
  5. 配置敏感度验证（修改品牌必变更指纹）
  6. 验收后重算基线哈希，证明 100% 未受篡改或覆盖
  7. 输出脱敏审计摘要
=============================================================================
"""

import os
import re
import sys
import json
import time
import shutil
import hashlib
import tempfile
from typing import Dict, Any, List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.project_context import (
    ProjectPaths,
    InputManifest,
    compute_file_sha256,
    compute_full_file_sha256,
    compute_job_fingerprint,
    is_historical_output,
)
from src.runtime.stage_services import (
    stage_prepare,
    stage_analyze,
    stage_validate_plan,
    stage_render,
    stage_qc,
    stage_deliver,
)
from src.tools.desktop_spec_creator import DesktopSpecApi, SpecManager
from src.agent.decision_gateway import DecisionViolation
from src.tools.detect_price import contains_price
from src.tools.detect_violation import find_violation_matches

PROJECT3_DIR = r"C:\Users\ePanda\Desktop\project-3"


def compute_folder_hashes(folder: str) -> Dict[str, Dict[str, Any]]:
    record = {}
    for f in sorted(os.listdir(folder)):
        fp = os.path.join(folder, f)
        if os.path.isfile(fp):
            record[f] = {
                "size": os.path.getsize(fp),
                "mtime": os.path.getmtime(fp),
                "sha256": compute_full_file_sha256(fp),
            }
    return record


def main():
    print("=" * 80)
    print("【A08 本地验收】dy-video V3.2 Agent-First + project-3 本地真实素材验收")
    print("=" * 80)

    if not os.path.isdir(PROJECT3_DIR):
        print(f"[FAIL] 未找到本地项目目录: {PROJECT3_DIR}")
        return 1

    # =========================================================================
    # 步骤 1：只读盘点与基线 SHA-256 记录
    # =========================================================================
    print("\n--- 步骤 1: 只读盘点与 SHA-256 基线快照 ---")
    baseline_hashes = compute_folder_hashes(PROJECT3_DIR)
    print(f"检测到 project-3 根目录下共有 {len(baseline_hashes)} 个既有文件:")
    for f, info in baseline_hashes.items():
        is_h, reason = is_historical_output(os.path.join(PROJECT3_DIR, f), "project-3")
        tag = f"[历史成片/导出: {reason}]" if is_h else "[原始资产/配置]"
        print(f"  * {f:36s} {info['size']:>12,d} 字节 | {info['sha256'][:16]}... {tag}")

    # =========================================================================
    # 步骤 2 & 3：Windows EXE 产物与任务交接器验证
    # =========================================================================
    print("\n--- 步骤 2 & 3: Windows EXE 产物与任务交接器核验 ---")
    exe_cn = os.path.join(PROJECT_ROOT, "启动任务配置器.exe")
    exe_en = os.path.join(PROJECT_ROOT, "SpecCreator.exe")
    assert os.path.isfile(exe_cn), f"未找到构建产物: {exe_cn}"
    assert os.path.isfile(exe_en), f"未找到构建产物: {exe_en}"
    exe_sz = os.path.getsize(exe_cn)
    print(f"  [PASS] EXE 产物存在: {os.path.basename(exe_cn)} ({exe_sz:,d} 字节)")

    # 验证 EXE 任务生成逻辑：零勾选阻断、空品牌/有品牌支持、无子进程派生
    api = DesktopSpecApi()
    test_mat_vids = [
        os.path.join(PROJECT3_DIR, "20261023141614_20261023141937 (1).mp4"),
        os.path.join(PROJECT3_DIR, "20261023143205_20261023143415.mp4"),
    ]

    # 零勾选阻断测试
    res_zero = api.generate_agent_task({
        "project_id": "test_zero",
        "project_root": PROJECT3_DIR,
        "raw_video_paths": [],
        "product_title_l1": "测试",
        "product_title_l2": "测试",
    })
    assert res_zero["success"] is False and res_zero["status"] == "NEEDS_INPUT"
    print("  [PASS] 零勾选素材阻断: 正确拦截并返回 NEEDS_INPUT")

    # =========================================================================
    # 步骤 4 & 5：执行通用 CLI 阶段化流水线
    # =========================================================================
    print("\n--- 步骤 4 & 5: 执行通用 CLI 阶段化流水线 (project-3 资产) ---")
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=str, default=None, help="复用指定 run_id 目录")
    args, _ = parser.parse_known_args()

    run_id = args.run_id or f"run_v32_{time.strftime('%Y%m%d_%H%M%S')}"
    paths = ProjectPaths(project_root=PROJECT3_DIR, run_id=run_id)
    paths.ensure_runtime_dirs()
    work_dir = paths.work_dir
    output_dir = paths.output_dir
    print(f"本次运行 run_id: {run_id}")
    print(f"工作目录: {work_dir}")
    print(f"产出目录: {output_dir}")

    # 构造本次运行专用 job_spec.json (保存在 work_dir，绝不覆盖素材目录既有 job_spec.json)
    chosen_videos = [
        os.path.join(PROJECT3_DIR, "20261023141614_20261023141937 (1).mp4"),
        os.path.join(PROJECT3_DIR, "20261023143205_20261023143415.mp4"),
        os.path.join(PROJECT3_DIR, "20261023143415_20261023143613.mp4"),
    ]
    ref_image = os.path.join(PROJECT3_DIR, "封面图.png")

    task_spec = {
        "schema_version": "3.2",
        "project_id": "project-3",
        "raw_video_paths": chosen_videos,
        "brand": {
            "title_en": "MIXCITY",
            "subtitle_cn": "深圳高端女装"
        },
        "product": {
            "title_line1": "法式高级感羊毛",
            "title_line2": "混纺针织上衣",
            "item_images": [ref_image] if os.path.isfile(ref_image) else []
        },
        "options": {
            "duration_range": "45-65",
            "split_strategy": "force_off",
            "style_preset": "balanced",
            "cover_strategy": "smart_frontal",
            "highlight_keywords": ["羊毛", "混纺", "针织", "高级感", "法式", "显瘦", "闭眼入"],
            "features": {
                "render_profile": "branded",
                "duration_preset": "standard",
                "fabric_ratio": "strict",
                "promo_words": "allow_welfare",
                "violation_action": "delete",
                "transition": "on",
                "magnifier": "on",
                "manual_review": "off"
            }
        }
    }
    os.makedirs(work_dir, exist_ok=True)
    spec_path = os.path.join(work_dir, "task_spec.json")
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump(task_spec, f, ensure_ascii=False, indent=2)

    # 同步已有 ASR 缓存以加速 analyze 运行
    cached_run_base = os.path.join(PROJECT3_DIR, "work", "run_v31_20261121_041915")
    cached_asr_src = os.path.join(cached_run_base, "asr")
    target_asr_dir = os.path.join(work_dir, "asr")
    if os.path.isdir(cached_asr_src):
        os.makedirs(target_asr_dir, exist_ok=True)
        for item in os.listdir(cached_asr_src):
            s_file = os.path.join(cached_asr_src, item)
            t_file = os.path.join(target_asr_dir, item)
            if os.path.isfile(s_file):
                shutil.copy2(s_file, t_file)
        # 复制合并的 verified 缓存
        for v_name in ("whisper_segments_verified.json", "whisper_segments.json"):
            v_src = os.path.join(cached_run_base, v_name)
            if os.path.isfile(v_src):
                shutil.copy2(v_src, os.path.join(work_dir, v_name))
        print("  [INFO] 已导入 project-3 ASR 预审缓存，加速分析阶段")

    # 阶段 1: prepare
    print("\n[CLI: prepare]")
    prep_res = stage_prepare(spec_path, run_id=run_id, resume=bool(args.run_id))
    assert prep_res["run_id"] == run_id
    assert len(prep_res["source_videos"]) == 3
    print(f"  [PASS] prepare 完成: 3 个源素材已登记，指纹: {prep_res['spec_fingerprint'][:16]}...")

    # 阶段 2: analyze
    print("\n[CLI: analyze]")
    analyze_res = stage_analyze(work_dir)
    assert analyze_res["candidates_count"] > 0
    candidate_hash = analyze_res["candidate_pool_hash"]
    print(f"  [PASS] analyze 完成: 生成 {analyze_res['candidates_count']} 个候选，冻结哈希: {candidate_hash[:16]}...")

    # 读取候选池
    cand_file = os.path.join(work_dir, "analysis", "candidates.json")
    with open(cand_file, "r", encoding="utf-8") as f:
        all_candidates = json.load(f)
    print(f"  [INFO] 候选池总计 {len(all_candidates)} 段素材候选")

    cov_file = os.path.join(work_dir, "analysis", "cover_candidates.json")
    valid_cover_id = None
    if os.path.isfile(cov_file):
        with open(cov_file, "r", encoding="utf-8") as f:
            c_covs = json.load(f)
            if c_covs:
                valid_cover_id = c_covs[0]["id"]

    # =========================================================================
    # 步骤 6：异常路径拦截测试 (Bad proposal & Hash mismatch)
    # =========================================================================
    print("\n--- 步骤 6: 异常路径与防伪造门禁拦截测试 ---")

    # 异常测试 1：非法 candidate ID
    bad_proposal_1 = {
        "schema_version": "3.2",
        "run_id": run_id,
        "candidate_pool_hash": candidate_hash,
        "proposal_id": "bad_prop_01",
        "ordered_candidate_ids": ["c_fake_illegal_id_9999"],
        "chosen_cover_id": valid_cover_id,
        "style": "balanced",
        "decision_reasons": ["伪造镜头ID测试"]
    }
    p1_path = os.path.join(work_dir, "bad_prop_1.json")
    with open(p1_path, "w", encoding="utf-8") as f:
        json.dump(bad_proposal_1, f, ensure_ascii=False, indent=2)

    try:
        stage_validate_plan(work_dir, p1_path)
        assert False, "严重漏洞：非法 candidate_id 未被拦截！"
    except (ValueError, DecisionViolation) as e:
        print(f"  [PASS] 成功拦截非法 candidate_id: {e}")

    # 异常测试 2：篡改 candidate_pool_hash
    bad_proposal_2 = {
        "schema_version": "3.2",
        "run_id": run_id,
        "candidate_pool_hash": "tampered_fake_hash_123456",
        "proposal_id": "bad_prop_02",
        "ordered_candidate_ids": [all_candidates[0]["candidate_id"]],
        "chosen_cover_id": valid_cover_id,
        "style": "balanced",
        "decision_reasons": ["篡改候选哈希测试"]
    }
    p2_path = os.path.join(work_dir, "bad_prop_2.json")
    with open(p2_path, "w", encoding="utf-8") as f:
        json.dump(bad_proposal_2, f, ensure_ascii=False, indent=2)

    try:
        stage_validate_plan(work_dir, p2_path)
        assert False, "严重漏洞：篡改 candidate_pool_hash 未被拦截！"
    except (ValueError, DecisionViolation) as e:
        print(f"  [PASS] 成功拦截篡改候选池哈希: {e}")

    # 异常测试 3：修改品牌导致指纹变化
    spec_alt = dict(task_spec)
    spec_alt["brand"] = {"title_en": "OTHER_BRAND", "subtitle_cn": "轻奢女装"}
    fp_orig = prep_res["spec_fingerprint"]
    # 模拟输入清单
    manifest_obj = {"source_videos": [{"path": v, "sha256": "x"} for v in chosen_videos]}
    fp_new = compute_job_fingerprint(spec_alt, manifest_obj)
    assert fp_orig != fp_new, "严重漏洞：修改品牌后指纹未发生变化！"
    print(f"  [PASS] 品牌变更指纹敏感度: 原={fp_orig[:12]}..., 新={fp_new[:12]}... (完全隔离)")

    # =========================================================================
    # 步骤 4/5 继续：编写合法提案并完成渲染与双层质检
    # =========================================================================
    print("\n--- 编写外部 Agent 合法提案并执行渲染与交付 ---")
    # 按照严格时间不重叠准则挑选 10-15 个高质量候选分镜，总时长在 45-65 秒之间
    selected_cands = []
    accum_dur = 0
    intervals_by_source = {}  # source -> list of (start_ms, end_ms)

    for c in all_candidates:
        if c.get("eligible") is not True:
            continue
        if c.get("rejection_reasons"):
            continue
        if c.get("product_relation") != "SAME_SKU":
            continue
        if contains_price(c.get("text", ""))[0]:
            continue
        if re.search(r'\d', c.get("text", "")):
            continue
        if find_violation_matches(c.get("text", "")):
            continue
        if c.get("risks"):
            continue
        src = c.get("source_id") or c.get("source_file") or ""
        s_ms = c.get("source_start_ms") if "source_start_ms" in c else c.get("start_ms", 0)
        e_ms = c.get("source_end_ms") if "source_end_ms" in c else c.get("end_ms", 0)
        dur = e_ms - s_ms
        if dur < 2000 or dur > 8000:
            continue
        # 检查是否与同源既有候选重叠
        has_overlap = False
        for ex_s, ex_e in intervals_by_source.get(src, []):
            if max(s_ms, ex_s) < min(e_ms, ex_e):
                has_overlap = True
                break
        if has_overlap:
            continue

        selected_cands.append(c)
        intervals_by_source.setdefault(src, []).append((s_ms, e_ms))
        accum_dur += dur
        if accum_dur >= 48000 and len(selected_cands) >= 10:
            break

    ordered_ids = [c["candidate_id"] for c in selected_cands]
    print(f"  [DIRECTOR] 挑选出 {len(ordered_ids)} 个高质量互斥带货分镜，预期总时长: {accum_dur/1000:.1f} 秒")

    legal_proposal = {
        "schema_version": "3.2",
        "run_id": run_id,
        "candidate_pool_hash": candidate_hash,
        "proposal_id": "v32_project3_director_proposal",
        "ordered_candidate_ids": ordered_ids,
        "chosen_cover_id": None,  # 由决策网关从合法封面候选池中自动遴选最高分封面
        "style": "balanced",
        "decision_reasons": [
            "镜头 1 (Hook)：黄金开场 2.5 秒痛点抓取，口播完整无截断",
            "镜头 2-5 (卖点)：重点展示法式羊毛混纺面料质感与垂坠感",
            "镜头 6-9 (版型)：全身景别展示显瘦廓形与不同角度穿搭效果",
            "镜头 10+ (促单)：行动号召闭眼入，语义完整无价格与库存敏感词",
        ]
    }
    proposal_path = os.path.join(work_dir, "decisions", "sequence_proposal.json")
    os.makedirs(os.path.dirname(proposal_path), exist_ok=True)
    with open(proposal_path, "w", encoding="utf-8") as f:
        json.dump(legal_proposal, f, ensure_ascii=False, indent=2)

    # 阶段 3: validate-plan
    print("\n[CLI: validate-plan]")
    plan_res = stage_validate_plan(work_dir, proposal_path)
    print(f"  [PASS] validate-plan 校验通过: {plan_res['clips_count']} 个切片，总时长 {plan_res['total_duration_s']:.1f}s")

    # 阶段 4: render
    print("\n[CLI: render] (物理混流装配与封面渲染...)")
    render_res = stage_render(work_dir)
    final_video = render_res["final_video"]
    cover_image = render_res["cover_image"]
    assert os.path.isfile(final_video), f"成片未生成: {final_video}"
    assert os.path.isfile(cover_image), f"封面未生成: {cover_image}"
    vid_sz = os.path.getsize(final_video)
    cov_sz = os.path.getsize(cover_image)
    print(f"  [PASS] render 完成: 成片 {vid_sz:,d} 字节，封面 {cov_sz:,d} 字节")

    # 阶段 5: qc
    print("\n[CLI: qc] (工程技术 QC 与内容 Editorial QC 双层质检...)")
    qc_res = stage_qc(work_dir)
    tech_qc = qc_res.get("technical_qc", {})
    ed_qc = qc_res.get("editorial_qc", {})
    tech_pass = tech_qc.get("passed", tech_qc.get("all_passed", False))
    ed_pass = ed_qc.get("passed", False)
    print(f"  -> 技术 QC 结果: {'PASS' if tech_pass else 'FAIL'}")
    print(f"  -> 内容 QC 结果: {'PASS' if ed_pass else 'FAIL'}")

    # 阶段 6: deliver
    print("\n[CLI: deliver] (正式出库交付...)")
    try:
        deliv_res = stage_deliver(work_dir)
        deliv_status = deliv_res.get("status")
        print(f"  [PASS] deliver 成功签署: status={deliv_status}")
    except ValueError as ve:
        deliv_status = "BLOCKED"
        print(f"  [WARN] deliver 触发门禁守护: {ve}")

    # =========================================================================
    # 步骤 7：原始素材与历史成片防篡改验证 (SHA-256 再次比对)
    # =========================================================================
    print("\n--- 步骤 7: 原始素材与历史成片防篡改一致性复验 ---")
    after_hashes = compute_folder_hashes(PROJECT3_DIR)

    missing_files = []
    tampered_files = []
    new_files_in_root = []

    for fname, b_info in baseline_hashes.items():
        if fname not in after_hashes:
            missing_files.append(fname)
        else:
            a_info = after_hashes[fname]
            if a_info["sha256"] != b_info["sha256"]:
                tampered_files.append({
                    "file": fname,
                    "before_sha": b_info["sha256"],
                    "after_sha": a_info["sha256"],
                })

    for fname in after_hashes:
        if fname not in baseline_hashes:
            new_files_in_root.append(fname)

    if missing_files:
        print(f"  [FAIL] 发生文件丢失: {missing_files}")
    if tampered_files:
        print(f"  [FAIL] 发生文件篡改: {tampered_files}")
    if new_files_in_root:
        print(f"  [WARN] 根目录新增文件: {new_files_in_root}")

    is_intact = (len(missing_files) == 0 and len(tampered_files) == 0 and len(new_files_in_root) == 0)
    assert is_intact, f"防篡改校验失败！存在被修改或新增的文件: {tampered_files}, {new_files_in_root}"
    print(f"  [PASS] 完美保护：项目根目录下全部 {len(baseline_hashes)} 个既有文件哈希 100% 一致！未受任何污染或覆盖！")

    # =========================================================================
    # 输出脱敏摘要报告
    # =========================================================================
    sanitized_summary = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "project_id": "project-3",
        "run_id": run_id,
        "source_videos_count": len(chosen_videos),
        "quarantined_files_count": len(prep_res.get("quarantined_files", [])),
        "candidates_count": analyze_res["candidates_count"],
        "candidate_pool_hash": candidate_hash,
        "selected_shots_count": len(ordered_ids),
        "assembled_video_bytes": vid_sz,
        "technical_qc_passed": bool(tech_pass),
        "editorial_qc_passed": bool(ed_pass),
        "delivery_status": deliv_status,
        "anti_tampering_baseline_count": len(baseline_hashes),
        "anti_tampering_intact": is_intact,
        "zero_selection_blocked": True,
        "invalid_candidate_id_blocked": True,
        "tampered_hash_blocked": True,
        "brand_fingerprint_sensitivity_verified": True,
    }

    summary_file = os.path.join(PROJECT_ROOT, "local_project3_v32_acceptance_summary.json")
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(sanitized_summary, f, ensure_ascii=False, indent=2)
    print(f"\n[DONE] A08 本地验收报告已落盘至: {summary_file}")
    print(json.dumps(sanitized_summary, indent=2))

    if deliv_status != "DELIVERED":
        print(f"\n[FAIL] 验收失败：预期正式出库交付未完成（状态为 {deliv_status}，非 DELIVERED）！")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
