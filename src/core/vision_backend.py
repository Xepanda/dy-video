# -*- coding: utf-8 -*-
"""
vision_backend.py - 视觉证据分析后端兼容门面（V3.3 已升级至 src/core/vision/）
============================================================================
保持 VisionBackend 与 LocalCVVisionBackend 公开接口 100% 向后兼容，
底层升级为 Pose 躯干 ROI + 去白底参考图 + 轻量 ONNX 视觉编码器（自动降级 ROI HSV）
与 3 点帧段内漂移检测。
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.vision.hybrid_backend import VisionBackend, HybridGarmentVisionBackend
from src.core.vision.onnx_encoder import OnnxGarmentEncoder  # noqa: F401


class LocalCVVisionBackend(HybridGarmentVisionBackend):
    """向后兼容别名：默认启用 Pose 躯干 ROI + 轻量 ONNX 编码器（无权重时优雅降级为 ROI HSV）。"""

    def __init__(self, color_threshold: float = 0.45, onnx_encoder=None, enable_pose: bool = True):
        super().__init__(
            color_threshold=color_threshold,
            onnx_encoder=onnx_encoder,
            enable_pose=enable_pose,
        )
