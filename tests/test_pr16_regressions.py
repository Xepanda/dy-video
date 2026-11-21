# -*- coding: utf-8 -*-
"""
test_pr16_regressions.py - PR #16 严格评审要求之 10 项全量回归测试

覆盖以下 10 项核心场景：
1. 没有 AGY 或授权失效：返回明确的 Agent 阻塞状态，禁止静默使用本地兜底冒充 Agent 决策
2. 商品为针织衫，素材中穿插裤装讲解：正确区分本品与搭配讲解，不因硬编码关键词误杀搭配镜头
3. 缺少候选帧或视觉识别失败：strict 模式下不得将文本匹配结果冒充为已验证的视觉证据
4. 长块句末附近没有 scene cut：分镜拆分前后内容守恒，不得丢失语句
5. ASR 在逗号或半句话处强制切分：识别为 INCOMPLETE_SENTENCE，不得被候选生成器视作完整单句
6. 所有合格候选高度重叠，实际不足 45 秒：明确返回素材不足，禁止静默降级到无约束拼接
7. 商品为羽绒服，候选属于面料介绍：不得生成“羊毛面料”等未经证实的商品声明
8. 工程 QC 通过、内容 QC 失败：生成最终状态不得为 DELIVERED，必须准确反映审核阻塞
9. 多源视频出现真实音量差：执行增益匹配，并在拼接处平滑过渡
10. 任务中断后重新启动：根据 checkpoint 恢复，跳过已完成阶段，保持幂等
"""

import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.runtime.adapters.agent_protocol import AgentStatus, AgentResponse
from src.runtime.adapters.agy_headless import AgyHeadlessAdapter
from src.runtime.job_runner import JobRunner, JobStage
from src.core.candidate_ranker import CandidateRanker
from src.core.sku_tracker import SkuTracker
from src.core.vision_backend import VisionBackend
from src.tools.build_plan import _split_long_block
from src.core.utterance_aligner import UtteranceAligner
from src.core.sequence_optimizer import SequenceOptimizer, InsufficientEvidenceError
from src.core.plan_compiler import _derive_verified_sticker, compile_v3_plan
from src.qc.editorial_qc import EditorialQcAuditor
from src.qc.continuity_qc import ContinuityAuditor
from src.core.cut_optimizer import CutOptimizer
from src.core.video_assembler import VideoAssembler
from src.core.project_context import compute_job_fingerprint


class TestPR16Regressions(unittest.TestCase):

    def test_scenario_01_agy_missing_or_unauthorized_blocks_safely(self):
        """场景 1：没有 AGY 或授权失效 -> 返回明确的 Agent 阻塞状态，禁止静默冒充。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = os.path.join(tmpdir, "work")
            out_dir = os.path.join(tmpdir, "out")
            dummy_video = os.path.join(tmpdir, "dummy.mp4")
            with open(dummy_video, "wb") as f:
                f.write(b"0000")

            spec = {
                "project_id": "test_auth_fail",
                "raw_video_path": dummy_video,
                "product": {"name": "针织衫"}
            }

            # 模拟 adapter 返回 AUTH_REQUIRED
            mock_adapter = MagicMock()
            mock_adapter.check_availability.return_value = (True, "OK")
            mock_adapter.invoke.return_value = AgentResponse(
                status=AgentStatus.AUTH_REQUIRED,
                error_message="Agent not authenticated. Please run `agy auth login`."
            )

            runner = JobRunner(spec, work_dir, out_dir, agent_adapter=mock_adapter)
            res = runner.run_pipeline()

            # 必须阻塞在 AGENT_AUTH_REQUIRED，不能静默当成成功
            self.assertEqual(runner.state, JobStage.AGENT_AUTH_REQUIRED)
            self.assertEqual(res["status"], JobStage.AGENT_AUTH_REQUIRED)
            self.assertIn("未登录", res["reason"])

    def test_scenario_02_sweater_with_pants_styling_not_falsely_killed(self):
        """场景 2：商品为针织衫，素材穿插裤装讲解 -> 搭配讲解不被关键词硬过滤误杀。"""
        ranker = CandidateRanker()
        product_profile = {
            "name": "高领针织衫",
            "category": "针织衫",
            "selling_points": ["羊绒感", "保暖", "修身"]
        }
        candidate = {
            "candidate_id": "cand_styling_01",
            "text": "今天身上穿的这件高领针织衫，下半身我们搭配一条高腰垂感西裤，拉长身型比例。",
            "source_start_ms": 1000,
            "source_end_ms": 6000,
            "duration_ms": 5000,
            "selling_point_evidence_refs": ["修身"],
            "product_relation": "ACCESSORY_OR_STYLING"
        }
        res = ranker.score_candidates([candidate], product_profile)
        scores = res.get("scores", [])
        self.assertEqual(len(scores), 1)
        scored = scores[0]
        # 不应被硬过滤判为 ineligible
        self.assertTrue(scored.get("eligible", True), "搭配讲解不应被硬过滤为不可选")
        self.assertEqual(scored.get("narrative_role"), "STYLING")

    def test_scenario_03_missing_frame_strict_mode_rejects_false_same_sku(self):
        """场景 3：缺少候选帧或视觉识别失败 -> strict 模式不得将文本匹配结果冒充已验证视觉证据。"""
        tracker = SkuTracker()
        product_profile = {
            "sku_id": "SKU-DOWN-01",
            "name": "短款立领羽绒服",
            "category": "羽绒服",
            "reference_image_ids": ["img1.jpg"],
            "selling_points": ["白鸭绒"]
        }
        text = "这款立领羽绒服非常保暖舒服。"
        # 1. 单句识别测试：没有提供 visual_decision，且处于 strict 模式
        rel, conf, reasons, gap = tracker.identify_relation(
            text, product_profile, visual_decision=None, strict_vision=True
        )
        self.assertNotEqual(rel, "SAME_SKU", "strict 模式缺失视觉证据不得认定为 SAME_SKU")
        self.assertEqual(rel, "UNCERTAIN")
        self.assertTrue(any("UNVERIFIABLE" in g for g in gap))

        # 2. 批量候选追踪测试：track_candidates 在 strict 模式下无帧提供同样坚决阻断 SAME_SKU
        candidates = [
            {"candidate_id": "cand_01", "text": text, "source_start_ms": 0, "source_end_ms": 3000}
        ]
        res = tracker.track_candidates(candidates, product_profile, frame_provider=None, strict_vision=True)
        dec = res["decisions"][0]
        self.assertNotEqual(dec["relation"], "SAME_SKU", "track_candidates strict 模式无帧不得判定为 SAME_SKU")
        self.assertEqual(dec["relation"], "UNCERTAIN")
        self.assertEqual(candidates[0]["product_relation"], "UNCERTAIN")

    def test_scenario_04_scene_cuts_conservation_in_long_block_split(self):
        """场景 4：长块句末附近没有 scene cut -> 分镜拆分前后内容守恒，不得丢失语句。"""
        # 5 个语句，总长 20 秒
        utts = [
            {"start": 0.0, "end": 4.0, "dur": 4.0, "text": "第一句话讲解面料。"},
            {"start": 4.0, "end": 8.0, "dur": 4.0, "text": "第二句话讲解版型。"},
            {"start": 8.0, "end": 12.0, "dur": 4.0, "text": "第三句话讲解颜色。"},
            {"start": 12.0, "end": 16.0, "dur": 4.0, "text": "第四句话讲解尺码。"},
            {"start": 16.0, "end": 20.0, "dur": 4.0, "text": "第五句话讲解优惠。"}
        ]
        block = {
            "start": 0.0,
            "end": 20.0,
            "dur": 20.0,
            "segments": utts
        }
        # 镜头切点位于 100.0s，与上述语句没有任何重叠
        scene_cuts = [100.0, 105.0]
        # max_sec = 9.0s 强制拆分
        res_blocks = _split_long_block(block, max_sec=9.0, min_sec=3.0, scene_cuts=scene_cuts)

        # 检查所有 5 个语句是否全部完整保留在拆分出的块中
        flattened_texts = [s["text"] for b in res_blocks for s in b["segments"]]
        expected_texts = [u["text"] for u in utts]
        self.assertEqual(flattened_texts, expected_texts, "拆分后所有语句必须守恒，不得因切点不匹配丢失")

    def test_scenario_05_incomplete_sentence_marked_risk(self):
        """场景 5：ASR 在逗号或半句话处强制切分 -> 标记 INCOMPLETE_SENTENCE。"""
        aligner = UtteranceAligner()
        # 从句逗号结尾，强制 flush（例如达到单句最大上限）
        raw_segs = [
            {"start": 0.0, "end": 8.0, "text": "因为这款面料经过特殊的拉毛复合工艺，"}
        ]
        utts = aligner.align_segments(raw_segs, source_id="src_01")
        self.assertEqual(len(utts), 1)
        self.assertFalse(utts[0]["ends_sentence"], "逗号结尾不应判定为完整单句")
        self.assertIn("INCOMPLETE_SENTENCE", utts[0]["risks"], "未完结语句必须标记 INCOMPLETE_SENTENCE 风险")

    def test_scenario_06_insufficient_evidence_stops_cleanly_no_silent_fallback(self):
        """场景 6：所有合格候选总时长不足 45 秒 -> 明确返回 INSUFFICIENT_EVIDENCE，禁止静默降级。"""
        optimizer = SequenceOptimizer(target_min_ms=45000, target_max_ms=60000)
        # 仅有 2 个候选，合计 8 秒
        short_cands = [
            {
                "candidate_id": "c1",
                "narrative_role": "HOOK",
                "source_start_ms": 0,
                "source_end_ms": 4000,
                "duration_ms": 4000,
                "eligible": True,
                "selling_point_evidence_refs": ["保暖"]
            },
            {
                "candidate_id": "c2",
                "narrative_role": "FABRIC",
                "source_start_ms": 4000,
                "source_end_ms": 8000,
                "duration_ms": 4000,
                "eligible": True,
                "selling_point_evidence_refs": ["显瘦"]
            }
        ]
        scores = {
            "scores": [
                {"candidate_id": "c1", "eligible": True, "product_relation": "SAME_SKU", "composite_score": 90.0, "subscores": {"relevance": 90}},
                {"candidate_id": "c2", "eligible": True, "product_relation": "SAME_SKU", "composite_score": 85.0, "subscores": {"relevance": 85}}
            ]
        }
        product_profile = {"sku_id": "SKU-TEST", "selling_points": ["保暖", "显瘦"]}
        opt_res = optimizer.optimize_sequence(short_cands, scores, product_profile)
        self.assertEqual(opt_res["status"], "INSUFFICIENT_EVIDENCE")
        self.assertTrue(len(opt_res["evidence_gap"]) > 0)

        # 真实端到端验证：当导演中枢收到 INSUFFICIENT_EVIDENCE 时，坚决抛出 InsufficientEvidenceError，
        # JobRunner 捕获后置入 NEEDS_INPUT 状态，绝对禁止静默降级到启发式拼接！
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = os.path.join(tmpdir, "work")
            out_dir = os.path.join(tmpdir, "out")
            dummy_video = os.path.join(tmpdir, "dummy.mp4")
            with open(dummy_video, "wb") as f:
                f.write(b"0000")
            spec = {
                "project_id": "test_ie_block",
                "raw_video_path": dummy_video,
                "product": {"name": "针织衫"}
            }
            # 1. 模拟 Agent 直接返回 INSUFFICIENT_EVIDENCE
            mock_adapter = MagicMock()
            mock_adapter.check_availability.return_value = (True, "OK")
            mock_adapter.invoke.return_value = AgentResponse(
                status=AgentStatus.SUCCESS,
                structured_output={"status": "INSUFFICIENT_EVIDENCE", "evidence_gap": ["时长不足 45 秒"]}
            )
            runner_agent = JobRunner(spec, work_dir, out_dir, agent_adapter=mock_adapter)
            res_agent = runner_agent.run_pipeline()
            self.assertEqual(res_agent["status"], JobStage.NEEDS_INPUT)
            self.assertIn("素材证据不足", res_agent["reason"])

            # 2. 模拟确定性导演总装抛出 InsufficientEvidenceError
            mock_director = MagicMock()
            mock_director.run_director_workflow.side_effect = InsufficientEvidenceError("本品素材证据不足: 可用本品素材累计时长不足 (8.0s < 目标下限 45.0s)")
            with patch("src.agent.agent_director.AgentDirector", return_value=mock_director):
                runner = JobRunner(spec, work_dir, out_dir, agent_adapter=None)
                res = runner.run_pipeline()
                self.assertEqual(res["status"], JobStage.NEEDS_INPUT)
                self.assertIn("本品素材证据不足", res["reason"])

    def test_scenario_07_down_jacket_not_generating_wool_claim(self):
        """场景 7：商品为羽绒服，面料镜头 -> 不得生成‘羊毛面料’等虚假宣传。"""
        product_profile = {
            "sku_id": "SKU-DOWN",
            "name": "高蓬松白鸭绒羽绒服",
            "category": "羽绒服",
            "fabric": "90白鸭绒",
            "selling_points": ["轻量保暖", "防风防水"]
        }
        cand = {
            "candidate_id": "c_fabric",
            "narrative_role": "FABRIC",
            "text": "里料填充了高品质 90 白鸭绒，特别保暖。",
            "matched_selling_points": ["轻量保暖"]
        }
        sticker = _derive_verified_sticker(cat="fabric", role="FABRIC", idx=2, total_clips=4, cand=cand, product=product_profile)
        self.assertNotIn("羊毛", sticker, "羽绒服面料不得生成‘羊毛面料’贴纸")
        self.assertTrue("白鸭绒" in sticker or "轻量保暖" in sticker or "面料" in sticker)

    def test_scenario_08_content_qc_failure_blocks_delivered_status(self):
        """场景 8：工程 QC 通过、内容 QC 失败 -> 生成最终状态不得为 DELIVERED，必须记录 FAILED。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = os.path.join(tmpdir, "work")
            out_dir = os.path.join(tmpdir, "out")
            dummy_video = os.path.join(tmpdir, "dummy.mp4")
            with open(dummy_video, "wb") as f:
                f.write(b"0000")

            spec = {
                "project_id": "qc_fail_test",
                "raw_video_path": dummy_video,
                "product": {"sku_id": "SKU-1", "name": "风衣"}
            }
            mock_adapter = MagicMock()
            mock_adapter.check_availability.return_value = (True, "OK")
            mock_adapter.invoke.return_value = AgentResponse(
                status=AgentStatus.SUCCESS,
                structured_output={
                    "status": "PROPOSED",
                    "proposals": [{
                        "proposal_id": "p1",
                        "ordered_candidate_ids": ["c1"]
                    }]
                }
            )

            runner = JobRunner(spec, work_dir, out_dir, agent_adapter=mock_adapter)

            mock_director = MagicMock()
            mock_director.run_director_workflow.return_value = {
                "qc_passed": True,  # 工程技术 QC 通过
                "editorial_qc": {
                    "passed": False,  # 内容 QC 失败
                    "block_count": 2,
                    "checks": [{"gate": "ProductIdentityGate", "status": "BLOCK"}]
                }
            }

            with patch("src.agent.agent_director.AgentDirector", return_value=mock_director):
                res = runner.run_pipeline()

            self.assertNotEqual(res["status"], JobStage.DELIVERED, "内容 QC 失败绝对不能为 DELIVERED")
            self.assertEqual(res["status"], JobStage.FAILED)
            self.assertIn("内容审核门禁阻断", res.get("reason", ""))
            self.assertFalse(res.get("qc_passed", True))

    def test_scenario_09_multi_source_audio_jump_triggers_gain_match(self):
        """场景 9：多源视频出现真实音量差 -> 执行增益匹配并在拼接处平滑过渡。"""
        clips = [
            {"candidate_id": "c1", "source_file": "vid_a.mp4", "st": 0.0, "end": 5.0, "dur": 5.0},
            {"candidate_id": "c2", "source_file": "vid_b.mp4", "st": 10.0, "end": 15.0, "dur": 5.0}
        ]
        # 构造跨源音量突变
        report = {
            "seams": [{
                "seam_id": "seam_01",
                "left_candidate_id": "c1",
                "right_candidate_id": "c2",
                "risk_type": "AUDIO_JUMP",
                "suggested_action": "GAIN_MATCH",
                "gain_adjustment_db": -4.5
            }]
        }
        optimizer = CutOptimizer()
        fixed_clips, actions = optimizer.optimize_cuts(clips, report)
        self.assertTrue(any("增益匹配" in a for a in actions))
        self.assertTrue(fixed_clips[0].get("_audio_gain_match", False))

        from src.core.video_assembler import build_transition_chain
        fc, v_out, a_out = build_transition_chain(
            2, False, 0.0, False,
            timeline={"c1": {"dur": 5.0}, "c2": {"dur": 5.0}},
            micro_afade_sec=0.015
        )
        self.assertTrue(any("afade" in line for line in fc), "启用了增益匹配时必须注入 afade 音频平滑淡入淡出")

    def test_scenario_10_checkpoint_resumption_and_idempotence(self):
        """场景 10：任务中断后重新启动 -> 根据 checkpoint 恢复，跳过已完成阶段，支持幂等交付与有限重试。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = os.path.join(tmpdir, "work")
            out_dir = os.path.join(tmpdir, "out")
            dummy_mp4 = os.path.join(out_dir, "resume_test.mp4")
            os.makedirs(work_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)
            with open(dummy_mp4, "wb") as f:
                f.write(b"0000")

            spec = {
                "project_id": "resume_test",
                "raw_video_path": dummy_mp4,
                "product": {"name": "羊绒衫"}
            }

            # 1. 幂等交付验证：当 checkpoint 记录为 DELIVERED 且成片已落盘，直接复用成片，不重复执行分析
            delivery_manifest = {
                "project_id": "resume_test",
                "status": "DELIVERED",
                "job_fingerprint": compute_job_fingerprint(spec),
                "video_path": dummy_mp4,
                "qc_passed": True,
            }
            with open(os.path.join(out_dir, "delivery_manifest.json"), "w", encoding="utf-8") as f:
                json.dump(delivery_manifest, f)
            with open(os.path.join(work_dir, "checkpoint.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "project_id": "resume_test",
                    "current_state": "DELIVERED",
                    "retry_count": 0,
                    "updated_at": 1000000000
                }, f)

            runner_idempotent = JobRunner(spec, work_dir, out_dir)
            self.assertEqual(runner_idempotent.state, JobStage.DELIVERED)
            res_idempotent = runner_idempotent.run_pipeline()
            self.assertEqual(res_idempotent.get("status"), "DELIVERED")
            self.assertEqual(res_idempotent.get("video_path"), dummy_mp4)

            # 2. 有限重试与失败阻断：当达到 max_retries 时，坚决阻断重复返工
            with open(os.path.join(work_dir, "checkpoint.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "project_id": "resume_test",
                    "current_state": "FAILED",
                    "retry_count": 2,
                    "updated_at": 1000000000
                }, f)
            runner_failed = JobRunner(spec, work_dir, out_dir, max_retries=2)
            res_failed = runner_failed.run_pipeline()
            self.assertEqual(res_failed["status"], JobStage.FAILED)
            self.assertIn("已达最大重试次数", res_failed["reason"])

    def test_scenario_11_missing_product_relation_strictly_blocked_by_qc(self):
        """场景 11：分镜缺失 product_relation -> compile_v3_plan 标记 UNVERIFIABLE，QC 坚决 BLOCK 拦截。"""
        cand = {
            "candidate_id": "c_no_relation",
            "source_start_ms": 0,
            "source_end_ms": 4000,
            "text": "这是一件没有经过商品关系认定的测试片段",
            # 缺失 product_relation 字段
        }
        proposal = {
            "proposal_id": "p_test",
            "ordered_candidate_ids": ["c_no_relation"],
        }
        spec = {"product": {"name": "针织衫"}}
        plan = compile_v3_plan(proposal, [cand], {}, spec)
        clip = plan["clips"][0]
        self.assertEqual(clip["product_relation"], "UNVERIFIABLE", "缺失商品关系的片段必须标记为 UNVERIFIABLE")

        auditor = EditorialQcAuditor(policy="strict")
        report = auditor.audit_editorial_quality(plan, {"name": "针织衫"})
        self.assertFalse(report["passed"], "缺失商品关系的分镜严禁通过内容质检")
        self.assertGreaterEqual(report["block_count"], 1)
        id_check = next(c for c in report["checks"] if c["gate"] == "ProductIdentityGate")
        self.assertEqual(id_check["status"], "BLOCK")
        self.assertIn("c_no_relation", id_check["affected_clips"])


def main():
    suite = unittest.TestLoader().loadTestsFromTestCase(TestPR16Regressions)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
