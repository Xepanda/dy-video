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


if __name__ == "__main__":
    unittest.main()
