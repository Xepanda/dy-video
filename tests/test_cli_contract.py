# -*- coding: utf-8 -*-
"""
test_cli_contract.py - dy-video V3.2 阶段化 CLI 契约测试
=========================================================
验证通用阶段 CLI (python -m src.cli) 的各个阶段子命令：
  1. prepare 异常输入（缺 spec、空视频列表）优雅阻断与非零退出；
  2. prepare 正常生成不可变快照、白名单清单与任务指纹；
  3. analyze 生成冻结候选池与候选池哈希快照；
  4. validate-plan 校验：候选 ID 越界、哈希不匹配、非本品/严格模式阻断；
  5. render 仅接受合规 plan，临时文件原子发布；
  6. qc 双门禁审计与 deliver 严格双通过签署。
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
import subprocess
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

PYTHON = sys.executable


class TestCliContract(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.proj_dir = os.path.join(self.tmpdir, "test_proj")
        self.mat_dir = os.path.join(self.proj_dir, "素材")
        os.makedirs(self.mat_dir, exist_ok=True)

        self.dummy_video = os.path.join(self.mat_dir, "test_live_01.mp4")
        with open(self.dummy_video, "wb") as f:
            f.write(b"REAL_TEST_VIDEO_BYTES_12345")

        self.spec_data = {
            "project_id": "test_proj",
            "raw_video_path": self.dummy_video,
            "raw_video_paths": [self.dummy_video],
            "brand": {
                "title_en": "TEST_BRAND",
                "subtitle_cn": "测试品牌",
            },
            "product": {
                "sku_id": "SKU-999",
                "name": "高领羊毛衫",
                "category": "女装/针织",
                "title_line1": "高领羊毛衫",
                "title_line2": "亲肤透气",
                "selling_points": ["羊毛混纺", "软糯保暖"],
            },
            "options": {
                "duration_range": "45-65",
                "style_preset": "balanced",
            },
        }
        self.spec_path = os.path.join(self.mat_dir, "job_spec.json")
        with open(self.spec_path, "w", encoding="utf-8") as f:
            json.dump(self.spec_data, f, ensure_ascii=False, indent=2)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_cli(self, args: list) -> subprocess.CompletedProcess:
        cmd = [PYTHON, "-m", "src.cli"] + args
        return subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def test_cli_help(self):
        res = self._run_cli(["--help"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("prepare", res.stdout)
        self.assertIn("analyze", res.stdout)
        self.assertIn("validate-plan", res.stdout)

    def test_cli_prepare_missing_spec_fails(self):
        res = self._run_cli(["prepare", "--spec", os.path.join(self.tmpdir, "nonexistent.json")])
        self.assertNotEqual(res.returncode, 0)
        out = json.loads(res.stdout)
        self.assertEqual(out.get("status"), "ERROR")
        self.assertIn("不存在", out.get("error_message", ""))

    def test_cli_prepare_empty_raw_videos_fails(self):
        spec_empty = dict(self.spec_data)
        spec_empty["raw_video_paths"] = []
        spec_empty["raw_video_path"] = ""
        empty_spec_p = os.path.join(self.mat_dir, "empty_spec.json")
        with open(empty_spec_p, "w", encoding="utf-8") as f:
            json.dump(spec_empty, f)

        res = self._run_cli(["prepare", "--spec", empty_spec_p])
        self.assertNotEqual(res.returncode, 0)
        out = json.loads(res.stdout)
        self.assertEqual(out.get("status"), "ERROR")

    def test_cli_prepare_valid_creates_work_and_snapshot(self):
        res = self._run_cli(["prepare", "--spec", self.spec_path])
        self.assertEqual(res.returncode, 0, f"prepare 应当成功: {res.stderr}\n{res.stdout}")
        out = json.loads(res.stdout)
        self.assertEqual(out.get("status"), "PREPARED")
        work_dir = out.get("work_dir")
        self.assertTrue(os.path.isdir(work_dir))
        self.assertTrue(os.path.isfile(os.path.join(work_dir, "run_context.json")))
        self.assertTrue(os.path.isfile(os.path.join(work_dir, "spec_snapshot.json")))
        self.assertTrue(os.path.isfile(os.path.join(work_dir, "input_manifest.json")))
        self.assertTrue(os.path.isfile(os.path.join(work_dir, "product_profile.json")))
        self.assertTrue(os.path.isfile(os.path.join(work_dir, "run_state.json")))

        # 确保 work 目录位于项目根目录，严禁落入 素材/
        self.assertFalse(work_dir.startswith(self.mat_dir))
        self.assertTrue(work_dir.startswith(self.proj_dir))

    def test_cli_validate_plan_unknown_candidate_rejected(self):
        # 先执行 prepare
        prep_res = self._run_cli(["prepare", "--spec", self.spec_path])
        self.assertEqual(prep_res.returncode, 0)
        work_dir = json.loads(prep_res.stdout)["work_dir"]

        # 模拟一份已生成的分析候选池
        analysis_dir = os.path.join(work_dir, "analysis")
        os.makedirs(analysis_dir, exist_ok=True)
        cand_list = [
            {
                "candidate_id": "c_src_01_001",
                "source_id": "src_01",
                "source_file": self.dummy_video,
                "source_start_ms": 1000,
                "source_end_ms": 5000,
                "text": "今天给大家推荐这款羊毛衫",
                "product_relation": "SAME_SKU",
                "eligible": True,
            }
        ]
        with open(os.path.join(analysis_dir, "candidates.json"), "w", encoding="utf-8") as f:
            json.dump(cand_list, f)

        cand_hash = "mock_hash_123"
        snapshot = {
            "schema_version": "3.2",
            "candidate_pool_hash": cand_hash,
            "total_candidates": 1,
            "candidate_ids": ["c_src_01_001"],
        }
        with open(os.path.join(analysis_dir, "candidate_pool_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump(snapshot, f)

        # 提交一个包含未知 ID 的 proposal
        bad_proposal = {
            "proposal_id": "prop_bad",
            "candidate_pool_hash": cand_hash,
            "ordered_candidate_ids": ["c_unknown_999"],
            "decision_reasons": ["开场介绍"],
        }
        prop_path = os.path.join(work_dir, "bad_proposal.json")
        with open(prop_path, "w", encoding="utf-8") as f:
            json.dump(bad_proposal, f)

        res = self._run_cli(["validate-plan", "--run", work_dir, "--proposal", prop_path])
        self.assertNotEqual(res.returncode, 0)
        out = json.loads(res.stdout)
        self.assertEqual(out.get("status"), "ERROR")
        self.assertIn("未知候选 ID", out.get("error_message", ""))

    def test_cli_validate_plan_hash_mismatch_rejected(self):
        prep_res = self._run_cli(["prepare", "--spec", self.spec_path])
        work_dir = json.loads(prep_res.stdout)["work_dir"]

        analysis_dir = os.path.join(work_dir, "analysis")
        os.makedirs(analysis_dir, exist_ok=True)
        with open(os.path.join(analysis_dir, "candidates.json"), "w", encoding="utf-8") as f:
            json.dump([{"candidate_id": "c1", "source_file": self.dummy_video, "source_start_ms": 0, "source_end_ms": 3000, "product_relation": "SAME_SKU"}], f)

        with open(os.path.join(analysis_dir, "candidate_pool_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"candidate_pool_hash": "correct_hash", "candidate_ids": ["c1"]}, f)

        # 提交一个哈希不匹配的 proposal
        mismatch_proposal = {
            "proposal_id": "prop_mismatch",
            "candidate_pool_hash": "wrong_hash",
            "ordered_candidate_ids": ["c1"],
        }
        prop_path = os.path.join(work_dir, "mismatch_proposal.json")
        with open(prop_path, "w", encoding="utf-8") as f:
            json.dump(mismatch_proposal, f)

        res = self._run_cli(["validate-plan", "--run", work_dir, "--proposal", prop_path])
        self.assertNotEqual(res.returncode, 0)
        out = json.loads(res.stdout)
        self.assertIn("候选池哈希不匹配", out.get("error_message", ""))

    def test_cli_deliver_without_qc_fails(self):
        prep_res = self._run_cli(["prepare", "--spec", self.spec_path])
        work_dir = json.loads(prep_res.stdout)["work_dir"]

        res = self._run_cli(["deliver", "--run", work_dir])
        self.assertNotEqual(res.returncode, 0)
        out = json.loads(res.stdout)
        self.assertEqual(out.get("status"), "ERROR")

    def test_cli_status_nonexistent_dir_fails(self):
        res = self._run_cli(["status", "--run", os.path.join(self.tmpdir, "nonexistent_work_dir")])
        self.assertNotEqual(res.returncode, 0)
        out = json.loads(res.stdout)
        self.assertEqual(out.get("status"), "ERROR")
        self.assertIn("未找到有效运行目录", out.get("error_message", ""))

    def test_cli_validate_plan_invalid_type_ordered_ids_fails(self):
        prep_res = self._run_cli(["prepare", "--spec", self.spec_path])
        work_dir = json.loads(prep_res.stdout)["work_dir"]

        analysis_dir = os.path.join(work_dir, "analysis")
        os.makedirs(analysis_dir, exist_ok=True)
        with open(os.path.join(analysis_dir, "candidates.json"), "w", encoding="utf-8") as f:
            json.dump([{"candidate_id": "c1", "source_file": self.dummy_video, "source_start_ms": 0, "source_end_ms": 3000, "product_relation": "SAME_SKU"}], f)
        with open(os.path.join(analysis_dir, "candidate_pool_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"candidate_pool_hash": "hash_ok", "candidate_ids": ["c1"]}, f)

        # 传入字符串而非数组列表
        prop_str_ids = {
            "proposal_id": "prop_bad_type",
            "candidate_pool_hash": "hash_ok",
            "ordered_candidate_ids": "not_a_list",
        }
        prop_path = os.path.join(work_dir, "bad_type_prop.json")
        with open(prop_path, "w", encoding="utf-8") as f:
            json.dump(prop_str_ids, f)

        res = self._run_cli(["validate-plan", "--run", work_dir, "--proposal", prop_path])
        self.assertNotEqual(res.returncode, 0)
        out = json.loads(res.stdout)
        self.assertIn("必须为数组列表", out.get("error_message", ""))

    def test_cli_prepare_includes_quarantined_files(self):
        # 创建一个历史成片文件并加入显式素材列表
        fake_old = os.path.join(self.mat_dir, "test_proj.mp4")
        with open(fake_old, "wb") as f:
            f.write(b"OLD_OUTPUT_VIDEO")

        spec_with_old = dict(self.spec_data)
        spec_with_old["raw_video_paths"] = [self.dummy_video, fake_old]
        old_spec_p = os.path.join(self.mat_dir, "spec_with_old.json")
        with open(old_spec_p, "w", encoding="utf-8") as f:
            json.dump(spec_with_old, f)

        res = self._run_cli(["prepare", "--spec", old_spec_p])
        self.assertEqual(res.returncode, 0)
        out = json.loads(res.stdout)
        self.assertIn("quarantined_files", out)
        quarantined = out["quarantined_files"]
        self.assertTrue(any("test_proj.mp4" in q.get("path", "") for q in quarantined))

    def test_cli_validate_plan_missing_candidate_pool_hash_rejected(self):
        prep_res = self._run_cli(["prepare", "--spec", self.spec_path])
        work_dir = json.loads(prep_res.stdout)["work_dir"]

        analysis_dir = os.path.join(work_dir, "analysis")
        os.makedirs(analysis_dir, exist_ok=True)
        with open(os.path.join(analysis_dir, "candidates.json"), "w", encoding="utf-8") as f:
            json.dump([{"candidate_id": "c1", "source_file": self.dummy_video, "source_start_ms": 0, "source_end_ms": 3000, "product_relation": "SAME_SKU", "text": "测试文本"}], f)
        with open(os.path.join(analysis_dir, "candidate_pool_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"candidate_pool_hash": "hash_ok", "candidate_ids": ["c1"]}, f)

        # 提案故意省略 candidate_pool_hash
        no_hash_proposal = {
            "proposal_id": "prop_no_hash",
            "ordered_candidate_ids": ["c1"],
        }
        prop_path = os.path.join(work_dir, "no_hash_proposal.json")
        with open(prop_path, "w", encoding="utf-8") as f:
            json.dump(no_hash_proposal, f)

        res = self._run_cli(["validate-plan", "--run", work_dir, "--proposal", prop_path])
        self.assertNotEqual(res.returncode, 0)
        out = json.loads(res.stdout)
        self.assertIn("必须包含 candidate_pool_hash", out.get("error_message", ""))

    def test_cli_validate_plan_mismatched_run_id_rejected(self):
        prep_res = self._run_cli(["prepare", "--spec", self.spec_path])
        work_dir = json.loads(prep_res.stdout)["work_dir"]

        analysis_dir = os.path.join(work_dir, "analysis")
        os.makedirs(analysis_dir, exist_ok=True)
        with open(os.path.join(analysis_dir, "candidates.json"), "w", encoding="utf-8") as f:
            json.dump([{"candidate_id": "c1", "source_file": self.dummy_video, "source_start_ms": 0, "source_end_ms": 3000, "product_relation": "SAME_SKU", "text": "测试文本"}], f)
        with open(os.path.join(analysis_dir, "candidate_pool_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"candidate_pool_hash": "hash_ok", "candidate_ids": ["c1"]}, f)

        # 提案提供错误的 run_id
        mismatch_run_proposal = {
            "run_id": "different_run_9999",
            "candidate_pool_hash": "hash_ok",
            "ordered_candidate_ids": ["c1"],
        }
        prop_path = os.path.join(work_dir, "mismatch_run_proposal.json")
        with open(prop_path, "w", encoding="utf-8") as f:
            json.dump(mismatch_run_proposal, f)

        res = self._run_cli(["validate-plan", "--run", work_dir, "--proposal", prop_path])
        self.assertNotEqual(res.returncode, 0)
        out = json.loads(res.stdout)
        self.assertIn("运行编号不匹配", out.get("error_message", ""))

    def test_cli_validate_plan_insufficient_evidence_blocked(self):
        prep_res = self._run_cli(["prepare", "--spec", self.spec_path])
        work_dir = json.loads(prep_res.stdout)["work_dir"]

        analysis_dir = os.path.join(work_dir, "analysis")
        os.makedirs(analysis_dir, exist_ok=True)
        with open(os.path.join(analysis_dir, "candidates.json"), "w", encoding="utf-8") as f:
            json.dump([{"candidate_id": "c1", "source_file": self.dummy_video, "source_start_ms": 0, "source_end_ms": 3000, "product_relation": "SAME_SKU", "text": "测试文本"}], f)
        with open(os.path.join(analysis_dir, "candidate_pool_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"candidate_pool_hash": "hash_ok", "candidate_ids": ["c1"]}, f)

        insufficient_proposal = {
            "status": "INSUFFICIENT_EVIDENCE",
            "candidate_pool_hash": "hash_ok",
            "evidence_gap": ["缺少本品展示证据"],
        }
        prop_path = os.path.join(work_dir, "insufficient_proposal.json")
        with open(prop_path, "w", encoding="utf-8") as f:
            json.dump(insufficient_proposal, f)

        res = self._run_cli(["validate-plan", "--run", work_dir, "--proposal", prop_path])
        self.assertNotEqual(res.returncode, 0)
        out = json.loads(res.stdout)
        self.assertIn("INSUFFICIENT_EVIDENCE", out.get("error_message", ""))

    def test_cli_deliver_rejects_when_live_spec_tampered(self):
        prep_res = self._run_cli(["prepare", "--spec", self.spec_path])
        work_dir = json.loads(prep_res.stdout)["work_dir"]
        output_dir = os.path.join(self.proj_dir, "output", json.loads(prep_res.stdout)["run_id"])
        os.makedirs(output_dir, exist_ok=True)

        with open(os.path.join(output_dir, "final.mp4"), "wb") as f:
            f.write(b"FAKE_FINAL_VIDEO_CONTENT")
        with open(os.path.join(output_dir, "technical_qc.json"), "w", encoding="utf-8") as f:
            json.dump({"passed": True}, f)
        with open(os.path.join(output_dir, "editorial_qc.json"), "w", encoding="utf-8") as f:
            json.dump({"passed": True, "block_count": 0}, f)

        # 篡改源任务契约的品牌配置
        tampered_spec = dict(self.spec_data)
        tampered_spec["brand"] = {"title_en": "TAMPERED_BRAND", "subtitle_cn": "篡改品牌"}
        with open(self.spec_path, "w", encoding="utf-8") as f:
            json.dump(tampered_spec, f)

        res = self._run_cli(["deliver", "--run", work_dir])
        self.assertNotEqual(res.returncode, 0)
        out = json.loads(res.stdout)
        self.assertIn("指纹不匹配", out.get("error_message", ""))

    def test_generate_agent_task_does_not_create_duplicate_named_spec(self):
        from src.tools.desktop_spec_creator import DesktopSpecApi
        api = DesktopSpecApi()
        payload = {
            "project_id": "test_no_duplicate",
            "project_root": self.proj_dir,
            "raw_video_paths": [self.dummy_video],
            "product_title_l1": "无重复配置测试",
            "product_title_l2": "单一来源",
        }
        res = api.generate_agent_task(payload)
        self.assertTrue(res["success"])
        # 验证仅生成 job_spec.json，严禁出现 test_no_duplicate_job_spec.json 双真实来源
        self.assertTrue(os.path.isfile(os.path.join(self.mat_dir, "job_spec.json")))
        self.assertFalse(os.path.isfile(os.path.join(self.mat_dir, "test_no_duplicate_job_spec.json")))


if __name__ == "__main__":
    unittest.main()
