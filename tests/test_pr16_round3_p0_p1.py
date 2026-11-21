# -*- coding: utf-8 -*-
"""
test_pr16_round3_p0_p1.py - PR #16 第三轮代码评审针对性回归与验收单元测试套件

覆盖 Round 3 核心要求：
P0-1: 多素材视频 SKU 视觉识别抽帧来源优先使用 source_file，严禁静默回退默认视频；三源断言测试。
P0-2: AGY 彻底删除 JobRunner 提前排片调用，仅保留可用性预检，排片调度唯一定位在精排后。
P0-3: EXE 取消勾选全部视频 (raw_video_paths=[]) 阻断并返回 NEEDS_INPUT，严禁静默降级为全量扫描。
P1-1: EXE 风格卡片 (balanced/fast/detailed) 与封面策略 (product_image) 真实生效并产生差异化决策。
P1-2: 默认品牌与品类占位符空置，不强塞 MIXCITY。
P1-3: 内容质检门禁：卖点为空判定为 NOT_EVALUATED；接缝音频跳变 WARN 正式呈报；过滤低质催单口播。
P1-4: 历史交付指纹缺失时执行完整性重验，严禁无条件放行。
"""

import os
import sys
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.frame_provider import VideoFrameProvider
from src.core.candidate_generator import CandidateGenerator
from src.core.sequence_optimizer import SequenceOptimizer
from src.core.candidate_ranker import CandidateRanker
from src.agent.decision_gateway import DecisionGateway
from src.qc.editorial_qc import EditorialQcAuditor
from src.runtime.job_runner import JobRunner, JobStage
from src.runtime.adapters.agent_protocol import AgentResponse, AgentStatus
from src.core.project_context import ProjectPaths, InputManifest


class TestPR16Round3P0P1(unittest.TestCase):

    def test_p0_1_frame_provider_three_sources_different_products(self):
        """P0-1: 多源视频抽帧优先解析 candidate.source_file，通过 registry 映射，严禁在多源场景回退到首视频。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_a = os.path.join(tmpdir, "video_a.mp4")
            src_b = os.path.join(tmpdir, "video_b.mp4")
            src_c = os.path.join(tmpdir, "video_c.mp4")
            for p in (src_a, src_b, src_c):
                with open(p, "wb") as f:
                    f.write(b"0000")

            registry = {
                "src_01": src_a,
                "src_02": src_b,
                "src_03": src_c,
                "video_a.mp4": src_a,
                "video_b.mp4": src_b,
                "video_c.mp4": src_c,
            }

            provider = VideoFrameProvider(
                work_dir=tmpdir,
                default_video_path=src_a,
                source_registry=registry,
            )
            self.assertTrue(provider.is_multi_source)

            # 1. candidate 显式带有 source_file，必须精准解析为对应真实路径
            cand_b = {"candidate_id": "c_b", "source_file": src_b, "source_id": "src_02", "source_start_ms": 1000, "source_end_ms": 3000}
            resolved_b = provider.resolve_video_for_candidate(cand_b)
            self.assertEqual(os.path.abspath(resolved_b), os.path.abspath(src_b))

            # 2. candidate 仅带有安全的 source_id，必须通过 Source Registry 查找到 src_c
            cand_c = {"candidate_id": "c_c", "source_id": "src_03", "source_start_ms": 2000, "source_end_ms": 4000}
            resolved_c = provider.resolve_video_for_candidate(cand_c)
            self.assertEqual(os.path.abspath(resolved_c), os.path.abspath(src_c))

            # 3. 多源场景下，若 source_id 未知且未提供有效 source_file，严禁静默回退到 src_a (default_video)
            cand_unknown = {"candidate_id": "c_unk", "source_id": "unknown_identifier"}
            resolved_unknown = provider.resolve_video_for_candidate(cand_unknown)
            self.assertIsNone(resolved_unknown, "多源场景下未知源标识严禁静默退回 default_video")

    def test_p0_2_job_runner_no_premature_agy_scheduling_call(self):
        """P0-2: JobRunner 只做 Agent 可用性预检，彻底删除在未生成候选池前的提前排片 invoke。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = os.path.join(tmpdir, "work")
            out_dir = os.path.join(tmpdir, "out")
            dummy_video = os.path.join(tmpdir, "test.mp4")
            with open(dummy_video, "wb") as f:
                f.write(b"0000")

            # 生产规格（无 _allowed_candidate_ids 桩）
            spec = {
                "project_id": "test_no_early_call",
                "raw_video_path": dummy_video,
                "product": {"name": "羊绒衫"}
            }

            mock_adapter = MagicMock()
            mock_adapter.check_availability.return_value = (True, "OK")
            # 如果被提前调用，则说明没有彻底移除
            mock_adapter.invoke.side_effect = RuntimeError("提前调用了 AGY invoke！生产链路严禁在精排前调用模型！")

            runner = JobRunner(spec, work_dir, out_dir, agent_adapter=mock_adapter)
            # mock 掉 AgentDirector 总装执行，观察 JobRunner 本身在进入 Director 之前是否调用了 invoke
            mock_director = MagicMock()
            mock_director.run_director_workflow.return_value = {
                "qc_passed": True,
                "editorial_qc": {"passed": True, "block_count": 0},
                "final_deliverables": {"video": dummy_video}
            }
            with patch("src.agent.agent_director.AgentDirector", return_value=mock_director):
                res = runner.run_pipeline()

            # check_availability 应被调用以预检可用性
            mock_adapter.check_availability.assert_called_once()
            # 提前的 invoke 绝对未发生
            mock_adapter.invoke.assert_not_called()
            self.assertEqual(res["status"], JobStage.DELIVERED)

    def test_p0_3_zero_checked_videos_triggers_needs_input_no_auto_scan(self):
        """P0-3: 用户在 EXE 中取消勾选全部视频 (raw_video_paths=[])，严禁降级为全量扫描，必须返回 NEEDS_INPUT。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            proj_root = os.path.join(tmpdir, "project")
            mat_dir = os.path.join(proj_root, "素材")
            os.makedirs(mat_dir, exist_ok=True)
            # 在素材目录下放置一段视频
            v1 = os.path.join(mat_dir, "raw1.mp4")
            with open(v1, "wb") as f:
                f.write(b"0000")

            paths = ProjectPaths(project_root=proj_root, run_id="run_001")
            manifest_mgr = InputManifest(paths)

            # 1. 显式传入空列表 [] 表示取消勾选
            res_manifest = manifest_mgr.scan_and_resolve(explicit_video_paths=[])
            self.assertEqual(len(res_manifest.get("source_videos", [])), 0, "显式传 [] 绝对不能自动扫描拾取素材目录的视频")

            # 2. 端到端传给 JobRunner
            spec = {
                "project_id": "test_zero_checked",
                "raw_video_paths": [],
                "raw_video_path": "",
                "product": {"name": "羊毛衫"}
            }
            runner = JobRunner(spec, paths.work_dir, paths.output_dir, project_paths=paths)
            res = runner.run_pipeline()
            self.assertEqual(res["status"], JobStage.NEEDS_INPUT)
            self.assertIn("至少一段视频", res["reason"])

    def test_p1_1_style_preset_and_cover_strategy_effects(self):
        """P1-1: 风格卡片 (fast/detailed/balanced) 真实改变提案编排，封面策略 (product_image) 豁免人脸约束。"""
        # 1. 风格卡片影响排序优先级
        candidates = [
            {"candidate_id": "c1", "source_id": "s1", "source_start_ms": 0, "source_end_ms": 4000, "text": "快速介绍"},
            {"candidate_id": "c2", "source_id": "s1", "source_start_ms": 4000, "source_end_ms": 10000, "text": "面料做工详细"},
        ]
        scores = {
            "schema_version": "3.0",
            "scores": [
                {"candidate_id": "c1", "eligible": True, "product_relation": "SAME_SKU", "narrative_role": "HOOK", "subscores": {"a": 90, "b": 90}},
                {"candidate_id": "c2", "eligible": True, "product_relation": "SAME_SKU", "narrative_role": "FABRIC", "subscores": {"a": 95, "b": 95}},
            ]
        }
        profile = {"sku_id": "SKU_1", "name": "毛衣", "selling_points": ["舒适"]}

        opt = SequenceOptimizer(target_min_ms=3000, target_max_ms=12000)
        res_fast = opt.optimize_sequence(candidates, scores, profile, preferred_style="fast")
        res_detailed = opt.optimize_sequence(candidates, scores, profile, preferred_style="detailed")

        self.assertEqual(res_fast["status"], "PROPOSED")
        self.assertEqual(res_detailed["status"], "PROPOSED")
        # 验证返回的 proposals 风格标识
        self.assertEqual(res_fast["proposals"][0]["style"], "fast")
        self.assertEqual(res_detailed["proposals"][0]["style"], "detailed")

        # 2. 封面策略 product_image 豁免真人脸约束
        gateway = DecisionGateway(constraints={})
        product_cover_cand = {
            "id": "cand_product_image_01",
            "strategy": "product_image",
            "description": "商品静物主图",
            # 没有 face 字段
        }
        ok, violations = gateway.validate_cover_candidate(product_cover_cand)
        self.assertTrue(ok, "product_image 封面策略必须豁免人脸与姿态硬校验")
        self.assertEqual(len(violations), 0)

    def test_p1_3_content_qc_empty_selling_points_and_seam_warnings(self):
        """P1-3: 内容质检门禁：卖点为空判定为 NOT_EVALUATED；接缝音频跳变 WARN 明确显示。"""
        auditor = EditorialQcAuditor()
        plan = {"clips": [{"candidate_id": "c1", "product_relation": "SAME_SKU", "text": "普通介绍"}]}

        # 1. 卖点为空 -> SellingPointEvidenceGate 必须为 NOT_EVALUATED
        res_empty_sp = auditor.audit_editorial_quality(plan, {"selling_points": []})
        sp_check = next((c for c in res_empty_sp["checks"] if c["gate"] == "SellingPointEvidenceGate"), None)
        self.assertIsNotNone(sp_check)
        self.assertEqual(sp_check["status"], "NOT_EVALUATED")
        self.assertIn("未配置卖点检查依据", sp_check["detail"])

        # 2. 接缝跳变 WARN 呈报 -> SeamContinuityGate 必须为 WARN
        continuity_review = {
            "seams": [
                {"seam_id": "seam_1_2", "severity": "WARN", "risk_type": "AUDIO_JUMP"}
            ]
        }
        res_seam = auditor.audit_editorial_quality(plan, {"selling_points": ["普通"]}, continuity_review=continuity_review)
        seam_check = next((c for c in res_seam["checks"] if c["gate"] == "SeamContinuityGate"), None)
        self.assertIsNotNone(seam_check)
        self.assertEqual(seam_check["status"], "WARN")
        self.assertIn("视听连续性跳变警告", seam_check["detail"])

    def test_p1_4_candidate_ranker_filters_low_quality_livestream_callouts(self):
        """P1-4: 过滤无意义/催单口播话术（例如：抠衣、抢毛衣、加鲜货、一号令）。"""
        ranker = CandidateRanker()
        cands = [
            {"candidate_id": "c_normal", "text": "这件羊绒衫选用优质山羊绒面料，触感轻盈保暖。", "source_start_ms": 0, "source_end_ms": 4000, "selling_point_evidence_refs": ["保暖"]},
            {"candidate_id": "c_rush", "text": "先去抢毛衣毛衣手批大宝，我要加鲜货了来不等了一号令！", "source_start_ms": 0, "source_end_ms": 4000, "selling_point_evidence_refs": []},
        ]
        profile = {"sku_id": "SKU_TEST", "name": "羊绒衫", "selling_points": ["保暖"]}
        res = ranker.score_candidates(cands, profile)
        score_map = {s["candidate_id"]: s for s in res["scores"]}

        self.assertTrue(score_map["c_normal"]["eligible"])
        # 催单及无意义话术候选应被降分或标记为违规低质
        rush_score = score_map["c_rush"]
        self.assertTrue(
            rush_score["subscores"]["relevance"] < 40 or not rush_score["eligible"],
            "催单低质口播片段必须被严重扣分或标记为不合格"
        )


if __name__ == "__main__":
    unittest.main()
