#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
main_pipeline.py - 抖音带货直播自动化剪辑端到端总控入口
=========================================================
用法:
  python src/main_pipeline.py --spec job_spec.json
  
全自动化执行流程:
1. 解析任务契约 (job_spec.json)；
2. Faster-Whisper 全量语音毫秒打点与关键词提取；
3. CV 智能选帧器自动挑出露膝、居中、正脸无重影底图；
4. MIXCITY 风格参数化高定品牌封面渲染；
5. 动态背包剪辑规划 (黄金3秒 + 卖点背包 45~65s)；
6. FFmpeg 物理强锁视频切片、推镜、变色字幕与 Boom 混流；
7. 自动化量化质检门禁 (QC Auditor) 全量断言并生成报告。
"""

import os
import sys
import json
import argparse
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.core.asr_engine import AsrEngine
from src.core.asr_agent_verifier import AsrContextVerifier
from src.core.frame_picker import SmartFramePicker
from src.graphics.cover_renderer import MixCityCoverRenderer
from src.core.plan_compiler import PlanCompiler
from src.core.video_assembler import VideoAssembler
from src.qc.qc_auditor import QcAuditor


def run_pipeline(spec_path: str, skip_asr: bool = False) -> bool:
    start_time = time.time()
    spec_path = os.path.abspath(spec_path)
    if not os.path.exists(spec_path):
        raise FileNotFoundError(f"任务契约文件不存在: {spec_path}")

    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    project_id = spec.get("project_id", "demo_project")
    raw_video = os.path.abspath(spec["raw_video_path"])
    
    # 建立工程工作区与输出目录
    work_dir = os.path.join(PROJECT_ROOT, "data", "work_dirs", project_id)
    out_dir = os.path.join(PROJECT_ROOT, "output", project_id)
    os.makedirs(work_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    print(f"\n=======================================================")
    print(f"🎬 启动端到端自动化流水线: {project_id}")
    print(f"📁 原始视频素材: {raw_video}")
    print(f"📂 成果输出目录: {out_dir}")
    print(f"=======================================================\n")

    # -----------------------------------------------------------------
    # Step 1: Faster-Whisper ASR 转录打点 (支持自动缓存与 Prompt 引导)
    # -----------------------------------------------------------------
    asr_cache = os.path.join(work_dir, "whisper_segments.json")
    asr = AsrEngine()
    verifier = AsrContextVerifier(spec)
    prompt = verifier.build_initial_prompt()
    raw_segments = asr.transcribe_video(
        raw_video, cache_json_path=asr_cache, force_recompute=skip_asr, initial_prompt=prompt
    )

    # -----------------------------------------------------------------
    # Step 1.5: ASR 语境智能体质检验校 (结合商品/前后文消除同音错词与语病)
    # -----------------------------------------------------------------
    asr_report_path = os.path.join(out_dir, "asr_verification_report.json")
    verified_cache = os.path.join(work_dir, "whisper_segments_verified.json")
    segments, asr_report = verifier.verify_and_correct(
        raw_segments, report_output_path=asr_report_path
    )
    with open(verified_cache, "w", encoding="utf-8") as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)

    # -----------------------------------------------------------------
    # Step 2: CV 智能选帧 (露膝、清晰、正脸表情)
    # -----------------------------------------------------------------
    picker = SmartFramePicker()
    best_t, best_frame, frame_meta = picker.pick_best_cover_frame(
        raw_video, sample_step_sec=2.0, max_duration_scan=180.0
    )
    clean_frame_path = os.path.join(work_dir, "cover_clean_frame.jpg")
    import cv2
    cv2.imwrite(clean_frame_path, best_frame)
    print(f"[STEP 2] 最优封面底图锁定在 @{best_t:.1f}s, 已保存至: {clean_frame_path}")

    # -----------------------------------------------------------------
    # Step 3: MIXCITY 高定品牌封面参数化排版渲染
    # -----------------------------------------------------------------
    cover_renderer = MixCityCoverRenderer()
    final_cover_path = os.path.join(out_dir, f"{project_id}_cover.jpg")
    cover_renderer.render_cover(
        base_frame_path=clean_frame_path,
        brand_info=spec.get("brand", {}),
        product_info=spec.get("product", {}),
        output_path=final_cover_path
    )
    print(f"[STEP 3] MIXCITY 品牌封面已生成: {final_cover_path}")

    # -----------------------------------------------------------------
    # Step 4: 动态背包时长剪辑规划 (45~65s)
    # -----------------------------------------------------------------
    compiler = PlanCompiler(target_duration_min=48.0, target_duration_max=58.0)
    plan = compiler.compile_plan(raw_video, segments, spec)
    plan_path = os.path.join(work_dir, "plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    print(f"[STEP 4] 剪辑计划已编译 (预计 {plan['total_expected_duration']}s): {plan_path}")

    # -----------------------------------------------------------------
    # Step 5: FFmpeg 物理音画强锁组装与字幕烧录
    # -----------------------------------------------------------------
    assembler = VideoAssembler(work_dir)
    final_mp4_path = os.path.join(out_dir, f"{project_id}_final.mp4")
    out_mp4, actual_dur = assembler.assemble_final_video(plan, final_mp4_path)
    print(f"[STEP 5] 视频总装完成: {out_mp4} ({actual_dur:.2f}s)")

    # -----------------------------------------------------------------
    # Step 6: 自动化量化质检门禁 (QC Auditor)
    # -----------------------------------------------------------------
    auditor = QcAuditor()
    ass_path = os.path.join(work_dir, "subtitles.ass")
    qc_report_path = os.path.join(out_dir, "qc_report.json")
    passed = auditor.audit_final_deliverables(
        video_path=out_mp4,
        cover_path=final_cover_path,
        ass_path=ass_path,
        report_output_path=qc_report_path
    )

    elapsed = time.time() - start_time
    print(f"\n=======================================================")
    print(f"🏁 全流水线端到端执行完毕! 总耗时: {elapsed:.1f} 秒 ({elapsed / 60:.2f} 分钟)")
    print(f"🎬 最终成品: {out_mp4}")
    print(f"🖼️ 高清封面: {final_cover_path}")
    print(f"📋 质检报告: {qc_report_path}")
    print(f"=======================================================\n")
    return passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="抖音带货直播自动化剪辑流水线")
    parser.add_argument("--spec", default="job_spec.json", help="任务契约文件路径")
    parser.add_argument("--skip-asr", action="store_true", help="强制重新转录 ASR")
    args = parser.parse_args()

    run_pipeline(args.spec, skip_asr=args.skip_asr)
