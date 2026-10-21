#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
detect_garment_match.py - 衣着一致性检测（C7 落地）
====================================================
会议纪要要求"模特身着衣服和商品图片一致"。本工具给出可量化的匹配分数。

引擎（constraints.yaml garment_match.engine）：
  auto      优先 CLIP（open_clip 可用时），否则直方图
  clip      open_clip ViT-B-32 中文权重（需 pip install open_clip_torch，
            首次运行自动下载权重 ~600MB）
  histogram 零依赖降级方案：封面人像躯干 ROI vs 商品平铺图的 HSV 2D
            直方图相关性（推荐阈值 histogram_min=0.60）

躯干 ROI 由 pose landmarks 推导：双肩中点与双髋中点之间的躯干区域
（剔除手臂与背景），与商品平铺图的主体区域对应。

CLI:
  python src/tools/detect_garment_match.py --frame cover.jpg \
      --product product.png [--pose-json pose.json]
"""

import os
import sys
import json
import math
import argparse
from typing import Dict, Any, Optional, Tuple, List

import cv2
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# HSV 2D 直方图参数（H: 0~180 → 30 bins, S: 0~256 → 32 bins）
H_BINS, S_BINS = 30, 32
H_RANGES, S_RANGES = [0, 180], [0, 256]


# ---------------------------------------------------------------- ROI 提取

def torso_roi_from_pose(lm: List[Any], w: int, h: int) -> Optional[Tuple[int, int, int, int]]:
    """由 pose landmarks 推导躯干 ROI（像素 x0,y0,x1,y1）。

    躯干 = 双肩中点与双髋中点之间，宽度取肩宽 × 0.72（剔除手臂）。
    """
    ls, rs, lh, rh = lm[11], lm[12], lm[23], lm[24]
    if min(ls.visibility, rs.visibility, lh.visibility, rh.visibility) < 0.3:
        return None
    shoulder_y = (ls.y + rs.y) / 2.0
    hip_y = (lh.y + rh.y) / 2.0
    shoulder_w = abs(ls.x - rs.x)
    if shoulder_w < 0.02 or hip_y <= shoulder_y:
        return None
    center_x = (ls.x + rs.x) / 2.0
    half_w = shoulder_w * 0.36  # 肩宽 72% 的一半
    # 垂直范围：肩部上方 8% 肩宽处 到 髋部
    y0 = max(0.0, shoulder_y - shoulder_w * 0.16)
    y1 = min(1.0, hip_y + shoulder_w * 0.05)
    x0 = max(0.0, center_x - half_w)
    x1 = min(1.0, center_x + half_w)
    box = (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))
    if box[2] - box[0] < 32 or box[3] - box[1] < 32:
        return None
    return box


def _hsv_hist(image_bgr: np.ndarray, mask: Optional[np.ndarray] = None) -> np.ndarray:
    """计算 HSV 2D 直方图（归一化）。"""
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], mask, [H_BINS, S_BINS],
                        [H_RANGES[0], H_RANGES[1], S_RANGES[0], S_RANGES[1]])
    cv2.normalize(hist, hist)
    return hist


def _mask_white_background(image_bgr: np.ndarray) -> np.ndarray:
    """商品平铺图白底遮罩：剔除近白/近灰背景像素。"""
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    # 低饱和度 或 高明度 → 背景
    bg = ((hsv[:, :, 1] < 40) & (hsv[:, :, 2] > 200)) | (hsv[:, :, 2] > 245)
    mask = (~bg).astype(np.uint8) * 255
    # 形态学开运算去噪点
    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    return mask


# ---------------------------------------------------------------- 引擎实现

def _score_histogram(frame_bgr: np.ndarray, roi: Tuple[int, int, int, int],
                     product_path: str) -> Tuple[float, Dict[str, Any]]:
    """直方图降级引擎：躯干 ROI vs 商品图（剔白底）HSV 相关性。"""
    x0, y0, x1, y1 = roi
    torso = frame_bgr[y0:y1, x0:x1]
    if torso.size == 0:
        return 0.0, {"error": "empty torso roi"}
    product = cv2.imread(product_path)
    if product is None:
        return 0.0, {"error": f"unreadable product image: {product_path}"}

    hist_torso = _hsv_hist(torso)
    mask = _mask_white_background(product)
    if mask.sum() == 0:  # 无有效前景，退化为全图
        mask = None
    hist_product = _hsv_hist(product, mask)

    corr = float(cv2.compareHist(hist_torso, hist_product, cv2.HISTCMP_CORREL))
    # 相关性可能为负，裁剪到 [0,1]
    score = max(0.0, min(1.0, corr))
    return score, {
        "engine": "histogram",
        "hist_corr": round(corr, 4),
        "product_fg_ratio": round(float(mask.mean() / 255.0), 3) if mask is not None else 1.0,
    }


def _score_clip(frame_bgr: np.ndarray, roi: Tuple[int, int, int, int],
                product_path: str) -> Tuple[float, Dict[str, Any]]:
    """CLIP 引擎（可选）：open_clip 图-图余弦相似度。"""
    try:
        import open_clip
        import torch
        from PIL import Image
    except ImportError:
        return 0.0, {"engine": "clip", "error": "open_clip_torch 未安装"}

    model_name = "ViT-B-32"
    pretrained = "laion2b_s34b_b79k"  # 中文语境可换 'cn_clip' 权重
    try:
        model, _, preprocess = open_clip.create_model_and_transforms(
            model_name, pretrained=pretrained)
        tokenizer = open_clip.get_tokenizer(model_name)
        model.eval()
    except Exception as e:
        return 0.0, {"engine": "clip", "error": f"模型加载失败: {e}"}

    x0, y0, x1, y1 = roi
    torso = cv2.cvtColor(frame_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2RGB)
    product = cv2.cvtColor(cv2.imread(product_path), cv2.COLOR_BGR2RGB)

    with torch.no_grad():
        t_tensor = preprocess(Image.fromarray(torso)).unsqueeze(0)
        p_tensor = preprocess(Image.fromarray(product)).unsqueeze(0)
        f_t = model.encode_image(t_tensor)
        f_p = model.encode_image(p_tensor)
        f_t = f_t / f_t.norm(dim=-1, keepdim=True)
        f_p = f_p / f_p.norm(dim=-1, keepdim=True)
        score = float((f_t @ f_p.T).item())
    # 余弦相似度 [-1,1] 映射到 [0,1]
    score01 = (score + 1.0) / 2.0
    return score01, {"engine": "clip", "cosine": round(score, 4)}


# ---------------------------------------------------------------- 主入口

def garment_match_score(
    frame_bgr: np.ndarray,
    product_image_path: str,
    pose_landmarks: Optional[List[Any]] = None,
    engine: str = "auto",
) -> Dict[str, Any]:
    """计算封面帧人像衣着与商品图的匹配分数。

    返回 {"score": float, "engine": str, "passed": bool, "roi": [...], "detail": {...}}
    """
    h, w = frame_bgr.shape[:2]
    if pose_landmarks is not None:
        roi = torso_roi_from_pose(pose_landmarks, w, h)
    else:
        roi = None
    if roi is None:
        # 无 pose 时退化为画面中央区域（置信度较低）
        roi = (int(w * 0.30), int(h * 0.25), int(w * 0.70), int(h * 0.60))
        detail_note = "pose 不可用，使用画面中央 ROI（置信度降低）"
    else:
        detail_note = "pose 躯干 ROI"

    use_clip = False
    if engine in ("auto", "clip"):
        try:
            import open_clip  # noqa: F401
            use_clip = True
        except ImportError:
            if engine == "clip":
                return {"score": 0.0, "engine": "clip", "passed": False,
                        "roi": roi, "detail": {"error": "open_clip_torch 未安装"}}
            use_clip = False

    if use_clip:
        score, detail = _score_clip(frame_bgr, roi, product_image_path)
        if "error" in detail and engine == "clip":
            return {"score": 0.0, "engine": "clip", "passed": False, "roi": roi, "detail": detail}
        if "error" not in detail:
            return {"score": round(score, 4), "engine": "clip", "roi": roi, "detail": detail}
    score, detail = _score_histogram(frame_bgr, roi, product_image_path)
    detail["note"] = detail_note
    return {"score": round(score, 4), "engine": "histogram", "roi": roi, "detail": detail}


def check_constraints(result: Dict[str, Any],
                      garment_c: Dict[str, Any]) -> Tuple[bool, List[str], str]:
    """对照 constraints 校验。

    返回 (passed, failed_keys, level)：
      - CLIP 引擎（精确语义匹配）→ BLOCK 级，阈值 garment_match_min=0.75
      - 直方图引擎（弱信号降级方案，仅能识别 gross 色差）→ WARN 级，
        阈值 histogram_min=0.15（基于有限样本标定：同款 ~0.25 / 异色 ~0.0；
        待计划中的 5 商品 × 20 帧标定数据收紧）
    """
    engine = result.get("engine")
    if engine == "clip":
        threshold = garment_c.get("garment_match_min", 0.75)
        level = "BLOCK"
    else:
        threshold = garment_c.get("histogram_min", 0.15)
        level = "WARN"
    score = result.get("score", 0.0)
    passed = score >= threshold
    return passed, ([] if passed else [f"garment_match({engine})<{threshold}"]), level


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="衣着一致性检测工具")
    parser.add_argument("--frame", required=True, help="封面候选帧/封面图路径")
    parser.add_argument("--product", required=True, help="商品平铺图路径")
    parser.add_argument("--pose-json", default=None, help="pose landmarks JSON（可选）")
    parser.add_argument("--engine", default="auto", choices=["auto", "clip", "histogram"])
    parser.add_argument("--constraints", default="contracts/constraints.yaml")
    args = parser.parse_args(argv)

    frame = cv2.imread(args.frame)
    if frame is None:
        print(f"[GARMENT][ERROR] 无法读取帧: {args.frame}")
        return 2

    pose_lm = None
    if args.pose_json and os.path.exists(args.pose_json):
        from types import SimpleNamespace
        with open(args.pose_json, "r", encoding="utf-8") as f:
            raw = json.load(f)
        pose_lm = [SimpleNamespace(x=p["x"], y=p["y"], z=p.get("z", 0.0),
                                   visibility=p.get("visibility", 1.0)) for p in raw]

    result = garment_match_score(frame, args.product, pose_lm, engine=args.engine)
    print(f"[GARMENT] 引擎 {result['engine']} | 匹配分 {result['score']:.4f} | ROI {result['roi']}")
    print(f"          detail: {result['detail']}")

    if os.path.exists(args.constraints):
        import yaml
        with open(args.constraints, "r", encoding="utf-8") as f:
            g_c = yaml.safe_load(f).get("garment_match", {})
        passed, failed, level = check_constraints(result, g_c)
        print(f"[GARMENT] 约束校验({level}级): {'PASS' if passed else 'FAIL ' + str(failed)}")
        return 0 if passed else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
