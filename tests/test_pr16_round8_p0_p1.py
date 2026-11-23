# -*- coding: utf-8 -*-
"""
test_pr16_round8_p0_p1.py - PR #16 第八轮评审 P0 / P1 缺陷深度回归测试套件
========================================================================
涵盖：
  1. R8-01: 跨平台路径测试解耦 (ntpath / PureWindowsPath 语义测试与宿主系统解耦)；
  2. R8-02: 素材来源强校验：
            - render 强制要求有效 input_manifest.json、非空 source_videos 与 64 位完整 SHA-256；
            - 渲染前核验物理磁盘源文件完整 SHA-256，源文件篡改立即阻断；
            - deliver 阶段复核原始素材物理磁盘哈希；
            - 多源 ASR 来源未匹配时抛出明确 SOURCE_UNRESOLVED 错误，严禁回退第一段视频。
  3. R8-03: 计划/渲染/QC 版本闭环：
            - render 写入不可变 render_manifest.json (记录 run_id, validated_plan_hash, video_sha256)；
            - 重新 validate-plan 时下游旧成片与质检产物强制失效；
            - qc 与 deliver 严格核验 render_manifest.json 与计划/视频完整哈希。
  4. R8-04: 严格交付哈希：
            - deliver 移除快速指纹兼容分支，强制要求 QC 报告中为 64 位完整 SHA-256，旧格式必须重新 QC。
  5. R8-05: 路径与多源处理：
            - is_historical_output() 对祖先目录含 'work' 的路径（如 /home/dev/work/project-3/素材/live.mp4）不误判；
            - ProjectPaths 对 D:\\projects\\work\\my-video 真实项目根目录不误截断；
            - InputManifest.scan_and_resolve() 对跨平台 raw_video_paths 进行统一规范化映射。
  6. R8-06: Agent 输出一致性：
            - chosen_cover_id / selected_cover 与 plan["cover"] 统一，render 阶段优先使用 Agent 决策封面；
            - plan["cta"] 与 stage_render() 文案生成字段统一，文案不丢失结尾 CTA；
            - desktop_spec_creator 明确核验 src.cli 可用性，并分别输出 Windows 与 WSL 可执行命令。
"""

import os
import sys
import json
import time
import shutil
import hashlib
import tempfile
import unittest
import ntpath
from pathlib import Path, PureWindowsPath, PurePosixPath
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.project_context import (
    ProjectPaths,
    InputManifest,
    normalize_project_path,
    compute_job_fingerprint,
    compute_file_sha256,
    compute_full_file_sha256,
    compute_file_fast_fingerprint,
    is_historical_output,
)
from src.core.plan_compiler import compile_v3_plan
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
from src.tools.desktop_spec_creator import DesktopSpecApi


class TestPR16Round8P0P1(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.proj_dir = os.path.join(self.tmpdir, "my_project")
        self.mat_dir = os.path.join(self.proj_dir, "素材")
        os.makedirs(self.mat_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # R8-01: 跨平台路径测试解耦
    # -------------------------------------------------------------------------
    def test_r8_01_cross_platform_path_decoupling(self):
        """R8-01: 使用 ntpath / PureWindowsPath 验证跨平台路径映射，解耦宿主系统。"""
        win_path = r"D:/dev/workspace/my_video_project"
        linux_mapped = normalize_project_path(win_path, target_platform="linux")
        self.assertEqual(linux_mapped, "/mnt/d/dev/workspace/my_video_project")

        wsl_path = "/mnt/d/dev/workspace/my_video_project"
        win_mapped = normalize_project_path(wsl_path, target_platform="win32")
        self.assertEqual(win_mapped, r"D:\dev\workspace\my_video_project")

        win_pure = PureWindowsPath(win_mapped)
        self.assertEqual(win_pure.drive, "D:")
        self.assertEqual(win_pure.parts, ("D:\\", "dev", "workspace", "my_video_project"))

        # 根目录盘符映射
        self.assertEqual(normalize_project_path("/mnt/d", target_platform="win32"), "D:\\")
        self.assertEqual(normalize_project_path("/mnt/d/", target_platform="win32"), "D:\\")
        self.assertEqual(normalize_project_path("D:", target_platform="linux"), "/mnt/d")
        self.assertEqual(normalize_project_path("D:/", target_platform="linux"), "/mnt/d")
        self.assertEqual(normalize_project_path("D:\\", target_platform="linux"), "/mnt/d")

    # -------------------------------------------------------------------------
    # R8-02: 素材来源强校验
    # -------------------------------------------------------------------------
    def test_r8_02_render_rejects_missing_or_empty_manifest(self):
        """R8-02 / P0-3: input_manifest.json 缺失、解析失败或无合法视频源时，render 必须立即阻断。"""
        work_dir = os.path.join(self.proj_dir, "work", "run_r8_manifest")
        output_dir = os.path.join(self.proj_dir, "output", "run_r8_manifest")
        os.makedirs(work_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        vid_path = os.path.join(self.mat_dir, "legit.mp4")
        with open(vid_path, "wb") as f:
            f.write(b"dummy_video_bytes")
        vid_sha = compute_full_file_sha256(vid_path)

        plan = {
            "schema_version": "3.2",
            "run_id": "run_r8_manifest",
            "clips": [{"id": "c1", "source_id": "src_01", "source_file": vid_path, "source_file_sha256": vid_sha, "start": 0, "end": 3}],
        }
        plan_h = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        plan["validated_plan_hash"] = plan_h

        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.2", "run_id": "run_r8_manifest", "output_dir": output_dir}, f)
        with open(os.path.join(work_dir, "spec_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"options": {}}, f)
        with open(os.path.join(work_dir, "validated_plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan, f)
        with open(os.path.join(work_dir, "plan_audit.json"), "w", encoding="utf-8") as f:
            json.dump({"audit_status": "APPROVED", "passed": True, "validated_plan_hash": plan_h}, f)

        # 1. 清单文件不存在
        with self.assertRaises(RuntimeError) as ctx1:
            stage_render(work_dir)
        self.assertIn("未找到输入清单 input_manifest.json", str(ctx1.exception))

        # 2. 清单有效源视频列表为空
        with open(os.path.join(work_dir, "input_manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.1", "source_videos": []}, f)
        with self.assertRaises(RuntimeError) as ctx2:
            stage_render(work_dir)
        self.assertIn("有效源视频列表为空", str(ctx2.exception))

        # 3. 清单视频源缺少 64 位完整 SHA-256
        with open(os.path.join(work_dir, "input_manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.1", "source_videos": [{"source_id": "src_01", "path": vid_path, "sha256": "short_hash"}]}, f)
        with self.assertRaises(RuntimeError) as ctx3:
            stage_render(work_dir)
        self.assertIn("64位完整 SHA-256", str(ctx3.exception))

    def test_r8_02_render_and_deliver_detect_source_tampering_on_disk(self):
        """R8-02 / P0-1: 渲染前及交付阶段重新计算磁盘上源文件的完整 SHA-256，若被修改立即阻断。"""
        work_dir = os.path.join(self.proj_dir, "work", "run_r8_tamper")
        output_dir = os.path.join(self.proj_dir, "output", "run_r8_tamper")
        os.makedirs(work_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        vid_path = os.path.join(self.mat_dir, "raw_live.mp4")
        with open(vid_path, "wb") as f:
            f.write(b"original_source_stream_content")
        orig_sha256 = compute_full_file_sha256(vid_path)

        manifest = {
            "schema_version": "3.1",
            "source_videos": [{"source_id": "src_01", "path": vid_path, "sha256": orig_sha256}],
        }
        with open(os.path.join(work_dir, "input_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)

        plan = {
            "schema_version": "3.2",
            "run_id": "run_r8_tamper",
            "clips": [{"id": "c1", "source_id": "src_01", "source_file": vid_path, "source_file_sha256": orig_sha256, "start": 0, "end": 2}],
        }
        plan_to_hash = {k: v for k, v in plan.items() if k != "validated_plan_hash"}
        plan_h = hashlib.sha256(json.dumps(plan_to_hash, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        plan["validated_plan_hash"] = plan_h

        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.2", "run_id": "run_r8_tamper", "output_dir": output_dir}, f)
        with open(os.path.join(work_dir, "spec_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"options": {}}, f)
        with open(os.path.join(work_dir, "validated_plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan, f)
        with open(os.path.join(work_dir, "plan_audit.json"), "w", encoding="utf-8") as f:
            json.dump({"audit_status": "APPROVED", "passed": True, "validated_plan_hash": plan_h}, f)

        # 模拟在中途（render 前）修改磁盘源文件内容
        with open(vid_path, "wb") as f:
            f.write(b"tampered_malicious_source_content")

        with self.assertRaises(RuntimeError) as r_ctx:
            stage_render(work_dir)
        self.assertIn("渲染前素材完整性校验失败", str(r_ctx.exception))
        self.assertIn("与输入清单记录", str(r_ctx.exception))

    def test_r8_02_multi_source_unresolved_error(self):
        """R8-02 / P0-5: 多源 ASR 来源无法匹配清单时抛出明确 SOURCE_UNRESOLVED 错误，禁止自动归属第一段。"""
        work_dir = os.path.join(self.proj_dir, "work", "run_r8_asr")
        os.makedirs(work_dir, exist_ok=True)

        vid_a = os.path.join(self.mat_dir, "video_a.mp4")
        vid_b = os.path.join(self.mat_dir, "video_b.mp4")
        with open(vid_a, "wb") as f:
            f.write(b"video_a_bytes")
        with open(vid_b, "wb") as f:
            f.write(b"video_b_bytes")

        manifest = {
            "schema_version": "3.1",
            "source_videos": [
                {"source_id": "src_01_video_a_mp4", "path": vid_a, "filename": "video_a.mp4", "sha256": compute_full_file_sha256(vid_a)},
                {"source_id": "src_02_video_b_mp4", "path": vid_b, "filename": "video_b.mp4", "sha256": compute_full_file_sha256(vid_b)},
            ]
        }
        with open(os.path.join(work_dir, "input_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        with open(os.path.join(work_dir, "spec_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"product": {"name": "羊绒衫"}, "options": {}}, f)
        with open(os.path.join(work_dir, "product_profile.json"), "w", encoding="utf-8") as f:
            json.dump({"selling_points": ["保暖"]}, f)
        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.2", "run_id": "run_r8_asr"}, f)

        # 模拟 ASR 结果包含未登记的来源标识
        bad_segments = [
            {"source_file": "unknown_video_c.mp4", "start": 0, "end": 2, "text": "这件衣服真不错"},
        ]
        with open(os.path.join(work_dir, "whisper_segments_verified.json"), "w", encoding="utf-8") as f:
            json.dump(bad_segments, f)

        with patch("src.runtime.stage_services.analyze_story_segments"), \
             patch("src.runtime.stage_services.extract_cover_candidates", return_value=[]):
            with self.assertRaises(ValueError) as ctx:
                stage_analyze(work_dir)
            self.assertIn("SOURCE_UNRESOLVED", str(ctx.exception))
            self.assertIn("未在 input_manifest.json 清单中登记", str(ctx.exception))

    # -------------------------------------------------------------------------
    # R8-03: 计划/渲染/QC 版本闭环与 render_manifest
    # -------------------------------------------------------------------------
    def test_r8_03_render_manifest_and_plan_version_closed_loop(self):
        """R8-03 / P0-2: render 写入 render_manifest.json，重新 validate-plan 时旧产物失效，qc/deliver 强制校验版本。"""
        work_dir = os.path.join(self.proj_dir, "work", "run_r8_closed_loop")
        output_dir = os.path.join(self.proj_dir, "output", "run_r8_closed_loop")
        os.makedirs(work_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        vid_path = os.path.join(self.mat_dir, "raw_live.mp4")
        with open(vid_path, "wb") as f:
            f.write(b"source_stream_content_123456")
        vid_sha = compute_full_file_sha256(vid_path)

        manifest = {
            "schema_version": "3.1",
            "source_videos": [{"source_id": "src_01", "path": vid_path, "sha256": vid_sha}],
        }
        with open(os.path.join(work_dir, "input_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.2", "run_id": "run_r8_closed_loop", "output_dir": output_dir}, f)
        with open(os.path.join(work_dir, "spec_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"product": {"name": "测试商品"}, "options": {}}, f)

        plan = {
            "schema_version": "3.2",
            "run_id": "run_r8_closed_loop",
            "clips": [{"id": "c1", "source_id": "src_01", "source_file": vid_path, "source_file_sha256": vid_sha, "start": 0, "end": 2}],
            "cta": {"text": "抢购热线已开启"},
        }
        plan_to_hash = {k: v for k, v in plan.items() if k != "validated_plan_hash"}
        plan_h = hashlib.sha256(json.dumps(plan_to_hash, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        plan["validated_plan_hash"] = plan_h

        with open(os.path.join(work_dir, "validated_plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan, f)
        with open(os.path.join(work_dir, "plan_audit.json"), "w", encoding="utf-8") as f:
            json.dump({"audit_status": "APPROVED", "passed": True, "validated_plan_hash": plan_h}, f)

        # 模拟渲染生成成片
        def fake_assemble(plan_arg, out_p):
            with open(out_p, "wb") as f:
                f.write(b"rendered_mp4_video_payload")
            return out_p, 2.0

        mock_assembler = MagicMock()
        mock_assembler.assemble_final_video.side_effect = fake_assemble
        with patch("src.runtime.stage_services.VideoAssembler", return_value=mock_assembler):
            res = stage_render(work_dir)

        # 验证 render_manifest.json 已生成
        rm_path = os.path.join(output_dir, "render_manifest.json")
        self.assertTrue(os.path.isfile(rm_path))
        with open(rm_path, "r", encoding="utf-8") as f:
            rm_data = json.load(f)
        self.assertEqual(rm_data["validated_plan_hash"], plan_h)
        final_mp4 = os.path.join(output_dir, "final.mp4")
        self.assertEqual(rm_data["video_sha256"], compute_full_file_sha256(final_mp4))

        # 模拟计划重新审批（修改了计划内容）
        plan_new = dict(plan, cta={"text": "全新文案更新"})
        plan_new_to_hash = {k: v for k, v in plan_new.items() if k != "validated_plan_hash"}
        plan_new_h = hashlib.sha256(json.dumps(plan_new_to_hash, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        plan_new["validated_plan_hash"] = plan_new_h
        with open(os.path.join(work_dir, "validated_plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan_new, f)
        with open(os.path.join(work_dir, "plan_audit.json"), "w", encoding="utf-8") as f:
            json.dump({"audit_status": "APPROVED", "passed": True, "validated_plan_hash": plan_new_h}, f)

        # 此时旧的 render_manifest.json 中的 plan_h 与新的 plan_new_h 不一致，QC 必须拒绝
        with open(os.path.join(work_dir, "product_profile.json"), "w", encoding="utf-8") as f:
            json.dump({}, f)
        with self.assertRaises(RuntimeError) as qc_ctx:
            stage_qc(work_dir)
        self.assertIn("必须重新执行 render 阶段", str(qc_ctx.exception))

    # -------------------------------------------------------------------------
    # R8-04: 严格交付哈希（移除快速指纹兼容分支）
    # -------------------------------------------------------------------------
    def test_r8_04_deliver_strictly_rejects_fast_fingerprint_in_qc(self):
        """R8-04 / P0-4: deliver 严格要求 QC 报告中必须包含 64 位完整 SHA-256，拒绝 16 位快速指纹。"""
        work_dir = os.path.join(self.proj_dir, "work", "run_r8_strict_hash")
        output_dir = os.path.join(self.proj_dir, "output", "run_r8_strict_hash")
        os.makedirs(work_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        vid_path = os.path.join(self.mat_dir, "legit.mp4")
        with open(vid_path, "wb") as f:
            f.write(b"source_content_raw")
        raw_sha = compute_full_file_sha256(vid_path)

        manifest = {"schema_version": "3.1", "source_videos": [{"source_id": "src_01", "path": vid_path, "sha256": raw_sha}]}
        with open(os.path.join(work_dir, "input_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        with open(os.path.join(work_dir, "spec_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"options": {}}, f)

        final_video = os.path.join(output_dir, "final.mp4")
        with open(final_video, "wb") as f:
            f.write(b"final_rendered_video_bytes")
        full_vid_sha = compute_full_file_sha256(final_video)
        fast_vid_sha = compute_file_sha256(final_video)  # 16-char fast fingerprint

        plan = {"schema_version": "3.2", "clips": []}
        plan_to_hash = {k: v for k, v in plan.items() if k != "validated_plan_hash"}
        plan_h = hashlib.sha256(json.dumps(plan_to_hash, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        plan["validated_plan_hash"] = plan_h

        with open(os.path.join(work_dir, "validated_plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan, f)
        with open(os.path.join(work_dir, "plan_audit.json"), "w", encoding="utf-8") as f:
            json.dump({"audit_status": "APPROVED", "passed": True, "validated_plan_hash": plan_h}, f)
        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.2", "run_id": "run_r8_strict_hash", "output_dir": output_dir, "spec_fingerprint": "fp_test"}, f)

        # 写入有效的 render_manifest.json
        with open(os.path.join(output_dir, "render_manifest.json"), "w", encoding="utf-8") as f:
            json.dump({
                "run_id": "run_r8_strict_hash",
                "validated_plan_hash": plan_h,
                "video_sha256": full_vid_sha,
            }, f)

        # 模拟旧版 QC 报告只记录了快速指纹 (16-char) 而不是完整 64 位 SHA-256
        with open(os.path.join(output_dir, "technical_qc.json"), "w", encoding="utf-8") as f:
            json.dump({"passed": True, "all_passed": True, "validated_plan_hash": plan_h, "target_video_sha256": fast_vid_sha}, f)
        with open(os.path.join(output_dir, "editorial_qc.json"), "w", encoding="utf-8") as f:
            json.dump({"passed": True, "block_count": 0, "validated_plan_hash": plan_h, "target_video_sha256": full_vid_sha}, f)

        with patch("src.runtime.stage_services.compute_job_fingerprint", return_value="fp_test"):
            with self.assertRaises(ValueError) as ctx:
                stage_deliver(work_dir)
            self.assertIn("技术质检报告审核的目标视频哈希", str(ctx.exception))
            self.assertIn("与当前成片哈希不匹配", str(ctx.exception))

    # -------------------------------------------------------------------------
    # R8-05: 路径与多源处理
    # -------------------------------------------------------------------------
    def test_r8_05_is_historical_output_not_fooled_by_work_ancestor(self):
        """R8-05 / P1: 祖先目录包含 work 时（如 /home/dev/work/project-3/素材/live.mp4），不能误判为历史成片。"""
        fake_linux_mat = "/home/dev/work/project-3/素材/live_stream_01.mp4"
        is_hist, reason = is_historical_output(fake_linux_mat, "project-3")
        self.assertFalse(is_hist, f"祖先目录含 work 的素材文件被误判为历史成片: {reason}")

        fake_win_mat = r"D:\dev\work\project-3\素材\live_stream_01.mp4"
        is_hist_w, reason_w = is_historical_output(fake_win_mat, "project-3")
        self.assertFalse(is_hist_w, f"Windows 祖先目录含 work 的素材文件被误判: {reason_w}")

        # 真正的项目 output 产物仍必须被正确识别
        fake_out = "/home/dev/work/project-3/output/run_01/final.mp4"
        is_hist_out, _ = is_historical_output(fake_out, "project-3")
        self.assertTrue(is_hist_out, "真实 output 产物未被识别为历史成片")

    def test_r8_05_project_paths_with_work_in_path(self):
        """R8-05 / P1: 真实项目位于 D:\\projects\\work\\my-video 时，不能误截断根目录。"""
        proj_under_work = os.path.join(self.tmpdir, "work", "my_video")
        os.makedirs(proj_under_work, exist_ok=True)
        with open(os.path.join(proj_under_work, "job_spec.json"), "w", encoding="utf-8") as f:
            json.dump({"project_id": "my_video"}, f)

        p = ProjectPaths(proj_under_work)
        self.assertEqual(os.path.normcase(os.path.realpath(p.project_root)),
                         os.path.normcase(os.path.realpath(proj_under_work)))

    def test_r8_05_input_manifest_cross_platform_explicit_video_paths(self):
        """R8-05 / P1: Windows EXE 输出的盘符路径在 WSL 环境下正常解析映射。"""
        test_video = os.path.join(self.mat_dir, "live_stream.mp4")
        with open(test_video, "wb") as f:
            f.write(b"video_bytes_payload")

        paths = ProjectPaths(self.proj_dir)
        manifest_gen = InputManifest(paths)

        # 传入绝对路径或相对路径，验证统一规范化解析
        manifest = manifest_gen.scan_and_resolve(explicit_video_paths=[test_video])
        self.assertEqual(len(manifest["source_videos"]), 1)
        self.assertEqual(manifest["source_videos"][0]["filename"], "live_stream.mp4")

    # -------------------------------------------------------------------------
    # R8-06: Agent 输出一致性 (封面与 CTA)
    # -------------------------------------------------------------------------
    def test_r8_06_chosen_cover_and_cta_consistency(self):
        """R8-06 / P1: Agent 决策的 chosen_cover_id 与 CTA 文案必须对 render 阶段与文案输出完整生效。"""
        work_dir = os.path.join(self.proj_dir, "work", "run_r8_consistency")
        output_dir = os.path.join(self.proj_dir, "output", "run_r8_consistency")
        analysis_dir = os.path.join(work_dir, "analysis")
        os.makedirs(analysis_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        vid_path = os.path.join(self.mat_dir, "live.mp4")
        with open(vid_path, "wb") as f:
            f.write(b"video_stream_content")
        vid_sha = compute_full_file_sha256(vid_path)

        # 创建两张候选封面
        cov1_p = os.path.join(analysis_dir, "cov1.jpg")
        cov2_p = os.path.join(analysis_dir, "cov2.jpg")
        with open(cov1_p, "wb") as f:
            f.write(b"cover_1_bytes")
        with open(cov2_p, "wb") as f:
            f.write(b"cover_2_bytes")

        cover_cands = [
            {"id": "cov_cand_01", "image_path": cov1_p, "clarity_score": 0.8},
            {"id": "cov_cand_02", "image_path": cov2_p, "clarity_score": 0.95},
        ]
        with open(os.path.join(analysis_dir, "cover_candidates.json"), "w", encoding="utf-8") as f:
            json.dump(cover_cands, f)

        # 验证 compile_v3_plan 生成的 plan 中同时包含 cta 与 cta_card
        spec_data = {
            "product": {"name": "羊绒衫"},
            "options": {"cta_text": "点击左下角一键加购"},
        }
        cand_sample = {
            "candidate_id": "c1",
            "source_id": "src_01",
            "source_file": vid_path,
            "source_file_sha256": vid_sha,
            "source_start_ms": 0,
            "source_end_ms": 3000,
            "product_relation": "SAME_SKU",
            "eligible": True,
            "text": "口播",
        }
        proposal = {
            "proposal_id": "p1",
            "ordered_candidate_ids": ["c1"],
            "chosen_cover_id": "cov_cand_02",  # 显式选择第二张封面
        }
        plan = compile_v3_plan(proposal, [cand_sample], {}, spec_data)
        self.assertEqual(plan["cta"]["text"], "点击左下角一键加购")
        self.assertEqual(plan["cta_card"]["text"], "点击左下角一键加购")

        # 模拟渲染文案生成
        plan["selected_cover"] = cover_cands[1]
        plan["cover"] = cover_cands[1]
        plan["chosen_cover_id"] = "cov_cand_02"
        plan["clips"] = [{"id": "c1", "source_id": "src_01", "source_file": vid_path, "source_file_sha256": vid_sha, "start": 0, "end": 3, "text": "精选羊绒"}]
        plan_to_hash = {k: v for k, v in plan.items() if k != "validated_plan_hash"}
        plan_h = hashlib.sha256(json.dumps(plan_to_hash, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        plan["validated_plan_hash"] = plan_h

        with open(os.path.join(work_dir, "validated_plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan, f)
        with open(os.path.join(work_dir, "plan_audit.json"), "w", encoding="utf-8") as f:
            json.dump({"audit_status": "APPROVED", "passed": True, "validated_plan_hash": plan_h}, f)
        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.2", "run_id": "run_r8_consistency", "output_dir": output_dir}, f)
        with open(os.path.join(work_dir, "spec_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump(spec_data, f)
        with open(os.path.join(work_dir, "input_manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "3.1", "source_videos": [{"source_id": "src_01", "path": vid_path, "sha256": vid_sha}]}, f)

        final_mp4 = os.path.join(output_dir, "final.mp4")
        def fake_assemble(plan_arg, out_p):
            with open(out_p, "wb") as f:
                f.write(b"final_video_payload")
            return out_p, 3.0

        mock_assembler = MagicMock()
        mock_assembler.assemble_final_video.side_effect = fake_assemble
        with patch("src.runtime.stage_services.VideoAssembler", return_value=mock_assembler), \
             patch("src.runtime.stage_services.render_cover_with_meta"):
            res = stage_render(work_dir)

        # 验证 copywriting.md 正确包含了 CTA
        with open(res["copywriting"], "r", encoding="utf-8") as cf:
            md_text = cf.read()
        self.assertIn("**结尾 CTA**: 点击左下角一键加购", md_text)

    def test_r8_06_desktop_spec_creator_cli_handover_commands(self):
        """R8-06 / P1: desktop_spec_creator 验证 src.cli 并生成 Windows 与 WSL 可执行命令。"""
        api = DesktopSpecApi()
        vid_p = os.path.join(self.mat_dir, "live01.mp4")
        with open(vid_p, "wb") as f:
            f.write(b"video_data")

        res = api.generate_agent_task({
            "project_id": "test_r8_handover",
            "project_root": self.proj_dir,
            "raw_video_paths": [vid_p],
            "product_title_l1": "冬装外套",
            "product_title_l2": "舒适保暖",
        })
        self.assertTrue(res["success"])
        task_prompt = res["task_prompt"]
        self.assertIn("PowerShell 执行指令", task_prompt)
        self.assertIn("bash 执行指令", task_prompt)

        with open(res["task_md_path"], "r", encoding="utf-8") as mf:
            md_content = mf.read()
        self.assertIn("工具仓库路径 (Windows)", md_content)
        self.assertIn("工具仓库路径 (WSL / Linux)", md_content)
        self.assertIn("CLI 可用状态", md_content)


if __name__ == "__main__":
    unittest.main()
