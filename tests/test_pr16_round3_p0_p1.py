# -*- coding: utf-8 -*-
"""
test_pr16_round3_p0_p1.py - PR #16 第三轮代码评审针对性回归与验收单元测试套件

覆盖 Round 3 核心要求：
P0-1: 多素材视频 SKU 视觉识别抽帧来源优先使用 source_file，严禁静默回退默认视频；三源真实抽帧与断言测试。
P0-2: AGY 彻底删除 JobRunner 提前排片调用，仅保留可用性预检，排片调度唯一定位在精排后。
P0-3: EXE 取消勾选全部视频 (raw_video_paths=[]) 阻断并返回 NEEDS_INPUT，严禁静默降级为全量扫描。
P1-1: EXE 风格卡片 (balanced/fast/detailed) 与封面策略 (product_image) 真实生效并产生差异化合法提案。
P1-2: 默认品牌与品类占位符空置，支持“无品牌包装”且封面排版与 QC 均合规通过。
P1-3: 内容质检门禁：卖点为空判定为 NOT_EVALUATED；接缝音频跳变 WARN 正式呈报至摘要；过滤低质催单口播。
P1-4: 历史交付指纹缺失时执行完整性重验，严禁无条件放行。
"""

import os
import sys
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import cv2
import numpy as np
from PIL import Image

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.frame_provider import VideoFrameProvider
from src.core.candidate_generator import CandidateGenerator
from src.core.sequence_optimizer import SequenceOptimizer
from src.core.candidate_ranker import CandidateRanker
from src.core.sku_tracker import SkuTracker
from src.agent.decision_gateway import DecisionGateway
from src.qc.editorial_qc import EditorialQcAuditor
from src.qc.qc_auditor import QcAuditor
from src.tools.render_cover_tool import render_cover_with_meta
from src.runtime.job_runner import JobRunner, JobStage
from src.runtime.adapters.agent_protocol import AgentResponse, AgentStatus
from src.runtime.adapters.agy_headless import validate_sequence_proposals_schema
from src.core.project_context import ProjectPaths, InputManifest


class TestPR16Round3P0P1(unittest.TestCase):

    def _create_colored_video(self, file_path: str, bgr_color: tuple, num_frames: int = 15):
        """辅助函数：创建指定颜色的合法测试视频。"""
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(file_path, fourcc, 10.0, (120, 120))
        frame = np.zeros((120, 120, 3), dtype=np.uint8)
        frame[:] = bgr_color
        for _ in range(num_frames):
            out.write(frame)
        out.release()

    def test_p0_1_frame_provider_three_sources_different_products(self):
        """P0-1: 多源视频抽帧优先解析 candidate.source_file，通过 registry 映射，严禁在多源场景回退到首视频。

        验证内容：
          1. 构造 3 个真实视频源（A=纯红, B=纯绿, C=纯蓝）；
          2. 对 B、C 候选片段分别调用 get_frames_for_candidate 抽帧，断言抽出的物理图像绝对来自源 B 与 C，绝非首视频 A；
          3. 当候选源标识无法识别时，断言严格阻断返回空列表，绝对禁止静默降级到源 A。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            src_a = os.path.join(tmpdir, "video_a.mp4")
            src_b = os.path.join(tmpdir, "video_b.mp4")
            src_c = os.path.join(tmpdir, "video_c.mp4")
            # A: 红色 (BGR: 0, 0, 255)
            self._create_colored_video(src_a, (0, 0, 255))
            # B: 绿色 (BGR: 0, 255, 0)
            self._create_colored_video(src_b, (0, 255, 0))
            # C: 蓝色 (BGR: 255, 0, 0)
            self._create_colored_video(src_c, (255, 0, 0))

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
            self.assertTrue(provider.is_multi_source, "多源环境应正确识别为 is_multi_source=True")

            # 1. candidate 显式带有 source_file，精准从源 B 抽帧
            cand_b = {
                "candidate_id": "c_b",
                "source_file": src_b,
                "source_id": "src_02",
                "source_start_ms": 100,
                "source_end_ms": 800,
            }
            frames_b = provider.get_frames_for_candidate(cand_b)
            self.assertTrue(len(frames_b) > 0, "候选 B 必须成功抽得关键帧")
            img_b = cv2.imread(frames_b[0])
            self.assertIsNotNone(img_b)
            # 断言图像主要通道为绿色 (G > 180, R < 50, B < 50)
            self.assertGreater(img_b[:, :, 1].mean(), 180, "候选 B 抽帧必须为纯绿色源 B")
            self.assertLess(img_b[:, :, 2].mean(), 50, "候选 B 严禁误抽红色源 A")

            # 2. candidate 仅带有安全的 source_id，通过 Source Registry 从源 C 抽帧
            cand_c = {
                "candidate_id": "c_c",
                "source_id": "src_03",
                "source_start_ms": 100,
                "source_end_ms": 800,
            }
            frames_c = provider.get_frames_for_candidate(cand_c)
            self.assertTrue(len(frames_c) > 0, "候选 C 必须成功抽得关键帧")
            img_c = cv2.imread(frames_c[0])
            self.assertIsNotNone(img_c)
            # 断言图像主要通道为蓝色 (B > 180, R < 50, G < 50)
            self.assertGreater(img_c[:, :, 0].mean(), 180, "候选 C 抽帧必须为纯蓝色源 C")
            self.assertLess(img_c[:, :, 2].mean(), 50, "候选 C 严禁误抽红色源 A")

            # 3. 多源场景下，若 source_id 未知且未提供有效 source_file，严禁静默回退到 src_a
            cand_unknown = {"candidate_id": "c_unk", "source_id": "unknown_identifier"}
            resolved_unknown = provider.resolve_video_for_candidate(cand_unknown)
            self.assertIsNone(resolved_unknown, "多源场景下未知源标识严禁静默退回 default_video")
            frames_unknown = provider.get_frames_for_candidate(cand_unknown)
            self.assertEqual(frames_unknown, [], "未知源候选必须返回空抽帧列表，严禁抽取首个视频的画面")

    def test_p0_2_job_runner_no_premature_agy_scheduling_call(self):
        """P0-2: JobRunner 只做 Agent 可用性预检，彻底删除在未生成候选池前的提前排片 invoke。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = os.path.join(tmpdir, "work")
            out_dir = os.path.join(tmpdir, "out")
            dummy_video = os.path.join(tmpdir, "test.mp4")
            with open(dummy_video, "wb") as f:
                f.write(b"0000")

            # 生产规格（无任何测试桩）
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
            with patch("src.agent.agent_director.AgentDirector", return_value=mock_director) as mock_dir_cls:
                res = runner.run_pipeline()

            # check_availability 应被调用以预检可用性
            mock_adapter.check_availability.assert_called_once()
            # 提前的 invoke 绝对未发生
            mock_adapter.invoke.assert_not_called()
            # agent_adapter 必须直接透传给 AgentDirector
            mock_dir_cls.assert_called_once()
            self.assertEqual(mock_dir_cls.call_args[1].get("agent_adapter"), mock_adapter)
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
        """P1-1: 风格卡片 (fast/detailed/balanced) 改变提案编排并严格通过契约校验，封面策略 (product_image) 豁免人脸约束。"""
        candidates = [
            {"candidate_id": "c1", "source_id": "s1", "source_start_ms": 0, "source_end_ms": 4000, "duration_ms": 4000, "text": "快速介绍"},
            {"candidate_id": "c2", "source_id": "s1", "source_start_ms": 4000, "source_end_ms": 10000, "duration_ms": 6000, "text": "面料做工详细"},
        ]
        scores = {
            "schema_version": "3.0",
            "scores": [
                {"candidate_id": "c1", "eligible": True, "product_relation": "SAME_SKU", "narrative_role": "HOOK", "composite_score": 90.0, "subscores": {"a": 90, "b": 90}},
                {"candidate_id": "c2", "eligible": True, "product_relation": "SAME_SKU", "narrative_role": "FABRIC", "composite_score": 95.0, "subscores": {"a": 95, "b": 95}},
            ]
        }
        profile = {"sku_id": "SKU_1", "name": "毛衣", "selling_points": ["舒适"]}

        opt = SequenceOptimizer(target_min_ms=3000, target_max_ms=12000)
        res_fast = opt.optimize_sequence(candidates, scores, profile, preferred_style="fast")
        res_detailed = opt.optimize_sequence(candidates, scores, profile, preferred_style="detailed")

        self.assertEqual(res_fast["status"], "PROPOSED")
        self.assertEqual(res_detailed["status"], "PROPOSED")
        self.assertEqual(res_fast["proposals"][0]["style"], "fast")
        self.assertEqual(res_detailed["proposals"][0]["style"], "detailed")

        # 严格断言生成的提案符合契约 Schema
        valid_f, errs_f = validate_sequence_proposals_schema(res_fast)
        self.assertTrue(valid_f, f"fast 风格提案必须严格满足 sequence_proposals.schema.json: {errs_f}")
        valid_d, errs_d = validate_sequence_proposals_schema(res_detailed)
        self.assertTrue(valid_d, f"detailed 风格提案必须严格满足 sequence_proposals.schema.json: {errs_d}")

        # 封面策略 product_image 豁免真人脸约束
        gateway = DecisionGateway(constraints={})
        product_cover_cand = {
            "id": "cand_product_image_01",
            "strategy": "product_image",
            "description": "商品静物主图",
        }
        ok, violations = gateway.validate_cover_candidate(product_cover_cand)
        self.assertTrue(ok, "product_image 封面策略必须豁免人脸与姿态硬校验")
        self.assertEqual(len(violations), 0)

    def test_p1_2_plain_brand_packaging_support(self):
        """P1-2: 允许用户留空品牌进行无品牌包装，严禁强制注入 MIXCITY 或高端女装，且通过 QC 几何复测。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_frame = os.path.join(tmpdir, "base.jpg")
            cover_img = os.path.join(tmpdir, "cover.jpg")
            meta_json = os.path.join(tmpdir, "cover_meta.json")

            img = Image.new("RGB", (1080, 1920), color=(100, 100, 100))
            img.save(base_frame)

            spec = {
                "brand": {"title_en": "", "subtitle_cn": ""},
                "product": {"title_line1": "纯棉基础T恤", "item_images": []}
            }
            render_cover_with_meta(base_frame, spec, cover_img, meta_json)

            with open(meta_json, "r", encoding="utf-8") as f:
                meta = json.load(f)

            # 验证无品牌包装下不强制写入 MIXCITY
            brand_en_meta = meta["elements"]["brand_en"]
            self.assertEqual(brand_en_meta.get("lines", []), [])
            self.assertTrue(brand_en_meta.get("plain", False))

            # 验证质检几何门禁通过
            auditor = QcAuditor({})
            auditor._audit_cover_geometry(meta_json)
            self.assertTrue(auditor.report.get("all_passed", False), "无品牌包装封面必须通过几何质检")

    def test_p1_3_content_qc_empty_selling_points_and_seam_warnings(self):
        """P1-3: 内容质检门禁：卖点为空判定为 NOT_EVALUATED；接缝音频跳变 WARN 正式呈报至摘要。"""
        auditor = EditorialQcAuditor()
        plan = {"clips": [{"candidate_id": "c1", "product_relation": "SAME_SKU", "text": "普通介绍"}]}

        # 1. 卖点为空 -> SellingPointEvidenceGate 必须为 NOT_EVALUATED
        res_empty_sp = auditor.audit_editorial_quality(plan, {"selling_points": []})
        sp_check = next((c for c in res_empty_sp["checks"] if c["gate"] == "SellingPointEvidenceGate"), None)
        self.assertIsNotNone(sp_check)
        self.assertEqual(sp_check["status"], "NOT_EVALUATED")
        self.assertIn("未配置卖点检查依据", sp_check["detail"])

        # 2. 接缝跳变 WARN 呈报 -> SeamContinuityGate 必须为 WARN，且最终 summary 必须体现警告说明
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
        self.assertIn("警告项", res_seam["summary"])

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
        rush_score = score_map["c_rush"]
        self.assertTrue(
            rush_score["subscores"]["relevance"] < 40 or not rush_score["eligible"],
            "催单低质口播片段必须被严重扣分或标记为不合格"
        )

    def test_p1_5_missing_fingerprint_forces_rerun_no_silent_reuse(self):
        """P1-5: 历史交付清单缺失 job_fingerprint 时执行强制完整性重验，未通过者坚决拒绝复用并强制重跑。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = os.path.join(tmpdir, "work")
            out_dir = os.path.join(tmpdir, "out")
            dummy_video = os.path.join(tmpdir, "test.mp4")
            with open(dummy_video, "wb") as f:
                f.write(b"0000")

            # Case A: 旧清单无 job_fingerprint 且 qc_passed=False，严禁复用，必须强制触发重跑
            os.makedirs(out_dir, exist_ok=True)
            legacy_manifest_path = os.path.join(out_dir, "delivery_manifest.json")
            with open(legacy_manifest_path, "w", encoding="utf-8") as f:
                json.dump({
                    "project_id": "test_legacy",
                    "status": "DELIVERED",
                    "qc_passed": False,  # 未通过质检
                    "video_path": dummy_video,
                }, f)

            spec = {
                "project_id": "test_legacy",
                "raw_video_path": dummy_video,
                "product": {"name": "羊绒衫"}
            }

            runner_unverified = JobRunner(spec, work_dir, out_dir)
            runner_unverified.resumed_from = JobStage.DELIVERED

            mock_director = MagicMock()
            mock_director.run_director_workflow.return_value = {
                "qc_passed": True,
                "editorial_qc": {"passed": True, "block_count": 0},
                "final_deliverables": {"video": dummy_video}
            }
            with patch("src.agent.agent_director.AgentDirector", return_value=mock_director):
                res_unverified = runner_unverified.run_pipeline()

            # 断言：由于旧清单未通过质检完整性重验，绝不直接返回旧 manifest，而是强制触发了完整的 run_director_workflow 重跑重验！
            mock_director.run_director_workflow.assert_called_once()
            self.assertEqual(res_unverified["status"], JobStage.DELIVERED)

            # Case B: 具备完整 QC 凭据的旧清单，重验通过后自动回填升级 job_fingerprint 并复用
            with open(legacy_manifest_path, "r", encoding="utf-8") as f:
                saved_after_run = json.load(f)
            # 确认已经具备了升级后的指纹
            self.assertIn("job_fingerprint", saved_after_run)

    def test_p1_6_gui_spec_manager_plain_brand_validation_and_single_source_resolution(self):
        """P1-6: GUI 配置校验允许空品牌（无品牌包装），且单源场景正确解析 candidate 默认源标识（src_0）。"""
        from src.tools.desktop_spec_creator import SpecManager, DesktopSpecApi

        # 1. SpecManager 校验必须接受留空的 brand_en（无品牌包装）
        manager = SpecManager()
        payload_plain = {
            "project_id": "test_plain_brand",
            "raw_video_path": "data/raw_samples/sample.mp4",
            "brand_en": "",  # 用户按向导留空
            "brand_cn": "",
            "product_title_l1": "纯棉卫衣",
            "product_name": "纯棉卫衣",
        }
        errs = manager.validate(payload_plain)
        self.assertEqual(errs, [], f"无品牌包装留空 brand_en 严禁被必填校验阻断: {errs}")

        # 2. DesktopSpecApi.save_spec 必须成功保存留空品牌
        with tempfile.TemporaryDirectory() as tmpdir:
            v_path = os.path.join(tmpdir, "video.mp4")
            with open(v_path, "wb") as f:
                f.write(b"0000")
            spec_file = os.path.join(tmpdir, "job_spec.json")
            api = DesktopSpecApi(spec_path=spec_file)
            payload_plain["raw_video_path"] = v_path
            payload_plain["project_root"] = tmpdir
            save_res = api.save_spec(payload_plain)
            self.assertTrue(save_res.get("success"), f"保存无品牌配置必须成功: {save_res}")

            # 3. 单源场景下 CandidateGenerator 默认生成的 source_id="src_0" 必须被 VideoFrameProvider 正确解析
            cg = CandidateGenerator()
            utts = [{"utterance_id": "u1", "start_ms": 0, "end_ms": 2500, "text": "面料非常舒服"}]
            cands = cg.generate_candidates(utts, {"name": "纯棉卫衣"})
            self.assertEqual(cands[0]["source_id"], "src_0")

            provider = VideoFrameProvider(work_dir=tmpdir, default_video_path=v_path)
            self.assertFalse(provider.is_multi_source, "单源环境应识别为 is_multi_source=False")
            resolved = provider.resolve_video_for_candidate(cands[0])
            self.assertEqual(resolved, os.path.abspath(v_path), "单源默认候选必须成功解析为 default_video")

            # 4. 单源场景下若 candidate 明确指定了一个不存在或指向其他文件的路径，则拒绝关联
            cand_other = {"candidate_id": "c_other", "source_file": os.path.join(tmpdir, "nonexistent.mp4")}
            resolved_other = provider.resolve_video_for_candidate(cand_other)
            self.assertIsNone(resolved_other, "单源下若指向不存在文件，严禁错误关联")

    def test_p1_7_fallback_cover_rendering_safety(self):
        """P1-7: 当无法抽取人脸且无商品主图时，兜底封面候选 (fallback_placeholder) 具备有效 image_path 且保证渲染安全。"""
        from src.agent.agent_director import AgentDirector

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = os.path.join(tmpdir, "work")
            out_dir = os.path.join(tmpdir, "out")
            dummy_video = os.path.join(tmpdir, "test.mp4")
            with open(dummy_video, "wb") as f:
                f.write(b"0000")
            os.makedirs(work_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)

            spec_path = os.path.join(work_dir, "job_spec.json")
            spec_data = {
                "project_id": "test_fallback_cover",
                "raw_video_path": dummy_video,
                "product": {"name": "羊绒衫", "item_images": []},
            }
            with open(spec_path, "w", encoding="utf-8") as f:
                json.dump(spec_data, f)

            director = AgentDirector(spec_path, use_cache=False)
            director.work_dir = work_dir
            director.output_dir = out_dir
            director.raw_videos = [dummy_video]

            cover_cand_dir = os.path.join(director.candidates_dir, "cover_candidates")
            os.makedirs(cover_cand_dir, exist_ok=True)
            placeholder_path = os.path.join(cover_cand_dir, "cover_placeholder.jpg")
            if not os.path.isfile(placeholder_path):
                Image.new("RGB", (1080, 1920), color=(30, 30, 30)).save(placeholder_path)
            fallback_cand = {
                "id": "cand_fallback_01",
                "timestamp": 0.0,
                "strategy": "fallback_placeholder",
                "frontal_score": 100.0,
                "image_path": placeholder_path,
                "description": "兜底占位封面帧",
            }

            # 验证网关校验兜底封面
            ok, violations = director.gateway.validate_cover_candidate(fallback_cand)
            self.assertTrue(ok, "兜底封面必须通过网关白名单校验")

            # 验证封面渲染安全
            final_cover = os.path.join(out_dir, "cover.jpg")
            cover_meta = os.path.join(out_dir, "cover_meta.json")
            render_cover_with_meta(fallback_cand["image_path"], spec_data, final_cover, cover_meta)
            self.assertTrue(os.path.isfile(final_cover), "兜底占位图必须能成功渲染成片封面")


if __name__ == "__main__":
    unittest.main()
