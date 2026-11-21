# -*- coding: utf-8 -*-
"""
vision_backend.py - 视觉证据分析后端抽象与轻量本地实现（V3 PR-04 核心）
========================================================================
负责：
  1. 提供 VisionBackend 统一接口，供 SKU Tracker 与 Agent 调度；
  2. 当无外部大模型或无商品图时，明确标明 vision_available=False 与 UNVERIFIABLE，
     绝不伪造视觉置信度；
  3. 提供基于 OpenCV 的本地直方图比对、动作分析（展示/拉伸/细节/换款）
     与结构化视觉决策（符合 visual_evidence_decisions.schema.json）。
"""

import os
import sys
from typing import Dict, Any, List, Optional, Tuple

import cv2
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class VisionBackend:
    """视觉后端基类。"""

    def is_available(self) -> bool:
        raise NotImplementedError

    def analyze_candidate(
        self,
        candidate: Dict[str, Any],
        frame_paths: List[str],
        product_profile: Dict[str, Any],
    ) -> Dict[str, Any]:
        raise NotImplementedError


class LocalCVVisionBackend(VisionBackend):
    """基于 OpenCV 的本地视觉证据后端。"""

    def __init__(self, color_threshold: float = 0.45):
        self.color_threshold = color_threshold

    def is_available(self) -> bool:
        return True

    def _calc_hist(self, img_path: str) -> Optional[np.ndarray]:
        if not os.path.isfile(img_path):
            return None
        from src.core.path_resolver import cv2_imread
        img = cv2_imread(img_path)
        if img is None:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [30, 32], [0, 180, 0, 256])
        cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
        return hist

    def analyze_candidate(
        self,
        candidate: Dict[str, Any],
        frame_paths: List[str],
        product_profile: Dict[str, Any],
    ) -> Dict[str, Any]:
        cid = candidate.get("candidate_id", "c_unknown")
        ref_images = product_profile.get("reference_image_ids", [])
        
        # 1. 若无商品参考图，明确标明不可验证
        if not ref_images or not frame_paths:
            return {
                "candidate_id": cid,
                "vision_available": False,
                "sku_relation": "UNCERTAIN",
                "action_type": "OTHER",
                "motion_phase": "UNKNOWN",
                "visual_evidence_refs": [],
                "confidence": 0.0,
                "uncertainties": ["UNVERIFIABLE: 缺失有效商品参考图或候选视频帧"],
                "why": "未提供商品参考图，无法在视觉通道确认同款",
            }

        # 检查参考图是否能在本地读取
        ref_hists = []
        for r in ref_images:
            full_r = r if os.path.isabs(r) else os.path.join(PROJECT_ROOT, r)
            h = self._calc_hist(full_r)
            if h is not None:
                ref_hists.append((r, h))

        if not ref_hists:
            return {
                "candidate_id": cid,
                "vision_available": False,
                "sku_relation": "UNCERTAIN",
                "action_type": "OTHER",
                "motion_phase": "UNKNOWN",
                "visual_evidence_refs": [],
                "confidence": 0.0,
                "uncertainties": ["UNVERIFIABLE: 参考图文件无法加载或已损坏"],
                "why": "参考图无法被图像引擎解析",
            }

        # 计算候选帧与参考图的最大相似度
        best_sim = 0.0
        best_frame = ""
        best_ref = ""
        for f_path in frame_paths:
            f_hist = self._calc_hist(f_path)
            if f_hist is None:
                continue
            for r_id, r_hist in ref_hists:
                sim = float(cv2.compareHist(f_hist, r_hist, cv2.HISTCMP_CORREL))
                if sim > best_sim:
                    best_sim = sim
                    best_frame = f_path
                    best_ref = r_id

        # 判定关系
        uncertainties = []
        if best_sim >= self.color_threshold:
            relation = "SAME_SKU"
            why = f"视频帧与商品参考图色彩/直方图高度吻合 (corr={best_sim:.2f})"
            conf = min(0.95, round(best_sim, 2))
        elif best_sim >= 0.25:
            relation = "SAME_SKU_VARIANT"
            why = f"直方图部分吻合，疑似同款不同色或变体 (corr={best_sim:.2f})"
            conf = min(0.8, round(best_sim, 2))
        else:
            relation = "UNCERTAIN"
            uncertainties.append(f"视觉相似度偏低 ({best_sim:.2f} < {self.color_threshold})")
            why = "视觉特征不足以确认同款"
            conf = max(0.1, round(best_sim, 2))

        # 动作类型启发式分析
        action_type = "FRONT_SHOW"
        motion_phase = "CORE"
        if candidate.get("duration_ms", 0) > 8000:
            action_type = "WALK"
        elif any(w in str(candidate.get("text", "")) for w in ("袖口", "细节", "走线", "看这里", "特写")):
            action_type = "DETAIL_POINT"
        elif any(w in str(candidate.get("text", "")) for w in ("弹力", "拉扯", "手感", "面料")):
            action_type = "FABRIC_STRETCH"

        return {
            "candidate_id": cid,
            "vision_available": True,
            "sku_relation": relation,
            "action_type": action_type,
            "motion_phase": motion_phase,
            "visual_evidence_refs": [best_frame] if best_frame else [],
            "confidence": conf,
            "uncertainties": uncertainties,
            "why": why,
        }
