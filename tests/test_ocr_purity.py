#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_ocr_purity.py - B11 封面背景文字纯净度检测合成用例
运行: python -u tests/test_ocr_purity.py
依赖: rapidocr-onnxruntime（模型首次自动下载）、PIL、中文字体（msyh.ttc）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from src.tools.detect_ocr import check_background_text, background_text_metrics

W, H = 1080, 1920
FONT_PATH = "C:/Windows/Fonts/msyh.ttc"


def _canvas(color=(60, 60, 66)):
    return Image.new("RGB", (W, H), color)


def _draw_text(img, xy, text, size, fill=(255, 255, 255), anchor="la"):
    font = ImageFont.truetype(FONT_PATH, size)
    d = ImageDraw.Draw(img)
    d.text(xy, text, font=font, fill=fill, anchor=anchor)
    return img


def _to_bgr(img):
    return np.asarray(img)[:, :, ::-1].copy()


def case_clean():
    """纯净封面（无任何文字）→ 必须通过"""
    img = _canvas()
    return _to_bgr(img), None


def case_shop_banner():
    """背景店招大字（占宽 80%、高约 12%）→ 必须拦截"""
    img = _canvas()
    img = _draw_text(img, (W // 2, 200), "店庆狂欢 全场五折", 150,
                     fill=(255, 80, 80), anchor="mm")
    img = _draw_text(img, (W // 2, 500), "新店开业 欢迎选购", 110,
                     fill=(255, 200, 60), anchor="mm")
    return _to_bgr(img), None


def case_template_text_exempt():
    """模板自有文字（品牌区+商品名区）+ render meta 豁免 → 必须通过"""
    img = _canvas()
    img = _draw_text(img, (W // 2, 300), "MIXCITY", 100, anchor="mm")
    img = _draw_text(img, (W // 2, 1650), "气质显瘦针织衫", 120, anchor="mm")
    meta = {"elements": {
        "brand_en": {"top_y": 240, "bottom_y": 360},
        "product_name": {"top_y": 1560, "bottom_y": 1740},
    }}
    return _to_bgr(img), meta


def case_tiny_noise():
    """极小噪点文字（低于 min_box_area_px）→ 不计入 → 通过"""
    img = _canvas()
    img = _draw_text(img, (100, 100), "小", 20)
    return _to_bgr(img), None


def case_small_but_counted():
    """中等背景文字（面积超 2%）→ 拦截"""
    img = _canvas()
    img = _draw_text(img, (W // 2, 900), "直播间专属福利", 90, anchor="mm")
    return _to_bgr(img), None


def main():
    from src.tools.detect_ocr import _load_constraints
    constraints = _load_constraints()
    ocr_c = constraints.get("ocr", {})
    print(f"[TEST-B11] 引擎={ocr_c.get('engine')} 阈值={ocr_c.get('background_text_area_max')} "
          f"min_box_area={ocr_c.get('min_box_area_px')}")

    cases = [
        ("纯净封面", case_clean, True),
        ("店招大字封杀", case_shop_banner, False),
        ("模板文字豁免", case_template_text_exempt, True),
        ("极小噪点豁免", case_tiny_noise, True),
        ("中等背景文字封杀", case_small_but_counted, False),
    ]
    failures = []
    for name, fn, expect_pass in cases:
        img, meta = fn()
        passed, metrics = check_background_text(img, constraints, render_meta=meta)
        status = "PASS" if passed == expect_pass else "FAIL"
        if status == "FAIL":
            failures.append(name)
        print(f"  [{status}] {name}: ratio={metrics['ratio']*100:.2f}% "
              f"(boxes={metrics['box_count']}, exempt={metrics['exempted_template_boxes']}) "
              f"期望{'通过' if expect_pass else '拦截'} 实测{'通过' if passed else '拦截'}")

    # 阈值边界：构造刚好在阈值上下的可控场景不可靠（OCR 检出面积不稳定），
    # 改验证 metrics 单调性：文字越多 ratio 越大
    m1 = background_text_metrics(_to_bgr(_canvas()), render_meta=None)["ratio"]
    m2 = background_text_metrics(_to_bgr(case_shop_banner()[0]), render_meta=None)["ratio"]
    mono = m2 > m1
    print(f"  [{'PASS' if mono else 'FAIL'}] 单调性: 纯净({m1*100:.2f}%) < 店招({m2*100:.2f}%)")
    if not mono:
        failures.append("单调性")

    print(f"\n[TEST-B11] {'全部通过' if not failures else '失败: ' + str(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
