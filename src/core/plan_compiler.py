#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
plan_compiler.py - 兼容性入口（已重构）
=======================================
历史版本在此文件中硬编码了 demo 视频的分镜时间戳，换素材即失效（技术评审 G1）。
现全面委托给 src/tools/build_plan.py 的泛化实现；本文件仅保留旧导入路径兼容。

新代码请直接使用：
    from src.tools.build_plan import build_plan
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.tools.build_plan import build_plan, build_blocks, is_blacklisted  # noqa: F401


class PlanCompiler:
    """兼容壳：旧接口签名保留，内部委托 build_plan。"""

    def __init__(self, target_duration_min: float = 45.0, target_duration_max: float = 55.0):
        self.target_min = target_duration_min
        self.target_max = target_duration_max

    def compile_plan(self, raw_video_path, segments, spec_data):
        constraints = {
            "video": {
                "duration_range": [self.target_min, self.target_max],
                "funnel": {
                    "hook": {"min": 3, "max": 8},
                    "fabric": {"min": 8, "max": 20},
                    "fit": {"min": 8, "max": 15},
                    "styling": {"min": 15, "max": 30},
                },
            }
        }
        return build_plan(raw_video_path, segments, spec_data, constraints)


if __name__ == "__main__":
    print("plan_compiler 已重构为 build_plan 兼容壳；请使用 src/tools/build_plan.py")
