# -*- coding: utf-8 -*-
"""
common.py - 阶段运行日志、状态文件、运行目录解析与候选池强锁哈希
"""

import os
import json
import time
import hashlib
from typing import Dict, Any, List, Optional

from src.core.project_context import normalize_project_path


def _log_event(work_dir: str, event_type: str, message: str, payload: Optional[Dict[str, Any]] = None) -> None:
    events_file = os.path.join(work_dir, "events.jsonl")
    event = {
        "timestamp": time.time(),
        "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "event": event_type,
        "message": message,
        "payload": payload or {},
    }
    try:
        with open(events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _write_run_state(
    work_dir: str,
    status: str,
    step: str,
    progress: float,
    message: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    state_file = os.path.join(work_dir, "run_state.json")
    state = {
        "status": status,
        "current_step": step,
        "progress": round(progress, 2),
        "message": message,
        "updated_at": time.time(),
        "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if extra:
        state.update(extra)
    tmp_file = state_file + ".tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, state_file)
    except Exception:
        pass


def resolve_run_dir(run_arg: str) -> str:
    """从给定的路径参数解析出具体的 work/<run_id> 目录。"""
    p = normalize_project_path(run_arg)
    if os.path.isfile(p):
        if os.path.basename(p) in ("run_context.json", "run_state.json", "spec_snapshot.json"):
            return os.path.dirname(p)
        p = os.path.dirname(p)
    if os.path.isfile(os.path.join(p, "run_context.json")):
        return p
    work_base = os.path.join(p, "work")
    if os.path.isdir(work_base):
        subdirs = [os.path.join(work_base, d) for d in os.listdir(work_base) if os.path.isdir(os.path.join(work_base, d))]
        if subdirs:
            subdirs.sort(key=lambda x: os.path.getmtime(x), reverse=True)
            return subdirs[0]
    return p


def compute_candidate_pool_hash(candidates: List[Dict[str, Any]]) -> str:
    """计算候选池强锁哈希 (包含素材源路径、文件完整哈希、时间区间、商品关系、合格性与证据)。"""
    hash_payload = [
        {
            "id": c.get("candidate_id"),
            "source_id": c.get("source_id"),
            "source_file": os.path.normpath(str(c.get("source_file", ""))) if c.get("source_file") else "",
            "source_file_sha256": str(c.get("source_file_sha256", "")),
            "st": c.get("source_start_ms"),
            "en": c.get("source_end_ms"),
            "rel": c.get("product_relation"),
            "eligible": c.get("eligible", False),
            "text": c.get("text", ""),
            "sp_refs": c.get("selling_point_evidence_refs") or [],
            "frame_ids": c.get("frame_ids") or [],
        }
        for c in candidates
    ]
    return hashlib.sha256(json.dumps(hash_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]
