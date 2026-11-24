# -*- coding: utf-8 -*-
"""
status.py - 阶段 7：只读任务状态与事件日志查询
"""

import os
import json
from typing import Dict, Any

from src.runtime.stages.common import resolve_run_dir


def stage_status(run_arg: str) -> Dict[str, Any]:
    work_dir = resolve_run_dir(run_arg)
    context_path = os.path.join(work_dir, "run_context.json")
    if not os.path.isdir(work_dir) or not os.path.isfile(context_path):
        raise FileNotFoundError(f"未找到有效运行目录或 run_context.json: {work_dir}")
    state_path = os.path.join(work_dir, "run_state.json")

    context = {}
    if os.path.isfile(context_path):
        with open(context_path, "r", encoding="utf-8") as f:
            context = json.load(f)

    state = {}
    if os.path.isfile(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

    events = []
    events_path = os.path.join(work_dir, "events.jsonl")
    if os.path.isfile(events_path):
        try:
            with open(events_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        events.append(json.loads(line))
        except Exception:
            pass

    return {
        "schema_version": "3.3",
        "work_dir": work_dir,
        "run_id": context.get("run_id"),
        "project_id": context.get("project_id"),
        "status": state.get("status", "UNKNOWN"),
        "current_step": state.get("current_step", "unknown"),
        "progress": state.get("progress", 0.0),
        "message": state.get("message", ""),
        "last_updated": state.get("updated_at_str", ""),
        "recent_events": events[-5:],
    }
