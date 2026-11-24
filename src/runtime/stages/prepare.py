# -*- coding: utf-8 -*-
"""
prepare.py - 阶段 1：项目初始化、素材白名单登记、不可变快照与任务指纹
"""

import os
import json
import time
from typing import Dict, Any, Optional

from src.core.project_context import ProjectPaths, InputManifest, compute_job_fingerprint
from src.core.product_profile import build_product_profile, save_product_profile, ProductProfileError
from src.core.path_resolver import resolve_path
from src.core.category_profile import load_category_profile
from src.tools.desktop_spec_creator import SpecManager
from src.runtime.stages._compat import resolve_dep
from src.runtime.stages.common import _log_event, _write_run_state


def stage_prepare(spec_path: str, run_id: Optional[str] = None, resume: bool = False) -> Dict[str, Any]:
    spec_abs = os.path.abspath(spec_path)
    if not os.path.isfile(spec_abs):
        raise FileNotFoundError(f"任务契约文件不存在: {spec_abs}")

    with open(spec_abs, "r", encoding="utf-8") as f:
        spec = json.load(f)

    mgr_cls = resolve_dep("SpecManager", SpecManager)
    mgr = mgr_cls()
    errors = mgr.validate(spec)
    if errors:
        raise ValueError("job_spec.json 校验未通过:\n" + "\n".join(f"- {e}" for e in errors))

    paths = ProjectPaths(project_root=spec_abs, run_id=run_id)
    fp_fn = resolve_dep("compute_job_fingerprint", compute_job_fingerprint)

    has_existing_work = os.path.isdir(paths.work_dir) and os.path.isfile(os.path.join(paths.work_dir, "run_context.json"))
    has_existing_output = os.path.isdir(paths.output_dir) and any(
        os.path.isfile(os.path.join(paths.output_dir, f)) for f in os.listdir(paths.output_dir)
    )
    if has_existing_work or has_existing_output:
        if not resume:
            raise FileExistsError(
                f"运行目录已存在历史运行产物: {paths.work_dir}。为保护历史交付物不被覆盖，禁止直接创建同名运行。如需恢复请指定 --resume 或更换 run_id"
            )
        ctx_file = os.path.join(paths.work_dir, "run_context.json")
        if not os.path.isfile(ctx_file):
            raise FileNotFoundError(f"无法恢复运行: 在 {paths.work_dir} 未找到历史 run_context.json")
        with open(ctx_file, "r", encoding="utf-8") as f:
            old_ctx = json.load(f)
        manifest_mgr = InputManifest(paths)
        manifest = manifest_mgr.scan_and_resolve(explicit_video_paths=spec.get("raw_video_paths"))
        spec_fingerprint = fp_fn(spec, manifest)
        if old_ctx.get("spec_fingerprint") != spec_fingerprint:
            raise ValueError(
                f"恢复运行校验失败: 当前配置指纹 ({spec_fingerprint}) 与原运行指纹 ({old_ctx.get('spec_fingerprint')}) 不匹配，严禁跨配置恢复"
            )
        return {
            "schema_version": "3.3",
            "status": "RESUMED",
            "run_id": paths.run_id,
            "project_id": paths.project_id,
            "work_dir": paths.work_dir,
            "output_dir": paths.output_dir,
            "spec_fingerprint": spec_fingerprint,
            "source_videos": [s["path"] for s in manifest.get("source_videos", [])],
            "quarantined_files": manifest.get("quarantined_files", []),
            "next_step": f"python -m src.cli analyze --run {paths.work_dir}",
        }

    paths.ensure_runtime_dirs()

    manifest_mgr = InputManifest(paths)
    explicit_vids = spec.get("raw_video_paths")
    if explicit_vids is not None:
        manifest = manifest_mgr.scan_and_resolve(explicit_video_paths=explicit_vids)
    elif spec.get("raw_video_path"):
        rvp = spec["raw_video_path"]
        resolved_rvp = resolve_path(rvp, paths.materials_dir) or os.path.abspath(rvp)
        if os.path.isdir(resolved_rvp):
            manifest = manifest_mgr.scan_and_resolve()
        elif os.path.isfile(resolved_rvp):
            manifest = manifest_mgr.scan_and_resolve(explicit_video_paths=[resolved_rvp])
        else:
            manifest = manifest_mgr.scan_and_resolve()
    else:
        manifest = manifest_mgr.scan_and_resolve()

    source_videos = manifest.get("source_videos", [])
    if not source_videos:
        fail_msg = "缺少有效原始视频素材（用户确认素材列表为空或所有候选均被确认为旧成片被隔离）"
        _write_run_state(paths.work_dir, "NEEDS_INPUT", "prepare", 0.0, fail_msg)
        raise ValueError(fail_msg)

    spec_fingerprint = fp_fn(spec, manifest)
    manifest_mgr.save_manifest(manifest)

    spec_snapshot_path = os.path.join(paths.work_dir, "spec_snapshot.json")
    with open(spec_snapshot_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)

    cat_profile = load_category_profile(spec)

    try:
        profile = build_product_profile(spec, spec_dir=paths.work_dir)
        profile["domain_profile"] = cat_profile.get("profile_id", "womenswear")
        profile_path = os.path.join(paths.work_dir, "product_profile.json")
        save_product_profile(profile, profile_path)
    except ProductProfileError as pe:
        _write_run_state(paths.work_dir, "NEEDS_INPUT", "prepare", 0.05, f"商品档案校验阻断: {pe}")
        raise ValueError(f"商品档案校验阻断: {pe}")

    context_data = {
        "schema_version": "3.3",
        "tool_version": "3.3",
        "run_id": paths.run_id,
        "project_id": paths.project_id,
        "project_root": paths.project_root,
        "materials_dir": paths.materials_dir,
        "work_dir": paths.work_dir,
        "output_dir": paths.output_dir,
        "spec_path": spec_abs,
        "spec_fingerprint": spec_fingerprint,
        "domain_profile": cat_profile.get("profile_id", "womenswear"),
        "source_count": len(source_videos),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "PREPARED",
    }
    context_path = os.path.join(paths.work_dir, "run_context.json")
    with open(context_path, "w", encoding="utf-8") as f:
        json.dump(context_data, f, ensure_ascii=False, indent=2)

    _log_event(paths.work_dir, "PREPARE_COMPLETED", f"项目初始化完成，登记 {len(source_videos)} 段有效源视频", context_data)
    _write_run_state(paths.work_dir, "PREPARED", "prepare", 0.1, "项目环境与素材白名单就绪，等待执行分析")

    return {
        "schema_version": "3.3",
        "status": "PREPARED",
        "run_id": paths.run_id,
        "project_id": paths.project_id,
        "work_dir": paths.work_dir,
        "output_dir": paths.output_dir,
        "spec_fingerprint": spec_fingerprint,
        "domain_profile": cat_profile.get("profile_id", "womenswear"),
        "source_videos": [s["path"] for s in source_videos],
        "quarantined_files": manifest.get("quarantined_files", []),
        "product_profile": profile["name"],
        "next_step": f"python -m src.cli analyze --run {paths.work_dir}",
    }
