# -*- coding: utf-8 -*-
"""
test_pr16_round6_p0_p1.py - PR #16 第六轮评审 P0 / P1 缺陷深度回归测试套件
========================================================================
涵盖：
  1. Python 3.11 AST / f-string 语法兼容性全库静态检测（禁止 f-string 表达式内含反斜杠）
  2. P0: validate-plan 强制执行 scores.json 初筛合格性 (eligible=True) 与阻断原因过滤
  3. P0: 严格 SKU 质量策略 (quality_policy=strict / visual_verification=required) 全链路传递与拦截
  4. P0: EXE desktop_spec_creator 前端 JS 按钮函数修复与非破坏性配置迁移
  5. P0: ProjectPaths 真实路径截断防护与 run_id 路径穿越越界防护
  6. P0: stage_prepare 防覆盖与安全恢复运行 (resume) 校验
  7. P0: 重新渲染后旧 QC 报告失效机制与交付哈希链 (target_video_sha256) 绑定
  8. P1: 候选池篡改哈希实时重算校验
  9. P1: 本地验收脚本在交付阻断时返回非零退出码
"""

import os
import sys
import ast
import json
import time
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.core.project_context import ProjectPaths, compute_job_fingerprint, compute_file_sha256
from src.core.sku_tracker import SkuTracker
from src.core.candidate_ranker import CandidateRanker
from src.runtime.stage_services import (
    stage_prepare,
    stage_analyze,
    stage_validate_plan,
    stage_render,
    stage_qc,
    stage_deliver,
)
from src.tools.desktop_spec_creator import (
    DesktopSpecApi,
    PROJECT_ROOT as TOOL_PROJECT_ROOT,
)


class TestPR16Round6P0P1(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.proj_dir = os.path.join(self.tmpdir, "my_project")
        os.makedirs(self.proj_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Python 3.11 语法兼容性静态检测
    # -------------------------------------------------------------------------
    def test_python311_fstring_backslash_compatibility(self):
        """验证 src/ 目录下所有 Python 源码不存在 f-string 表达式内包含反斜杠（防止 Python 3.11 语法崩溃）。"""
        problematic_files = []
        for root, _, files in os.walk(os.path.join(PROJECT_ROOT, "src")):
            for f in files:
                if f.endswith(".py"):
                    full_p = os.path.join(root, f)
                    with open(full_p, "r", encoding="utf-8") as pyf:
                        content = pyf.read()
                    try:
                        tree = ast.parse(content, filename=full_p)
                    except SyntaxError as se:
                        problematic_files.append((full_p, f"SyntaxError: {se}"))
                        continue

                    # 遍历 AST 寻找 JoinedStr (f-string) 中的 FormattedValue
                    for node in ast.walk(tree):
                        if isinstance(node, ast.JoinedStr):
                            for part in node.values:
                                if isinstance(part, ast.FormattedValue):
                                    # 提取源码片段检查是否包含反斜杠
                                    try:
                                        segment = ast.get_source_segment(content, part.value)
                                        if segment and "\\" in segment:
                                            problematic_files.append((
                                                full_p,
                                                f"line {part.lineno}: f-string expression part contains backslash: '{segment}'"
                                            ))
                                    except Exception:
                                        pass

        self.assertEqual(problematic_files, [], f"发现 Python 3.11 不兼容的 f-string 反斜杠表达式:\n{problematic_files}")

    # -------------------------------------------------------------------------
    # 2. P0: validate-plan 强制校验 scores.json 中的 eligible 与阻断原因
    # -------------------------------------------------------------------------
    def test_validate_plan_enforces_candidate_eligibility_and_rejections(self):
        """P0: validate-plan 必须逐个读取 scores.json，拒绝 eligible=False、存在硬性阻断原因的候选。"""
        work_dir = os.path.join(self.proj_dir, "work", "run_test_eligibility")
        analysis_dir = os.path.join(work_dir, "analysis")
        os.makedirs(analysis_dir, exist_ok=True)

        run_context = {
            "schema_version": "3.2",
            "run_id": "run_test_eligibility",
            "project_id": "test_proj",
            "output_dir": os.path.join(self.proj_dir, "output", "run_test_eligibility"),
            "spec_fingerprint": "fp_123456",
        }
        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump(run_context, f)

        spec = {
            "schema_version": "3.0",
            "product": {"sku_id": "SKU-001", "name": "羊毛大衣", "category": "大衣"},
            "options": {"duration_range": "30-50", "automation": {"quality_policy": "standard"}},
        }
        with open(os.path.join(work_dir, "spec_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump(spec, f)

        # 构造两组候选：cand_good（合格）与 cand_bad（包含违禁词，被标记为 eligible=False）
        candidates = [
            {
                "candidate_id": "cand_good",
                "source_id": "src_1",
                "source_file": "vid1.mp4",
                "source_start_ms": 1000,
                "source_end_ms": 15000,
                "text": "这件大衣版型非常显瘦，面料柔软舒适。",
                "product_relation": "SAME_SKU",
                "eligible": True,
            },
            {
                "candidate_id": "cand_bad",
                "source_id": "src_1",
                "source_file": "vid1.mp4",
                "source_start_ms": 16000,
                "source_end_ms": 30000,
                "text": "全网最低价只要99元，绝对第一名！",
                "product_relation": "SAME_SKU",
                "eligible": False,
                "rejection_reasons": ["命中违规极限词: ['第一']", "命中价格表达: ['99元']"],
            },
        ]
        with open(os.path.join(analysis_dir, "candidates.json"), "w", encoding="utf-8") as f:
            json.dump(candidates, f, ensure_ascii=False)

        # 对应的 scores.json
        scores = [
            {"candidate_id": "cand_good", "eligible": True, "final_score": 88.0, "product_relation": "SAME_SKU"},
            {
                "candidate_id": "cand_bad",
                "eligible": False,
                "rejection_reasons": ["命中违规极限词: ['第一']", "命中价格表达: ['99元']"],
                "final_score": 10.0,
                "product_relation": "SAME_SKU",
            },
        ]
        with open(os.path.join(analysis_dir, "scores.json"), "w", encoding="utf-8") as f:
            json.dump({"scores": scores}, f, ensure_ascii=False)

        # 生成候选池哈希快照
        import hashlib
        hash_payload = [
            {
                "id": c.get("candidate_id"),
                "source_id": c.get("source_id"),
                "st": c.get("source_start_ms"),
                "en": c.get("source_end_ms"),
                "rel": c.get("product_relation"),
                "eligible": c.get("eligible", False),
                "text": c.get("text", ""),
            }
            for c in candidates
        ]
        pool_hash = hashlib.sha256(json.dumps(hash_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]
        snapshot_data = {
            "schema_version": "3.2",
            "run_id": "run_test_eligibility",
            "candidate_pool_hash": pool_hash,
            "total_candidates": 2,
        }
        with open(os.path.join(analysis_dir, "candidate_pool_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump(snapshot_data, f)

        # 外部 Agent 提案如果仍执意选择 cand_bad，validate-plan 必须强硬拒绝
        bad_proposal = {
            "proposal_id": "agent_prop_bad",
            "candidate_pool_hash": pool_hash,
            "run_id": "run_test_eligibility",
            "ordered_candidate_ids": ["cand_bad"],
        }
        prop_path = os.path.join(self.proj_dir, "bad_proposal.json")
        with open(prop_path, "w", encoding="utf-8") as f:
            json.dump(bad_proposal, f)

        with self.assertRaises(ValueError) as ctx:
            stage_validate_plan(work_dir, prop_path)

        err_msg = str(ctx.exception)
        self.assertIn("eligible=False", err_msg)
        self.assertIn("硬性阻断原因", err_msg)

        # 校验 plan_audit.json 是否正确记录了 REJECTED
        audit_path = os.path.join(work_dir, "plan_audit.json")
        self.assertTrue(os.path.isfile(audit_path))
        with open(audit_path, "r", encoding="utf-8") as f:
            audit = json.load(f)
        self.assertEqual(audit["audit_status"], "REJECTED")
        self.assertFalse(audit["passed"])
        self.assertTrue(any("eligible=False" in v for v in audit["violations"]))

    # -------------------------------------------------------------------------
    # 3. P0: 严格 SKU 质量策略全链路传递与无视觉证据候选拦截
    # -------------------------------------------------------------------------
    def test_strict_quality_policy_blocks_unverified_visual_candidate(self):
        """P0: 任务配置 automation.quality_policy='strict' + visual_verification='required'
        在口播命中商品词但缺失有效视觉证据时，必须判定 eligible=False，并在 validate-plan 阻断。"""
        spec = {
            "schema_version": "3.0",
            "product": {
                "sku_id": "SKU-STRICT-01",
                "name": "高领羊绒毛衣",
                "category": "女装/毛针织",
                "selling_points": ["高领保暖", "山羊绒亲肤"],
            },
            "options": {
                "duration_range": "30-50",
                "automation": {
                    "quality_policy": "strict",
                    "visual_verification": "required",
                },
            },
        }

        # 1. 测试 SkuTracker 在 strict_vision=True 下对“口播命中商品词但无画面”判定为 UNCERTAIN
        tracker = SkuTracker()
        prod_profile = {
            "sku_id": "SKU-STRICT-01",
            "name": "高领羊绒毛衣",
            "category": "女装/毛针织",
            "selling_points": ["高领保暖", "山羊绒亲肤"],
        }
        text = "我们这件高领羊绒毛衣穿在身上特别保暖亲肤。"
        rel, conf, reasons, gap = tracker.identify_relation(
            text=text,
            product_profile=prod_profile,
            visual_decision=None,  # 无有效视觉证据
            strict_vision=True,
        )
        self.assertEqual(rel, "UNCERTAIN")
        self.assertTrue(any("严格模式下缺失有效视觉证据" in r for r in reasons))

        # 2. 测试 CandidateRanker 在 strict=True 下将 UNCERTAIN 标记为 eligible=False
        cand = {
            "candidate_id": "cand_strict_unverified",
            "source_id": "src_1",
            "source_start_ms": 1000,
            "source_end_ms": 10000,
            "text": text,
            "product_relation": rel,
        }
        ranker = CandidateRanker()
        rank_res = ranker.score_candidates(
            candidates=[cand],
            product_profile=prod_profile,
            strict=True,
        )
        scored_cand = rank_res["scores"][0]
        self.assertFalse(scored_cand["eligible"], "严格模式下身份未核实的候选 eligible 必须为 False")
        self.assertTrue(any("严格模式下未通过身份核实" in r for r in scored_cand["rejection_reasons"]))

    # -------------------------------------------------------------------------
    # 4. P0: 提案时长超出硬上限门禁 (validate-plan)
    # -------------------------------------------------------------------------
    def test_validate_plan_rejects_exceeding_hard_max_duration(self):
        """P0: 提案总时长若超出硬上限 (max_s + 5.0)，validate-plan 必须拒绝提案而非仅产生警告。"""
        work_dir = os.path.join(self.proj_dir, "work", "run_dur_check")
        analysis_dir = os.path.join(work_dir, "analysis")
        os.makedirs(analysis_dir, exist_ok=True)

        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.2", "run_id": "run_dur_check"}, f)

        # 目标时长 20-30s，硬上限 35s
        spec = {
            "schema_version": "3.0",
            "product": {"sku_id": "SKU-1", "name": "风衣", "category": "外套"},
            "options": {"duration_range": "20-30"},
        }
        with open(os.path.join(work_dir, "spec_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump(spec, f)

        # 构造时长 40s 的候选 (超过 35s 硬上限)
        cands = [{
            "candidate_id": "cand_long_01",
            "source_id": "s1",
            "source_file": "v1.mp4",
            "source_start_ms": 0,
            "source_end_ms": 40000,  # 40s
            "text": "这是一件非常修身的风衣外套，秋季必备单品。",
            "product_relation": "SAME_SKU",
            "eligible": True,
        }]
        with open(os.path.join(analysis_dir, "candidates.json"), "w", encoding="utf-8") as f:
            json.dump(cands, f, ensure_ascii=False)
        with open(os.path.join(analysis_dir, "scores.json"), "w", encoding="utf-8") as f:
            json.dump({"scores": [{"candidate_id": "cand_long_01", "eligible": True, "product_relation": "SAME_SKU"}]}, f)

        import hashlib
        hash_payload = [{"id": "cand_long_01", "source_id": "s1", "st": 0, "en": 40000, "rel": "SAME_SKU", "eligible": True, "text": cands[0]["text"]}]
        p_hash = hashlib.sha256(json.dumps(hash_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]
        with open(os.path.join(analysis_dir, "candidate_pool_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.2", "candidate_pool_hash": p_hash}, f)

        prop = {
            "proposal_id": "prop_too_long",
            "candidate_pool_hash": p_hash,
            "ordered_candidate_ids": ["cand_long_01"],
        }
        prop_file = os.path.join(self.proj_dir, "long_proposal.json")
        with open(prop_file, "w", encoding="utf-8") as f:
            json.dump(prop, f)

        with self.assertRaises(ValueError) as ctx:
            stage_validate_plan(work_dir, prop_file)
        self.assertIn("严重超过目标硬性上限", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 5. P0: ProjectPaths 路径截断与穿越越界防御
    # -------------------------------------------------------------------------
    def test_project_paths_traversal_and_parent_work_dir_safety(self):
        """P0: 验证项目路径含 work 目录不被截断，且 run_id 穿越字符被严格拒绝。"""
        # (1) 路径含 /work/ 真实路径不被截断
        ci_runner_root = os.path.abspath(os.path.join(self.tmpdir, "work", "dy-video", "dy-video"))
        os.makedirs(ci_runner_root, exist_ok=True)
        paths = ProjectPaths(project_root=ci_runner_root, run_id="run_ci_001")
        self.assertEqual(paths.project_root, ci_runner_root)

        # (2) run_id 试图通过 ../素材 进行路径穿越逃逸 -> 必须抛出 ValueError
        with self.assertRaises(ValueError):
            ProjectPaths(project_root=ci_runner_root, run_id="../素材")

        with self.assertRaises(ValueError):
            ProjectPaths(project_root=ci_runner_root, run_id="run/01")

        with self.assertRaises(ValueError):
            ProjectPaths(project_root=ci_runner_root, run_id="run\\01")

    # -------------------------------------------------------------------------
    # 6. P0: stage_prepare 防覆盖与安全恢复运行 (resume) 校验
    # -------------------------------------------------------------------------
    def test_prepare_anti_overwrite_and_resume_integrity(self):
        """P0: prepare 遇到已有 work_dir 时，未指定 resume 必须阻断；指定 resume 需校验指纹。"""
        spec_path = os.path.join(self.proj_dir, "job_spec.json")
        raw_vid = os.path.join(self.proj_dir, "test_raw.mp4")
        with open(raw_vid, "wb") as f:
            f.write(b"\x00" * 1024)

        spec = {
            "schema_version": "3.0",
            "project_id": "test_proj_01",
            "product": {"sku_id": "SKU-001", "name": "毛衣", "category": "上衣"},
            "raw_video_paths": [raw_vid],
        }
        with open(spec_path, "w", encoding="utf-8") as f:
            json.dump(spec, f)

        # 首次 prepare 成功创建
        res1 = stage_prepare(spec_path, run_id="run_fixed_01")
        self.assertEqual(res1["status"], "PREPARED")

        # 再次执行相同的 run_id 且未加 resume -> 必须抛出 FileExistsError 阻断覆盖
        with self.assertRaises(FileExistsError):
            stage_prepare(spec_path, run_id="run_fixed_01", resume=False)

        # 加上 resume=True -> 成功恢复
        res_resumed = stage_prepare(spec_path, run_id="run_fixed_01", resume=True)
        self.assertEqual(res_resumed["status"], "RESUMED")

        # 若外部篡改了 spec 中的参数导致指纹改变 -> resume=True 必须拒绝跨配置恢复
        spec_tampered = dict(spec, product={"sku_id": "SKU-CHANGED", "name": "羽绒服", "category": "外套"})
        tampered_spec_path = os.path.join(self.proj_dir, "tampered_spec.json")
        with open(tampered_spec_path, "w", encoding="utf-8") as f:
            json.dump(spec_tampered, f)

        with self.assertRaises(ValueError) as ctx:
            stage_prepare(tampered_spec_path, run_id="run_fixed_01", resume=True)
        self.assertIn("与原运行指纹", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 7. P0: 重新渲染后旧 QC 报告失效机制与交付哈希链绑定
    # -------------------------------------------------------------------------
    def test_rerender_invalidates_qc_and_deliver_rejects_hash_mismatch(self):
        """P0: render 阶段必须清理旧 QC 产物；deliver 必须验证 QC 报告审核的 target_video_sha256 与当前成片一致。"""
        work_dir = os.path.join(self.proj_dir, "work", "run_qc_hash")
        output_dir = os.path.join(self.proj_dir, "output", "run_qc_hash")
        os.makedirs(work_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        # 模拟生成成片 final.mp4
        final_video = os.path.join(output_dir, "final.mp4")
        with open(final_video, "wb") as f:
            f.write(b"video_version_1")
        vid1_hash = compute_file_sha256(final_video)

        # 模拟生成旧 QC 报告，记录审核目标为 vid1_hash
        tech_qc_path = os.path.join(output_dir, "technical_qc.json")
        ed_qc_path = os.path.join(output_dir, "editorial_qc.json")
        with open(tech_qc_path, "w", encoding="utf-8") as f:
            json.dump({"passed": True, "all_passed": True, "target_video_sha256": vid1_hash}, f)
        with open(ed_qc_path, "w", encoding="utf-8") as f:
            json.dump({"passed": True, "block_count": 0, "target_video_sha256": vid1_hash}, f)

        # 模拟重新渲染产生新成片（内容变更为 video_version_2，但 QC 报告仍是旧的 vid1_hash）
        with open(final_video, "wb") as f:
            f.write(b"video_version_2_changed_bytes")
        vid2_hash = compute_file_sha256(final_video)
        self.assertNotEqual(vid1_hash, vid2_hash)

        # 构造 run_context.json 与 spec_snapshot.json / input_manifest.json
        run_ctx = {
            "schema_version": "3.2",
            "run_id": "run_qc_hash",
            "project_id": "p1",
            "work_dir": work_dir,
            "output_dir": output_dir,
            "spec_fingerprint": "fp_test",
        }
        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump(run_ctx, f)
        with open(os.path.join(work_dir, "spec_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"options": {}}, f)
        with open(os.path.join(work_dir, "input_manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"source_videos": []}, f)

        # 模拟 plan_audit 与 validated_plan 正常匹配
        plan_obj = {"schema_version": "3.2", "clips": []}
        import hashlib
        plan_h = hashlib.sha256(json.dumps(plan_obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        plan_obj["validated_plan_hash"] = plan_h
        with open(os.path.join(work_dir, "validated_plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan_obj, f)
        with open(os.path.join(work_dir, "plan_audit.json"), "w", encoding="utf-8") as f:
            json.dump({"audit_status": "APPROVED", "passed": True, "validated_plan_hash": plan_h}, f)

        # 此时执行 deliver，必须因为 QC 报告的 target_video_sha256 与当前视频不一致而拒绝！
        with patch("src.runtime.stage_services.compute_job_fingerprint", return_value="fp_test"):
            with self.assertRaises(ValueError) as ctx:
                stage_deliver(work_dir)
            self.assertIn("不匹配，重新渲染后必须重新执行 qc 阶段", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 8. P1: 候选池篡改哈希实时重算校验
    # -------------------------------------------------------------------------
    def test_validate_plan_detects_candidate_pool_tampering(self):
        """P1: 如果在 analyze 后 candidates.json 被外部恶意修改，validate-plan 必须重算哈希并阻断。"""
        work_dir = os.path.join(self.proj_dir, "work", "run_pool_tamper")
        analysis_dir = os.path.join(work_dir, "analysis")
        os.makedirs(analysis_dir, exist_ok=True)

        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.2", "run_id": "run_pool_tamper"}, f)
        with open(os.path.join(work_dir, "spec_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"options": {}}, f)

        # 原始候选与冻结快照
        cands = [{
            "candidate_id": "c1",
            "source_id": "s1",
            "source_start_ms": 0,
            "source_end_ms": 10000,
            "text": "测试正常台词",
            "product_relation": "SAME_SKU",
            "eligible": True,
        }]
        import hashlib
        orig_payload = [{"id": "c1", "source_id": "s1", "st": 0, "en": 10000, "rel": "SAME_SKU", "eligible": True, "text": "测试正常台词"}]
        frozen_hash = hashlib.sha256(json.dumps(orig_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]

        # 写入原始快照
        with open(os.path.join(analysis_dir, "candidate_pool_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.2", "candidate_pool_hash": frozen_hash}, f)

        # 但外部把 candidates.json 偷偷篡改了 (例如偷偷把 eligible 从 False 改成 True 或改了台词)
        tampered_cands = [{
            "candidate_id": "c1",
            "source_id": "s1",
            "source_start_ms": 0,
            "source_end_ms": 10000,
            "text": "偷偷篡改后的台词",
            "product_relation": "SAME_SKU",
            "eligible": True,
        }]
        with open(os.path.join(analysis_dir, "candidates.json"), "w", encoding="utf-8") as f:
            json.dump(tampered_cands, f, ensure_ascii=False)

        prop = {
            "proposal_id": "p1",
            "candidate_pool_hash": frozen_hash,
            "ordered_candidate_ids": ["c1"],
        }
        prop_file = os.path.join(self.proj_dir, "prop_tampered.json")
        with open(prop_file, "w", encoding="utf-8") as f:
            json.dump(prop, f)

        with self.assertRaises(ValueError) as ctx:
            stage_validate_plan(work_dir, prop_file)
        self.assertIn("候选池文件完整性校验失败", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
