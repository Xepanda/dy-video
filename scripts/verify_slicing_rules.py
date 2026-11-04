#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_slicing_rules.py - 切片规则自检工具（参考成片复现 / 成片抽检）
============================================================
用途：
  1. **复现验证**：把一条参考成品喂进规则引擎，看规则是否描述了它的真实行为。
     理想结果：参考成片自身应**全部通过**（R2~R5 全 PASS）——
     若参考成片都过不了，说明规则定错了方向。
  2. **成片抽检**：对本仓库产出的 plan.json 跑同一套规则，输出逐项判定。

用法：
  python -u scripts/verify_slicing_rules.py --video "参考成片.mp4"
  python -u scripts/verify_slicing_rules.py --video "参考成片.mp4" --cuts 0,4.72,6.5
  python -u scripts/verify_slicing_rules.py --plan data/work_dirs/<pid>/plan.json
"""
import argparse
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml

from src.tools.slicing_rules import (evaluate_plan_slicing, load_slicing_rules,
                                     check_intra_sentence_ratio)
from src.tools.extract_shot_features import clip_features, _framing_of


def _constraints():
    p = os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _detect_cuts(video: str) -> list:
    """多特征联合硬切检测（像素差 + 边缘直方图 + 颜色直方图）。"""
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(video)
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(cv2.resize(f, (160, 284)))
    cap.release()
    if not frames:
        return []

    def feat(f):
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        e = cv2.Canny(g, 60, 160)
        eh = cv2.calcHist([e], [0], None, [64], [0, 256]).flatten()
        eh /= eh.sum()
        hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
        ch = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256]).flatten()
        ch /= ch.sum()
        return g.astype(np.float32), eh, ch

    feats = [feat(f) for f in frames]
    scores = []
    for k in range(1, len(feats)):
        g0, e0, c0 = feats[k - 1]
        g1, e1, c1 = feats[k]
        px = float(np.abs(g0 - g1).mean())
        eh = float(np.abs(e0 - e1).sum())
        ch = float(np.abs(c0 - c1).sum())
        scores.append((k / cap.get(cv2.CAP_PROP_FPS) if False else k / 21.8309,
                       px * 0.5 + eh * 18 + ch * 22))
    arr = np.array([s[1] for s in scores])
    thr = max(float(np.percentile(arr, 99)), 12.0)
    cuts = [t for t, v in scores if v > thr]
    # 合并 0.3s 内的相邻切点
    merged = []
    for t in cuts:
        if merged and t - merged[-1][-1] < 0.3:
            merged[-1].append(t)
        else:
            merged.append([t])
    return [round(sum(g) / len(g), 2) for g in merged]


def verify_video(video: str, cuts=None, constraints=None) -> dict:
    constraints = constraints or _constraints()
    if cuts is None:
        cuts = _detect_cuts(video)
    bounds = [0.0] + list(cuts)
    clips = []
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        if b - a < 0.3:
            continue
        f = clip_features(video, a, b, 3)
        f["framing"] = _framing_of({}, f)
        f["id"] = f"seg{i + 1:02d}"
        f["dur"] = round(b - a, 2)
        clips.append(f)
    rep = evaluate_plan_slicing(clips, {"options": {}}, constraints)
    return {"video": os.path.basename(video), "cuts": cuts,
            "n_segments": len(clips), "report": rep, "clips": clips}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="切片规则自检")
    ap.add_argument("--video", help="待检视频（参考成片）")
    ap.add_argument("--cuts", default=None,
                    help="手动指定切点（逗号分隔秒），跳过自动检测")
    ap.add_argument("--plan", help="待检 plan.json")
    ap.add_argument("--out-json", default=None)
    args = ap.parse_args(argv)

    constraints = _constraints()
    payload = None

    if args.video:
        cuts = [float(x) for x in args.cuts.split(",")] if args.cuts else None
        payload = verify_video(args.video, cuts, constraints)
        print(f"=== 切片规则自检：{payload['video']} ===")
        print(f"  切点 {len(payload['cuts'])} 个 → {payload['n_segments']} 段")
        for c in payload["clips"]:
            print(f"    {c['id']} {c['dur']:5.2f}s span={c.get('span', 0):.3f} "
                  f"sh_w={c.get('shoulder_w', 0):.3f} "
                  f"face={c.get('face_area', 0):.4f} "
                  f"fabric={c.get('fabric_block', 0):.3f} -> {c['framing']}")
        print()
        for ck in payload["report"]["checks"]:
            mark = "PASS" if ck.get("passed") else "FAIL"
            print(f"  [{mark}/{ck.get('level')}] {ck['id']}: {ck.get('detail')}")
        passed = payload["report"].get("passed")
        print(f"\n  总体：{'ALL PASS' if passed else 'HAS FAIL'}")

    elif args.plan:
        with open(args.plan, "r", encoding="utf-8") as f:
            plan = json.load(f)
        clips = plan.get("clips", [])
        rep = evaluate_plan_slicing(clips, plan.get("spec") or {}, constraints)
        payload = {"plan": args.plan, "n_segments": len(clips), "report": rep}
        print(f"=== 切片规则自检：{args.plan}（{len(clips)} 段）===")
        for ck in rep["checks"]:
            mark = "PASS" if ck.get("passed") else "FAIL"
            print(f"  [{mark}/{ck.get('level')}] {ck['id']}: {ck.get('detail')}")
        print(f"\n  总体：{'ALL PASS' if rep.get('passed') else 'HAS FAIL'}")
        passed = rep.get("passed")
    else:
        ap.print_help()
        return 2

    if args.out_json:
        os.makedirs(os.path.dirname(os.path.abspath(args.out_json)), exist_ok=True)
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[VERIFY] 已落盘: {args.out_json}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
