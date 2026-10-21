#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断脚本：逐门禁统计封面候选拒绝原因（泛化验收排查用）"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
import cv2, numpy as np, mediapipe as mp
from mediapipe.tasks import python
from mediapipe.tasks.python import vision
from src.tools.detect_face import FaceAnalyzer

POSE_MODEL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "pose_landmarker_lite.task")

def diagnose(video_path, scan_limit=180.0, step=1.5):
    opts = vision.PoseLandmarkerOptions(
        base_options=python.BaseOptions(model_asset_path=POSE_MODEL),
        output_segmentation_masks=True, num_poses=1)
    pose = vision.PoseLandmarker.create_from_options(opts)
    fa = FaceAnalyzer()
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    dur = min(total / fps, scan_limit)
    stats = {"frames": 0, "no_pose": 0, "dz_fail": 0, "ankle_fail": 0, "occluded": 0,
             "sharp_fail": 0, "knee_fail": 0, "face_fail": 0, "passed": 0}
    face_details = {"yaw": [], "ear": [], "mar": [], "face_area": [], "face_sharp": []}
    fail_reasons = []
    t = 1.0
    while t < dur:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
        ret, frame = cap.read()
        if not ret:
            t += step; continue
        stats["frames"] += 1
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        res = pose.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
        if not res.pose_landmarks:
            stats["no_pose"] += 1; t += step; continue
        lm = res.pose_landmarks[0]
        ls, rs, la, ra = lm[11], lm[12], lm[27], lm[28]
        dz = abs(ls.z - rs.z)
        if dz > 0.45:
            stats["dz_fail"] += 1; t += step; continue
        ankles = (la.y <= 0.99 and ra.y <= 0.99)
        knees_ok = (lm[25].y <= 0.92 and lm[26].y <= 0.92
                    and lm[25].visibility > 0.30 and lm[26].visibility > 0.30)
        occ = (lm[15].y < lm[0].y or lm[16].y < lm[0].y)
        if occ or not ankles or not knees_ok:
            stats["occluded" if occ else ("ankle_fail" if not ankles else "knee_fail")] += 1
            t += step; continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        if sharp < 200:
            stats["sharp_fail"] += 1; t += step; continue
        fm = fa.analyze(frame, pose_landmarks=lm)
        if fm:
            face_details["yaw"].append(fm.get("yaw_deg", 999))
            face_details["ear"].append(fm.get("ear", 0))
            face_details["mar"].append(fm.get("mar", 9))
            face_details["face_area"].append(fm.get("face_area_ratio", 0))
            face_details["face_sharp"].append(fm.get("face_sharpness", 0))
        ok, failed = fa.check_constraints(fm, {"face_yaw_max_deg": 12, "ear_min": 0.18,
                                               "mar_max": 0.35, "face_sharpness_min": 150,
                                               "face_area_ratio_min": 0.003,
                                               "face_area_ratio_max": 0.25})
        if not ok:
            stats["face_fail"] += 1
            fail_reasons.append({"t": round(t, 1), "failed": failed,
                                 "yaw": fm.get("yaw_deg") if fm else None,
                                 "ear": fm.get("ear") if fm else None,
                                 "sharp": round(sharp, 1)})
            t += step; continue
        stats["passed"] += 1
        t += step
    cap.release()
    print(f"\n=== {os.path.basename(video_path)} (scan {dur:.0f}s) ===")
    print(json.dumps(stats, ensure_ascii=False))
    for k, v in face_details.items():
        if v:
            arr = np.array(v)
            print(f"  {k}: n={len(arr)} min={arr.min():.3f} p25={np.percentile(arr,25):.3f} "
                  f"median={np.median(arr):.3f} p75={np.percentile(arr,75):.3f} max={arr.max():.3f}")
    if fail_reasons:
        print(f"  人脸门禁失败样例(前8): {json.dumps(fail_reasons[:8], ensure_ascii=False)}")

if __name__ == "__main__":
    for v in sys.argv[1:]:
        diagnose(v)
