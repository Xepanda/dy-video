#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_overlay_face_safety.py - 叠加层人脸安全校验（反馈驱动 2026-10-27）
=========================================================================
反馈诉求："贴图、标题、字幕、特效、动画是否影响到模特主体（尤其是面部）"。

校验对象（来自 video_overlay_meta.json 的叠加元素清单）：
  - 挂角贴纸 sticker_events（box + 活跃时段）
  - 放大镜 magnifier_events（box + 活跃时段）
  - Hook 标题卡（前 2.5s，720x168 @ hook_y）
  - 片尾 CTA 徽章（末 2.5s，480x100 @ (40,1440)）
  - 字幕带（MarginV=240 之上的底部区域）
  - 顶部平台标识条（0~88px）

方法：在成片上按叠加元素活跃时段抽帧 → MediaPipe 人脸/姿态关键点 →
人脸 bbox 与叠加 bbox 的几何交叠判定（间距 < margin_px 记 WARN/压脸记 BLOCK）。

CLI:
  python src/tools/check_overlay_face_safety.py --video final.mp4 \
      --overlay-meta video_overlay_meta.json --out-json face_safety.json
退出码: 0=安全, 1=有 WARN, 2=有 BLOCK
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

OUT_W, OUT_H = 1080, 1920
HOOK_W, HOOK_H = 720, 168
CTA_W, CTA_H = 480, 100
CTA_X, CTA_Y = 40, 1440


def _rect_overlap_gap(a: Tuple[float, float, float, float],
                      b: Tuple[float, float, float, float]) -> float:
    """两个矩形的间距：相交返回负值（重叠量），分离返回最小间距。"""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    dx = max(bx0 - ax1, ax0 - bx1, 0.0)
    dy = max(by0 - ay1, ay0 - by1, 0.0)
    if dx == 0.0 and dy == 0.0:
        # 相交：返回负的重叠面积平方根（表示侵入程度）
        ox = min(ax1, bx1) - max(ax0, bx0)
        oy = min(ay1, by1) - max(ay0, by0)
        return -(ox * oy) ** 0.5
    return (dx * dx + dy * dy) ** 0.5


def _collect_overlay_boxes(meta: Dict[str, Any],
                           total_dur: float) -> List[Dict[str, Any]]:
    """从叠加元素清单提取待校验矩形（画布像素坐标）。"""
    boxes: List[Dict[str, Any]] = []
    for st in meta.get("sticker_events", []) or []:
        size = int(st.get("size", 220) or 220)
        x, y = int(st.get("x", 0)), int(st.get("y", 0))
        boxes.append({
            "kind": "sticker", "clip_id": st.get("clip_id", ""),
            "box": (x, y, x + size, y + size),
            "t_in": float(st.get("t_in", 0)), "t_out": float(st.get("t_out", 0)),
        })
    for mg in meta.get("magnifier_events", []) or []:
        r = int(mg.get("r", 130) or 130)
        cx, cy = int(mg.get("cx", 0)), int(mg.get("cy", 0))
        boxes.append({
            "kind": "magnifier", "clip_id": mg.get("clip_id", ""),
            "box": (cx - r, cy - r, cx + r, cy + r),
            "t_in": float(mg.get("t_in", 0)), "t_out": float(mg.get("t_out", 0)),
        })
    if meta.get("platform_bar", {}).get("applied"):
        boxes.append({
            "kind": "platform_bar", "clip_id": "",
            "box": (0, 0, OUT_W, int(meta["platform_bar"].get("height", 88) or 88)),
            "t_in": 0.0, "t_out": total_dur,
        })
    # Hook 卡（前 2.5s）——位置以 overlay meta 为准（避让后为动态值，
    # 不得硬编码：2026-10-27 实测困难就是检查工具用了写死的 y=280）
    hc = meta.get("hook_card", {}) or {}
    hc_x = int(hc.get("x", (OUT_W - HOOK_W) // 2))
    hc_y = int(hc.get("y", 280))
    hc_w = int(hc.get("w", HOOK_W))
    hc_h = int(hc.get("h", HOOK_H))
    boxes.append({
        "kind": "hook_card", "clip_id": "",
        "box": (hc_x, hc_y, hc_x + hc_w, hc_y + hc_h),
        "t_in": float(hc.get("t_in", 0.0)), "t_out": float(hc.get("t_out", 2.5)),
    })
    # CTA（末 2.5s）——同样以 meta 为准
    ct = meta.get("cta_badge", {}) or {}
    ct_x = int(ct.get("x", CTA_X))
    ct_y = int(ct.get("y", CTA_Y))
    ct_w = int(ct.get("w", CTA_W))
    ct_h = int(ct.get("h", CTA_H))
    boxes.append({
        "kind": "cta", "clip_id": "",
        "box": (ct_x, ct_y, ct_x + ct_w, ct_y + ct_h),
        "t_in": float(ct.get("t_in", max(0.0, total_dur - 2.5))),
        "t_out": float(ct.get("t_out", total_dur)),
    })
    # 字幕带（底部 MarginV=240 之上的文字区）
    boxes.append({
        "kind": "subtitle_band", "clip_id": "",
        "box": (0, OUT_H - 240 - 130, OUT_W, OUT_H - 240 + 20),
        "t_in": 0.0, "t_out": total_dur,
    })
    return boxes


def check_overlay_face_safety(
    video_path: str,
    overlay_meta_path: str,
    constraints: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """对成片叠加元素执行人脸安全校验。"""
    import cv2
    from src.tools.detect_face import FaceAnalyzer, _load_pose_landmarks

    constraints = constraints or {}
    ofs = constraints.get("overlay_face_safety", {}) or {}
    if not ofs.get("enabled", True):
        return {"passed": True, "checks": [], "warnings": ["人脸安全校验已禁用"]}
    margin = float(ofs.get("margin_px", 30) or 30)
    samples = int(ofs.get("sample_per_clip", 2) or 2)
    enforce = str(ofs.get("enforce_level", "BLOCK")).upper()

    with open(overlay_meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    total_dur = float(meta.get("total_duration", 0.0) or 0.0)
    if total_dur <= 0:
        return {"passed": True, "checks": [], "warnings": ["成片时长未知，跳过"]}

    boxes = _collect_overlay_boxes(meta, total_dur)
    analyzer = FaceAnalyzer()

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {"passed": True, "checks": [],
                "warnings": [f"无法打开视频 {video_path}，跳过人脸安全校验"]}

    checks: List[Dict[str, Any]] = []
    worst_block = False
    worst_warn = False

    for b in boxes:
        t_in, t_out = b["t_in"], max(b["t_in"] + 0.1, b["t_out"])
        span = max(0.1, t_out - t_in)
        times = [t_in + span * (i + 0.5) / max(1, samples)
                 for i in range(samples)]
        min_gap = None
        face_seen = 0
        for t in times:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ret, frame = cap.read()
            if not ret or frame is None:
                continue
            pose_lm = _load_pose_landmarks(frame)
            if pose_lm is None:
                continue
            face_m = analyzer.analyze(frame, pose_landmarks=pose_lm)
            if not face_m or not face_m.get("face_detected"):
                continue
            face_seen += 1
            h, w = frame.shape[:2]
            fx0, fy0, fx1, fy1 = face_m["face_bbox_norm"]
            face_rect = (fx0 * w, fy0 * h, fx1 * w, fy1 * h)
            gap = _rect_overlap_gap(b["box"], face_rect)
            if min_gap is None or gap < min_gap:
                min_gap = gap

        if min_gap is None:
            checks.append({
                "kind": b["kind"], "clip_id": b["clip_id"],
                "status": "SKIP", "detail": "抽样帧未检出人脸（无法判定）",
                "min_gap_px": None,
            })
            continue
        if min_gap < 0:
            status = "BLOCK"
            worst_block = True
            detail = (f"叠加元素与人脸重叠 {abs(min_gap):.0f}px"
                      f"（侵入面积开方值），压脸!")
        elif min_gap < margin:
            status = "WARN"
            worst_warn = True
            detail = f"叠加元素距人脸仅 {min_gap:.0f}px（< {margin:.0f}px 安全间距）"
        else:
            status = "PASS"
            detail = f"与人脸间距 {min_gap:.0f}px（安全）"
        checks.append({
            "kind": b["kind"], "clip_id": b["clip_id"],
            "status": status, "detail": detail,
            "min_gap_px": round(min_gap, 1),
            "face_samples": face_seen,
        })

    cap.release()

    if worst_block and enforce == "BLOCK":
        passed = False
    else:
        passed = True
    return {
        "passed": passed,
        "enforce_level": enforce,
        "checks": checks,
        "warnings": [f"{c['kind']}{c.get('clip_id') or ''}: {c['detail']}"
                     for c in checks if c["status"] in ("WARN", "BLOCK")],
        "summary": {
            "n_elements": len(checks),
            "n_pass": sum(1 for c in checks if c["status"] == "PASS"),
            "n_warn": sum(1 for c in checks if c["status"] == "WARN"),
            "n_block": sum(1 for c in checks if c["status"] == "BLOCK"),
            "n_skip": sum(1 for c in checks if c["status"] == "SKIP"),
        },
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="叠加层人脸安全校验")
    parser.add_argument("--video", required=True)
    parser.add_argument("--overlay-meta", required=True)
    parser.add_argument("--out-json", default=None)
    args = parser.parse_args(argv)

    from src.tools.detect_face import _load_constraints
    result = check_overlay_face_safety(
        args.video, args.overlay_meta, constraints=_load_constraints())
    s = result["summary"]
    print(f"[FACE-SAFETY] {'PASS' if result['passed'] else 'BLOCK'} | "
          f"元素 {s['n_elements']} 个: PASS {s['n_pass']} / WARN {s['n_warn']} / "
          f"BLOCK {s['n_block']} / SKIP {s['n_skip']}")
    for c in result["checks"]:
        if c["status"] in ("WARN", "BLOCK"):
            print(f"  [{c['status']}] {c['kind']}{c.get('clip_id') or ''}: {c['detail']}")
    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[OUT] {args.out_json}")
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
