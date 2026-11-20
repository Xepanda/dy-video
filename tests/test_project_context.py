# -*- coding: utf-8 -*-
"""
test_project_context.py - 统一项目上下文与防旧成片污染单元测试 (V3 PR-16)
"""

import os
import sys
import json
import tempfile
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.project_context import (
    ProjectPaths,
    InputManifest,
    is_historical_output,
    sanitize_id,
)


def test_sanitize_id():
    assert sanitize_id("C:\\Users\\test/video.mp4") == "C_Users_test_video_mp4"
    assert sanitize_id("src-01_clean") == "src-01_clean"
    assert sanitize_id("") == "src"


def test_project_paths_structure():
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_root = os.path.join(tmpdir, "my_project")
        os.makedirs(os.path.join(proj_root, "素材"), exist_ok=True)

        paths = ProjectPaths(proj_root, run_id="run_20261120_test")
        assert paths.project_id == "my_project"
        assert paths.materials_dir == os.path.join(proj_root, "素材")
        assert paths.work_dir == os.path.join(proj_root, "work", "run_20261120_test")
        assert paths.output_dir == os.path.join(proj_root, "output", "run_20261120_test")
        assert paths.latest_pointer == os.path.join(proj_root, "output", "latest.json")

        paths.ensure_runtime_dirs()
        assert os.path.isdir(paths.work_dir)
        assert os.path.isdir(paths.output_dir)
        # 绝不在素材目录下创建 work 或 output
        assert not os.path.exists(os.path.join(paths.materials_dir, "work"))
        assert not os.path.exists(os.path.join(paths.materials_dir, "output"))


def test_historical_output_detection():
    pid = "project-3"

    # 1. 同名成片
    is_hist, reason = is_historical_output(f"/some/dir/{pid}.mp4", pid)
    assert is_hist is True
    assert "成片" in reason

    # 2. 带 final 后缀
    is_hist, _ = is_historical_output(f"/some/dir/{pid}_final.mp4", pid)
    assert is_hist is True

    is_hist, _ = is_historical_output("/some/dir/final.mp4", pid)
    assert is_hist is True

    # 3. 位于 output 目录
    is_hist, reason = is_historical_output(f"/root/output/run_01/clip_01.mp4", pid)
    assert is_hist is True
    assert "output" in reason

    # 4. 真实原始素材
    is_hist, _ = is_historical_output("/some/dir/20261018_live_stream_01.mp4", pid)
    assert is_hist is False


def test_input_manifest_scans_and_excludes_old_outputs():
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_root = os.path.join(tmpdir, "project-3")
        mat_dir = os.path.join(proj_root, "素材")
        os.makedirs(mat_dir, exist_ok=True)

        # 真实原始视频
        raw1 = os.path.join(mat_dir, "20261018_live_part1.mp4")
        raw2 = os.path.join(mat_dir, "20261018_live_part2.mp4")
        with open(raw1, "wb") as f: f.write(b"video1_data")
        with open(raw2, "wb") as f: f.write(b"video2_data")

        # 历史旧成片
        old_out = os.path.join(mat_dir, "project-3.mp4")
        old_final = os.path.join(mat_dir, "final.mp4")
        with open(old_out, "wb") as f: f.write(b"old_rendered_video")
        with open(old_final, "wb") as f: f.write(b"old_final_video")

        # 商品图
        img = os.path.join(mat_dir, "product_front.jpg")
        with open(img, "wb") as f: f.write(b"image_data")

        paths = ProjectPaths(proj_root, run_id="test_run_01")
        manifest_mgr = InputManifest(paths)
        manifest = manifest_mgr.scan_and_resolve()

        # 验证提取的有效源视频只有 2 个真实直播片段，历史成片被排除
        src_files = [s["filename"] for s in manifest["source_videos"]]
        assert "20261018_live_part1.mp4" in src_files
        assert "20261018_live_part2.mp4" in src_files
        assert "project-3.mp4" not in src_files
        assert "final.mp4" not in src_files

        # 验证 source_id 为规范且不含路径字符的标识符
        for s in manifest["source_videos"]:
            assert ":" not in s["source_id"]
            assert "/" not in s["source_id"]
            assert "\\" not in s["source_id"]
            assert s["source_id"].startswith("src_")

        # 验证排除列表中记录了被排除的旧成片
        excl_paths = [e["path"] for e in manifest["excluded_files"]]
        assert old_out in excl_paths
        assert old_final in excl_paths

        # 验证保存到 work/<run_id>，绝不写素材目录
        saved_p = manifest_mgr.save_manifest()
        assert os.path.isfile(saved_p)
        assert paths.work_dir in saved_p
        assert not os.path.exists(os.path.join(mat_dir, "input_manifest.json"))


def test_input_manifest_explicit_list_rejects_historical_output():
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_root = os.path.join(tmpdir, "project-3")
        os.makedirs(proj_root, exist_ok=True)
        raw_vid = os.path.join(proj_root, "raw_live.mp4")
        with open(raw_vid, "wb") as f: f.write(b"raw")
        fake_old = os.path.join(proj_root, "project-3.mp4")
        with open(fake_old, "wb") as f: f.write(b"old")

        paths = ProjectPaths(proj_root, run_id="run_exp")
        manifest_mgr = InputManifest(paths)
        # 即使显式传入旧成片，也必须被拦截拦截排除
        res = manifest_mgr.scan_and_resolve(explicit_video_paths=[raw_vid, fake_old])
        src_paths = [s["path"] for s in res["source_videos"]]
        assert raw_vid in src_paths
        assert fake_old not in src_paths
        assert any("project-3.mp4" in e["path"] for e in res["excluded_files"])


def main():
    test_sanitize_id()
    test_project_paths_structure()
    test_historical_output_detection()
    test_input_manifest_scans_and_excludes_old_outputs()
    test_input_manifest_explicit_list_rejects_historical_output()
    print("[PASS] All project_context tests passed!")


if __name__ == "__main__":
    main()
