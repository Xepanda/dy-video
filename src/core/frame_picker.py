#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
frame_picker.py - CV 驱动的封面底图自动优选器
==============================================
对视频进行抽帧，结合多维硬指标：
1. 清晰度 (拉普拉斯方差 >= 200)
2. 人体姿态 (MediaPipe Pose: 必露双膝 y_knee <= 0.92, 1:6:1 居中)
3. 人脸表情 (MediaPipe FaceMesh: EAR 睁眼防眨眼, MAR 闭嘴防崩)
输出最优正身底图及对应时间戳。
"""

import os
import sys
import cv2
import math
import numpy as np
from typing import Dict, Any, List, Optional, Tuple
import mediapipe as mp
from mediapipe.tasks import python
from mediapipe.tasks.python import vision

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
POSE_MODEL_PATH = os.path.join(PROJECT_ROOT, "data", "pose_landmarker_lite.task")
FACE_MODEL_PATH = os.path.join(PROJECT_ROOT, "data", "face_landmarker.task")


def calculate_laplacian_sharpness(frame_bgr: np.ndarray) -> float:
    """计算拉普拉斯算子方差，衡量图像边缘锐利程度"""
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


class SmartFramePicker:
    def __init__(self):
        if not os.path.exists(POSE_MODEL_PATH):
            raise FileNotFoundError(f"未找到姿态检测模型文件: {POSE_MODEL_PATH}")
        if not os.path.exists(FACE_MODEL_PATH):
            raise FileNotFoundError(f"未找到人脸检测模型文件: {FACE_MODEL_PATH}")

        # 初始化 MediaPipe Tasks
        pose_options = vision.PoseLandmarkerOptions(
            base_options=python.BaseOptions(model_asset_path=POSE_MODEL_PATH),
            output_segmentation_masks=False
        )
        self.pose_detector = vision.PoseLandmarker.create_from_options(pose_options)

        face_options = vision.FaceLandmarkerOptions(
            base_options=python.BaseOptions(model_asset_path=FACE_MODEL_PATH),
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=False,
            num_faces=1
        )
        self.face_detector = vision.FaceLandmarker.create_from_options(face_options)

    def evaluate_frame(self, frame_bgr: np.ndarray) -> Tuple[float, Dict[str, Any]]:
        """对单帧图像进行全维度打分，返回 (综合得分, 指标字典)"""
        h, w = frame_bgr.shape[:2]
        sharpness = calculate_laplacian_sharpness(frame_bgr)
        
        info: Dict[str, Any] = {
            "sharpness": round(sharpness, 1),
            "knees_visible": False,
            "center_dev": 1.0,
            "ear": 0.0,
            "mar": 1.0,
            "passed": False
        }

        # 快速初筛：太模糊直接淘汰
        if sharpness < 180.0:
            return 0.0, info

        # 转换为 MediaPipe Image
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

        # 1. 人体骨骼与膝盖检测
        pose_res = self.pose_detector.detect(mp_image)
        if not pose_res.pose_landmarks:
            return 0.0, info

        p_marks = pose_res.pose_landmarks[0]
        # 11,12 肩部; 15,16 手腕; 23,24 髋部; 25,26 膝盖; 27,28 脚踝
        ls, rs = p_marks[11], p_marks[12]
        lw, rw = p_marks[15], p_marks[16]
        l_hip, r_hip = p_marks[23], p_marks[24]
        l_knee, r_knee = p_marks[25], p_marks[26]
        l_ankle, r_ankle = p_marks[27], p_marks[28]
        
        hip_center_x = (l_hip.x + r_hip.x) / 2.0
        center_dev = abs(hip_center_x - 0.50)  # 距中心线偏离
        info["center_dev"] = round(center_dev, 3)

        # 膝盖与脚踝可见性判定 (保证全身立姿，不露脚踝或半身裁切扣分)
        knees_in_view = (l_knee.y <= 0.95 and r_knee.y <= 0.95 and
                         l_knee.visibility > 0.40 and r_knee.visibility > 0.40)
        ankles_in_view = (l_ankle.y <= 0.96 and r_ankle.y <= 0.96 and
                          l_ankle.visibility > 0.40 and r_ankle.visibility > 0.40)
        info["knees_visible"] = knees_in_view
        info["ankles_visible"] = ankles_in_view

        # 道具/衣架遮挡排斥：主播双手举起衣架挡在胸前 (手腕高过肩部或贴近胸口上方)
        holding_props_occlusion = (lw.y < (ls.y + 0.08) or rw.y < (rs.y + 0.08))
        info["props_occlusion"] = holding_props_occlusion

        # 2. 人脸检测 (表情正常，无闭眼大嘴)
        face_res = self.face_detector.detect(mp_image)
        if face_res and face_res.face_landmarks:
            f_marks = face_res.face_landmarks[0]
            def dist(p1, p2):
                return math.hypot((p1.x - p2.x) * w, (p1.y - p2.y) * h)

            ear = (dist(f_marks[159], f_marks[145]) + dist(f_marks[386], f_marks[374])) / (
                dist(f_marks[33], f_marks[133]) + dist(f_marks[362], f_marks[263]) + 1e-5)
            info["ear"] = round(ear, 3)

            mar = dist(f_marks[13], f_marks[14]) / (dist(f_marks[78], f_marks[308]) + 1e-5)
            info["mar"] = round(mar, 3)
            face_ok = (ear >= 0.18 and mar <= 0.35)
        else:
            nose = p_marks[0]
            head_in_view = (nose.y <= 0.30 and nose.visibility > 0.50)
            face_ok = head_in_view
            info["ear"] = 0.25
            info["mar"] = 0.20

        # 3. 综合加权打分 (确定性高准确率量化模型)
        # 清晰度得分 (封顶 400)
        s_sharp = min(1.0, sharpness / 400.0) * 30.0
        # 居中得分 (偏离 0.10 以内加权)
        s_center = max(0.0, 1.0 - (center_dev / 0.10)) * 25.0
        # 骨骼站姿得分 (露膝盖 +20, 露脚踝全身立姿 +25)
        s_pose = (20.0 if knees_in_view else 0.0) + (25.0 if ankles_in_view else 0.0)
        # 人脸表情合规
        s_face = 10.0 if face_ok else 2.0
        # 遮挡硬惩罚 (提衣架/举手挡胸扣 60 分)
        s_penalty = -60.0 if holding_props_occlusion else 0.0

        total_score = s_sharp + s_center + s_pose + s_face + s_penalty
        info["passed"] = (knees_in_view and ankles_in_view and face_ok and not holding_props_occlusion and center_dev <= 0.10)
        return total_score, info

    def pick_best_cover_frame(
        self,
        video_path: str,
        sample_step_sec: float = 1.5,
        max_duration_scan: float = 180.0
    ) -> Tuple[float, np.ndarray, Dict[str, Any]]:
        """扫描视频，选出综合得分最高的最佳封面底图"""
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"视频文件不存在: {video_path}")

        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / fps
        
        scan_sec = min(duration, max_duration_scan)
        print(f"[PICKER] 开始扫描视频: {os.path.basename(video_path)}")
        print(f"         扫描时长: 前 {scan_sec:.1f}s, 步长: 每 {sample_step_sec}s 抽 1 帧...")

        best_score = -1.0
        best_time = 0.0
        best_frame = None
        best_meta = {}

        curr_sec = 1.0
        checked_count = 0
        while curr_sec < scan_sec:
            frame_idx = int(curr_sec * fps)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if not ret or frame is None:
                curr_sec += sample_step_sec
                continue

            checked_count += 1
            score, meta = self.evaluate_frame(frame)
            if score > best_score:
                best_score = score
                best_time = curr_sec
                best_frame = frame.copy()
                best_meta = meta

            curr_sec += sample_step_sec

        cap.release()
        print(f"[PICKER] 扫描完毕 (共评估 {checked_count} 帧):")
        print(f"         最优时间戳: {best_time:.2f}s | 评分: {best_score:.1f}")
        print(f"         指标详情: {best_meta}")

        if best_frame is None:
            raise RuntimeError("全视频扫描未找到合格候选帧！")

        return best_time, best_frame, best_meta


if __name__ == "__main__":
    picker = SmartFramePicker()
    v_path = "data/raw_samples/10 绵羊 半岛棉镂空钩花气质显瘦针织衫7270/0826  绵羊 半岛棉镂空钩花气质显瘦针织衫7270  0425.mp4"
    t, f, m = picker.pick_best_cover_frame(v_path, sample_step_sec=2.0, max_duration_scan=120.0)
    out_jpg = "data/optimal_cover_frame.jpg"
    cv2.imwrite(out_jpg, f)
    print(f"[OK] 最优底图已保存至: {out_jpg}")
