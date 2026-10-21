#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
stage_cache.py - 阶段产物缓存原语（B13）
=======================================
缓存键设计（契约 B1：sha256(输入文件 + 参数 + constraints 版本)）：

  pipeline_key = sha256(
      原始视频文件内容
      + job_spec 文件内容
      + constraints.yaml 文件内容
      + [可选] 计划覆盖文件内容（--plan 干预时）
  )

阶段产物（scenes / cover_candidates / story / plan / final_video / qc）均以
`<stage>_<key[:16]>.json`（视频为 .mp4）落盘 work_dir，载荷内嵌完整 key。
加载时校验 key 与当前输入一致，不一致则重算——保证"换素材/改参数不串味"。

断点续跑：pipeline 中断后重跑，各阶段命中缓存即跳过重算；
批量运行器（batch_runner）据此实现 10 条/晚 的批量能力。
"""

import os
import sys
import json
import hashlib
from typing import Dict, Any, Optional, List

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STAGES = ("scenes", "cover_candidates", "story", "plan", "final_video", "qc")

# 核心代码指纹文件：内容变更即全阶段缓存失效（代码-缓存一致性）
_CODE_FINGERPRINT_FILES = [
    "src/core/video_assembler.py",
    "src/tools/extract_cover_candidates.py",
    "src/tools/build_plan.py",
    "src/tools/detect_scenes.py",
    "src/tools/analyze_story_segments.py",
    "src/core/asr_engine.py",
    "src/core/asr_agent_verifier.py",
    "src/qc/qc_auditor.py",
    "src/graphics/cover_renderer.py",
    "contracts/constraints.yaml",
]


def _sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def compute_pipeline_key(
    video_path,
    spec_path: str,
    constraints_path: str = None,
    extra_files: Optional[List[str]] = None,
) -> str:
    """计算流水线缓存键：sha256(视频 + spec + constraints + 额外文件 + 核心代码指纹)。

    video_path 可以是单个路径，也可以是路径列表（多源素材）：全部源文件内容
    均参与哈希，保证任一源变更都会使缓存失效（2026-10-21 多源选段需求）。

    代码指纹：核心管线模块内容哈希——代码变更（如装配器响度链修复）
    自动使全部阶段缓存失效，避免"输入没变但行为变了"的陈旧缓存命中。
    """
    constraints_path = constraints_path or os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
    videos = [video_path] if isinstance(video_path, str) else list(video_path)
    h = hashlib.sha256()
    h.update(b"dy-live-pipeline-key-v3")
    for p in videos + [spec_path, constraints_path] + list(extra_files or []):
        if p and os.path.exists(p):
            h.update(os.path.basename(p).encode("utf-8"))
            h.update(_sha256_file(p).encode("ascii"))
        else:
            h.update(b"<missing>")
            h.update(str(p).encode("utf-8"))
    # 核心代码指纹
    for rel in _CODE_FINGERPRINT_FILES:
        p = os.path.join(PROJECT_ROOT, rel)
        if os.path.exists(p):
            h.update(rel.encode("utf-8"))
            h.update(_sha256_file(p).encode("ascii"))
    return h.hexdigest()


def _stage_path(work_dir: str, stage: str, key: str, ext: str = "json") -> str:
    return os.path.join(work_dir, f"{stage}_{key[:16]}.{ext}")


def load_stage(work_dir: str, stage: str, key: str) -> Optional[Dict[str, Any]]:
    """加载阶段缓存；key 不匹配或文件缺失返回 None。"""
    path = _stage_path(work_dir, stage, key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if payload.get("cache_key") != key:
        return None  # 输入已变更，缓存失效
    return payload.get("data")


def save_stage(work_dir: str, stage: str, key: str, data: Dict[str, Any]) -> str:
    """保存阶段缓存（载荷内嵌完整 key）。"""
    os.makedirs(work_dir, exist_ok=True)
    path = _stage_path(work_dir, stage, key)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"cache_key": key, "stage": stage, "data": data},
                  f, ensure_ascii=False, indent=2)
    return path


def load_stage_video(work_dir: str, key: str) -> Optional[str]:
    """加载 final_video 阶段缓存；返回成片路径（校验存在性与 key）。"""
    path = _stage_path(work_dir, "final_video", key, ext="json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if payload.get("cache_key") != key:
        return None
    video = (payload.get("data") or {}).get("video_path", "")
    if video and os.path.exists(video):
        return video
    return None


def save_stage_video(work_dir: str, key: str, video_path: str, duration: float) -> str:
    os.makedirs(work_dir, exist_ok=True)
    path = _stage_path(work_dir, "final_video", key, ext="json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"cache_key": key, "stage": "final_video",
                   "data": {"video_path": video_path, "duration": duration}},
                  f, ensure_ascii=False, indent=2)
    return path


def gc_old_stage_files(work_dir: str, keep_key: str) -> int:
    """清理同阶段的历史缓存文件（仅保留 keep_key 对应的），返回清理数量。"""
    removed = 0
    for stage in STAGES:
        prefix = f"{stage}_"
        for name in os.listdir(work_dir) if os.path.isdir(work_dir) else []:
            if name.startswith(prefix) and f"_{keep_key[:16]}." not in name:
                try:
                    os.remove(os.path.join(work_dir, name))
                    removed += 1
                except OSError:
                    pass
    return removed
