# -*- coding: utf-8 -*-
"""
test_job_runner_and_adapters.py - PR-06 状态机、AGY 适配器与一键任务测试
"""

import os
import sys
import json
import tempfile
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.runtime.adapters.agent_protocol import AgentStatus, AgentResponse
from src.runtime.adapters.agy_headless import AgyHeadlessAdapter
from src.runtime.job_runner import JobRunner, JobStage


def test_agy_not_found_handling():
    """测试 AGY CLI 不存在时的优雅阻断，返回 AUTH_REQUIRED / 安装引导。"""
    adapter = AgyHeadlessAdapter(agy_executable="non_existent_agy_binary_xyz")
    ok, msg = adapter.check_availability()
    assert ok is False
    assert "未找到" in msg

    resp = adapter.invoke("prompt", "schema.json", ".")
    assert resp.status == AgentStatus.AUTH_REQUIRED
    assert "不可用" in resp.error_message


def test_agy_auth_and_permission_detection():
    """模拟探测 AGY 未登录或权限软拒绝识别。"""
    auth_resp = AgentResponse(
        status=AgentStatus.AUTH_REQUIRED,
        error_message="not logged in",
        exit_code=1
    )
    assert auth_resp.is_success is False
    assert auth_resp.status == AgentStatus.AUTH_REQUIRED


def test_job_runner_state_transitions_and_cancel():
    """测试状态机推进与中间取消 (CANCELED)。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        work_dir = os.path.join(tmpdir, "work")
        out_dir = os.path.join(tmpdir, "out")

        spec = {
            "project_id": "test_runner",
            "raw_video_path": "dummy.mp4",
            "product": {"name": "针织衫", "category": "上衣"}
        }
        runner = JobRunner(spec, work_dir, out_dir)
        assert runner.state == JobStage.QUEUED

        # 触发取消
        runner.cancel()
        assert runner.state == JobStage.CANCELED
        assert runner.is_canceled is True

        res = runner.run_pipeline()
        assert res["status"] == JobStage.CANCELED


def test_job_runner_missing_video_needs_input():
    """测试缺少素材视频时状态机进入 NEEDS_INPUT。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        work_dir = os.path.join(tmpdir, "work")
        out_dir = os.path.join(tmpdir, "out")

        spec = {
            "project_id": "test_missing_video",
            "raw_video_path": "",
            "raw_video_paths": [],
            "product": {"name": "大衣"}
        }
        runner = JobRunner(spec, work_dir, out_dir)
        res = runner.run_pipeline()
        assert res["status"] == JobStage.NEEDS_INPUT
        assert "缺少" in res["reason"]


def test_workdir_with_chinese_and_spaces():
    """测试包含中文与空格的工作目录路径健壮性。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        chinese_dir = os.path.join(tmpdir, "直播 电商 任务目录 2026")
        work_dir = os.path.join(chinese_dir, "work space")
        out_dir = os.path.join(chinese_dir, "output dir")

        spec = {
            "project_id": "chinese_space_test",
            "raw_video_path": "a.mp4",
            "product": {"name": "羽绒服"}
        }
        runner = JobRunner(spec, work_dir, out_dir)
        res = runner.run_pipeline()
        assert os.path.exists(runner.checkpoint_file)
        assert os.path.exists(runner.events_log)


def test_agy_headless_json_extraction_with_markdown_and_logs():
    """测试 AGY 适配器对带有 Markdown 代码块与前置日志的鲁棒 JSON 提取。"""
    adapter = AgyHeadlessAdapter()
    raw_output = """
[INFO] Initializing Google Antigravity Agent...
[INFO] Model loaded successfully.
```json
{
  "status": "PROPOSED",
  "structured_output": {
    "proposal_id": "p_01",
    "selected_clips": ["c1", "c2"]
  }
}
```
Done.
"""
    # 模拟 proc
    class DummyProc:
        returncode = 0
        stdout = raw_output
        stderr = ""

    import subprocess
    orig_run = subprocess.run
    try:
        subprocess.run = lambda *args, **kwargs: DummyProc()
        resp = adapter.invoke("prompt", __file__, ".")
        assert resp.is_success is True
        assert resp.structured_output.get("proposal_id") == "p_01"
    finally:
        subprocess.run = orig_run


def test_cut_optimizer_with_st_dur_not_inflating():
    """验证 CutOptimizer 处理仅携带 st（而非 start）的真实分镜时不会将 dur 算错膨胀。"""
    from src.core.cut_optimizer import CutOptimizer
    clips = [
        {"candidate_id": "c1", "st": 120.0, "end": 125.0, "dur": 5.0},
        {"candidate_id": "c2", "st": 130.0, "end": 135.0, "dur": 5.0}
    ]
    report = {
        "seams": [{
            "seam_id": "seam_001",
            "left_candidate_id": "c1",
            "suggested_action": "SHIFT_CUT",
            "suggested_time_shift_ms": 250
        }]
    }
    opt = CutOptimizer()
    fixed, actions = opt.optimize_cuts(clips, report)
    assert len(actions) == 1
    assert fixed[0]["end"] == 125.25
    assert fixed[0]["dur"] == 5.25, f"dur 应当为 5.25s 而非 125.25s，实际为 {fixed[0]['dur']}"


def test_candidate_generator_emits_text_and_shot_alignment():
    """验证 CandidateGenerator 输出 text 字段且与镜头切换点共振对齐。"""
    from src.core.candidate_generator import CandidateGenerator
    cg = CandidateGenerator(min_candidate_ms=2000, max_candidate_ms=8000)
    utts = [
        {
            "utterance_id": "u1",
            "start_ms": 0,
            "end_ms": 3000,
            "text": "今天给大家带来这款重磅连帽卫衣，",
            "ends_sentence": False
        },
        {
            "utterance_id": "u2",
            "start_ms": 3000,
            "end_ms": 5000,
            "text": "采用绵羊毛混纺面料。",
            "ends_sentence": True
        }
    ]
    prof = {"name": "连帽卫衣", "selling_points": ["绵羊毛"]}
    shots = [
        {"shot_id": "s_01", "start_ms": 0, "end_ms": 5150}  # 句末 5000ms 附近有 5150ms 的镜头切点
    ]
    cands = cg.generate_candidates(utts, prof, shots=shots)
    assert len(cands) >= 1
    # 找到完整包含卖点与吸附镜头的合规候选
    c = next(cand for cand in cands if "绵羊毛" in cand.get("text", "") and cand.get("eligible", False))
    assert "text" in c
    assert "绵羊毛" in c["text"]
    assert c["source_end_ms"] == 5150, f"应吸附到镜头切换时刻 5150ms，实际为 {c['source_end_ms']}"
    assert "s_01" in c["shot_ids"]


def main():
    test_agy_not_found_handling()
    test_agy_auth_and_permission_detection()
    test_job_runner_state_transitions_and_cancel()
    test_job_runner_missing_video_needs_input()
    test_workdir_with_chinese_and_spaces()
    test_agy_headless_json_extraction_with_markdown_and_logs()
    test_cut_optimizer_with_st_dur_not_inflating()
    test_candidate_generator_emits_text_and_shot_alignment()
    print("[TEST-JOB-RUNNER] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
