#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
main_pipeline.py - 抖音带货直播自动化剪辑端到端总控入口
=========================================================
用法:
  python src/main_pipeline.py --spec job_spec.json
  python src/main_pipeline.py --spec job_spec.json --cover-id cand_01
  python src/main_pipeline.py --spec job_spec.json --plan my_plan.json

执行架构 (Agent 决策中枢 + 确定性工具链):
1. extract_cover_candidates: 确定性提取严格正面、统一固定比例的 Top-N 候选底图;
2. analyze_story_segments: ASR 语境智能体质检并分类候选分镜池;
3. Agent 决策介入: 审视并决策最优封面 (主播正面/神态生动) 与故事编排 (纯产品介绍/零价格);
4. render_cover_tool: 高定 3D 浮雕与动感斜体封面渲染;
5. assemble_video_tool: FFmpeg 物理音画强锁组装成片;
6. qc_auditor: 自动化量化验收门禁。
"""

import os
import sys
import argparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.agent.agent_director import AgentDirector


def run_pipeline(spec_path: str, cover_id: str = None, plan_path: str = None,
                 timeline_path: str = None) -> bool:
    director = AgentDirector(spec_path)
    res = director.run_director_workflow(
        chosen_cover_id=cover_id,
        custom_plan_path=plan_path,
        edited_timeline_path=timeline_path
    )
    return res.get("qc_passed", False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="抖音带货直播自动化剪辑流水线 (Agent 决策中枢)")
    parser.add_argument("--spec", default="job_spec.json", help="任务契约文件路径")
    parser.add_argument("--cover-id", default=None, help="干预指定封面候选 ID (如 cand_01)")
    parser.add_argument("--plan", default=None, help="干预指定自定义剪辑计划路径")
    parser.add_argument("--timeline", default=None,
                        help="回注人工精修后的时间线 JSON（OpenChatCut 导出，2026-10-24 接入生产）")
    args = parser.parse_args()

    run_pipeline(args.spec, cover_id=args.cover_id, plan_path=args.plan,
                 timeline_path=args.timeline)
