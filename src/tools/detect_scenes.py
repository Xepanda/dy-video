#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
detect_scenes.py - 镜头边界/场景切换检测工具（B2 泛化地基）
=============================================================
替代旧实现中硬编码的时间窗黑名单（55~108s 卖裤子窗、121s+ 换衣窗），
为 build_plan 提供确定性的 shot 边界，使分镜编译可泛化到任意直播素材。

方法（纯 OpenCV，零新增依赖）：
  1. 以 sample_fps 采样帧，计算相邻帧的归一化差异分：
       diff = 0.5 * mean(|gray_t - gray_{t-1}|)/255 + 0.5 * (1 - hist_corr)
  2. 自适应阈值：threshold = max(base_threshold, mean + k * std)
  3. 非极大抑制：间隔 < min_gap 的 cut 只保留差分最大的一个
  4. 输出 cut 时间戳列表与 shot 区间 [(start, end), ...]

CLI:
  python src/tools/detect_scenes.py --video x.mp4 --out-json scenes.json
"""

import os
import sys
import json
import argparse
from typing import List, Tuple, Dict, Any, Optional

import cv2
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _frame_diff(gray_a: np.ndarray, gray_b: np.ndarray,
                hist_a: np.ndarray, hist_b: np.ndarray) -> float:
    """归一化帧间差异：0.5*像素均值差 + 0.5*直方图差异。"""
    small_a = cv2.resize(gray_a, (160, 90))
    small_b = cv2.resize(gray_b, (160, 90))
    pixel = float(np.mean(np.abs(small_a.astype(np.int16) - small_b.astype(np.int16)))) / 255.0
    corr = cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_CORREL)
    hist_d = max(0.0, 1.0 - float(corr))
    return 0.5 * pixel + 0.5 * hist_d


def detect_scene_cuts(
    video_path: str,
    sample_fps: float = 5.0,
    base_threshold: float = 0.30,
    k_std: float = 3.0,
    min_gap_sec: float = 0.8,
    max_scan_sec: float = 600.0,
    cache_key: str = None,
    work_dir: str = None,
) -> List[float]:
    """检测镜头切换点，返回 cut 时间戳（秒，升序）。

    cache_key + work_dir 提供时启用阶段缓存（B13）：命中则跳过重算。
    """
    if cache_key and work_dir:
        from src.tools.stage_cache import load_stage, save_stage
        cached = load_stage(work_dir, "scenes", cache_key)
        if cached is not None:
            print(f"[SCENES] 命中阶段缓存（{len(cached)} 个切换点）")
            return cached
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise FileNotFoundError(f"无法打开视频: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = min(total_frames / fps, max_scan_sec)
    step = max(1, int(round(fps / sample_fps)))

    diffs: List[Tuple[float, float]] = []  # (timestamp, diff_score)
    prev_gray, prev_hist = None, None
    idx = 0
    while True:
        t = idx / fps
        if t >= duration:
            break
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if not ret or frame is None:
            idx += step
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        hist = cv2.calcHist([gray], [0], None, [64], [0, 256])
        cv2.normalize(hist, hist)
        if prev_gray is not None:
            diffs.append((t, _frame_diff(prev_gray, gray, prev_hist, hist)))
        prev_gray, prev_hist = gray, hist
        idx += step
    cap.release()

    if len(diffs) < 3:
        if cache_key and work_dir:
            from src.tools.stage_cache import save_stage
            save_stage(work_dir, "scenes", cache_key, [])
        return []

    scores = np.array([d for _, d in diffs])
    threshold = max(base_threshold, float(scores.mean() + k_std * scores.std()))

    raw_cuts = [(t, s) for t, s in diffs if s > threshold]
    # 非极大抑制：min_gap 内只留最高分
    cuts: List[float] = []
    for t, s in raw_cuts:
        if cuts and t - cuts[-1] < min_gap_sec:
            if s > max(s2 for t2, s2 in raw_cuts if abs(t2 - cuts[-1]) < min_gap_sec):
                cuts[-1] = t
        else:
            cuts.append(t)
    result = sorted(round(t, 2) for t in cuts)
    if cache_key and work_dir:
        from src.tools.stage_cache import save_stage
        save_stage(work_dir, "scenes", cache_key, result)
    return result


def cuts_to_shots(cuts: List[float], duration: float) -> List[Tuple[float, float]]:
    """由 cut 列表生成 shot 区间（忽略 <0.3s 的碎 shot）。"""
    bounds = [0.0] + list(cuts) + [duration]
    shots = []
    for i in range(len(bounds) - 1):
        st, en = bounds[i], bounds[i + 1]
        if en - st >= 0.3:
            shots.append((round(st, 2), round(en, 2)))
    return shots


def analyze_video_shots(video_path: str, **kwargs) -> Dict[str, Any]:
    """完整分析：cuts + shots + 每 shot 的稳定代表帧时间。"""
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total / fps
    cap.release()

    cuts = detect_scene_cuts(video_path, **kwargs)
    shots = cuts_to_shots(cuts, duration)
    return {
        "video": os.path.abspath(video_path),
        "duration": round(duration, 2),
        "fps": round(fps, 3),
        "cuts": cuts,
        "shots": [
            {"shot_id": f"s{i+1:02d}", "start": st, "end": en,
             "dur": round(en - st, 2), "stable_t": round((st + en) / 2, 2)}
            for i, (st, en) in enumerate(shots)
        ],
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="镜头边界/场景切换检测工具")
    parser.add_argument("--video", required=True, help="视频路径")
    parser.add_argument("--out-json", default=None, help="输出 JSON 路径")
    parser.add_argument("--sample-fps", type=float, default=5.0)
    parser.add_argument("--base-threshold", type=float, default=0.30)
    parser.add_argument("--max-scan-sec", type=float, default=600.0)
    args = parser.parse_args(argv)

    result = analyze_video_shots(
        args.video, sample_fps=args.sample_fps,
        base_threshold=args.base_threshold, max_scan_sec=args.max_scan_sec)

    print(f"[SCENES] 时长 {result['duration']}s | 检出 {len(result['cuts'])} 个镜头切换 | "
          f"{len(result['shots'])} 个 shot")
    for s in result["shots"][:30]:
        print(f"          {s['shot_id']}: {s['start']:>7.2f}s ~ {s['end']:>7.2f}s ({s['dur']:>5.2f}s)")

    if args.out_json:
        os.makedirs(os.path.dirname(os.path.abspath(args.out_json)), exist_ok=True)
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[SCENES] 结果已落盘: {args.out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
