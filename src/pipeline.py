#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pipeline.py - 兼容性入口重定向
指向当前的 Agent 驱动工业级端到端流水线 main_pipeline.py
"""

import sys
from src.main_pipeline import run_pipeline

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="抖音带货直播自动化剪辑流水线 (兼容性入口)")
    parser.add_argument("--spec", default="job_spec.json", help="任务契约文件路径")
    parser.add_argument("--cover-id", default=None, help="干预指定封面候选 ID")
    parser.add_argument("--plan", default=None, help="干预指定自定义剪辑计划路径")
    args = parser.parse_args()

    run_pipeline(args.spec, cover_id=args.cover_id, plan_path=args.plan)
