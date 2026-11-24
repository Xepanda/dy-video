# -*- coding: utf-8 -*-
"""
onnx_encoder.py - 本地轻量 ONNX 视觉编码器（MobileCLIP / SigLIP / CLIP ONNX 通用接口）
===================================================================================
- 零外部云端 API 费用；
- 自动优先 CUDAExecutionProvider，缺失 GPU 时平滑回退 CPUExecutionProvider；
- 本地未放置权重时 is_ready() 返回 False，不阻塞离线运行与 CI 单测。
"""

import os
from typing import Optional, List
import cv2
import numpy as np

DEFAULT_MODEL_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "dy-video", "models", "garment_encoder.onnx")


class OnnxGarmentEncoder:
    """通用 ONNX 图像特征向量编码器（输入 RGB 归一化 224×224，输出 L2 归一化向量）。"""

    def __init__(self, model_path: Optional[str] = None, input_size: int = 224):
        self.model_path = (
            model_path
            or os.environ.get("DY_VIDEO_VISION_ONNX", "").strip()
            or DEFAULT_MODEL_CACHE
        )
        self.input_size = int(input_size)
        self._session = None
        self._input_name: Optional[str] = None
        self._provider: str = "none"
        self._init_tried: bool = False

    def _ensure_session(self) -> bool:
        if self._init_tried:
            return self._session is not None
        self._init_tried = True
        if not self.model_path or not os.path.isfile(self.model_path):
            return False
        try:
            import onnxruntime as ort
            avail = ort.get_available_providers()
            providers: List[str] = []
            if "CUDAExecutionProvider" in avail:
                providers.append("CUDAExecutionProvider")
            providers.append("CPUExecutionProvider")
            self._session = ort.InferenceSession(self.model_path, providers=providers)
            self._input_name = self._session.get_inputs()[0].name
            active = self._session.get_providers()
            self._provider = active[0] if active else "CPUExecutionProvider"
            return True
        except Exception:
            self._session = None
            return False

    def is_ready(self) -> bool:
        return self._ensure_session()

    @property
    def provider(self) -> str:
        self._ensure_session()
        return self._provider

    def _preprocess(self, image_bgr: np.ndarray) -> np.ndarray:
        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, (self.input_size, self.input_size), interpolation=cv2.INTER_AREA)
        arr = resized.astype(np.float32) / 255.0
        mean = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
        std = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
        arr = (arr - mean) / std
        arr = np.transpose(arr, (2, 0, 1))[None, ...]
        return arr

    def encode(self, image_bgr: np.ndarray) -> Optional[np.ndarray]:
        if not self._ensure_session() or self._session is None or self._input_name is None:
            return None
        try:
            inp = self._preprocess(image_bgr)
            outputs = self._session.run(None, {self._input_name: inp})
            vec = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
            norm = float(np.linalg.norm(vec))
            if norm <= 1e-8:
                return None
            return vec / norm
        except Exception:
            return None

    @staticmethod
    def cosine_similarity(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
        sim = float(np.dot(vec_a, vec_b))
        return max(0.0, min(1.0, (sim + 1.0) / 2.0 if sim < 0.0 else sim))
