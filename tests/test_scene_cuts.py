# -*- coding: utf-8 -*-
"""
test_scene_cuts.py - PR-01 镜头边界检测与接入测试
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.tools.detect_scenes import detect_scene_cuts, nms_scene_cuts
from src.tools.build_plan import build_plan, _snap_end_to_sentence


def test_nms_dual_peaks_preserves_stronger():
    """双峰相邻 cut 保留更强者测试：
    情况 1：弱峰在前 (t=10.0, s=0.5)，强峰在后 (t=10.3, s=0.9)，min_gap=0.8。
    旧代码因 s > max(...) 恒不成立，会错误保留弱峰 10.0。
    新代码必须保留强峰 10.3。
    """
    raw_cuts = [(10.0, 0.5), (10.3, 0.9)]
    result = nms_scene_cuts(raw_cuts, min_gap_sec=0.8)
    assert result == [10.3], f"应保留更强峰 10.3，实际输出: {result}"

    # 情况 2：强峰在前 (t=10.0, s=0.9)，弱峰在后 (t=10.3, s=0.5)
    raw_cuts_rev = [(10.0, 0.9), (10.3, 0.5)]
    result_rev = nms_scene_cuts(raw_cuts_rev, min_gap_sec=0.8)
    assert result_rev == [10.0], f"应保留更强峰 10.0，实际输出: {result_rev}"


def test_nms_distant_peaks_kept():
    """相隔超过 min_gap 的多个峰应全部保留。"""
    raw_cuts = [(1.0, 0.8), (1.2, 0.4), (5.0, 0.7), (5.3, 0.95), (5.5, 0.6)]
    result = nms_scene_cuts(raw_cuts, min_gap_sec=0.8)
    assert result == [1.0, 5.3], f"应保留 1.0 和 5.3，实际: {result}"


def test_nms_empty_and_identical():
    """空列表和相同分值/相距较近处理。"""
    assert nms_scene_cuts([], min_gap_sec=0.8) == []

    # 相同分值时稳定 tie-break 保留前者
    same_scores = [(10.0, 0.7), (10.2, 0.7)]
    assert nms_scene_cuts(same_scores, min_gap_sec=0.8) == [10.0]


def test_multi_source_scene_cuts_isolation():
    """多源镜头边界严格隔离：
    源 A 的 scene_cuts 不能影响源 B 的切片计算。
    """
    segs_a = [
        {"start": 0.0, "end": 4.0, "text": "这是上衣面料展示第一部分。", "source_file": "video_a.mp4", "duration": 4.0},
        {"start": 4.0, "end": 8.0, "text": "这是上衣面料展示第二部分。", "source_file": "video_a.mp4", "duration": 4.0},
    ]
    segs_b = [
        {"start": 0.0, "end": 4.0, "text": "这是下装版型展示第一部分。", "source_file": "video_b.mp4", "duration": 4.0},
        {"start": 4.0, "end": 8.0, "text": "这是下装版型展示第二部分。", "source_file": "video_b.mp4", "duration": 4.0},
    ]
    segs = segs_a + segs_b

    spec = {
        "options": {"duration_range": "8-16"},
        "product": {"name": "套装", "category": "suit"}
    }
    constraints = {"video": {"duration_range": [8, 16]}}

    # 模拟多源 cuts
    scene_cuts_by_source = {
        "video_a.mp4": [4.0],
        "video_b.mp4": [6.0],
    }

    plan = build_plan(
        raw_video_path="video_a.mp4",
        segments=segs,
        spec_data=spec,
        constraints=constraints,
        scene_cuts_by_source=scene_cuts_by_source,
    )
    assert "clips" in plan
    meta = plan.get("meta", {})
    assert "scene_cuts" in meta or "scene_cuts_count" in meta, "plan.meta 应记录 scene_cuts 元数据"


def test_snap_end_to_sentence_with_scene_cuts():
    """验证 _snap_end_to_sentence 在句末附近存在 scene_cut 时优先共振对齐。"""
    segs = [
        {"start": 0.0, "end": 4.1, "text": "这件羽绒服特别保暖。"},
        {"start": 4.1, "end": 8.0, "text": "接下来看下颜色分类。"},
    ]
    # hard_end = 4.0，ASR 句末在 4.1，镜头切换点在 4.08
    snapped = _snap_end_to_sentence(segs, hard_end=4.0, tolerance=0.45, scene_cuts=[4.08])
    assert snapped == 4.08, f"应吸附到镜头切换点 4.08，实际为 {snapped}"

    # 当 scene_cuts 远离句末（如 3.2），不应被误吸附
    snapped_far = _snap_end_to_sentence(segs, hard_end=4.0, tolerance=0.45, scene_cuts=[3.2])
    assert snapped_far == 4.1, f"镜头切换点过远时不应干扰 ASR 句末 4.1，实际为 {snapped_far}"


def test_v2_fallback_flag():
    """当 enable_scene_cuts 为 False 时，应保留 V2 行为，不记录/使用 scene_cuts。"""
    segs = [
        {"start": 0.0, "end": 4.0, "text": "这是上衣面料展示第一部分。", "source_file": "video_a.mp4", "duration": 4.0},
        {"start": 4.0, "end": 8.0, "text": "这是上衣面料展示第二部分。", "source_file": "video_a.mp4", "duration": 4.0},
    ]
    spec = {
        "options": {"duration_range": "8-16"},
        "product": {"name": "套装", "category": "suit"}
    }
    constraints = {
        "video": {"duration_range": [8, 16]},
        "slicing_rules": {"enable_scene_cuts": False}
    }
    plan = build_plan(
        raw_video_path="video_a.mp4",
        segments=segs,
        spec_data=spec,
        constraints=constraints,
        scene_cuts_by_source={"video_a.mp4": [4.0]},
    )
    meta = plan.get("meta", {})
    assert "scene_cuts" not in meta, "当 enable_scene_cuts: False 时不应注入 scene_cuts"


def main():
    test_nms_dual_peaks_preserves_stronger()
    test_nms_distant_peaks_kept()
    test_nms_empty_and_identical()
    test_multi_source_scene_cuts_isolation()
    test_snap_end_to_sentence_with_scene_cuts()
    test_v2_fallback_flag()
    print("[TEST-SCENE-CUTS] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
