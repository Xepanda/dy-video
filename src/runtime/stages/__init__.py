# -*- coding: utf-8 -*-
"""
src.runtime.stages - V3.3 阶段化媒体与门禁服务包
"""

from src.runtime.stages.common import (
    _log_event,
    _write_run_state,
    resolve_run_dir,
    compute_candidate_pool_hash,
)
from src.runtime.stages.security import resolve_trusted_paths, resolve_and_verify_work_ref
from src.runtime.stages.prepare import stage_prepare
from src.runtime.stages.analyze import stage_analyze
from src.runtime.stages.validate_plan import stage_validate_plan
from src.runtime.stages.render import stage_render
from src.runtime.stages.qc_deliver import stage_qc, stage_deliver
from src.runtime.stages.status import stage_status
from src.runtime.stages.inspect import stage_inspect_candidate, stage_inspect_render
from src.runtime.stages.fetch_product import stage_fetch_product

__all__ = [
    "_log_event",
    "_write_run_state",
    "resolve_run_dir",
    "compute_candidate_pool_hash",
    "resolve_trusted_paths",
    "resolve_and_verify_work_ref",
    "stage_fetch_product",
    "stage_prepare",
    "stage_analyze",
    "stage_inspect_candidate",
    "stage_validate_plan",
    "stage_render",
    "stage_inspect_render",
    "stage_qc",
    "stage_deliver",
    "stage_status",
]
