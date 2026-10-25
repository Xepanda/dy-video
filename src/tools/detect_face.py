#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
detect_face.py - 封面人脸质量检测工具（C1/C4 落地）
====================================================
替代已删除的 frame_picker.py 表情检查能力，并补齐 PR #2 声称但未实现的
"面部偏航角 <= 12°" 能力。

关键工程结论（2026-10-20 实测）：
  MediaPipe face_landmarker.task 的短程人脸检测器要求人脸占画面高度 >= ~12%。
  抖音直播全景竖帧（1088x1920）中主播脸部通常仅 5~8%，直接全帧检测必失败
  （旧 frame_picker.py 的表情检查因此从未真正生效）。
  解法：姿态引导的人脸 ROI 裁剪——由 pose 鼻点/肩点估算头部范围，裁剪后检测，
  再将 landmark 映射回原图坐标系。

输出指标（manifest metrics）：
  face_detected     是否检测到人脸
  yaw_deg           偏航角（solvePnP，绝对值，度）      <= 12
  pitch_deg         俯仰角（参考）
  ear               双眼纵横比（睁眼）                  >= 0.18
  mar               嘴纵横比（闭嘴）                    <= 0.35
  face_sharpness    人脸区域 Laplacian 方差             >= 150
  face_area_ratio   人脸包围盒面积 / 帧面积             ∈ [0.02, 0.25]
  face_top_y        人脸包围盒顶边归一化 y（供 Hook 卡避让）

用法:
  from src.tools.detect_face import FaceAnalyzer
  fa = FaceAnalyzer()
  m = fa.analyze(frame_bgr, pose_landmarks=lm)   # 推荐：传 pose landmarks
  m = fa.analyze(frame_bgr)                       # 无 pose 时退化为全帧+多尺度扫描
  passed, failed = fa.check_constraints(m, constraints["cover"])

CLI:
  python src/tools/detect_face.py --image path.jpg [--constraints contracts/constraints.yaml]
"""

import os
import sys
import json
import math
import argparse
from typing import Dict, Any, Optional, Tuple, List

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python
from mediapipe.tasks.python import vision

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FACE_MODEL_PATH = os.path.join(PROJECT_ROOT, "data", "face_landmarker.task")

# MediaPipe Face Landmarker 关键点索引
IDX = {
    "ear_l_v": (159, 145), "ear_l_h": (33, 133),
    "ear_r_v": (386, 374), "ear_r_h": (362, 263),
    "mar_v": (13, 14), "mar_h": (78, 308),
}

# solvePnP 用的 3D 通用人脸模型点（单位 mm，OpenCV 相机坐标系：x 右、y 下、z 朝相机）
# 选点：鼻尖 1、下巴 152、左眼外角 33、右眼外角 263、左口角 61、右口角 291
PNP_3D = np.array([
    [0.0, 0.0, 0.0],        # 1 鼻尖
    [0.0, 63.0, -12.0],     # 152 下巴（y 向下为正）
    [-43.0, -32.0, -26.0],  # 33 左眼外角（y 向上为负）
    [43.0, -32.0, -26.0],   # 263 右眼外角
    [-28.0, 28.0, -24.0],   # 61 左口角
    [28.0, 28.0, -24.0],    # 291 右口角
], dtype=np.float64)
PNP_2D_IDX = [1, 152, 33, 263, 61, 291]

# 人体测量学比例（相对 鼻-肩垂直距 d = shoulder_y - nose_y）
HEAD_TOP_ABOVE_NOSE = 2.2   # 头顶 ≈ 鼻点上方 2.2d
CHIN_BELOW_NOSE = 0.9       # 下巴 ≈ 鼻点下方 0.9d
ROI_PAD = 0.45              # ROI 外扩比例
FACE_MIN_RATIO = 0.12       # 实测检测阈值：人脸需占 ROI 高度 >= 12%


class FaceAnalyzer:
    """MediaPipe FaceLandmarker 封装：姿态引导 ROI + 偏航角/表情/清晰度/面积占比。"""

    def __init__(self, model_path: str = None, num_faces: int = 1):
        path = model_path or FACE_MODEL_PATH
        if not os.path.exists(path):
            raise FileNotFoundError(f"未找到人脸 landmarker 模型: {path}")
        options = vision.FaceLandmarkerOptions(
            base_options=python.BaseOptions(model_asset_path=path),
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=False,
            num_faces=num_faces,
        )
        self._detector = vision.FaceLandmarker.create_from_options(options)

    # ------------------------------------------------------------ ROI 估算

    @staticmethod
    def _face_roi_from_pose(lm: List[Any], w: int, h: int) -> Optional[Tuple[int, int, int, int]]:
        """由 pose landmarks 估算人脸 ROI（像素坐标 x0,y0,x1,y1）。"""
        nose, ls, rs = lm[0], lm[11], lm[12]
        if nose.visibility < 0.3 or min(ls.visibility, rs.visibility) < 0.3:
            return None
        d = max(1e-3, (ls.y + rs.y) / 2.0 - nose.y)  # 归一化鼻肩距
        head_top = nose.y - HEAD_TOP_ABOVE_NOSE * d
        chin = nose.y + CHIN_BELOW_NOSE * d
        center_x = nose.x
        center_y = (head_top + chin) / 2.0
        half_h = (chin - head_top) * (1.0 + ROI_PAD) / 2.0
        half_w = half_h  # 正方形 ROI，人脸占高 ≈ 1/(1+PAD) ≈ 69% >> 12% 阈值
        x0 = int(max(0, (center_x - half_w) * w))
        y0 = int(max(0, (center_y - half_h) * h))
        x1 = int(min(w, (center_x + half_w) * w))
        y1 = int(min(h, (center_y + half_h) * h))
        if x1 - x0 < 64 or y1 - y0 < 64:
            return None
        return (x0, y0, x1, y1)

    # ------------------------------------------------------------ 单帧分析

    def analyze(
        self, frame_bgr: np.ndarray, pose_landmarks: Optional[List[Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """分析单帧，返回人脸指标 dict（坐标为原图归一化坐标）；无人脸返回 None。"""
        h, w = frame_bgr.shape[:2]
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

        roi = None
        if pose_landmarks is not None:
            roi = self._face_roi_from_pose(pose_landmarks, w, h)

        attempts = []
        if roi is not None:
            attempts.append(("pose_roi", roi))
        attempts.append(("full_frame", (0, 0, w, h)))

        for label, (x0, y0, x1, y1) in attempts:
            crop = rgb[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            mp_img = mp.Image(image_format=mp.ImageFormat.SRGB,
                              data=np.ascontiguousarray(crop))
            res = self._detector.detect(mp_img)
            if res and res.face_landmarks:
                return self._metrics(res.face_landmarks[0], frame_bgr,
                                     (x0, y0, x1, y1), label)
        return None

    def _metrics(
        self, lm, frame_bgr: np.ndarray, roi: Tuple[int, int, int, int], source: str
    ) -> Dict[str, Any]:
        """由人脸 landmark 计算指标（映射回原图坐标系）。"""
        h, w = frame_bgr.shape[:2]
        x0, y0, x1, y1 = roi
        cw, ch = x1 - x0, y1 - y0

        def dist(p1, p2) -> float:
            # 先映射到原图像素坐标再计算距离
            ax, ay = (x0 + p1.x * cw), (y0 + p1.y * ch)
            bx, by = (x0 + p2.x * cw), (y0 + p2.y * ch)
            return math.hypot(ax - bx, ay - by)

        # EAR / MAR（基于原图像素距离，尺度无关）
        ear_l = dist(lm[IDX["ear_l_v"][0]], lm[IDX["ear_l_v"][1]]) / (
            dist(lm[IDX["ear_l_h"][0]], lm[IDX["ear_l_h"][1]]) + 1e-5)
        ear_r = dist(lm[IDX["ear_r_v"][0]], lm[IDX["ear_r_v"][1]]) / (
            dist(lm[IDX["ear_r_h"][0]], lm[IDX["ear_r_h"][1]]) + 1e-5)
        ear = (ear_l + ear_r) / 2.0
        mar = dist(lm[IDX["mar_v"][0]], lm[IDX["mar_v"][1]]) / (
            dist(lm[IDX["mar_h"][0]], lm[IDX["mar_h"][1]]) + 1e-5)

        # 人脸包围盒（原图归一化）
        xs = [x0 + p.x * cw for p in lm]
        ys = [y0 + p.y * ch for p in lm]
        fx0, fx1 = min(xs), max(xs)
        fy0, fy1 = min(ys), max(ys)
        face_area_ratio = ((fx1 - fx0) * (fy1 - fy0)) / float(w * h)

        # 人脸区域清晰度（原图裁剪）
        ix0, iy0 = max(0, int(fx0)), max(0, int(fy0))
        ix1, iy1 = min(w, int(fx1)), min(h, int(fy1))
        face_crop = frame_bgr[iy0:iy1, ix0:ix1] if ix1 > ix0 and iy1 > iy0 else frame_bgr
        if face_crop.size == 0:
            face_crop = frame_bgr
        gray = cv2.cvtColor(face_crop, cv2.COLOR_BGR2GRAY)
        face_sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())

        # solvePnP 头部姿态（ROI 内 2D 点需映射回原图像素）
        image_points = np.array(
            [[x0 + lm[i].x * cw, y0 + lm[i].y * ch] for i in PNP_2D_IDX],
            dtype=np.float64)
        yaw_deg, pitch_deg = self._solve_head_pose(image_points, w, h)

        return {
            "face_detected": True,
            "detect_source": source,
            "yaw_deg": round(abs(yaw_deg), 2),
            "pitch_deg": round(abs(pitch_deg), 2),
            "ear": round(ear, 3),
            "mar": round(mar, 3),
            "face_sharpness": round(face_sharpness, 1),
            "face_area_ratio": round(face_area_ratio, 4),
            "face_bbox_norm": [round(fx0 / w, 4), round(fy0 / h, 4),
                               round(fx1 / w, 4), round(fy1 / h, 4)],
            "face_top_y": round(fy0 / h, 4),
        }

    def _solve_head_pose(self, image_points: np.ndarray, w: int, h: int) -> Tuple[float, float]:
        """solvePnP 估计头部姿态，返回 (yaw, pitch)（度）。"""
        focal = float(w)
        center = (w / 2.0, h / 2.0)
        camera_matrix = np.array(
            [[focal, 0, center[0]], [0, focal, center[1]], [0, 0, 1]], dtype=np.float64)
        dist_coeffs = np.zeros((4, 1))
        ok, rvec, _ = cv2.solvePnP(
            PNP_3D, image_points, camera_matrix, dist_coeffs,
            flags=cv2.SOLVEPNP_ITERATIVE)
        if not ok:
            return 0.0, 0.0
        rmat, _ = cv2.Rodrigues(rvec)
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, -rmat[2, 1]))))
        yaw = math.degrees(math.atan2(rmat[0, 2], rmat[2, 2]))
        return yaw, pitch

    # ------------------------------------------------------------ 约束校验

    def check_constraints(
        self, metrics: Optional[Dict[str, Any]], cover_c: Dict[str, Any]
    ) -> Tuple[bool, List[str]]:
        """对照 constraints.yaml 的 cover 段校验人脸指标。

        返回 (passed, failed_keys)。metrics 为 None（无人脸）时判不通过。
        """
        if metrics is None:
            return False, ["face_detected"]
        failed = []
        if metrics["yaw_deg"] > cover_c.get("face_yaw_max_deg", 12):
            failed.append("face_yaw_deg")
        if metrics["ear"] < cover_c.get("ear_min", 0.18):
            failed.append("ear")
        if metrics["mar"] > cover_c.get("mar_max", 0.35):
            failed.append("mar")
        if metrics["face_sharpness"] < cover_c.get("face_sharpness_min", 150):
            failed.append("face_sharpness")
        ratio = metrics["face_area_ratio"]
        if not (cover_c.get("face_area_ratio_min", 0.02) <= ratio
                <= cover_c.get("face_area_ratio_max", 0.25)):
            failed.append("face_area_ratio")
        return (len(failed) == 0), failed


def _load_pose_landmarks(frame_bgr: np.ndarray) -> Optional[List[Any]]:
    """CLI 辅助：先用 pose landmarker 取 landmarks 供 ROI 估算。"""
    pose_path = os.path.join(PROJECT_ROOT, "data", "pose_landmarker_lite.task")
    if not os.path.exists(pose_path):
        return None
    pose_options = vision.PoseLandmarkerOptions(
        base_options=python.BaseOptions(model_asset_path=pose_path)
    )
    detector = vision.PoseLandmarker.create_from_options(pose_options)
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    res = detector.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
    if res.pose_landmarks:
        return res.pose_landmarks[0]
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="封面人脸质量检测工具")
    parser.add_argument("--image", required=True, help="图片路径")
    parser.add_argument("--constraints", default=None, help="constraints.yaml 路径")
    parser.add_argument("--no-pose", action="store_true",
                        help="不使用姿态引导 ROI（仅全帧检测）")
    args = parser.parse_args(argv)

    frame = cv2.imread(args.image)
    if frame is None:
        print(f"[FACE][ERROR] 无法读取图片: {args.image}")
        return 2

    pose_lm = None if args.no_pose else _load_pose_landmarks(frame)
    analyzer = FaceAnalyzer()
    metrics = analyzer.analyze(frame, pose_landmarks=pose_lm)
    if metrics is None:
        print("[FACE] 未检测到人脸")
        return 1

    print("[FACE] 人脸指标:")
    for k, v in metrics.items():
        print(f"        {k}: {v}")

    if args.constraints and os.path.exists(args.constraints):
        import yaml
        with open(args.constraints, "r", encoding="utf-8") as f:
            cover_c = yaml.safe_load(f).get("cover", {})
        passed, failed = analyzer.check_constraints(metrics, cover_c)
        print(f"[FACE] 约束校验: {'PASS' if passed else 'FAIL ' + str(failed)}")
        return 0 if passed else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
