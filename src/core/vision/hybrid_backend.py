# -*- coding: utf-8 -*-
"""
hybrid_backend.py - V3.3 混合本地视觉证据引擎（Pose 躯干 ROI + ONNX 向量 / HSV 降级 + 3 点漂移检测）
"""

import os
from typing import Dict, Any, List, Optional, Tuple
import cv2
import numpy as np

from src.core.path_resolver import cv2_imread
from src.core.category_profile import load_category_profile, resolve_subcategory
from src.core.vision.roi import extract_garment_roi, crop_product_foreground
from src.core.vision.onnx_encoder import OnnxGarmentEncoder

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _load_vision_v33_thresholds() -> Dict[str, float]:
    v: Dict[str, Any] = {}
    try:
        import yaml
        cp = os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
        if os.path.isfile(cp):
            with open(cp, "r", encoding="utf-8") as f:
                c = yaml.safe_load(f) or {}
            v = c.get("vision_v33") or {}
    except Exception:
        v = {}
    return {
        "onnx_high_threshold": float(v.get("onnx_high_threshold", 0.76)),
        "onnx_low_threshold": float(v.get("onnx_low_threshold", 0.45)),
        "hsv_high_threshold": float(v.get("hsv_high_threshold", 0.45)),
        "hsv_low_threshold": float(v.get("hsv_low_threshold", 0.22)),
        "drift_threshold": float(v.get("drift_threshold", 0.30)),
    }


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


class HybridGarmentVisionBackend(VisionBackend):
    """V3.3 混合本地视觉证据后端：
    1. Stage 1: Pose 躯干 ROI 裁切 + 商品参考图去白底；
    2. Stage 2: 优先 ONNX 视觉编码器（CPU/CUDA），无权重时显式降级为前景 ROI HSV 直方图；
    3. Stage 3: 3 点帧聚合 + 段内漂移风险 (MID_CLIP_SKU_DRIFT) + 真实景别/清晰度特征回填。
    """

    def __init__(
        self,
        color_threshold: Optional[float] = None,
        onnx_encoder: Optional[OnnxGarmentEncoder] = None,
        enable_pose: bool = True,
    ):
        self.thresholds = _load_vision_v33_thresholds()
        self.color_threshold = float(color_threshold) if color_threshold is not None else self.thresholds["hsv_high_threshold"]
        self.hsv_low_threshold = self.thresholds["hsv_low_threshold"]
        self.onnx_high_threshold = self.thresholds["onnx_high_threshold"]
        self.onnx_low_threshold = self.thresholds["onnx_low_threshold"]
        self.drift_threshold = self.thresholds["drift_threshold"]
        self.encoder = onnx_encoder or OnnxGarmentEncoder()
        self.enable_pose = enable_pose
        self._ref_cache: Dict[str, Dict[str, Any]] = {}
        self._frame_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def is_available(self) -> bool:
        return True

    @staticmethod
    def _calc_hsv_hist(img_bgr: np.ndarray, mask: Optional[np.ndarray] = None) -> np.ndarray:
        hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], mask, [30, 32], [0, 180, 0, 256])
        cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
        return hist

    def _get_ref_features(self, ref_path: str, use_onnx: bool) -> Optional[Dict[str, Any]]:
        full_r = ref_path if os.path.isabs(ref_path) else os.path.join(PROJECT_ROOT, ref_path)
        cache_key = f"{full_r}|onnx={use_onnx}"
        if cache_key in self._ref_cache:
            return self._ref_cache[cache_key]
        if not os.path.isfile(full_r):
            return None
        img = cv2_imread(full_r)
        if img is None:
            return None
        fg_crop, fg_mask = crop_product_foreground(img)
        feat: Dict[str, Any] = {
            "ref_id": ref_path,
            "hist": self._calc_hsv_hist(fg_crop, fg_mask),
            "vec": self.encoder.encode(fg_crop) if use_onnx else None,
        }
        self._ref_cache[cache_key] = feat
        return feat

    def _get_frame_features(self, frame_path: str, roi_region: str, use_onnx: bool) -> Optional[Dict[str, Any]]:
        ck = (frame_path, f"{roi_region}|onnx={use_onnx}")
        if ck in self._frame_cache:
            return self._frame_cache[ck]
        if not os.path.isfile(frame_path):
            return None
        img = cv2_imread(frame_path)
        if img is None:
            return None
        roi_bgr, meta = extract_garment_roi(img, roi_region=roi_region, enable_pose=self.enable_pose)
        feat: Dict[str, Any] = {
            "frame_path": frame_path,
            "hist": self._calc_hsv_hist(roi_bgr),
            "vec": self.encoder.encode(roi_bgr) if use_onnx else None,
            "meta": meta,
        }
        self._frame_cache[ck] = feat
        return feat

    def analyze_candidate(
        self,
        candidate: Dict[str, Any],
        frame_paths: List[str],
        product_profile: Dict[str, Any],
    ) -> Dict[str, Any]:
        cid = candidate.get("candidate_id", "c_unknown")
        ref_images = product_profile.get("reference_image_ids") or product_profile.get("item_images") or []

        if not ref_images or not frame_paths:
            return {
                "candidate_id": cid,
                "vision_available": False,
                "vision_engine": "none",
                "vision_tier": "unavailable",
                "visual_gray_zone": False,
                "roi_mode": "none",
                "sku_relation": "UNCERTAIN",
                "action_type": "OTHER",
                "motion_phase": "UNKNOWN",
                "shot_scale": "UNKNOWN",
                "visual_evidence_refs": [],
                "confidence": 0.0,
                "uncertainties": ["UNVERIFIABLE: 缺失有效商品参考图或候选视频帧"],
                "why": "未提供商品参考图，无法在视觉通道确认同款",
            }

        cat_profile = load_category_profile(product_profile)
        sub_info = resolve_subcategory(
            f"{product_profile.get('category', '')} {product_profile.get('name', '')}",
            profile=cat_profile,
        )
        roi_region = sub_info["roi_region"]
        use_onnx = self.encoder.is_ready()

        ref_feats = []
        for r in ref_images:
            rf = self._get_ref_features(str(r), use_onnx=use_onnx)
            if rf is not None:
                ref_feats.append(rf)

        if not ref_feats:
            return {
                "candidate_id": cid,
                "vision_available": False,
                "vision_engine": "none",
                "vision_tier": "unavailable",
                "visual_gray_zone": False,
                "roi_mode": "none",
                "sku_relation": "UNCERTAIN",
                "action_type": "OTHER",
                "motion_phase": "UNKNOWN",
                "shot_scale": "UNKNOWN",
                "visual_evidence_refs": [],
                "confidence": 0.0,
                "uncertainties": ["UNVERIFIABLE: 参考图文件无法加载或已损坏"],
                "why": "参考图无法被图像引擎解析",
            }

        frame_sims: List[float] = []
        best_sim = -1.0
        best_frame = ""
        roi_modes: List[str] = []
        shot_scales: List[str] = []
        sharpness_vals: List[float] = []
        engine_used = "onnx_semantic" if use_onnx else "hsv_roi_fallback"

        for f_path in frame_paths:
            ff = self._get_frame_features(f_path, roi_region=roi_region, use_onnx=use_onnx)
            if ff is None:
                continue
            meta = ff["meta"]
            roi_modes.append(str(meta.get("roi_mode", "center_crop")))
            if meta.get("shot_scale") and meta["shot_scale"] != "UNKNOWN":
                shot_scales.append(str(meta["shot_scale"]))
            sharpness_vals.append(float(meta.get("sharpness", 0.0)))

            f_best = -1.0
            for rf in ref_feats:
                if use_onnx and ff["vec"] is not None and rf["vec"] is not None:
                    sim = OnnxGarmentEncoder.cosine_similarity(ff["vec"], rf["vec"])
                else:
                    engine_used = "hsv_roi_fallback"
                    corr = float(cv2.compareHist(ff["hist"], rf["hist"], cv2.HISTCMP_CORREL))
                    sim = max(0.0, min(1.0, corr))
                if sim > f_best:
                    f_best = sim
            if f_best >= 0.0:
                frame_sims.append(f_best)
                if f_best > best_sim:
                    best_sim = f_best
                    best_frame = f_path

        if not frame_sims:
            return {
                "candidate_id": cid,
                "vision_available": False,
                "vision_engine": engine_used,
                "vision_tier": "unavailable",
                "visual_gray_zone": False,
                "roi_mode": "none",
                "sku_relation": "UNCERTAIN",
                "action_type": "OTHER",
                "motion_phase": "UNKNOWN",
                "shot_scale": "UNKNOWN",
                "visual_evidence_refs": [],
                "confidence": 0.0,
                "uncertainties": ["UNVERIFIABLE: 候选视频帧无法解码"],
                "why": "候选帧无法被图像引擎解析",
            }

        sim_min = min(frame_sims)
        sim_max = max(frame_sims)
        sim_mean = float(sum(frame_sims) / len(frame_sims))
        drift = sim_max - sim_min
        has_drift = len(frame_sims) >= 2 and drift >= self.drift_threshold

        high_thr = self.onnx_high_threshold if engine_used == "onnx_semantic" else self.color_threshold
        low_thr = self.onnx_low_threshold if engine_used == "onnx_semantic" else self.hsv_low_threshold
        vision_tier = "semantic_onnx" if engine_used == "onnx_semantic" else "weak_fallback"

        uncertainties: List[str] = []
        visual_gray_zone = False
        if has_drift:
            uncertainties.append(f"MID_CLIP_SKU_DRIFT: 片段3点帧视觉相似度落差过大 ({sim_min:.2f}~{sim_max:.2f})")

        # 三区间判定
        if best_sim >= high_thr and not has_drift:
            relation = "SAME_SKU"
            why = f"躯干ROI与商品参考图吻合 ({engine_used}, max={best_sim:.2f}, mean={sim_mean:.2f})"
            conf = min(0.95, round(best_sim, 2))
        elif best_sim >= low_thr:
            if engine_used == "hsv_roi_fallback" and best_sim >= 0.25 and best_sim < high_thr:
                relation = "SAME_SKU_VARIANT" if not has_drift and best_sim >= max(0.32, low_thr + 0.08) else "UNCERTAIN"
            else:
                relation = "UNCERTAIN"
            visual_gray_zone = True
            uncertainties.append(
                f"GRAY_ZONE: 视觉相似度落在灰区 ({best_sim:.2f}, 区间 [{low_thr:.2f}, {high_thr:.2f}))，建议外部 Agent 审阅拼图确认"
            )
            why = f"视觉相似度位于灰区 ({engine_used}, max={best_sim:.2f})，待多模态导演复核"
            conf = min(0.78, max(0.30, round(best_sim, 2)))
        else:
            relation = "UNCERTAIN"
            visual_gray_zone = best_sim >= (low_thr * 0.6)
            uncertainties.append(f"视觉相似度偏低 ({best_sim:.2f} < {low_thr:.2f})")
            why = f"视觉特征不足以确认同款 ({engine_used}, max={best_sim:.2f})"
            conf = max(0.10, round(best_sim, 2))

        primary_roi = "pose_torso" if "pose_torso" in roi_modes else (roi_modes[0] if roi_modes else "center_crop")
        primary_scale = shot_scales[0] if shot_scales else "UNKNOWN"
        avg_sharpness = round(sum(sharpness_vals) / len(sharpness_vals), 1) if sharpness_vals else 0.0

        # 基于真实画面景别 + 文本语义推导动作类型
        c_text = str(candidate.get("text", ""))
        if primary_scale == "CLOSEUP" or any(w in c_text for w in ("袖口", "细节", "走线", "看这里", "特写")):
            action_type = "DETAIL_POINT"
        elif any(w in c_text for w in ("弹力", "拉扯", "手感", "面料", "软糯")):
            action_type = "FABRIC_STRETCH"
        elif primary_scale == "FULL_BODY":
            action_type = "FULL_BODY_SHOW"
        else:
            action_type = "FRONT_SHOW"

        motion_phase = "INCOMPLETE" if has_drift else "CORE"

        # 回填真实画面特征到候选，供 Director Brief 与 Ranker 消费
        candidate["shot_scale"] = primary_scale
        candidate["frame_sharpness"] = avg_sharpness
        candidate["roi_mode"] = primary_roi
        candidate["vision_engine"] = engine_used
        candidate["vision_tier"] = vision_tier
        candidate["vision_similarity"] = round(best_sim, 3)
        candidate["vision_sim_stats"] = {
            "min": round(sim_min, 3),
            "mean": round(sim_mean, 3),
            "max": round(sim_max, 3),
            "n_frames": len(frame_sims),
        }
        candidate["visual_gray_zone"] = visual_gray_zone
        candidate["motion_phase"] = motion_phase
        candidate["action_type"] = action_type
        if has_drift and "MID_CLIP_SKU_DRIFT" not in (candidate.get("risks") or []):
            candidate.setdefault("risks", []).append("MID_CLIP_SKU_DRIFT")

        return {
            "candidate_id": cid,
            "vision_available": True,
            "vision_engine": engine_used,
            "vision_tier": vision_tier,
            "visual_gray_zone": visual_gray_zone,
            "roi_mode": primary_roi,
            "shot_scale": primary_scale,
            "sku_relation": relation,
            "action_type": action_type,
            "motion_phase": motion_phase,
            "visual_evidence_refs": [best_frame] if best_frame else [],
            "confidence": conf,
            "uncertainties": uncertainties,
            "why": why,
        }
