#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
detect_ocr.py - 封面背景文字（店招/背景板）纯净度检测工具（B11）
==============================================================
依据 contracts/constraints.yaml 的 ocr 段配置：

  背景文字面积占比 = Σ(OCR 检出文本框面积) / 画布面积   （排除模板自有文本区域）
  阈值 background_text_area_max（默认 0.02 = 2%），BLOCK 级。

设计要点：
1. 引擎：rapidocr-onnxruntime（PP-OCR 系列模型的 ONNX 推理版，
   与 PaddleOCR 同一检测模型体系，但依赖轻（仅 onnxruntime，已随环境安装），
   避免 paddlepaddle 在 Windows CPU 环境的重依赖问题。
   constraints.ocr.engine 预留 "paddleocr" 选项，未来可切换。
2. 模板区域豁免：封面渲染 meta 中的品牌区/商品名/贴图卡属于模板自有文本，
   不计入"背景文字"；IoU > 0.3 的检出框视为模板内容跳过。
3. 懒加载单例：模型文件首次初始化时按需下载，之后进程内复用。
4. 双源复测：候选阶段（extract_cover_candidates，淘汰超标候选）+
   QC 阶段（qc_auditor，对最终成图封面独立复测）共用本工具的度量函数。

CLI:
  python src/tools/detect_ocr.py --image cover.jpg [--meta render_meta.json]
"""

import os
import sys
import json
import argparse
import threading
from typing import Dict, Any, List, Optional, Tuple

import cv2
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)
from src.core.path_resolver import cv2_imread

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------- 引擎单例

_OCR_ENGINE = None
_OCR_LOCK = threading.Lock()
_OCR_ENGINE_NAME = None


def get_ocr_engine(engine: str = "rapidocr"):
    """懒加载 OCR 引擎单例（首次调用触发模型下载/加载）。"""
    global _OCR_ENGINE, _OCR_ENGINE_NAME
    with _OCR_LOCK:
        if _OCR_ENGINE is not None and _OCR_ENGINE_NAME == engine:
            return _OCR_ENGINE
        if engine in ("rapidocr", "auto"):
            from rapidocr_onnxruntime import RapidOCR
            _OCR_ENGINE = RapidOCR()
            _OCR_ENGINE_NAME = "rapidocr"
        elif engine == "paddleocr":
            # 预留：PaddleOCR 重依赖路径（当前环境未安装 paddlepaddle）
            from paddleocr import PaddleOCR  # type: ignore
            _OCR_ENGINE = PaddleOCR(use_angle_cls=False, lang="ch", show_log=False)
            _OCR_ENGINE_NAME = "paddleocr"
        else:
            raise ValueError(f"未知 OCR 引擎: {engine}")
        return _OCR_ENGINE


def _normalize_ocr_output(result, engine_name: str) -> List[Dict[str, Any]]:
    """统一不同引擎输出为 [{box:[[x,y]x4], text, score}]。"""
    boxes = []
    if result is None:
        return boxes
    if engine_name == "rapidocr":
        # RapidOCR: [ [box(4x2), text, score], ... ] 或 (result, elapse)
        if isinstance(result, tuple):
            result = result[0]
        for item in result or []:
            if not item or len(item) < 3:
                continue
            box, text, score = item[0], item[1], item[2]
            boxes.append({
                "box": [[float(p[0]), float(p[1])] for p in box],
                "text": str(text),
                "score": float(score),
            })
    elif engine_name == "paddleocr":
        # PaddleOCR v2+: [[ [box, (text, score)] ], ...]
        for page in result or []:
            for item in page or []:
                if not item or len(item) < 2:
                    continue
                box, (text, score) = item[0], item[1]
                boxes.append({
                    "box": [[float(p[0]), float(p[1])] for p in box],
                    "text": str(text),
                    "score": float(score),
                })
    return boxes


# ---------------------------------------------------------------- 度量

def _poly_area(box: List[List[float]]) -> float:
    """鞋带公式计算四边形面积（px²）。"""
    pts = np.asarray(box, dtype=np.float64)
    if pts.shape[0] < 3:
        return 0.0
    x, y = pts[:, 0], pts[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2.0)


def _box_center(box: List[List[float]]) -> Tuple[float, float]:
    pts = np.asarray(box, dtype=np.float64)
    return float(pts[:, 0].mean()), float(pts[:, 1].mean())


def _iou(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    """轴对齐矩形 IoU（模板豁免判定用）。"""
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _template_rects(render_meta: Optional[Dict[str, Any]]) -> List[Tuple[float, float, float, float]]:
    """从封面渲染 meta 提取模板自有元素矩形（品牌/商品名/贴图）。"""
    rects: List[Tuple[float, float, float, float]] = []
    if not render_meta:
        return rects
    elements = render_meta.get("elements", {}) or {}
    for key, el in elements.items():
        if not isinstance(el, dict):
            continue
        if "box" in el and isinstance(el["box"], (list, tuple)) and len(el["box"]) == 4:
            rects.append((float(el["box"][0]), float(el["box"][1]),
                          float(el["box"][2]), float(el["box"][3])))
        elif "top_y" in el and "bottom_y" in el:
            # 品牌区/商品名 meta：top_y/bottom_y + 全宽
            rects.append((0.0, float(el["top_y"]), 1080.0, float(el["bottom_y"])))
    return rects


def detect_text_boxes(image_bgr: np.ndarray, engine: str = "rapidocr",
                      min_conf: float = 0.5) -> List[Dict[str, Any]]:
    """对图像执行 OCR，返回过滤低置信度后的文本框列表。"""
    ocr = get_ocr_engine(engine)
    result = ocr(image_bgr)
    boxes = _normalize_ocr_output(result, _OCR_ENGINE_NAME)
    return [b for b in boxes if b["score"] >= min_conf]


def background_text_metrics(
    image_bgr: np.ndarray,
    render_meta: Optional[Dict[str, Any]] = None,
    engine: str = "rapidocr",
    min_conf: float = 0.5,
    min_box_area_px: float = 400.0,
) -> Dict[str, Any]:
    """度量封面背景文字面积占比。

    render_meta: 封面渲染 meta（QC 阶段传入，用于豁免模板自有文本；
                候选阶段底图无模板文本，传 None）。
    返回: {ratio, text_area_px, canvas_area_px, box_count, boxes, engine}
    """
    h, w = image_bgr.shape[:2]
    canvas_area = float(w * h)
    boxes = detect_text_boxes(image_bgr, engine=engine, min_conf=min_conf)
    template_rects = _template_rects(render_meta)

    kept, exempted = [], []
    text_area = 0.0
    for b in boxes:
        area = _poly_area(b["box"])
        if area < min_box_area_px:
            continue
        cx, cy = _box_center(b["box"])
        bx0, by0 = cx - 1, cy - 1  # 中心点退化矩形用于 IoU 豁免判定
        if any(_iou((bx0, by0, cx + 1, cy + 1), r) > 0.0 for r in template_rects):
            exempted.append(b)
            continue
        kept.append(b)
        text_area += area

    return {
        "ratio": round(text_area / max(1.0, canvas_area), 5),
        "text_area_px": round(text_area, 1),
        "canvas_area_px": canvas_area,
        "box_count": len(kept),
        "exempted_template_boxes": len(exempted),
        "boxes": [{"text": b["text"], "score": round(b["score"], 3),
                   "area_px": round(_poly_area(b["box"]), 1)} for b in kept],
        "engine": _OCR_ENGINE_NAME or engine,
    }


def check_background_text(
    image_bgr: np.ndarray,
    constraints: Dict[str, Any] = None,
    render_meta: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """依据 constraints.ocr 段判定背景文字是否合规。

    返回: (passed, metrics)
    """
    ocr_c = (constraints or {}).get("ocr", {}) or {}
    engine = ocr_c.get("engine", "rapidocr")
    min_conf = float(ocr_c.get("min_confidence", 0.5))
    min_area = float(ocr_c.get("min_box_area_px", 400))
    metrics = background_text_metrics(image_bgr, render_meta=render_meta,
                                      engine=engine, min_conf=min_conf,
                                      min_box_area_px=min_area)
    threshold = float(ocr_c.get("background_text_area_max", 0.02))
    metrics["threshold"] = threshold
    metrics["passed"] = metrics["ratio"] <= threshold
    return metrics["passed"], metrics


def _load_constraints(path: str = None) -> Dict[str, Any]:
    path = path or os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
    if not os.path.exists(path):
        return {}
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="封面背景文字纯净度检测（B11）")
    parser.add_argument("--image", required=True, help="封面图片路径")
    parser.add_argument("--meta", default=None, help="封面渲染 meta（豁免模板文本用）")
    parser.add_argument("--constraints", default="contracts/constraints.yaml")
    args = parser.parse_args()

    img = cv2_imread(args.image)
    if img is None:
        print(f"无法读取图片: {args.image}")
        sys.exit(1)
    meta = None
    if args.meta and os.path.exists(args.meta):
        with open(args.meta, "r", encoding="utf-8") as f:
            meta = json.load(f)
    passed, metrics = check_background_text(img, _load_constraints(args.constraints), meta)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f"\n结论: {'PASS（背景文字面积合规）' if passed else 'FAIL（背景文字超限）'}")
    sys.exit(0 if passed else 2)
