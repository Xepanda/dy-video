# -*- coding: utf-8 -*-
"""
stage_services.py - V3.3 Agent-First 阶段化媒体与门禁服务门面
=============================================================
原 1848 行单体服务已按职责拆分为 src/runtime/stages/ 包；
本文件作为 100% 向后兼容门面，保留全部公开函数与可 patch 依赖符号。
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 保留可被既有单测 patch("src.runtime.stage_services.<dep>") 拦截的依赖引用
from src.core.project_context import (  # noqa: F401
    ProjectPaths,
    InputManifest,
    compute_job_fingerprint,
    compute_file_sha256,
    compute_full_file_sha256,
    compute_file_fast_fingerprint,
    is_historical_output,
    sanitize_id,
    normalize_project_path,
)
from src.core.video_assembler import VideoAssembler  # noqa: F401
from src.tools.extract_cover_candidates import extract_cover_candidates, _load_constraints  # noqa: F401
from src.tools.analyze_story_segments import analyze_story_segments  # noqa: F401
from src.tools.detect_scenes import detect_scene_cuts  # noqa: F401
from src.tools.render_cover_tool import render_cover_with_meta  # noqa: F401
from src.qc.qc_auditor import QcAuditor  # noqa: F401
from src.qc.editorial_qc import EditorialQcAuditor  # noqa: F401
from src.qc.continuity_qc import ContinuityAuditor  # noqa: F401
from src.tools.desktop_spec_creator import SpecManager  # noqa: F401

from src.runtime.stages import (  # noqa: F401
    _log_event,
    _write_run_state,
    resolve_run_dir,
    compute_candidate_pool_hash,
    resolve_trusted_paths,
    resolve_and_verify_work_ref,
    stage_fetch_product,
    stage_prepare,
    stage_analyze,
    stage_inspect_candidate,
    stage_validate_plan,
    stage_render,
    stage_inspect_render,
    stage_qc,
    stage_deliver,
    stage_status,
)
