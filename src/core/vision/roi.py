# -*- coding: utf-8 -*-
"""
roi.py - 躯干衣着 ROI 提取、商品参考图去白底与真实画面景别特征提取（V3.3）
===========================================================================
复用 detect_garment_match.py 的 Pose 躯干 ROI 与白底剔除原语，并支持女装子品类区域策略：
  - upper_torso: 肩 -> 髋（针织衫/毛衣/上衣）
  - upper_torso_extended: 肩 -> 大腿上段（外套/大衣/羽绒服）
  - full_body_dress: 肩 -> 膝（连衣裙）
  - lower_body: 髋 -> 踝（裤装）
"""

import os
from typing import Dict, Any, Optional, Tuple
import cv2
import numpy as np

from src.tools.detect_garment_match import _mask_white_background, torso_roi_from_pose

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_POSE_DETECTOR_CACHE: Dict[str, Any] = {"tried": False, "detector": None, "mp": None}


def _detect_pose_landmarks_cached(frame_bgr: np.ndarray) -> Optional[Any]:
    """进程级单例 MediaPipe PoseLandmarker，避免每帧重复加载 TFLite 模型导致 50x 开销。"""
    if not _POSE_DETECTOR_CACHE["tried"]:
        _POSE_DETECTOR_CACHE["tried"] = True
        try:
            pose_path = os.path.join(PROJECT_ROOT, "data", "pose_landmarker_lite.task")
            if os.path.isfile(pose_path):
                import mediapipe as mp
                from mediapipe.tasks import python as mp_python
                from mediapipe.tasks.python import vision as mp_vision
                opts = mp_vision.PoseLandmarkerOptions(
                    base_options=mp_python.BaseOptions(model_asset_path=pose_path)
                )
                _POSE_DETECTOR_CACHE["detector"] = mp_vision.PoseLandmarker.create_from_options(opts)
                _POSE_DETECTOR_CACHE["mp"] = mp
        except Exception:
            _POSE_DETECTOR_CACHE["detector"] = None
    det = _POSE_DETECTOR_CACHE["detector"]
    mp = _POSE_DETECTOR_CACHE["mp"]
    if det is None or mp is None:
        return None
    try:
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        res = det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
        if res and res.pose_landmarks:
            return res.pose_landmarks[0]
    except Exception:
        pass
    return None


def _region_roi_from_pose(
    lm: Any,
    w: int,
    h: int,
    roi_region: str = "upper_torso",
) -> Optional[Tuple[int, int, int, int]]:
    """根据子品类区域策略从 Pose landmarks 裁切衣着 ROI。"""
    if roi_region == "upper_torso":
        return torso_roi_from_pose(lm, w, h)
    try:
        ls, rs, lh, rh = lm[11], lm[12], lm[23], lm[24]
        lk, rk = lm[25], lm[26]
        la, ra = lm[27], lm[28]
        if min(ls.visibility, rs.visibility, lh.visibility, rh.visibility) < 0.3:
            return None
        shoulder_y = (ls.y + rs.y) / 2.0
        hip_y = (lh.y + rh.y) / 2.0
        knee_y = (lk.y + rk.y) / 2.0
        ankle_y = (la.y + ra.y) / 2.0
        shoulder_w = abs(ls.x - rs.x)
        if shoulder_w < 0.02:
            return None
        cx = (ls.x + rs.x) / 2.0
        half_w = shoulder_w * 0.42
        if roi_region == "upper_torso_extended":
            y0 = max(0.0, shoulder_y - shoulder_w * 0.14)
            y1 = min(1.0, hip_y + (knee_y - hip_y) * 0.45 if knee_y > hip_y else hip_y + shoulder_w * 0.35)
        elif roi_region == "full_body_dress":
            y0 = max(0.0, shoulder_y - shoulder_w * 0.10)
            y1 = min(1.0, knee_y if knee_y > hip_y else hip_y + shoulder_w * 0.65)
        elif roi_region == "lower_body":
            y0 = max(0.0, hip_y - shoulder_w * 0.08)
            y1 = min(1.0, ankle_y if ankle_y > hip_y else min(1.0, hip_y + shoulder_w * 1.1))
            half_w = shoulder_w * 0.48
        else:
            return torso_roi_from_pose(lm, w, h)
        box = (
            max(0, int((cx - half_w) * w)),
            max(0, int(y0 * h)),
            min(w, int((cx + half_w) * w)),
            min(h, int(y1 * h)),
        )
        if box[2] - box[0] < 32 or box[3] - box[1] < 32:
            return None
        return box
    except Exception:
        return torso_roi_from_pose(lm, w, h)


def extract_garment_roi(
    frame_bgr: np.ndarray,
    roi_region: str = "upper_torso",
    enable_pose: bool = True,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """提取视频帧中的衣着前景 ROI 与真实景别/清晰度特征。

    返回: (roi_bgr, meta_dict)
    """
    h, w = frame_bgr.shape[:2]
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    meta: Dict[str, Any] = {
        "roi_mode": "center_crop",
        "roi_region": roi_region,
        "shot_scale": "UNKNOWN",
        "body_span": 0.0,
        "pose_ok": False,
        "sharpness": round(sharpness, 1),
    }

    if enable_pose and h >= 64 and w >= 64:
        try:
            lm = _detect_pose_landmarks_cached(frame_bgr)
            if lm is not None:
                meta["pose_ok"] = True
                head_top = min(float(lm[0].y), float(lm[1].y))
                ank_y = (float(lm[27].y) + float(lm[28].y)) / 2.0
                span = abs(ank_y - head_top)
                meta["body_span"] = round(span, 3)
                ank_vis = max(float(getattr(lm[27], "visibility", 0.0)), float(getattr(lm[28], "visibility", 0.0)))
                if span >= 0.45 and ank_y <= 1.02 and ank_vis >= 0.35:
                    meta["shot_scale"] = "FULL_BODY"
                elif span >= 0.25:
                    meta["shot_scale"] = "HALF_BODY"
                else:
                    meta["shot_scale"] = "CLOSEUP"

                box = _region_roi_from_pose(lm, w, h, roi_region=roi_region)
                if box is not None:
                    x0, y0, x1, y1 = box
                    crop = frame_bgr[y0:y1, x0:x1]
                    if crop.size > 0:
                        meta["roi_mode"] = "pose_torso"
                        meta["roi_box"] = list(box)
                        return crop, meta
        except Exception:
            pass

    # 降级：安全中心躯干启发裁切（排除边缘货架、顶部标题栏与底部评论区）
    if h >= 64 and w >= 64:
        x0, x1 = int(w * 0.22), int(w * 0.78)
        if roi_region == "lower_body":
            y0, y1 = int(h * 0.42), int(h * 0.88)
        elif roi_region == "full_body_dress":
            y0, y1 = int(h * 0.20), int(h * 0.82)
        else:
            y0, y1 = int(h * 0.22), int(h * 0.68)
        crop = frame_bgr[y0:y1, x0:x1]
        if crop.size > 0:
            meta["roi_mode"] = "center_crop"
            meta["roi_box"] = [x0, y0, x1, y1]
            return crop, meta

    meta["roi_mode"] = "full_frame"
    return frame_bgr, meta


def crop_product_foreground(product_bgr: np.ndarray) -> Tuple[np.ndarray, Optional[np.ndarray]]:
    """剔除商品平铺图的纯白/近灰背景，返回前景区域与前景掩码。"""
    mask = _mask_white_background(product_bgr)
    if mask is None or int(mask.sum()) == 0:
        return product_bgr, None
    ys, xs = np.where(mask > 0)
    if len(xs) < 16 or len(ys) < 16:
        return product_bgr, None
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    fg_crop = product_bgr[y0:y1, x0:x1]
    fg_mask = mask[y0:y1, x0:x1]
    return fg_crop, fg_mask
