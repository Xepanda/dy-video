# -*- coding: utf-8 -*-
"""
render.py - 阶段 4：物理混流装配、封面渲染与不可变渲染清单签发
"""

import os
import json
import time
import uuid
import hashlib
from typing import Dict, Any

from src.core.project_context import (
    compute_file_sha256,
    compute_full_file_sha256,
    is_historical_output,
    normalize_project_path,
)
from src.core.video_assembler import VideoAssembler
from src.tools.render_cover_tool import render_cover_with_meta
from src.runtime.stages._compat import resolve_dep
from src.runtime.stages.common import _log_event, _write_run_state, resolve_run_dir
from src.runtime.stages.security import resolve_trusted_paths


def stage_render(run_arg: str) -> Dict[str, Any]:
    work_dir = resolve_run_dir(run_arg)
    context_path = os.path.join(work_dir, "run_context.json")
    if not os.path.isfile(context_path):
        raise FileNotFoundError(f"未找到 run_context.json: {context_path}")

    with open(context_path, "r", encoding="utf-8") as f:
        context = json.load(f)

    paths = resolve_trusted_paths(work_dir, context)
    output_dir = paths.output_dir

    deliv_manifest_path = os.path.join(output_dir, "delivery_manifest.json")
    if os.path.isfile(deliv_manifest_path):
        raise RuntimeError(f"当前运行 ({context.get('run_id')}) 已完成正式交付签署，禁止重新渲染覆盖已交付成片！如需重新制作请创建新 run_id")

    plan_path = os.path.join(work_dir, "validated_plan.json")
    if not os.path.isfile(plan_path):
        raise FileNotFoundError(f"未找到已验证的计划文件: {plan_path}，必须先通过 validate-plan 校验")

    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)

    audit_path = os.path.join(work_dir, "plan_audit.json")
    if not os.path.isfile(audit_path):
        raise ValueError(f"未找到计划审计报告: {audit_path}，必须先通过 validate-plan 校验")
    with open(audit_path, "r", encoding="utf-8") as af:
        audit_rep = json.load(af)
    if not (audit_rep.get("passed") and audit_rep.get("audit_status") == "APPROVED"):
        raise ValueError("分镜计划未通过质量门禁审计 (必须处于 APPROVED 状态且 passed=True)")
    if audit_rep.get("run_id") and audit_rep.get("run_id") != context.get("run_id"):
        raise ValueError(f"分镜计划审计报告 run_id ({audit_rep.get('run_id')}) 与当前运行 ({context.get('run_id')}) 不符")
    if plan.get("run_id") and plan.get("run_id") != context.get("run_id"):
        raise ValueError(f"执行计划 run_id ({plan.get('run_id')}) 与当前运行 ({context.get('run_id')}) 不符")

    plan_to_hash = {k: v for k, v in plan.items() if k != "validated_plan_hash"}
    plan_bytes = json.dumps(plan_to_hash, sort_keys=True, ensure_ascii=False).encode("utf-8")
    actual_plan_hash = hashlib.sha256(plan_bytes).hexdigest()
    expected_plan_hash = audit_rep.get("validated_plan_hash")
    if not expected_plan_hash or actual_plan_hash != expected_plan_hash:
        raise ValueError("分镜计划完整性校验失败: 当前计划哈希与审计签名不一致，禁止渲染未经审计的修改计划")

    spec_path = os.path.join(work_dir, "spec_snapshot.json")
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    os.makedirs(output_dir, exist_ok=True)

    clips = plan.get("clips", [])
    if not clips:
        raise RuntimeError("分镜计划 clips 列表为空，无法执行渲染")

    for clip in clips:
        clip_src = clip.get("source_file")
        if not clip_src or not os.path.isfile(clip_src):
            raise RuntimeError(f"渲染素材源文件不存在: {clip_src}")
        is_hist, hist_reason = is_historical_output(clip_src, context.get("project_id", ""), paths.project_root)
        if is_hist:
            raise RuntimeError(f"渲染使用了历史成片作为输入素材: {clip_src} ({hist_reason})")

    manifest_path = os.path.join(work_dir, "input_manifest.json")
    if not os.path.isfile(manifest_path):
        raise RuntimeError("未找到输入清单 input_manifest.json，严禁无清单渲染！")
    try:
        with open(manifest_path, "r", encoding="utf-8") as mf:
            manifest_data = json.load(mf)
        if not isinstance(manifest_data, dict):
            raise ValueError("清单格式非字典")
    except Exception as e:
        raise RuntimeError(f"输入清单 input_manifest.json 解析失败: {e}")

    source_videos = manifest_data.get("source_videos", [])
    if not source_videos:
        raise RuntimeError("输入清单 input_manifest.json 中有效源视频列表为空，拒绝渲染")

    valid_source_paths = set()
    valid_sources_by_id = {}
    for s in source_videos:
        sid = s.get("source_id")
        sp = s.get("path")
        sha = s.get("sha256")
        if not sid or not sp or not sha or len(sha) != 64:
            raise RuntimeError(f"输入清单包含不合规素材记录 (缺少 source_id、path 或 64位完整 SHA-256): {s}")
        valid_sources_by_id[sid] = s
        valid_source_paths.add(os.path.normcase(os.path.realpath(normalize_project_path(sp))))

    checked_raw_files = {}
    for clip in clips:
        clip_src = clip.get("source_file")
        norm_clip_src = os.path.normcase(os.path.realpath(normalize_project_path(clip_src)))
        if norm_clip_src not in valid_source_paths:
            raise RuntimeError(f"渲染使用了非白名单素材源文件: {clip_src}")
        clip_sid = clip.get("source_id")
        if not clip_sid or clip_sid not in valid_sources_by_id:
            raise RuntimeError(f"渲染使用了非白名单素材 source_id: {clip_sid}")
        expected_p = os.path.normcase(os.path.realpath(normalize_project_path(valid_sources_by_id[clip_sid].get("path", ""))))
        if norm_clip_src != expected_p:
            raise RuntimeError(f"渲染分镜素材路径与 source_id ({clip_sid}) 白名单绑定不匹配: {clip_src}")

        expected_sha = valid_sources_by_id[clip_sid].get("sha256")
        clip_sha = clip.get("source_file_sha256")
        if expected_sha and clip_sha and clip_sha != expected_sha:
            raise RuntimeError(f"渲染分镜素材哈希与白名单绑定不匹配: {clip_sha} != {expected_sha}")

        if norm_clip_src not in checked_raw_files:
            disk_sha = compute_full_file_sha256(clip_src)
            checked_raw_files[norm_clip_src] = disk_sha
        else:
            disk_sha = checked_raw_files[norm_clip_src]

        if disk_sha != expected_sha:
            raise RuntimeError(
                f"渲染前素材完整性校验失败: 磁盘源文件 '{clip_src}' 的实际 SHA-256 ({disk_sha}) "
                f"与输入清单记录 ({expected_sha}) 不匹配！素材可能在中途被篡改或替换。"
            )
        if clip_sha and clip_sha != disk_sha:
            raise RuntimeError(
                f"渲染分镜素材哈希与磁盘文件实际哈希不一致: {clip_sha} != {disk_sha}"
            )

    for old_artifact in ("technical_qc.json", "editorial_qc.json", "continuity_review.json", "render_manifest.json", "seams_review.jpg"):
        old_art_p = os.path.join(output_dir, old_artifact)
        if os.path.isfile(old_art_p):
            try:
                os.remove(old_art_p)
            except Exception:
                pass
    work_rm_old = os.path.join(work_dir, "render_manifest.json")
    if os.path.isfile(work_rm_old):
        try:
            os.remove(work_rm_old)
        except Exception:
            pass

    _write_run_state(work_dir, "RENDERING", "render", 0.70, "启动 FFmpeg 音画物理强锁渲染...")
    _log_event(work_dir, "RENDER_START", "开始视频物理合成与封面生成")

    render_cover_fn = resolve_dep("render_cover_with_meta", render_cover_with_meta)
    assembler_cls = resolve_dep("VideoAssembler", VideoAssembler)

    final_cover_path = os.path.join(output_dir, "cover.jpg")
    cover_meta_path = os.path.join(output_dir, "cover_meta.json")
    target_cov_id = plan.get("chosen_cover_id")
    cover_info = plan.get("selected_cover") or plan.get("cover") or {}
    cov_cand_file = os.path.join(work_dir, "analysis", "cover_candidates.json")

    found_by_id = None
    if target_cov_id and os.path.isfile(cov_cand_file):
        try:
            with open(cov_cand_file, "r", encoding="utf-8") as cf:
                cc_list = json.load(cf)
            for c in cc_list:
                if c.get("id") == target_cov_id and os.path.isfile(c.get("image_path", "")):
                    found_by_id = c
                    break
        except Exception:
            pass

    if found_by_id:
        cover_info = found_by_id
        cover_source_img = found_by_id.get("image_path")
    else:
        cover_source_img = cover_info.get("image_path")
        if not cover_source_img or not os.path.isfile(cover_source_img):
            if os.path.isfile(cov_cand_file):
                try:
                    with open(cov_cand_file, "r", encoding="utf-8") as cf:
                        cc_list = json.load(cf)
                    if cc_list and os.path.isfile(cc_list[0].get("image_path", "")):
                        cover_info = cc_list[0]
                        cover_source_img = cc_list[0]["image_path"]
                except Exception:
                    pass

    if cover_source_img and os.path.isfile(cover_source_img):
        render_cover_fn(cover_source_img, spec, final_cover_path, cover_meta_path)
    else:
        from PIL import Image
        Image.new("RGB", (1080, 1920), color=(20, 20, 20)).save(final_cover_path)

    tmp_final_video = os.path.join(output_dir, f"temp_final_{uuid.uuid4().hex[:6]}.mp4")
    final_video = os.path.join(output_dir, "final.mp4")

    if os.path.exists(tmp_final_video):
        try:
            os.remove(tmp_final_video)
        except Exception:
            pass

    assembler = assembler_cls(work_dir=work_dir)
    try:
        assembler.assemble_final_video(plan, tmp_final_video)
    except Exception as e:
        if os.path.exists(tmp_final_video):
            try:
                os.remove(tmp_final_video)
            except Exception:
                pass
        _write_run_state(work_dir, "FAILED", "render", 0.75, f"视频渲染失败: {e}")
        raise RuntimeError(f"FFmpeg 渲染合成失败: {e}")

    if not os.path.isfile(tmp_final_video) or os.path.getsize(tmp_final_video) == 0:
        raise RuntimeError("成片文件渲染异常（产物文件缺失或大小为 0）")

    if os.path.exists(final_video):
        try:
            os.remove(final_video)
        except Exception:
            pass
    os.replace(tmp_final_video, final_video)

    copywriting_path = os.path.join(output_dir, "copywriting.md")
    hook_info = plan.get("hook_card") or plan.get("hook") or {}
    hook_text = hook_info.get("text", "") if isinstance(hook_info, dict) else str(hook_info)
    cta_info = plan.get("cta") or plan.get("cta_card") or {}
    cta_text = cta_info.get("text", "") if isinstance(cta_info, dict) else str(cta_info)
    prod_name = (spec.get("product") or {}).get("name", "本品")
    md_content = f"""# 带货视频文案与分镜说明

- **项目编号**: `{context.get("project_id")}`
- **运行编号**: `{context.get("run_id")}`
- **推荐商品**: {prod_name}
- **开场 Hook**: {hook_text}
- **结尾 CTA**: {cta_text}

## 核心分镜列表
"""
    for i, clip in enumerate(plan.get("clips", []), 1):
        md_content += f"{i}. [{clip.get('st', clip.get('start', 0)):.1f}s ~ {clip.get('end', 0):.1f}s] {clip.get('text', '')}\n"

    with open(copywriting_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    final_video_sha256 = compute_full_file_sha256(final_video)
    final_video_fast = compute_file_sha256(final_video)
    cover_sha256 = compute_full_file_sha256(final_cover_path) if os.path.isfile(final_cover_path) else ""

    render_manifest = {
        "schema_version": "3.3",
        "run_id": context.get("run_id"),
        "project_id": context.get("project_id"),
        "validated_plan_hash": actual_plan_hash,
        "video_path": final_video,
        "video_sha256": final_video_sha256,
        "video_fast_fingerprint": final_video_fast,
        "cover_path": final_cover_path,
        "cover_sha256": cover_sha256,
        "rendered_at": time.time(),
        "rendered_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    render_manifest_path = os.path.join(output_dir, "render_manifest.json")
    with open(render_manifest_path, "w", encoding="utf-8") as f:
        json.dump(render_manifest, f, ensure_ascii=False, indent=2)
    work_render_manifest_path = os.path.join(work_dir, "render_manifest.json")
    with open(work_render_manifest_path, "w", encoding="utf-8") as f:
        json.dump(render_manifest, f, ensure_ascii=False, indent=2)

    _write_run_state(work_dir, "RENDERED", "render", 0.85, f"成片与封面渲染完成 ({os.path.basename(final_video)})")
    _log_event(work_dir, "RENDER_COMPLETED", f"渲染成片已就绪: {final_video}")

    return {
        "schema_version": "3.3",
        "status": "RENDERED",
        "run_id": context.get("run_id"),
        "final_video": final_video,
        "cover_image": final_cover_path,
        "copywriting": copywriting_path,
        "next_step": f"python -m src.cli inspect-render --run {work_dir}",
    }
