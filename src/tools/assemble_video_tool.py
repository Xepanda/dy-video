#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
assemble_video_tool.py - FFmpeg 音画物理强锁组装工具
===================================================
向 Agent 提供确定性的视频组装与渲染能力:
1. 接收由 Agent 决策生成的 plan.json 剪辑计划;
2. 自动原子清空旧切片目录, 杜绝历史缓存污染;
3. 采用 -ss/-t 双轨 PTS 绝对归零, 物理强锁视频与原声音频, 杜绝口型偏差;
4. 复合滤镜网络混流:
   - 毫秒精准 ASS 变色发光字幕烧录;
   - 0.0s~2.5s 极简黄金 Hook 悬浮卡片;
   - 片尾 2.5s 小黄车行动召唤 (CTA) 贴片;
   - 0.0s Sub-bass Boom (45~90Hz) 重音冲击波音频混音。
"""

import os
import sys
import json
import argparse
from typing import Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.core.video_assembler import VideoAssembler


def assemble_video_tool(
    plan_path: str,
    output_mp4_path: str,
    work_dir: str = None
) -> Tuple[str, float]:
    """根据剪辑计划 plan.json 执行确定性总装"""
    if not os.path.exists(plan_path):
        raise FileNotFoundError(f"剪辑计划文件不存在: {plan_path}")

    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)

    if not work_dir:
        project_id = plan.get("project_id", "demo")
        work_dir = os.path.join(PROJECT_ROOT, "data", "work_dirs", project_id)

    assembler = VideoAssembler(work_dir)
    out_mp4, duration = assembler.assemble_final_video(plan, output_mp4_path)
    print(f"[ASSEMBLE-TOOL] 成片总装完成: {out_mp4} (实测物理时长: {duration:.2f}s)")
    return out_mp4, duration


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FFmpeg 音画物理强锁组装工具")
    parser.add_argument("--plan", required=True, help="Agent 决策编译的 plan.json 路径")
    parser.add_argument("--output", required=True, help="最终导出 MP4 路径")
    parser.add_argument("--work-dir", default=None, help="临时工作区路径")
    args = parser.parse_args()

    assemble_video_tool(args.plan, args.output, args.work_dir)
