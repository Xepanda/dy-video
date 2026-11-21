# -*- coding: utf-8 -*-
"""
test_pr16_round4_p0_p1.py - PR #16 第四轮评审 P0 / P1 缺陷深度回归测试
========================================================================
涵盖：
  P0-1: 商品身份严格门禁防搭配话术绕过 (SkuTracker + CandidateRanker)
  P0-2: 旧版交付清单缺失指纹时拒绝错误成片复用 (JobRunner)
  P1-1: 无品牌包装配置全链路保存/启动及默认品类不误导 (SpecManager + DesktopSpecApi)
  P1-2: 占位商品图隔离于项目 work 目录，不污染源码仓库 (make_placeholder_cards + AgentDirector)
  P1-3: 取消令牌即时中断底层运行中的 AGY / FFmpeg 子进程 (agy_headless + video_assembler)
  P1-4: 目录安全清理路径 containment 判定，杜绝同前缀误判 (ProjectPaths)
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
import subprocess
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.core.sku_tracker import SkuTracker
from src.core.candidate_ranker import CandidateRanker
from src.core.project_context import ProjectPaths, compute_job_fingerprint
from src.runtime.job_runner import JobRunner, JobStage, CancellationToken, JobCanceledError
from src.runtime.adapters.agent_protocol import AgentResponse, AgentStatus
from src.tools.desktop_spec_creator import SpecManager, DesktopSpecApi
from src.tools.make_placeholder_cards import ensure_placeholder_cards


class TestPR16Round4P0P1(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.work_dir = os.path.join(self.tmpdir, "work")
        self.out_dir = os.path.join(self.tmpdir, "out")
        os.makedirs(self.work_dir, exist_ok=True)
        os.makedirs(self.out_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # P0-1: 搭配话术绕过商品身份门禁修复验证
    # -------------------------------------------------------------------------
    def test_p0_1_sku_tracker_strict_styling_blocks_without_visual_evidence(self):
        """P0-1 (a): SkuTracker 在严格模式下，即使口播命中搭配话术，缺失有效视觉证据时严禁返回 ACCESSORY_OR_STYLING。"""
        tracker = SkuTracker()
        product_profile = {
            "sku_id": "SKU-SWEATER-01",
            "name": "羊绒高领毛衣",
            "category": "女装/毛针织",
            "reference_image_ids": ["img1.jpg"],
            "quality_policy": "strict",
            "strict": True,
        }
        text = "我们身上这件衣服下半身搭一条高腰垂感西裤姐妹们看下。"

        # 1. 严格模式 + 缺失视觉证据 (visual_decision=None) -> 必须返回 UNCERTAIN，证据缺口标记 UNVERIFIABLE
        rel, conf, reasons, gap = tracker.identify_relation(
            text, product_profile, visual_decision=None, strict_vision=True
        )
        self.assertEqual(rel, "UNCERTAIN", f"严格模式下无视觉证据严禁判定为合规搭配: {rel}")
        self.assertLessEqual(conf, 0.45, "缺失视觉证据置信度必须低于门槛")
        self.assertTrue(any("UNVERIFIABLE" in g for g in gap), "必须显式记录视觉未核实缺口")

        # 2. 严格模式 + 画面判定为 OTHER_SKU -> 必须返回 OTHER_SKU
        mock_vis_other = {
            "vision_available": True,
            "sku_relation": "OTHER_SKU",
            "confidence": 0.88,
            "uncertainties": [],
        }
        rel_other, conf_other, reasons_other, _ = tracker.identify_relation(
            text, product_profile, visual_decision=mock_vis_other, strict_vision=True
        )
        self.assertEqual(rel_other, "OTHER_SKU", "画面为其他商品时搭配话术严禁逃逸")

        # 3. 严格模式 + 画面证实主品在场 (SAME_SKU) -> 合规返回 ACCESSORY_OR_STYLING
        mock_vis_same = {
            "vision_available": True,
            "sku_relation": "SAME_SKU",
            "confidence": 0.92,
            "uncertainties": [],
        }
        rel_same, conf_same, reasons_same, _ = tracker.identify_relation(
            text, product_profile, visual_decision=mock_vis_same, strict_vision=True
        )
        self.assertEqual(rel_same, "ACCESSORY_OR_STYLING", "画面核实主品在场方可认定为合规搭配")
        self.assertGreaterEqual(conf_same, 0.85)

        # 4. 非严格模式下，仅凭搭配话术允许降级认定
        loose_profile = {
            "sku_id": "SKU-SWEATER-01",
            "name": "羊绒高领毛衣",
            "category": "女装/毛针织",
        }
        rel_loose, _, _, _ = tracker.identify_relation(
            text, loose_profile, visual_decision=None, strict_vision=False
        )
        self.assertEqual(rel_loose, "ACCESSORY_OR_STYLING")

    def test_p0_1_candidate_ranker_cannot_upgrade_uncertain_to_styling(self):
        """P0-1 (b): CandidateRanker 只能降低或维持上游可信度，严禁仅凭语义将 UNCERTAIN 升级为 ACCESSORY_OR_STYLING。"""
        ranker = CandidateRanker()
        product_profile = {
            "name": "羊绒衫",
            "category": "女装/毛针织",
            "quality_policy": "strict",
            "strict": True,
        }

        # Case 1: 上游识别为 UNCERTAIN，口播包含“搭西裤”
        cand_uncertain = {
            "candidate_id": "c_unc",
            "text": "我们下半身搭一条高腰西裤，整体气质立刻拉满。",
            "product_relation": "UNCERTAIN",
            "source_start_ms": 1000,
            "source_end_ms": 5000,
            "duration_ms": 4000,
            "risks": [],
        }
        res = ranker.score_candidates([cand_uncertain], product_profile)
        score_info = res["scores"][0]
        self.assertEqual(
            score_info["product_relation"], "UNCERTAIN",
            "CandidateRanker 严禁将 UNCERTAIN 升级为 ACCESSORY_OR_STYLING"
        )
        self.assertFalse(score_info["eligible"], "严格模式下 UNCERTAIN 候选严禁标记为 eligible=True")
        self.assertTrue(any("未通过身份核实" in r for r in score_info["rejection_reasons"]))

        # Case 2: 上游原本已验证为 SAME_SKU，口播提到搭配西裤 -> 合规维持为 ACCESSORY_OR_STYLING，不被西裤负向词误杀
        cand_verified_same = {
            "candidate_id": "c_verified",
            "text": "我们身上这件羊绒衫，下半身搭一条西裤非常利落。",
            "product_relation": "SAME_SKU",
            "source_start_ms": 5000,
            "source_end_ms": 9000,
            "duration_ms": 4000,
            "risks": [],
        }
        res_v = ranker.score_candidates([cand_verified_same], product_profile)
        score_v = res_v["scores"][0]
        self.assertEqual(score_v["product_relation"], "ACCESSORY_OR_STYLING")
        self.assertTrue(score_v["eligible"], "已验证主品搭配讲解应合规保留")

        # Case 3: 口播虽然包含搭配词，但核心是主推下装换款 ("主推这条裤子给大家安排") -> 必须降级为 OTHER_SKU 且 eligible=False
        cand_promote_pants = {
            "candidate_id": "c_promo",
            "text": "今天主推这条西裤给大家安排，配上身非常显瘦。",
            "product_relation": "ACCESSORY_OR_STYLING",
            "source_start_ms": 9000,
            "source_end_ms": 13000,
            "duration_ms": 4000,
            "risks": [],
        }
        res_p = ranker.score_candidates([cand_promote_pants], product_profile)
        score_p = res_p["scores"][0]
        self.assertEqual(score_p["product_relation"], "OTHER_SKU", "主推他款必须判定为 OTHER_SKU")
        self.assertFalse(score_p["eligible"], "主推他款严禁通过门禁")

    # -------------------------------------------------------------------------
    # P0-2: 旧版交付清单缺失指纹时拒绝错误复用
    # -------------------------------------------------------------------------
    def test_p0_2_legacy_manifest_without_fingerprint_rejects_cross_product_reuse(self):
        """P0-2: 历史交付清单缺少指纹时，严禁单凭 DELIVERED 和 QC 直接复用旧成片；若商品变更必须强制重跑。"""
        dummy_video = os.path.join(self.tmpdir, "legacy_product_a.mp4")
        with open(dummy_video, "wb") as f:
            f.write(b"REAL_VIDEO_BYTES_PROD_A")

        delivery_manifest_p = os.path.join(self.out_dir, "delivery_manifest.json")

        # 写入一份旧版交付清单：没有 job_fingerprint，但 qc_passed=True，status=DELIVERED
        legacy_manifest_data = {
            "project_id": "legacy_run",
            "status": "DELIVERED",
            "qc_passed": True,
            "video_path": dummy_video,
            # 原始历史记录显示此前剪辑的是 Product A (羽绒服)
            "spec": {
                "project_id": "legacy_run",
                "raw_video_path": dummy_video,
                "product": {"name": "短款立领羽绒服", "category": "羽绒服"},
            },
            "input_manifest": {"source_videos": [{"path": dummy_video, "sha256": "hash_a", "size": 100}]},
        }
        with open(delivery_manifest_p, "w", encoding="utf-8") as f:
            json.dump(legacy_manifest_data, f, ensure_ascii=False, indent=2)

        # 当前任务配置已改为 Product B (羊毛开衫)
        current_spec = {
            "project_id": "new_project_run",
            "raw_video_path": dummy_video,
            "product": {"name": "重磅羊毛针织开衫", "category": "女装/毛针织"},
        }

        runner = JobRunner(current_spec, self.work_dir, self.out_dir)
        runner.resumed_from = JobStage.DELIVERED

        mock_director = MagicMock()
        mock_director.run_director_workflow.return_value = {
            "status": "DELIVERED",
            "qc_passed": True,
            "editorial_qc": {"passed": True},
            "final_deliverables": {"video": dummy_video},
        }

        with patch("src.agent.agent_director.AgentDirector", return_value=mock_director):
            res = runner.run_pipeline()

        # 核心断言：由于指纹不匹配（商品从羽绒服变为羊毛衫），严禁复用旧结果，必须调用 AgentDirector 重新生成！
        mock_director.run_director_workflow.assert_called_once()
        self.assertEqual(res["status"], JobStage.DELIVERED)

        # 场景 2：若旧清单不仅缺指纹，而且缺失历史 spec，无法推算指纹 -> 同样坚决拒绝复用
        with open(delivery_manifest_p, "w", encoding="utf-8") as f:
            json.dump({
                "project_id": "no_spec_run",
                "status": "DELIVERED",
                "qc_passed": True,
                "video_path": dummy_video,
            }, f)

        mock_director.reset_mock()
        runner_no_hist = JobRunner(current_spec, self.work_dir, self.out_dir)
        runner_no_hist.resumed_from = JobStage.DELIVERED
        with patch("src.agent.agent_director.AgentDirector", return_value=mock_director):
            res_no_hist = runner_no_hist.run_pipeline()

        mock_director.run_director_workflow.assert_called_once()
        self.assertEqual(res_no_hist["status"], JobStage.DELIVERED)

        # 场景 3：若历史 spec 与当前完全一致（确实是同一任务恢复），独立推算指纹一致 -> 允许安全迁移指纹复用
        identical_spec = dict(current_spec)
        from src.core.project_context import InputManifest
        current_manifest = InputManifest(runner.project_paths).scan_and_resolve(explicit_video_paths=[dummy_video])
        correct_fp = compute_job_fingerprint(identical_spec, current_manifest)

        with open(delivery_manifest_p, "w", encoding="utf-8") as f:
            json.dump({
                "project_id": "new_project_run",
                "status": "DELIVERED",
                "qc_passed": True,
                "video_path": dummy_video,
                "spec": identical_spec,
                "input_manifest": current_manifest,
            }, f)

        mock_director.reset_mock()
        runner_same = JobRunner(identical_spec, self.work_dir, self.out_dir)
        runner_same.resumed_from = JobStage.DELIVERED
        with patch("src.agent.agent_director.AgentDirector", return_value=mock_director):
            res_same = runner_same.run_pipeline()

        # 历史指纹推算完全一致时，直接复用既有成片，无需触发 run_director_workflow
        mock_director.run_director_workflow.assert_not_called()
        self.assertEqual(res_same["status"], JobStage.DELIVERED)

    # -------------------------------------------------------------------------
    # P1-1: 无品牌包装全链路（校验 -> 保存 -> 启动）及默认品类不误导
    # -------------------------------------------------------------------------
    def test_p1_1_plain_brand_gui_save_start_flow_and_default_category(self):
        """P1-1: 无品牌模式全链路闭环，默认品类为空不误导。"""
        # 1. 默认数据中 category 必须为空字符串，不可预置特定品类
        api = DesktopSpecApi()
        default_data = api.load_initial_data()
        self.assertEqual(default_data["category"], "", "新项目默认品类必须为空，避免误导非女装毛针织商品")

        # 2. SpecManager 校验：brand_en 留空代表无品牌，校验通过
        manager = SpecManager()
        dummy_video = os.path.join(self.tmpdir, "vid.mp4")
        with open(dummy_video, "wb") as f:
            f.write(b"0000")

        plain_spec_payload = {
            "project_id": "test_plain_brand_pipeline",
            "raw_video_path": dummy_video,
            "project_root": self.tmpdir,
            "brand_en": "",
            "brand_cn": "",
            "product_name": "纯色棉袜",
            "category": "通用电商",
        }
        val_errors = manager.validate(plain_spec_payload)
        self.assertEqual(val_errors, [], f"无品牌留空不得被校验拦截: {val_errors}")

        # 3. DesktopSpecApi.save_spec 保存测试
        spec_file = os.path.join(self.work_dir, "job_spec.json")
        api = DesktopSpecApi(spec_path=spec_file)
        save_res = api.save_spec(plain_spec_payload)
        self.assertTrue(save_res.get("success"), f"保存无品牌配置失败: {save_res}")

        # 4. DesktopSpecApi.start_pipeline 启动测试（验证后台任务能正常拉起而未被 brand_en 阻断）
        with patch("src.runtime.job_runner.JobRunner.run_pipeline") as mock_run:
            mock_run.return_value = {"status": "DELIVERED", "final_video": dummy_video}
            start_res = api.start_pipeline(plain_spec_payload)
            self.assertTrue(start_res.get("success"), f"启动任务失败: {start_res}")

    # -------------------------------------------------------------------------
    # P1-2: 占位商品图隔离于项目 work 目录，不写入仓库
    # -------------------------------------------------------------------------
    def test_p1_2_placeholder_images_isolated_to_project_work_dir(self):
        """P1-2: 占位商品图写入当前项目的 work 目录，严禁写入仓库 data/placeholders/。"""
        spec = {
            "project_id": "test_ph_isolation",
            "work_dir": self.work_dir,
            "product": {"item_images": []},
        }

        # 调用 ensure_placeholder_cards
        cards = ensure_placeholder_cards(spec, "test_ph_isolation", out_dir=None, n_cards=2)
        self.assertEqual(len(cards), 2)

        # 验证所有生成的占位图均位于 work_dir 下
        for p in cards:
            abs_p = os.path.abspath(p)
            self.assertTrue(
                abs_p.startswith(os.path.abspath(self.work_dir)),
                f"占位图路径 {p} 必须位于项目 work 目录下，而非仓库根目录"
            )

        # 验证仓库根目录 data/placeholders 下没有产生本任务的图片
        repo_ph_dir = os.path.join(PROJECT_ROOT, "data", "placeholders")
        leaked_files = [f for f in os.listdir(repo_ph_dir) if "test_ph_isolation" in f] if os.path.exists(repo_ph_dir) else []
        self.assertEqual(leaked_files, [], f"严禁向仓库 data/placeholders 写入占位图: {leaked_files}")

    # -------------------------------------------------------------------------
    # P1-3: 取消令牌即时中断运行中子进程 (AGY 与 FFmpeg)
    # -------------------------------------------------------------------------
    def test_p1_3_cancellation_token_terminates_active_processes(self):
        """P1-3: CancellationToken 取消时，能实时杀掉正在运行的子进程。"""
        token = CancellationToken()

        # 启动一个长时间挂起的子进程模拟 AGY / FFmpeg
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        token.register_process(proc)

        self.assertIsNone(proc.poll(), "进程应当正在运行中")

        # 触发取消令牌
        token.cancel()
        time.sleep(0.3)

        # 验证子进程已被强制杀掉
        self.assertIsNotNone(proc.poll(), "触发取消令牌后子进程必须已被终止")

    # -------------------------------------------------------------------------
    # P1-4: 目录清理安全判断杜绝同前缀误判
    # -------------------------------------------------------------------------
    def test_p1_4_project_paths_is_safe_to_clean_no_prefix_false_positive(self):
        """P1-4: ProjectPaths.is_safe_to_clean() 使用 commonpath，杜绝 run_001_backup 误判为 run_001 子目录。"""
        base_dir = os.path.join(self.tmpdir, "projects")
        proj_root = os.path.join(base_dir, "my_project")
        paths = ProjectPaths(project_root=proj_root, run_id="run_001")

        # run_001_backup 拥有相同前缀，但绝非 run_001 的子目录
        sibling_backup_dir = os.path.join(paths.work_base, "run_001_backup", "test.txt")
        self.assertFalse(
            paths.is_safe_to_clean(sibling_backup_dir),
            "前缀相同的同级目录 run_001_backup 严禁被误判为可清理路径"
        )

        # work_dir 自身不能被清理
        self.assertFalse(paths.is_safe_to_clean(paths.work_dir), "work_dir 自身不可作为子文件清理")

        # work_dir 下的子文件是安全的
        child_file = os.path.join(paths.work_dir, "intermediate", "clip_01.mp4")
        self.assertTrue(paths.is_safe_to_clean(child_file), "work_dir 内部子路径应判定为安全可清理")

        # work_dir 外部目录不可清理
        external_file = os.path.join(self.tmpdir, "other", "data.json")
        self.assertFalse(paths.is_safe_to_clean(external_file), "外部路径严禁判定为可清理")


if __name__ == "__main__":
    unittest.main()
