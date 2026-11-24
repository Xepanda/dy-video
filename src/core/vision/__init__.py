# -*- coding: utf-8 -*-
"""src.core.vision - V3.3 混合本地视觉感知子包"""

from src.core.vision.hybrid_backend import VisionBackend, HybridGarmentVisionBackend
from src.core.vision.onnx_encoder import OnnxGarmentEncoder
from src.core.vision.roi import extract_garment_roi, crop_product_foreground

__all__ = [
    "VisionBackend",
    "HybridGarmentVisionBackend",
    "OnnxGarmentEncoder",
    "extract_garment_roi",
    "crop_product_foreground",
]
