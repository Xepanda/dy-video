# -*- coding: utf-8 -*-
"""
security.py - 可信项目路径推导、输出目录隔离与工作区边界校验
"""

import os
import json
from pathlib import Path
from typing import Dict, Any, Optional

from src.core.project_context import ProjectPaths, normalize_project_path


def resolve_trusted_paths(work_dir: str, context: Optional[Dict[str, Any]] = None) -> ProjectPaths:
    """根据工作区真实路径与规范结构重新推导并校验可信项目路径。
    防范 run_context.json 中的 output_dir 被外部篡改或指向素材目录/外部目录/历史交付目录。
    """
    norm_work = normalize_project_path(work_dir)
    if context is None:
        ctx_path = os.path.join(norm_work, "run_context.json")
        if os.path.isfile(ctx_path):
            with open(ctx_path, "r", encoding="utf-8") as f:
                context = json.load(f)
        else:
            context = {}

    run_id = context.get("run_id") or os.path.basename(norm_work)
    parts = Path(norm_work).parts
    if len(parts) >= 3 and parts[-2] == "work":
        derived_root = str(Path(*parts[:-2]))
    else:
        derived_root = context.get("project_root") or os.path.dirname(norm_work)

    paths = ProjectPaths(
        project_root=derived_root,
        run_id=run_id,
        materials_dir=context.get("materials_dir"),
    )

    real_trusted_root = os.path.realpath(paths.project_root)
    real_trusted_out = os.path.realpath(paths.output_dir)
    real_mat_dir = os.path.realpath(paths.materials_dir) if paths.materials_dir else ""

    if os.path.normcase(real_trusted_out) == os.path.normcase(real_trusted_root):
        raise ValueError(f"安全阻断: 输出目录不能为项目根目录 ({paths.output_dir})")

    # 仅当 materials_dir 是独立于 project_root 的子目录（如 <root>/素材）时，禁止 output_dir 落入其下；
    # 若为扁平项目（materials_dir == project_root），允许 <root>/output/<run_id>，仅禁止 output_dir 等于 materials_dir。
    if real_mat_dir:
        if os.path.normcase(real_trusted_out) == os.path.normcase(real_mat_dir):
            raise ValueError(
                f"安全阻断: 输出目录 ({paths.output_dir}) 与原始素材目录 ({paths.materials_dir}) 重合，存在污染素材风险"
            )
        if os.path.normcase(real_mat_dir) != os.path.normcase(real_trusted_root):
            try:
                is_inside_mat = os.path.commonpath([real_mat_dir, real_trusted_out]) == real_mat_dir
            except ValueError:
                is_inside_mat = False
            if is_inside_mat:
                raise ValueError(
                    f"安全阻断: 输出目录 ({paths.output_dir}) 位于原始素材目录 ({paths.materials_dir}) 下，存在污染素材风险"
                )

    try:
        if os.path.commonpath([real_trusted_root, real_trusted_out]) != real_trusted_root:
            raise ValueError(
                f"安全阻断: 输出目录 ({paths.output_dir}) 越界脱离项目根目录 ({paths.project_root})"
            )
    except ValueError as ve:
        if "安全阻断" in str(ve):
            raise
        raise ValueError(
            f"安全阻断: 输出目录 ({paths.output_dir}) 与项目根目录不在同一驱动器或文件系统"
        )

    ctx_out = context.get("output_dir")
    if ctx_out:
        real_ctx_out = os.path.realpath(normalize_project_path(ctx_out))
        if os.path.normcase(real_ctx_out) != os.path.normcase(real_trusted_out):
            raise ValueError(
                f"安全阻断: run_context.json 中的 output_dir ({ctx_out}) "
                f"与可信项目推导路径 ({paths.output_dir}) 不匹配，存在篡改或越界风险"
            )
        if real_mat_dir:
            if os.path.normcase(real_ctx_out) == os.path.normcase(real_mat_dir):
                raise ValueError(f"安全阻断: run_context.json 中的 output_dir 指向素材目录 ({ctx_out})")
            if os.path.normcase(real_mat_dir) != os.path.normcase(real_trusted_root):
                try:
                    ctx_inside_mat = os.path.commonpath([real_mat_dir, real_ctx_out]) == real_mat_dir
                except ValueError:
                    ctx_inside_mat = False
                if ctx_inside_mat:
                    raise ValueError(f"安全阻断: run_context.json 中的 output_dir 指向素材目录 ({ctx_out})")

    return paths


def resolve_and_verify_work_ref(work_dir: str, ref_path: str) -> Optional[str]:
    """解析并校验帧/拼图证据路径必须严格落在当前 work_dir 内且文件存在（防路径穿越）。

    支持带网格格号后缀，例如 'analysis/storyboards/sheet_01.jpg#04'。
    """
    if not ref_path or not isinstance(ref_path, str):
        return None
    raw_file = ref_path.split("#", 1)[0].strip()
    if not raw_file:
        return None
    norm_work = os.path.realpath(normalize_project_path(work_dir))
    cand_p = normalize_project_path(raw_file)
    if not os.path.isabs(cand_p):
        cand_p = os.path.join(norm_work, cand_p)
    real_cand = os.path.realpath(cand_p)
    try:
        if os.path.commonpath([norm_work, real_cand]) != norm_work:
            return None
    except ValueError:
        return None
    if not os.path.isfile(real_cand) or os.path.getsize(real_cand) == 0:
        return None
    return real_cand
