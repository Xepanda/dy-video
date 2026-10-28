#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
apply_violation_handling.py - 违规词三通道处理准备器（反馈驱动 2026-10-27）
=================================================================================
对齐飞书《违规整合》要求：命中词必须**彻底处理**，不残留于最终视频。

三通道（与 constraints.violation_words.handling 对应）：
  通道 1 字幕屏蔽 —— detector.mask_text 把字幕命中词替换为 ***（assembler 执行）；
  通道 2 语音消音 —— 本工具计算每个分镜内的相对消音时段（assembler 执行 volume 表达式）；
  通道 3 视频打码 —— 本工具 OCR 扫描分镜抽帧，定位画面可见命中词并生成马赛克区域
                   （assembler 执行 crop+pixelate+overlay）。

产出 violation_handling.json（assembler 与 QC 的共同依据）：
  {
    "clips": { "<id>": {"audio_mute": [[a,b]...], "video_mosaic": [...],
                        "asr_hits": [...] } },
    "subtitle_hits": [...],
    "summary": {...}
  }

CLI:
  python src/tools/apply_violation_handling.py --plan plan.json \
      --segments whisper_segments_verified.json --work-dir <dir>
"""

import os
import sys
import json
import argparse
from typing import List, Dict, Any, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

import cv2
from src.tools.detect_violation import ViolationDetector
from src.tools.detect_ocr import detect_text_boxes

OUT_W, OUT_H = 1080, 1920
# 消音时段向左右各外扩的秒数（词边界 ASR 段粒度粗，外扩保底）
MUTE_PAD_SEC = 0.12
# OCR 命中帧的马赛克时间窗（相对帧时刻）
MOSAIC_PRE_SEC = 0.6
MOSAIC_POST_SEC = 1.2


def _load_constraints(path: str = None) -> Dict[str, Any]:
    path = path or os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
    if not os.path.exists(path):
        return {}
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _overlap(a0: float, a1: float, b0: float, b1: float) -> Optional[Tuple[float, float]]:
    """两个区间的重叠部分；无重叠返回 None。"""
    lo, hi = max(a0, b0), min(a1, b1)
    return (lo, hi) if hi - lo > 0.01 else None


def _clip_canvas_map(src_w: int, src_h: int, effect: str) -> Tuple[float, float, float]:
    """源帧坐标 → 分镜画布坐标的线性映射参数。
    返回 (scale, x_off, mirrored)。assemble 的 crop 为居中 1080 宽、无垂直裁剪。"""
    scale = OUT_W / float(src_w) if src_w else 1.0
    x_off = (src_w - OUT_W) / 2.0 if src_w > OUT_W else 0.0
    return scale, x_off, (effect == "hflip")


def prepare_violation_handling(
    plan: Dict[str, Any],
    segments: List[Dict[str, Any]],
    work_dir: str,
    constraints: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """计算三通道处理参数并落盘 violation_handling.json。"""
    constraints = constraints or _load_constraints()
    vw_c = constraints.get("violation_words", {}) or {}
    detect_c = vw_c.get("detect", {}) or {}
    handling_c = vw_c.get("handling", {}) or {}
    scan_frames = int(detect_c.get("scan_frames", 6) or 6)
    pad_px = int(handling_c.get("mosaic_pad_px", 24) or 24)

    det = ViolationDetector()
    asr_hits = det.scan_segments(segments)

    clips_out: Dict[str, Any] = {}
    n_mute_clips = 0
    n_mosaic_clips = 0

    for c in plan.get("clips", []):
        cid = c["id"]
        src = c.get("source_file") or plan.get("raw_video_path")
        st, end = float(c.get("st", 0)), float(c.get("end", 0))
        dur = float(c.get("dur", end - st))
        effect = c.get("effect", "none")
        zoom = float(c.get("zoom", 1.0) or 1.0)

        clip_hits = []
        for h in asr_hits:
            if h.get("source_file") and src and \
                    os.path.abspath(h["source_file"]) != os.path.abspath(src):
                continue
            ov = _overlap(st, end, h.get("t_start") or 0, h.get("t_end") or 0)
            if ov is None:
                continue
            clip_hits.append({**h, "rel_range": [
                round(max(0.0, ov[0] - st - MUTE_PAD_SEC), 3),
                round(min(dur, ov[1] - st + MUTE_PAD_SEC), 3)]})

        # 通道 2：语音消音时段（相对分镜起点；合并重叠区间）
        audio_mute: List[List[float]] = []
        if handling_c.get("audio_mute", True) and clip_hits:
            ranges = sorted([h["rel_range"] for h in clip_hits], key=lambda r: r[0])
            for r in ranges:
                if audio_mute and r[0] <= audio_mute[-1][1] + 0.05:
                    audio_mute[-1][1] = max(audio_mute[-1][1], r[1])
                else:
                    audio_mute.append(list(r))
            n_mute_clips += 1

        # 通道 3：视频打码（OCR 扫描画面可见命中词）
        video_mosaic: List[Dict[str, Any]] = []
        if handling_c.get("video_mosaic", True) and src and os.path.exists(src) and zoom <= 1.0:
            video_mosaic = _scan_clip_frames_for_violation(
                src, st, dur, effect, det, scan_frames, pad_px)
            if video_mosaic:
                n_mosaic_clips += 1

        clips_out[cid] = {
            "audio_mute": audio_mute,
            "video_mosaic": video_mosaic,
            "asr_hits": [{k: h.get(k) for k in
                          ("word", "category", "category_name", "t_start", "t_end")}
                         for h in clip_hits],
        }

    # 通道 1 依据：字幕将由 assembler 在 ASS 生成时屏蔽，这里登记全量命中供 QC 审计
    subtitle_hits = [{
        "word": h["word"], "category": h["category_name"],
        "t_start": h.get("t_start"), "t_end": h.get("t_end"),
        "source_file": h.get("source_file", ""),
    } for h in asr_hits]

    result = {
        "clips": clips_out,
        "subtitle_hits": subtitle_hits,
        "summary": {
            "n_asr_hits": len(asr_hits),
            "n_clips_with_mute": n_mute_clips,
            "n_clips_with_mosaic": n_mosaic_clips,
            "n_subtitle_hits": len(subtitle_hits),
            "mute_pad_sec": MUTE_PAD_SEC,
            "channels": {
                "subtitle_mask": bool(handling_c.get("subtitle_mask", True)),
                "audio_mute": bool(handling_c.get("audio_mute", True)),
                "video_mosaic": bool(handling_c.get("video_mosaic", True)),
            },
        },
        "applied": {},   # assembler 回填实际执行情况
    }
    os.makedirs(work_dir, exist_ok=True)
    out_path = os.path.join(work_dir, "violation_handling.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    s = result["summary"]
    print(f"[VIOLATION] 三通道处理参数已生成: {out_path}")
    print(f"  ASR 命中 {s['n_asr_hits']} 处 | 消音分镜 {s['n_clips_with_mute']} 个 | "
          f"打码分镜 {s['n_clips_with_mosaic']} 个 | 字幕命中 {s['n_subtitle_hits']} 处")
    return result


def _scan_clip_frames_for_violation(
    src: str, st: float, dur: float, effect: str,
    det: ViolationDetector, scan_frames: int, pad_px: int,
) -> List[Dict[str, Any]]:
    """OCR 扫描分镜抽帧，返回画面可见违规词的马赛克区域列表（画布坐标）。"""
    if dur <= 0.5:
        return []
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        return []
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    scale, x_off, mirrored = _clip_canvas_map(src_w, src_h, effect)

    regions: List[Dict[str, Any]] = []
    times = [st + dur * (i + 0.5) / max(1, scan_frames)
             for i in range(scan_frames)]
    seen_words = set()
    for t in times:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ret, frame = cap.read()
        if not ret or frame is None:
            continue
        try:
            boxes = detect_text_boxes(frame)
        except Exception:
            continue
        for b in boxes:
            text = (b.get("text") or "").strip()
            if not text:
                continue
            for h in det.scan_text(text):
                key = (h["word"], round(h["start"] / 2))
                if key in seen_words:
                    continue
                seen_words.add(key)
                bx = b.get("box") or []
                if len(bx) < 4:
                    continue
                x0, y0, x1, y1 = [float(v) for v in bx[:4]]
                # 源帧 → 画布坐标（含镜像翻转）
                cx0 = (x0 - x_off) * scale
                cx1 = (x1 - x_off) * scale
                if mirrored:
                    cx0, cx1 = OUT_W - cx1, OUT_W - cx0
                cy0, cy1 = y0 * (OUT_H / src_h if src_h else 1.0), \
                    y1 * (OUT_H / src_h if src_h else 1.0)
                cx0 = max(0, int(cx0) - pad_px)
                cy0 = max(0, int(cy0) - pad_px)
                cx1 = min(OUT_W, int(cx1) + pad_px)
                cy1 = min(OUT_H, int(cy1) + pad_px)
                if cx1 - cx0 < 8 or cy1 - cy0 < 8:
                    continue
                regions.append({
                    "box": [cx0, cy0, cx1, cy1],
                    "t0": round(max(0.0, t - st - MOSAIC_PRE_SEC), 3),
                    "t1": round(min(dur, t - st + MOSAIC_POST_SEC), 3),
                    "word": h["word"],
                    "category": h["category_name"],
                })
    cap.release()
    return regions


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="违规词三通道处理准备器")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--segments", required=True)
    parser.add_argument("--work-dir", required=True)
    args = parser.parse_args(argv)

    with open(args.plan, "r", encoding="utf-8") as f:
        plan = json.load(f)
    with open(args.segments, "r", encoding="utf-8") as f:
        segments = json.load(f)

    result = prepare_violation_handling(plan, segments, args.work_dir)
    return 0 if result["summary"]["n_asr_hits"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
