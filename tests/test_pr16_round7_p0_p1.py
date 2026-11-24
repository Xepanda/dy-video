# -*- coding: utf-8 -*-
"""
test_pr16_round7_p0_p1.py - PR #16 第七轮评审 P0 / P1 缺陷深度回归测试套件
========================================================================
涵盖：
  1. P0-1: 候选池哈希必须锁定素材真实来源 (source_file, source_file_sha256) 与证据字段；
           篡改 source_file 必遭 validate-plan 阻断；
           候选源必须属于 input_manifest.json 白名单且严禁为历史成片；
           render 阶段必须重新核验每个剪辑片段的来源白名单与防历史成片。
  2. P0-2: 各阶段严格重新推导与校验可信 output/<run_id> 目录；
           篡改 run_context.output_dir（如指向外部、素材目录或逃逸）必须被强制阻断。
  3. P0-3: deliver 阶段强制要求全部前序产物存在性 (spec, manifest, plan, audit) 与强哈希绑定；
           已交付产物 (delivery_manifest.json 已存在) 具有不可变性，严禁重写。
  4. P1: 完整 SHA-256 (compute_full_file_sha256) 能够检测大视频中间字节篡改；
  5. P1: 同名多目录素材 (如 dirA/live01.mp4 与 dirB/live01.mp4) 生成不冲突的唯一 source_id 与 candidate_id。
"""

import os
import sys
import json
import time
import shutil
import hashlib
import tempfile
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.project_context import (
    ProjectPaths,
    InputManifest,
    compute_job_fingerprint,
    compute_file_sha256,
    compute_full_file_sha256,
    compute_file_fast_fingerprint,
    is_historical_output,
)
from src.core.candidate_generator import CandidateGenerator
from src.runtime.stage_services import (
    stage_prepare,
    stage_analyze,
    stage_validate_plan,
    stage_render,
    stage_qc,
    stage_deliver,
    resolve_trusted_paths,
    compute_candidate_pool_hash,
)


class TestPR16Round7P0P1(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.proj_dir = os.path.join(self.tmpdir, "my_project")
        self.mat_dir = os.path.join(self.proj_dir, "素材")
        os.makedirs(self.mat_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. P0-1: 候选池哈希锁定真实素材来源与篡改拦截
    # -------------------------------------------------------------------------
    def test_candidate_pool_hash_locks_source_file_and_tamper_detected(self):
        """P0-1: candidate_pool_hash 必须包含 source_file，篡改 source_file 必导致哈希改变并阻断。"""
        cand_orig = {
            "candidate_id": "c_001",
            "source_id": "src_01_live01_mp4",
            "source_file": os.path.normpath("/path/to/real_raw_video.mp4"),
            "source_file_sha256": "abc12345fullhash",
            "source_start_ms": 1000,
            "source_end_ms": 6000,
            "product_relation": "SAME_SKU",
            "eligible": True,
            "text": "这件羊绒衫亲肤保暖闭眼入",
            "selling_point_evidence_refs": ["保暖"],
            "frame_ids": ["f_001"],
        }
        hash_orig = compute_candidate_pool_hash([cand_orig])

        # 模拟篡改 source_file 指向另一段素材
        cand_tampered = dict(cand_orig, source_file=os.path.normpath("/path/to/malicious_other_video.mp4"))
        hash_tampered = compute_candidate_pool_hash([cand_tampered])

        self.assertNotEqual(hash_orig, hash_tampered, "修改 source_file 必须导致 candidate_pool_hash 改变！")

        # 模拟在 validate-plan 阶段执行篡改检测
        work_dir = os.path.join(self.proj_dir, "work", "run_cand_tamper")
        analysis_dir = os.path.join(work_dir, "analysis")
        os.makedirs(analysis_dir, exist_ok=True)

        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.2", "run_id": "run_cand_tamper"}, f)
        with open(os.path.join(work_dir, "spec_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"options": {}}, f)

        # 写入原始快照
        snapshot = {
            "schema_version": "3.2",
            "run_id": "run_cand_tamper",
            "candidate_pool_hash": hash_orig,
        }
        with open(os.path.join(analysis_dir, "candidate_pool_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump(snapshot, f)

        # 写入被篡改 source_file 的 candidates.json
        with open(os.path.join(analysis_dir, "candidates.json"), "w", encoding="utf-8") as f:
            json.dump([cand_tampered], f)

        prop = {
            "proposal_id": "prop_01",
            "candidate_pool_hash": hash_orig,
            "ordered_candidate_ids": ["c_001"],
        }
        prop_path = os.path.join(self.proj_dir, "proposal.json")
        with open(prop_path, "w", encoding="utf-8") as f:
            json.dump(prop, f)

        with self.assertRaises(ValueError) as ctx:
            stage_validate_plan(work_dir, prop_path)
        self.assertIn("候选池文件完整性校验失败", str(ctx.exception))

    def test_validate_plan_rejects_candidate_source_not_in_input_manifest(self):
        """P0-1: 候选源如果不在 input_manifest.json 白名单内，validate-plan 必须拒绝。"""
        work_dir = os.path.join(self.proj_dir, "work", "run_whitelist_check")
        analysis_dir = os.path.join(work_dir, "analysis")
        os.makedirs(analysis_dir, exist_ok=True)

        # 合法源文件
        legit_file = os.path.join(self.mat_dir, "legit_live.mp4")
        with open(legit_file, "wb") as f:
            f.write(b"legit_data")

        # 非法外部源文件
        rogue_file = os.path.join(self.tmpdir, "rogue_material.mp4")
        with open(rogue_file, "wb") as f:
            f.write(b"rogue_data")

        # input_manifest.json 仅登记合法素材
        manifest_data = {
            "schema_version": "3.1",
            "source_videos": [
                {
                    "source_id": "src_01_legit_live_mp4",
                    "path": legit_file,
                    "sha256": compute_full_file_sha256(legit_file),
                }
            ]
        }
        with open(os.path.join(work_dir, "input_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest_data, f)
        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.2", "run_id": "run_whitelist_check", "project_id": "my_project"}, f)
        with open(os.path.join(work_dir, "spec_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"options": {}}, f)

        # 候选使用了非白名单文件 rogue_file
        cand_rogue = {
            "candidate_id": "c_rogue",
            "source_id": "src_99_rogue",
            "source_file": rogue_file,
            "source_file_sha256": compute_full_file_sha256(rogue_file),
            "source_start_ms": 0,
            "source_end_ms": 5000,
            "product_relation": "SAME_SKU",
            "eligible": True,
            "text": "优质好货推荐",
        }
        cand_hash = compute_candidate_pool_hash([cand_rogue])
        with open(os.path.join(analysis_dir, "candidate_pool_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.2", "candidate_pool_hash": cand_hash}, f)
        with open(os.path.join(analysis_dir, "candidates.json"), "w", encoding="utf-8") as f:
            json.dump([cand_rogue], f)

        prop = {
            "proposal_id": "prop_rogue",
            "candidate_pool_hash": cand_hash,
            "ordered_candidate_ids": ["c_rogue"],
        }
        prop_path = os.path.join(self.proj_dir, "prop_rogue.json")
        with open(prop_path, "w", encoding="utf-8") as f:
            json.dump(prop, f)

        with self.assertRaises(ValueError) as ctx:
            stage_validate_plan(work_dir, prop_path)
        self.assertIn("不在 input_manifest.json 白名单中", str(ctx.exception))

    def test_validate_plan_and_render_reject_historical_output_as_source(self):
        """P0-1: 候选或分镜使用历史成片作为剪辑素材源，必须在 validate-plan 与 render 阶段被双重拦截。"""
        work_dir = os.path.join(self.proj_dir, "work", "run_hist_check")
        output_dir = os.path.join(self.proj_dir, "output", "run_hist_check")
        analysis_dir = os.path.join(work_dir, "analysis")
        os.makedirs(analysis_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        # 伪造一个历史成片 final.mp4
        hist_out = os.path.join(self.mat_dir, "final.mp4")
        with open(hist_out, "wb") as f:
            f.write(b"hist_final_video")

        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump({
                "schema_version": "3.2",
                "run_id": "run_hist_check",
                "project_id": "my_project",
                "project_root": self.proj_dir,
                "output_dir": output_dir,
            }, f)
        with open(os.path.join(work_dir, "spec_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"options": {}}, f)

        cand_hist = {
            "candidate_id": "c_hist",
            "source_id": "src_final",
            "source_file": hist_out,
            "source_start_ms": 0,
            "source_end_ms": 5000,
            "product_relation": "SAME_SKU",
            "eligible": True,
            "text": "这是成片台词",
        }
        h_cand = compute_candidate_pool_hash([cand_hist])
        with open(os.path.join(analysis_dir, "candidate_pool_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.2", "candidate_pool_hash": h_cand}, f)
        with open(os.path.join(analysis_dir, "candidates.json"), "w", encoding="utf-8") as f:
            json.dump([cand_hist], f)

        prop = {
            "proposal_id": "prop_hist",
            "candidate_pool_hash": h_cand,
            "ordered_candidate_ids": ["c_hist"],
        }
        prop_p = os.path.join(self.proj_dir, "prop_hist.json")
        with open(prop_p, "w", encoding="utf-8") as f:
            json.dump(prop, f)

        # 1. 验证 validate-plan 拦截历史成片
        with self.assertRaises(ValueError) as ctx:
            stage_validate_plan(work_dir, prop_p)
        self.assertIn("引用了历史成片作为剪辑素材源", str(ctx.exception))

        # 2. 验证即使绕过 validate-plan，render 阶段也必须拦截历史成片分镜
        fake_plan = {
            "schema_version": "3.2",
            "clips": [{"id": "c1", "source_file": hist_out, "start": 0, "end": 5}],
        }
        plan_h = hashlib.sha256(json.dumps(fake_plan, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        fake_plan["validated_plan_hash"] = plan_h
        with open(os.path.join(work_dir, "validated_plan.json"), "w", encoding="utf-8") as f:
            json.dump(fake_plan, f)
        with open(os.path.join(work_dir, "plan_audit.json"), "w", encoding="utf-8") as f:
            json.dump({"audit_status": "APPROVED", "passed": True, "validated_plan_hash": plan_h}, f)

        with self.assertRaises(RuntimeError) as r_ctx:
            stage_render(work_dir)
        self.assertIn("历史成片", str(r_ctx.exception))

    # -------------------------------------------------------------------------
    # 2. P0-2: 严禁信任被篡改的 output_dir 与路径逃逸阻断
    # -------------------------------------------------------------------------
    def test_resolve_trusted_paths_and_render_reject_tampered_output_dir(self):
        """P0-2: 篡改 run_context.json 的 output_dir 必须在路径推导与 render 阶段被强制拦截。"""
        work_dir = os.path.join(self.proj_dir, "work", "run_tamper_out")
        legit_output = os.path.join(self.proj_dir, "output", "run_tamper_out")
        os.makedirs(work_dir, exist_ok=True)
        os.makedirs(legit_output, exist_ok=True)

        # 1. 正常情况可信解析
        ctx_normal = {
            "schema_version": "3.2",
            "run_id": "run_tamper_out",
            "project_id": "my_project",
            "project_root": self.proj_dir,
            "output_dir": legit_output,
        }
        paths = resolve_trusted_paths(work_dir, ctx_normal)
        self.assertEqual(os.path.realpath(paths.output_dir), os.path.realpath(legit_output))

        # 2. 篡改 output_dir 指向外部敏感目录 (如 /tmp/rogue 或 C:\rogue)
        external_out = os.path.join(self.tmpdir, "rogue_external_dir")
        ctx_tampered = dict(ctx_normal, output_dir=external_out)
        with self.assertRaises(ValueError) as ctx:
            resolve_trusted_paths(work_dir, ctx_tampered)
        self.assertIn("安全阻断: run_context.json 中的 output_dir", str(ctx.exception))

        # 3. 篡改 output_dir 指向素材目录 (素材/) 必须拦截
        ctx_mat_tampered = dict(ctx_normal, output_dir=self.mat_dir)
        with self.assertRaises(ValueError) as ctx:
            resolve_trusted_paths(work_dir, ctx_mat_tampered)
        self.assertTrue(
            "安全阻断" in str(ctx.exception) or "重叠" in str(ctx.exception) or "素材目录" in str(ctx.exception)
        )

        # 4. 篡改 output_dir 指向历史交付目录 (如 output/run_hist_01) 必须拦截
        hist_delivery_dir = os.path.join(self.proj_dir, "output", "run_hist_01")
        os.makedirs(hist_delivery_dir, exist_ok=True)
        ctx_hist_tampered = dict(ctx_normal, output_dir=hist_delivery_dir)
        with self.assertRaises(ValueError) as ctx:
            resolve_trusted_paths(work_dir, ctx_hist_tampered)
        self.assertIn("安全阻断: run_context.json 中的 output_dir", str(ctx.exception))

        # 5. 篡改 output_dir 指向项目根目录本身必须拦截
        ctx_root_tampered = dict(ctx_normal, output_dir=self.proj_dir)
        with self.assertRaises(ValueError) as ctx:
            resolve_trusted_paths(work_dir, ctx_root_tampered)
        self.assertIn("安全阻断", str(ctx.exception))

        # 6. 在 stage_render 运行时验证负向篡改拦截
        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump(ctx_tampered, f)
        with open(os.path.join(work_dir, "validated_plan.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.2", "validated_plan_hash": "h1"}, f)
        with open(os.path.join(work_dir, "plan_audit.json"), "w", encoding="utf-8") as f:
            json.dump({"audit_status": "APPROVED", "passed": True, "validated_plan_hash": "h1"}, f)

        with self.assertRaises(ValueError) as r_ctx:
            stage_render(work_dir)
        self.assertIn("安全阻断", str(r_ctx.exception))

    # -------------------------------------------------------------------------
    # 3. P0-3: deliver 强制要求前序全部产物且已有交付不可变
    # -------------------------------------------------------------------------
    def test_deliver_rejects_missing_prerequisite_artifacts(self):
        """P0-3: 缺失任何前序产物 (spec_snapshot, input_manifest, validated_plan, plan_audit)，deliver 必须拒绝。"""
        work_dir = os.path.join(self.proj_dir, "work", "run_deliv_missing")
        output_dir = os.path.join(self.proj_dir, "output", "run_deliv_missing")
        os.makedirs(work_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        final_vid = os.path.join(output_dir, "final.mp4")
        with open(final_vid, "wb") as f:
            f.write(b"deliv_vid_data")
        vid_sha = compute_full_file_sha256(final_vid)

        with open(os.path.join(output_dir, "technical_qc.json"), "w", encoding="utf-8") as f:
            json.dump({"passed": True, "all_passed": True, "target_video_sha256": vid_sha}, f)
        with open(os.path.join(output_dir, "editorial_qc.json"), "w", encoding="utf-8") as f:
            json.dump({"passed": True, "block_count": 0, "target_video_sha256": vid_sha}, f)

        ctx_data = {
            "schema_version": "3.2",
            "run_id": "run_deliv_missing",
            "project_id": "my_project",
            "project_root": self.proj_dir,
            "output_dir": output_dir,
            "spec_fingerprint": "fp_123",
        }
        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump(ctx_data, f)

        # 此时缺少 spec_snapshot, input_manifest, validated_plan, plan_audit
        with self.assertRaises(ValueError) as ctx:
            stage_deliver(work_dir)
        err = str(ctx.exception)
        self.assertIn("缺失必要任务契约快照", err)
        self.assertIn("缺失必要输入清单文件", err)
        self.assertIn("缺失必要执行计划文件", err)
        self.assertIn("缺失必要分镜计划审计报告", err)

    def test_deliver_forbids_rewriting_existing_delivery_manifest(self):
        """P0-3: delivery_manifest.json 已存在时代表成片已交付，严禁重复交付重写已交付物。"""
        work_dir = os.path.join(self.proj_dir, "work", "run_deliv_immutable")
        output_dir = os.path.join(self.proj_dir, "output", "run_deliv_immutable")
        os.makedirs(work_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        # 写入已完成的 delivery_manifest.json
        deliv_path = os.path.join(output_dir, "delivery_manifest.json")
        with open(deliv_path, "w", encoding="utf-8") as f:
            json.dump({"status": "DELIVERED", "run_id": "run_deliv_immutable"}, f)

        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump({
                "schema_version": "3.2",
                "run_id": "run_deliv_immutable",
                "project_id": "my_project",
                "project_root": self.proj_dir,
                "output_dir": output_dir,
            }, f)

        with self.assertRaises(RuntimeError) as ctx:
            stage_deliver(work_dir)
        self.assertIn("已完成正式交付签署", str(ctx.exception))
        self.assertIn("交付清单具有不可变性", str(ctx.exception))

        # 同时验证 stage_render 与 stage_qc 在交付后也必须禁止重写交付物
        with self.assertRaises(RuntimeError) as r_ctx:
            stage_render(work_dir)
        self.assertIn("已完成正式交付签署", str(r_ctx.exception))

        with self.assertRaises(RuntimeError) as q_ctx:
            stage_qc(work_dir)
        self.assertIn("已完成正式交付签署", str(q_ctx.exception))

    def test_render_rejects_unauthorized_clip_source_id_and_hash_tampering(self):
        """P0-1: render 阶段必须核验分镜 source_id 白名单与素材 SHA-256 完整性。"""
        work_dir = os.path.join(self.proj_dir, "work", "run_render_source_audit")
        output_dir = os.path.join(self.proj_dir, "output", "run_render_source_audit")
        os.makedirs(work_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        legit_file = os.path.join(self.mat_dir, "render_legit.mp4")
        with open(legit_file, "wb") as f:
            f.write(b"render_legit_video_payload")
        legit_sha = compute_full_file_sha256(legit_file)

        manifest_data = {
            "schema_version": "3.1",
            "source_videos": [
                {
                    "source_id": "src_01_render_legit_mp4",
                    "path": legit_file,
                    "sha256": legit_sha,
                }
            ]
        }
        with open(os.path.join(work_dir, "input_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest_data, f)
        with open(os.path.join(work_dir, "spec_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"options": {}}, f)
        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump({
                "schema_version": "3.2",
                "run_id": "run_render_source_audit",
                "project_id": "my_project",
                "output_dir": output_dir,
            }, f)

        # 1. 尝试使用未授权的 source_id 进行渲染
        rogue_clip_plan = {
            "schema_version": "3.2",
            "run_id": "run_render_source_audit",
            "clips": [
                {
                    "id": "c01",
                    "source_id": "src_rogue_unauthorized",
                    "source_file": legit_file,
                    "source_file_sha256": legit_sha,
                    "st": 0, "end": 3,
                }
            ]
        }
        plan_to_hash = {k: v for k, v in rogue_clip_plan.items() if k != "validated_plan_hash"}
        h1 = hashlib.sha256(json.dumps(plan_to_hash, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        rogue_clip_plan["validated_plan_hash"] = h1

        with open(os.path.join(work_dir, "validated_plan.json"), "w", encoding="utf-8") as f:
            json.dump(rogue_clip_plan, f)
        with open(os.path.join(work_dir, "plan_audit.json"), "w", encoding="utf-8") as f:
            json.dump({
                "audit_status": "APPROVED",
                "passed": True,
                "run_id": "run_render_source_audit",
                "validated_plan_hash": h1,
            }, f)

        with self.assertRaises(RuntimeError) as ctx1:
            stage_render(work_dir)
        self.assertIn("非白名单素材 source_id", str(ctx1.exception))

        # 2. 尝试篡改素材哈希进行渲染
        tampered_hash_plan = {
            "schema_version": "3.2",
            "run_id": "run_render_source_audit",
            "clips": [
                {
                    "id": "c01",
                    "source_id": "src_01_render_legit_mp4",
                    "source_file": legit_file,
                    "source_file_sha256": "tampered_fake_sha256",
                    "st": 0, "end": 3,
                }
            ]
        }
        plan_to_hash2 = {k: v for k, v in tampered_hash_plan.items() if k != "validated_plan_hash"}
        h2 = hashlib.sha256(json.dumps(plan_to_hash2, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        tampered_hash_plan["validated_plan_hash"] = h2

        with open(os.path.join(work_dir, "validated_plan.json"), "w", encoding="utf-8") as f:
            json.dump(tampered_hash_plan, f)
        with open(os.path.join(work_dir, "plan_audit.json"), "w", encoding="utf-8") as f:
            json.dump({
                "audit_status": "APPROVED",
                "passed": True,
                "run_id": "run_render_source_audit",
                "validated_plan_hash": h2,
            }, f)

        with self.assertRaises(RuntimeError) as ctx2:
            stage_render(work_dir)
        self.assertIn("素材哈希与白名单绑定不匹配", str(ctx2.exception))

    def test_render_and_deliver_reject_unapproved_audit_or_run_id_mismatch(self):
        """P0-3: render 与 deliver 阶段必须强制要求 plan_audit 为 APPROVED 状态且 run_id 严格一致。"""
        work_dir = os.path.join(self.proj_dir, "work", "run_audit_mismatch")
        output_dir = os.path.join(self.proj_dir, "output", "run_audit_mismatch")
        os.makedirs(work_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        plan = {
            "schema_version": "3.2",
            "run_id": "run_audit_mismatch",
            "clips": [],
        }
        plan_to_hash = {k: v for k, v in plan.items() if k != "validated_plan_hash"}
        h = hashlib.sha256(json.dumps(plan_to_hash, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        plan["validated_plan_hash"] = h

        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump({
                "schema_version": "3.2",
                "run_id": "run_audit_mismatch",
                "project_id": "my_project",
                "output_dir": output_dir,
            }, f)
        with open(os.path.join(work_dir, "validated_plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan, f)

        # 1. audit 状态为 REJECTED
        with open(os.path.join(work_dir, "plan_audit.json"), "w", encoding="utf-8") as f:
            json.dump({
                "audit_status": "REJECTED",
                "passed": False,
                "run_id": "run_audit_mismatch",
                "validated_plan_hash": h,
            }, f)

        with self.assertRaises(ValueError) as r_ctx:
            stage_render(work_dir)
        self.assertIn("未通过质量门禁审计", str(r_ctx.exception))

        # 2. audit 的 run_id 与当前运行不符
        with open(os.path.join(work_dir, "plan_audit.json"), "w", encoding="utf-8") as f:
            json.dump({
                "audit_status": "APPROVED",
                "passed": True,
                "run_id": "other_historical_run",
                "validated_plan_hash": h,
            }, f)

        with self.assertRaises(ValueError) as r_ctx2:
            stage_render(work_dir)
        self.assertIn("不符", str(r_ctx2.exception))

    # -------------------------------------------------------------------------
    # 4. P1: 完整 SHA-256 检测大文件中间篡改
    # -------------------------------------------------------------------------
    def test_full_sha256_detects_middle_byte_tampering(self):
        """P1: 完整 SHA-256 能够精准识别大视频中间区域字节篡改，而仅采样的快速指纹保持不变。"""
        test_file = os.path.join(self.tmpdir, "sample_large_video.mp4")
        # 写入 3MB 数据 (超过 1MB 采样头部与 1MB 尾部)
        with open(test_file, "wb") as f:
            f.write(b"\x00" * (3 * 1024 * 1024))

        fast_before = compute_file_fast_fingerprint(test_file)
        full_before = compute_full_file_sha256(test_file)

        # 仅修改 1.5MB 正中间位置的 100 个字节
        with open(test_file, "r+b") as f:
            f.seek(1572864)
            f.write(b"TAMPERED_MIDDLE_BYTES" * 5)

        fast_after = compute_file_fast_fingerprint(test_file)
        full_after = compute_full_file_sha256(test_file)

        self.assertEqual(fast_before, fast_after, "首尾采样指纹未能反映中间修改（符合采样行为）")
        self.assertNotEqual(full_before, full_after, "完整 SHA-256 必须成功检测到中间修改！")
        self.assertEqual(len(full_before), 64, "完整 SHA-256 应为 64 位十六进制字符")

    # -------------------------------------------------------------------------
    # 5. P1: 同名多目录素材不冲突唯一 source_id 与 candidate_id
    # -------------------------------------------------------------------------
    def test_multi_directory_same_name_materials_have_unique_ids(self):
        """P1: 位于不同子目录但同名的素材 (如 part1/live.mp4 与 part2/live.mp4) 必须生成不冲突的 source_id 与 candidate_id。"""
        dir_a = os.path.join(self.proj_dir, "part1")
        dir_b = os.path.join(self.proj_dir, "part2")
        os.makedirs(dir_a, exist_ok=True)
        os.makedirs(dir_b, exist_ok=True)

        vid_a = os.path.join(dir_a, "live.mp4")
        vid_b = os.path.join(dir_b, "live.mp4")
        with open(vid_a, "wb") as f:
            f.write(b"video_data_from_part_1")
        with open(vid_b, "wb") as f:
            f.write(b"video_data_from_part_2")

        paths = ProjectPaths(self.proj_dir, run_id="run_multi_dir")
        manifest_mgr = InputManifest(paths)
        manifest = manifest_mgr.scan_and_resolve(explicit_video_paths=[vid_a, vid_b])

        source_ids = [s["source_id"] for s in manifest["source_videos"]]
        self.assertEqual(len(source_ids), 2)
        self.assertNotEqual(source_ids[0], source_ids[1], f"同名不同路径素材的 source_id 严禁冲突: {source_ids}")

        # 验证 CandidateGenerator 复用 InputManifest 的唯一 source_id 时生成不冲突的 candidate_id
        cg = CandidateGenerator()
        utts = [{"utterance_id": "u1", "start_ms": 0, "end_ms": 3000, "text": "优质羊绒闭眼入", "ends_sentence": True}]
        cands_a = cg.generate_candidates(utts, {"name": "羊绒衫"}, source_id=source_ids[0], source_file=vid_a)
        cands_b = cg.generate_candidates(utts, {"name": "羊绒衫"}, source_id=source_ids[1], source_file=vid_b)

        cid_a = cands_a[0]["candidate_id"]
        cid_b = cands_b[0]["candidate_id"]
        self.assertNotEqual(cid_a, cid_b, f"同名多目录候选 candidate_id 严禁冲突: {cid_a} vs {cid_b}")
        self.assertIn(source_ids[0], cid_a)
        self.assertIn(source_ids[1], cid_b)


if __name__ == "__main__":
    unittest.main()
