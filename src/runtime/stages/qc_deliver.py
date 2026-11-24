# -*- coding: utf-8 -*-
"""
qc_deliver.py - 阶段 5 & 6：双层质量门禁（接入 ContinuityAuditor）与正式不可变交付签署
"""

import os
import json
import time
import hashlib
from typing import Dict, Any

from src.core.project_context import (
    compute_job_fingerprint,
    compute_file_sha256,
    compute_full_file_sha256,
    compute_file_fast_fingerprint,
    normalize_project_path,
)
from src.tools.extract_cover_candidates import _load_constraints
from src.qc.qc_auditor import QcAuditor
from src.qc.editorial_qc import EditorialQcAuditor
from src.qc.continuity_qc import ContinuityAuditor
from src.runtime.stages._compat import resolve_dep
from src.runtime.stages.common import _log_event, _write_run_state, resolve_run_dir
from src.runtime.stages.security import resolve_trusted_paths


def stage_qc(run_arg: str) -> Dict[str, Any]:
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
        raise RuntimeError(
            f"当前运行 ({context.get('run_id')}) 已完成正式交付签署，交付物与质检结论已被锁定为不可变！如需重新质检请创建新 run_id"
        )
    final_video = os.path.join(output_dir, "final.mp4")
    if not os.path.isfile(final_video):
        raise FileNotFoundError(f"未找到待检成片: {final_video}，请先执行 render 阶段")

    plan_path = os.path.join(work_dir, "validated_plan.json")
    if not os.path.isfile(plan_path):
        raise FileNotFoundError("未找到已验证的分镜计划 validated_plan.json，请先执行 validate-plan")
    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)

    render_manifest_path = os.path.join(output_dir, "render_manifest.json")
    if not os.path.isfile(render_manifest_path):
        work_rm = os.path.join(work_dir, "render_manifest.json")
        if os.path.isfile(work_rm):
            render_manifest_path = work_rm
        else:
            raise FileNotFoundError("未找到渲染清单 render_manifest.json，请先执行 render 阶段")

    with open(render_manifest_path, "r", encoding="utf-8") as rmf:
        render_manifest = json.load(rmf)

    if render_manifest.get("run_id") and render_manifest.get("run_id") != context.get("run_id"):
        raise RuntimeError(
            f"渲染清单 run_id ({render_manifest.get('run_id')}) 与当前运行 ({context.get('run_id')}) 不符"
        )

    plan_to_hash = {k: v for k, v in plan.items() if k != "validated_plan_hash"}
    current_plan_hash = hashlib.sha256(json.dumps(plan_to_hash, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    if render_manifest.get("validated_plan_hash") != current_plan_hash:
        raise RuntimeError(
            f"渲染清单记录的计划哈希 ({render_manifest.get('validated_plan_hash')}) 与当前执行计划哈希 ({current_plan_hash}) 不一致！"
            f"计划重新审批后，下游成片必须重新执行 render 阶段"
        )

    current_video_sha256 = compute_full_file_sha256(final_video)
    if render_manifest.get("video_sha256") != current_video_sha256:
        raise RuntimeError(
            f"成片文件被外部修改：实际 SHA-256 ({current_video_sha256}) 与渲染清单记录 ({render_manifest.get('video_sha256')}) 不一致！"
        )

    spec_path = os.path.join(work_dir, "spec_snapshot.json")
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    profile_path = os.path.join(work_dir, "product_profile.json")
    with open(profile_path, "r", encoding="utf-8") as f:
        product_profile = json.load(f)

    _write_run_state(work_dir, "QC_EVALUATING", "qc", 0.90, "执行工程技术 QC、接缝连续性审计与内容 Editorial QC...")
    _log_event(work_dir, "QC_START", "开始双层质检")

    constraints = _load_constraints()
    qc_auditor_cls = resolve_dep("QcAuditor", QcAuditor)
    ed_auditor_cls = resolve_dep("EditorialQcAuditor", EditorialQcAuditor)
    cont_auditor_cls = resolve_dep("ContinuityAuditor", ContinuityAuditor)

    # 1. 工程技术 QC (QcAuditor)
    tech_qc_path = os.path.join(output_dir, "technical_qc.json")
    tech_auditor = qc_auditor_cls(constraints=constraints, spec=spec)
    cover_path = os.path.join(output_dir, "cover.jpg")
    cover_meta_path = os.path.join(output_dir, "cover_meta.json")
    overlay_meta_path = os.path.join(work_dir, "video_overlay_meta.json")
    ass_path = os.path.join(work_dir, "subtitles.ass")

    try:
        tech_passed = tech_auditor.audit_final_deliverables(
            video_path=final_video,
            cover_path=cover_path,
            ass_path=ass_path,
            report_output_path=tech_qc_path,
            cover_meta_path=cover_meta_path,
            overlay_meta_path=overlay_meta_path,
            plan_path=plan_path,
            spec=spec,
        )
        if os.path.isfile(tech_qc_path):
            with open(tech_qc_path, "r", encoding="utf-8") as f:
                tech_report = json.load(f)
        else:
            tech_report = {"passed": tech_passed}
    except Exception as e:
        tech_passed = False
        tech_report = {
            "passed": False,
            "fatal_error": str(e),
            "summary": {"n_passed": 0, "n_failed": 1, "n_warn": 0},
            "checks": [{"name": "tech_audit_execution", "status": "FAIL", "reason": str(e)}],
        }
        with open(tech_qc_path, "w", encoding="utf-8") as f:
            json.dump(tech_report, f, ensure_ascii=False, indent=2)

    current_video_sha256 = compute_full_file_sha256(final_video)
    current_video_fast = compute_file_fast_fingerprint(final_video)

    # 2. P1-01 接线：视听接缝连续性审计 (ContinuityAuditor) -> 传入 EditorialQcAuditor
    cand_map: Dict[str, Dict[str, Any]] = {}
    candidates_path = os.path.join(work_dir, "analysis", "candidates.json")
    if os.path.isfile(candidates_path):
        try:
            with open(candidates_path, "r", encoding="utf-8") as cf:
                for item in json.load(cf):
                    if isinstance(item, dict) and item.get("candidate_id"):
                        cand_map[item["candidate_id"]] = item
        except Exception:
            pass
    # 确保已通过审批的 visual_override 同步反映在 cand_map 副本中，避免接缝审计误报已覆写灰区
    for ov in (plan.get("agent_visual_overrides") or []):
        cid = ov.get("candidate_id")
        if cid in cand_map:
            c_copy = dict(cand_map[cid])
            c_copy["product_relation"] = ov.get("override_relation", c_copy.get("product_relation"))
            cand_map[cid] = c_copy

    continuity_review = cont_auditor_cls().audit_sequence_seams(plan.get("clips", []), cand_map)
    continuity_review_path = os.path.join(output_dir, "continuity_review.json")
    with open(continuity_review_path, "w", encoding="utf-8") as f:
        json.dump(continuity_review, f, ensure_ascii=False, indent=2)

    # 3. 内容审核门禁 (EditorialQcAuditor)
    editorial_qc_path = os.path.join(output_dir, "editorial_qc.json")
    auto_cfg = spec.get("options", {}).get("automation", {}) or {}
    ed_policy = (
        "strict"
        if (
            auto_cfg.get("quality_policy") == "strict"
            or auto_cfg.get("visual_verification") == "required"
            or spec.get("options", {}).get("strict") is True
            or product_profile.get("quality_policy") == "strict"
        )
        else "standard"
    )
    ed_auditor = ed_auditor_cls(policy=ed_policy)
    try:
        ed_report = ed_auditor.audit_editorial_quality(
            final_plan=plan,
            product_profile=product_profile,
            continuity_review=continuity_review,
        )
    except Exception as e:
        ed_report = {
            "passed": False,
            "block_count": 1,
            "summary": {"status": "BLOCK", "block_count": 1, "warn_count": 0},
            "checks": [{"gate": "editorial_audit_execution", "status": "BLOCK", "detail": str(e)}],
        }

    # 记录外部 Agent 灰区视觉覆盖溯源
    agent_overrides = plan.get("agent_visual_overrides") or []
    ed_report["has_agent_visual_overrides"] = bool(agent_overrides)
    ed_report["agent_visual_overrides"] = agent_overrides
    ed_report["continuity_review_path"] = continuity_review_path
    ed_report["target_video_sha256"] = current_video_sha256
    ed_report["video_sha256"] = current_video_sha256
    ed_report["video_fast_fingerprint"] = current_video_fast
    ed_report["validated_plan_hash"] = current_plan_hash
    rm_sha = compute_full_file_sha256(render_manifest_path)
    ed_report["render_manifest_sha256"] = rm_sha
    with open(editorial_qc_path, "w", encoding="utf-8") as f:
        json.dump(ed_report, f, ensure_ascii=False, indent=2)

    tech_passed = bool(tech_report.get("all_passed", tech_report.get("passed", False)))
    tech_report["passed"] = tech_passed
    tech_report["target_video_sha256"] = current_video_sha256
    tech_report["video_sha256"] = current_video_sha256
    tech_report["video_fast_fingerprint"] = current_video_fast
    tech_report["validated_plan_hash"] = current_plan_hash
    tech_report["render_manifest_sha256"] = rm_sha
    with open(tech_qc_path, "w", encoding="utf-8") as f:
        json.dump(tech_report, f, ensure_ascii=False, indent=2)

    ed_passed = bool(ed_report.get("passed", False) and ed_report.get("block_count", 0) == 0)
    dual_pass = tech_passed and ed_passed

    qc_status = "QC_PASSED" if dual_pass else "QC_BLOCKED"
    msg = (
        "双层质检全部通过，可签署正式交付"
        if dual_pass
        else f"质检未通过 (技术通过={tech_passed}, 内容通过={ed_passed}, BLOCK={ed_report.get('block_count', 0)})"
    )
    _write_run_state(work_dir, qc_status, "qc", 0.95, msg, {"technical_qc_passed": tech_passed, "editorial_qc_passed": ed_passed})
    _log_event(work_dir, "QC_COMPLETED", msg, {"tech_passed": tech_passed, "ed_passed": ed_passed})

    return {
        "schema_version": "3.3",
        "status": qc_status,
        "run_id": context.get("run_id"),
        "dual_qc_passed": dual_pass,
        "technical_qc": {
            "passed": tech_passed,
            "report_path": tech_qc_path,
        },
        "editorial_qc": {
            "passed": ed_passed,
            "block_count": ed_report.get("block_count", 0),
            "warn_count": ed_report.get("warn_count", 0),
            "report_path": editorial_qc_path,
        },
        "continuity_review_path": continuity_review_path,
        "next_step": f"python -m src.cli deliver --run {work_dir}",
    }


def stage_deliver(run_arg: str) -> Dict[str, Any]:
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
        raise RuntimeError(
            f"当前运行 ({context.get('run_id')}) 已完成正式交付签署 (delivery_manifest.json 已存在)，"
            f"交付清单具有不可变性，严禁重复签署或覆盖！如需更新请创建新 run_id"
        )

    spec_path = os.path.join(work_dir, "spec_snapshot.json")
    manifest_path = os.path.join(work_dir, "input_manifest.json")
    plan_audit_path = os.path.join(work_dir, "plan_audit.json")
    plan_path = os.path.join(work_dir, "validated_plan.json")
    final_video = os.path.join(output_dir, "final.mp4")
    tech_qc_path = os.path.join(output_dir, "technical_qc.json")
    editorial_qc_path = os.path.join(output_dir, "editorial_qc.json")

    fp_fn = resolve_dep("compute_job_fingerprint", compute_job_fingerprint)
    rejections = []

    if not os.path.isfile(spec_path):
        rejections.append("缺失必要任务契约快照: spec_snapshot.json 不存在")
    if not os.path.isfile(manifest_path):
        rejections.append("缺失必要输入清单文件: input_manifest.json 不存在")
    if not os.path.isfile(plan_path):
        rejections.append("缺失必要执行计划文件: validated_plan.json 不存在")
    if not os.path.isfile(plan_audit_path):
        rejections.append("缺失必要分镜计划审计报告: plan_audit.json 不存在")

    if os.path.isfile(spec_path) and os.path.isfile(manifest_path):
        with open(spec_path, "r", encoding="utf-8") as f:
            spec = json.load(f)
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        curr_fp = fp_fn(spec, manifest)
        expected_fp = context.get("spec_fingerprint")
        if not expected_fp or curr_fp != expected_fp:
            rejections.append("任务输入指纹发生变更或缺失，严禁跨配置交付")

        live_spec_path = context.get("spec_path")
        if live_spec_path and os.path.isfile(live_spec_path):
            try:
                with open(live_spec_path, "r", encoding="utf-8") as f:
                    live_spec = json.load(f)
                live_fp = fp_fn(live_spec, manifest)
                if live_fp != expected_fp:
                    rejections.append("活任务配置已被外部修改导致指纹不匹配，严禁跨配置交付")
            except Exception as e:
                rejections.append(f"活任务配置指纹校验异常: {e}")

        for s in manifest.get("source_videos", []):
            sp = s.get("path")
            norm_sp = normalize_project_path(sp) if sp else ""
            if not norm_sp or not os.path.isfile(norm_sp):
                rejections.append(f"输入清单登记的原始素材文件不存在: {sp}")
            else:
                disk_src_sha = compute_full_file_sha256(norm_sp)
                expected_src_sha = s.get("sha256")
                if expected_src_sha and disk_src_sha != expected_src_sha:
                    rejections.append(
                        f"原始素材 '{sp}' 磁盘实际 SHA-256 ({disk_src_sha}) 与清单记录 ({expected_src_sha}) 不一致，素材在交付前被篡改"
                    )

    pa_data = {}
    actual_plan_hash = None
    plan_obj: Dict[str, Any] = {}
    if os.path.isfile(plan_audit_path):
        with open(plan_audit_path, "r", encoding="utf-8") as paf:
            pa_data = json.load(paf)
        if not (pa_data.get("passed") and pa_data.get("audit_status") == "APPROVED"):
            rejections.append("分镜计划审计未通过或未经批准 (处于 REJECTED 状态)")
        if pa_data.get("run_id") and pa_data.get("run_id") != context.get("run_id"):
            rejections.append(f"分镜计划审计报告 run_id ({pa_data.get('run_id')}) 与当前运行 ({context.get('run_id')}) 不符")

    if os.path.isfile(plan_path):
        with open(plan_path, "r", encoding="utf-8") as pf:
            plan_obj = json.load(pf)
        if plan_obj.get("run_id") and plan_obj.get("run_id") != context.get("run_id"):
            rejections.append(f"执行计划 run_id ({plan_obj.get('run_id')}) 与当前运行 ({context.get('run_id')}) 不符")
        plan_to_hash = {k: v for k, v in plan_obj.items() if k != "validated_plan_hash"}
        plan_bytes = json.dumps(plan_to_hash, sort_keys=True, ensure_ascii=False).encode("utf-8")
        actual_plan_hash = hashlib.sha256(plan_bytes).hexdigest()
        expected_plan_hash = pa_data.get("validated_plan_hash") if pa_data else None
        if not expected_plan_hash or actual_plan_hash != expected_plan_hash:
            rejections.append("执行计划哈希与审计签名不一致，禁止跨计划交付")

    render_manifest_path = os.path.join(output_dir, "render_manifest.json")
    if not os.path.isfile(render_manifest_path):
        work_rm = os.path.join(work_dir, "render_manifest.json")
        if os.path.isfile(work_rm):
            render_manifest_path = work_rm
        else:
            rejections.append("未找到渲染清单 render_manifest.json")

    actual_rm_sha256 = ""
    rm_data = None
    if os.path.isfile(render_manifest_path):
        actual_rm_sha256 = compute_full_file_sha256(render_manifest_path)
        try:
            with open(render_manifest_path, "r", encoding="utf-8") as rmf:
                rm_data = json.load(rmf)
            if rm_data.get("run_id") and rm_data.get("run_id") != context.get("run_id"):
                rejections.append(f"渲染清单 run_id ({rm_data.get('run_id')}) 与当前运行不符")
            if actual_plan_hash and rm_data.get("validated_plan_hash") != actual_plan_hash:
                rejections.append("渲染清单记录的计划哈希与当前执行计划不一致，计划更新后必须重新 render 与 qc")
        except Exception as e:
            rejections.append(f"渲染清单解析异常: {e}")

    current_video_sha256 = compute_full_file_sha256(final_video) if os.path.isfile(final_video) else ""
    current_video_fast = compute_file_sha256(final_video) if os.path.isfile(final_video) else ""
    if not os.path.isfile(final_video) or os.path.getsize(final_video) == 0:
        rejections.append("成片视频文件不存在或为空")

    if rm_data is not None:
        if current_video_sha256 and rm_data.get("video_sha256") != current_video_sha256:
            rejections.append("成片视频实际完整 SHA-256 与渲染清单记录不一致")

    if not os.path.isfile(tech_qc_path):
        rejections.append("未找到技术质检报告 technical_qc.json")
    else:
        with open(tech_qc_path, "r", encoding="utf-8") as f:
            t_data = json.load(f)
        if not (t_data.get("all_passed", False) or t_data.get("passed", False)):
            rejections.append("工程技术质检未通过")
        t_plan_h = t_data.get("validated_plan_hash")
        if actual_plan_hash and (not t_plan_h or t_plan_h != actual_plan_hash):
            rejections.append("技术质检报告未绑定当前批准的计划哈希或哈希不匹配，必须重新执行 qc 阶段")
        t_rm_sha = t_data.get("render_manifest_sha256")
        if actual_rm_sha256 and (not t_rm_sha or t_rm_sha != actual_rm_sha256):
            rejections.append("技术质检报告绑定的渲染清单哈希与当前渲染清单不一致，重新渲染后必须重新执行 qc 阶段")
        t_target_sha = t_data.get("target_video_sha256") or t_data.get("video_sha256")
        if not t_target_sha or len(t_target_sha) != 64 or t_target_sha != current_video_sha256:
            rejections.append(f"技术质检报告审核的目标视频哈希 ({t_target_sha}) 与当前成片哈希不匹配，重新渲染后必须重新执行 qc 阶段")

    if not os.path.isfile(editorial_qc_path):
        rejections.append("未找到内容质检报告 editorial_qc.json")
    else:
        with open(editorial_qc_path, "r", encoding="utf-8") as f:
            e_data = json.load(f)
        if not e_data.get("passed", False) or e_data.get("block_count", 0) > 0:
            rejections.append(f"内容质检门禁阻断 (BLOCK={e_data.get('block_count', 0)})")
        e_plan_h = e_data.get("validated_plan_hash")
        if actual_plan_hash and (not e_plan_h or e_plan_h != actual_plan_hash):
            rejections.append("内容质检报告未绑定当前批准的计划哈希或哈希不匹配，必须重新执行 qc 阶段")
        e_rm_sha = e_data.get("render_manifest_sha256")
        if actual_rm_sha256 and (not e_rm_sha or e_rm_sha != actual_rm_sha256):
            rejections.append("内容质检报告绑定的渲染清单哈希与当前渲染清单不一致，重新渲染后必须重新执行 qc 阶段")
        e_target_sha = e_data.get("target_video_sha256") or e_data.get("video_sha256")
        if not e_target_sha or len(e_target_sha) != 64 or e_target_sha != current_video_sha256:
            rejections.append(f"内容质检报告审核的目标视频哈希 ({e_target_sha}) 与当前成片哈希不匹配，重新渲染后必须重新执行 qc 阶段")

    if rejections:
        msg = f"交付硬门禁拒绝签署: {'; '.join(rejections)}"
        _write_run_state(work_dir, "DELIVERY_BLOCKED", "deliver", 0.95, msg)
        _log_event(work_dir, "DELIVERY_BLOCKED", msg)
        raise ValueError(msg)

    agent_overrides = plan_obj.get("agent_visual_overrides") or pa_data.get("agent_visual_overrides") or []
    deliv_manifest = {
        "schema_version": "3.3",
        "project_id": context.get("project_id"),
        "run_id": context.get("run_id"),
        "status": "DELIVERED",
        "job_fingerprint": context.get("spec_fingerprint"),
        "proposal_hash": pa_data.get("proposal_hash"),
        "validated_plan_hash": actual_plan_hash,
        "render_manifest_path": render_manifest_path,
        "render_manifest_sha256": compute_full_file_sha256(render_manifest_path) if os.path.isfile(render_manifest_path) else "",
        "video_path": final_video,
        "video_sha256": current_video_sha256 or compute_full_file_sha256(final_video),
        "video_fast_fingerprint": current_video_fast or compute_file_sha256(final_video),
        "cover_path": os.path.join(output_dir, "cover.jpg"),
        "copywriting_path": os.path.join(output_dir, "copywriting.md"),
        "technical_qc_path": tech_qc_path,
        "technical_qc_sha256": compute_full_file_sha256(tech_qc_path),
        "editorial_qc_path": editorial_qc_path,
        "editorial_qc_sha256": compute_full_file_sha256(editorial_qc_path),
        "has_agent_visual_overrides": bool(agent_overrides),
        "agent_visual_overrides": agent_overrides,
        "qc_passed": True,
        "completed_at": time.time(),
        "completed_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    deliv_path = os.path.join(output_dir, "delivery_manifest.json")
    with open(deliv_path, "w", encoding="utf-8") as f:
        json.dump(deliv_manifest, f, ensure_ascii=False, indent=2)

    output_base = os.path.dirname(output_dir)
    latest_path = os.path.join(output_base, "latest.json")
    latest_pointer = {
        "latest_run_id": context.get("run_id"),
        "status": "DELIVERED",
        "video_path": final_video,
        "delivery_manifest": deliv_path,
        "delivered_at": deliv_manifest["completed_at_str"],
    }
    try:
        with open(latest_path, "w", encoding="utf-8") as f:
            json.dump(latest_pointer, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    _write_run_state(work_dir, "DELIVERED", "deliver", 1.0, f"成片及全部物料正式交付: {final_video}")
    _log_event(work_dir, "DELIVERED", f"成片正式交付完成: {final_video}", deliv_manifest)

    return {
        "schema_version": "3.3",
        "status": "DELIVERED",
        "run_id": context.get("run_id"),
        "final_video": final_video,
        "cover_path": deliv_manifest["cover_path"],
        "delivery_manifest": deliv_path,
        "latest_pointer": latest_path,
        "has_agent_visual_overrides": bool(agent_overrides),
    }
