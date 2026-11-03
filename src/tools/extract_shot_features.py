#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
extract_shot_features.py - 素材池画面特征提取（切片规则 R3/R4 的检索原语）
============================================================
为什么需要：
  参考成品 SSY410163 实测：10 段中 **9 段是全身景别**（人体高度占比 body_h
  中位 0.556、区间 0.520~0.681），且 **10 段呈现 10 种不同展示角度**、
  相邻不重复。而本仓库此前的选段**完全不感知画面**——只看 ASR 文本，
  必然出现"连续多段都是同一个 talking head"的成片。

  本工具把"画面长什么样"变成可检索的量化特征，供选段阶段按 R3/R4 过滤：
    body_h        肩→踝的归一化跨度（景别：全身 / 半身 / 特写）
    shoulder_w    肩宽归一化（身体朝向：正面 / 侧身）
    face_area     人脸面积占比（人脸远近）
    fabric_block  画面下部织物色块占比（是否手持/展示商品）

CLI:
  python src/tools/extract_shot_features.py --video a.mp4 --st 10 --end 25 \
      --out-json feats.json
  python src/tools/extract_shot_features.py --video a.mp4 --st 10 --end 25 --print
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from typing import Any, Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _sample_frame(video_path: str, t: float):
    """按时间戳取一帧（失败返回 None）。"""
    import cv2
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000)
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


def _fabric_block(frame) -> float:
    """画面下部"织物色块"占比——近似"手持/展示商品"的视觉信号。

    做法：取画面下部 40% 区域，按饱和度+亮度阈值统计"非背景、非人体肤色"的
    大面积色块占比。用于区分"空手上身"与"手持商品展示"。
    """
    import cv2
    import numpy as np
    h, w = frame.shape[:2]
    lower = frame[int(h * 0.55):, :]
    hsv = cv2.cvtColor(lower, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    # 织物：中等饱和 + 中高亮度（排除背景白墙与肤色高饱和红）
    mask = ((sat > 40) & (sat < 170) & (val > 50) & (val < 235)).astype(np.float32)
    # 大面积色块：3x3 盒滤波后仍高于阈值
    k = np.ones((9, 9), np.float32) / 81.0
    block = cv2.filter2D(mask, -1, k)
    return float((block > 0.45).mean())


def frame_features(frame) -> Dict[str, Any]:
    """单帧的画面特征（MediaPipe Pose + 人脸 + 织物色块 + 动态骨骼坐标）。"""
    from src.tools.detect_face import FaceAnalyzer, _load_pose_landmarks
    out = {"body_h": 0.0, "span": 0.0, "shoulder_w": 0.0, "face_area": 0.0,
           "fabric_block": 0.0, "pose_ok": False, "face_ok": False,
           "ankles_in_frame": True, "ankles_y": 0.0}
    try:
        lm = _load_pose_landmarks(frame)
    except Exception:
        lm = None
    if lm is not None:
        try:
            sh_x = (lm[11].x + lm[12].x) / 2.0
            sh_y = (lm[11].y + lm[12].y) / 2.0
            ank_y = (lm[27].y + lm[28].y) / 2.0
            head_top = min(lm[0].y, lm[1].y)
            # body_h：肩→踝（躯干+腿的可见跨度）
            out["body_h"] = float(abs(ank_y - sh_y))
            # span：头顶→踝 = 人物在画面中的**完整可见高度**（景别主判据）
            # 用 span 而非 body_h：后者受站姿/抬腿影响大，参考片实测波动 ±0.08。
            out["span"] = float(abs(ank_y - head_top))
            out["shoulder_w"] = float(abs(lm[11].x - lm[12].x))
            out["pose_ok"] = True

            # T5 手腕相对躯干/肩中心归一化坐标供时序动作性计算
            out["torso_xy"] = (float(sh_x), float(sh_y))
            out["wrist_l_rel"] = (float(lm[15].x - sh_x), float(lm[15].y - sh_y))
            out["wrist_r_rel"] = (float(lm[16].x - sh_x), float(lm[16].y - sh_y))

            # T6 脚踝出画判定（防 MediaPipe 外推虚高导致半身/膝上景判全身）
            ank_y_l, ank_y_r = float(lm[27].y), float(lm[28].y)
            ank_vis_l = float(getattr(lm[27], "visibility", 1.0))
            ank_vis_r = float(getattr(lm[28], "visibility", 1.0))
            ankles_out = (
                (ank_y_l > 1.0 and ank_y_r > 1.0) or
                (ank_vis_l < 0.5 and ank_vis_r < 0.5) or
                (ank_y > 1.0) or
                (ank_y_l > 1.05 or ank_y_r > 1.05)
            )
            out["ankles_in_frame"] = not bool(ankles_out)
            out["ankles_y"] = float(ank_y)
        except Exception:
            pass
    try:
        fa = FaceAnalyzer()
        face = fa.analyze(frame, pose_landmarks=lm) if lm is not None else None
        bbox = (face or {}).get("face_bbox_norm")
        if bbox:
            out["face_area"] = float((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
            out["face_ok"] = True
    except Exception:
        pass
    try:
        out["fabric_block"] = _fabric_block(frame)
    except Exception:
        pass
    return out


def clip_features(video_path: str, st: float, end: float,
                  samples: int = 3) -> Dict[str, Any]:
    """片段的画面特征（段内多点采样取中位，避免单帧偶然性）。"""
    dur = max(0.0, float(end) - float(st))
    if dur <= 0:
        return {"body_h": 0.0, "span": 0.0, "shoulder_w": 0.0, "face_area": 0.0,
                "fabric_block": 0.0, "wrist_motion": 0.0, "torso_motion": 0.0,
                "ankles_in_frame": False, "n_samples": 0, "pose_ok": False}
    ts = [float(st) + dur * (i + 1) / (samples + 1) for i in range(samples)]
    rows: List[Dict[str, Any]] = []
    for t in ts:
        fr = _sample_frame(video_path, t)
        if fr is None:
            continue
        rows.append(frame_features(fr))
    if not rows:
        return {"body_h": 0.0, "span": 0.0, "shoulder_w": 0.0, "face_area": 0.0,
                "fabric_block": 0.0, "wrist_motion": 0.0, "torso_motion": 0.0,
                "ankles_in_frame": False, "n_samples": 0, "pose_ok": False}

    def med(k: str) -> float:
        vals = sorted(float(r.get(k, 0.0)) for r in rows if r.get(k) is not None)
        return round(vals[len(vals) // 2], 4) if vals else 0.0

    # T5: 计算跨采样帧的手部动态动作性（手腕相对躯干位移中位）
    wrist_disps = []
    torso_disps = []
    valid_pose_rows = [r for r in rows if r.get("pose_ok") and "wrist_l_rel" in r]
    if len(valid_pose_rows) >= 2:
        for i in range(len(valid_pose_rows) - 1):
            r1, r2 = valid_pose_rows[i], valid_pose_rows[i + 1]
            dl = math.hypot(r2["wrist_l_rel"][0] - r1["wrist_l_rel"][0],
                            r2["wrist_l_rel"][1] - r1["wrist_l_rel"][1])
            dr = math.hypot(r2["wrist_r_rel"][0] - r1["wrist_r_rel"][0],
                            r2["wrist_r_rel"][1] - r1["wrist_r_rel"][1])
            wrist_disps.append(max(dl, dr))
            dt = math.hypot(r2["torso_xy"][0] - r1["torso_xy"][0],
                            r2["torso_xy"][1] - r1["torso_xy"][1])
            torso_disps.append(dt)

    wrist_motion = round(float(statistics.median(wrist_disps)), 4) if wrist_disps else 0.0
    torso_motion = round(float(statistics.median(torso_disps)), 4) if torso_disps else 0.0
    ankles_in = bool(sum(1 for r in rows if r.get("ankles_in_frame", True)) >= (len(rows) / 2.0))

    return {"body_h": med("body_h"), "span": med("span"),
            "shoulder_w": med("shoulder_w"),
            "face_area": med("face_area"), "fabric_block": med("fabric_block"),
            "wrist_motion": wrist_motion, "torso_motion": torso_motion,
            "ankles_in_frame": ankles_in,
            "n_samples": len(rows),
            "pose_ok": any(r.get("pose_ok") for r in rows),
            "face_ok": any(r.get("face_ok") for r in rows)}


def annotate_clips(clips: List[Dict[str, Any]], default_video: str,
                    work_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """给分镜列表补齐画面特征（R3/R4 判定所需），带缓存。"""
    import hashlib
    cache: Dict[str, Any] = {}
    cache_path = None
    if work_dir:
        cache_path = os.path.join(work_dir, "shot_features_cache.json")
        try:
            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    cache = json.load(f)
        except Exception:
            cache = {}
    changed = False
    for c in clips:
        vid = c.get("source_file") or default_video
        st, en = float(c.get("st", 0)), float(c.get("end", 0))
        key = hashlib.sha256(
            f"{os.path.basename(vid)}|{st:.2f}|{en:.2f}".encode("utf-8")).hexdigest()[:16]
        if key in cache:
            feats = cache[key]
        else:
            feats = clip_features(vid, st, en)
            cache[key] = feats
            changed = True
        c["body_h"] = feats.get("body_h", 0.0)
        c["span"] = feats.get("span", 0.0)
        c["shoulder_w"] = feats.get("shoulder_w", 0.0)
        c["face_area"] = feats.get("face_area", 0.0)
        c["fabric_block"] = feats.get("fabric_block", 0.0)
        c["wrist_motion"] = feats.get("wrist_motion", 0.0)
        c["torso_motion"] = feats.get("torso_motion", 0.0)
        c["ankles_in_frame"] = feats.get("ankles_in_frame", True)
        c["framing"] = _framing_of(c, feats)
    if cache_path and changed:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return clips


def _framing_of(clip: Dict[str, Any], feats: Dict[str, Any]) -> str:
    """按 body_h / span 判定景别（阈值与 constraints.slicing_rules.shot_framing 一致）。"""
    import yaml
    thr = 0.50
    try:
        cp = os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
        with open(cp, "r", encoding="utf-8") as f:
            cons = yaml.safe_load(f) or {}
        sf = ((cons.get("slicing_rules") or {}).get("shot_framing") or {})
        thr = float(sf.get("min_body_height_ratio", 0.50))
    except Exception:
        pass
    bh = float(feats.get("span", 0.0) or feats.get("body_h", 0.0) or 0.0)
    if bh <= 0:
        return "invalid"
    # T6 景别出画校验：脚踝出画时（如膝上/半身景），强制降级为 partial，防止 span 外推误判 full_body
    if not feats.get("ankles_in_frame", True):
        return "partial"
    return "full_body" if bh >= thr else "partial"


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description="素材池画面特征提取（R3/R4 检索原语）")
    ap.add_argument("--video", required=True)
    ap.add_argument("--st", type=float, required=True, help="片段起点秒")
    ap.add_argument("--end", type=float, required=True, help="片段终点秒")
    ap.add_argument("--samples", type=int, default=3, help="段内采样帧数")
    ap.add_argument("--out-json", default=None)
    ap.add_argument("--print", dest="do_print", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.exists(args.video):
        print(f"[FATAL] 视频不存在: {args.video}")
        return 2
    feats = clip_features(args.video, args.st, args.end, args.samples)
    feats["framing"] = _framing_of({}, feats)
    feats["video"] = os.path.basename(args.video)
    feats["st"] = args.st
    feats["end"] = args.end
    if args.out_json:
        os.makedirs(os.path.dirname(os.path.abspath(args.out_json)), exist_ok=True)
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(feats, f, ensure_ascii=False, indent=2)
        print(f"[SHOT-FEATURES] 已落盘: {args.out_json}")
    if args.do_print or not args.out_json:
        print(json.dumps(feats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
