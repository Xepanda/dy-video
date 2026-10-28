#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_product_image_adapter.py - 商品图封面适配器测试套件（2026-10-28）

覆盖：四种模式可运行 / 尺寸与留白 / 硬边可被 V3 捕获 / 羽化通过 V3 /
深色边缘商品图不产生 halo（V4）/ 任意画布 / 自定义背景 / 验证器误报防护。
"""

import os
import sys
import json
import unittest

import numpy as np
from PIL import Image, ImageDraw

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.graphics.product_image_adapter import (
    ProductImageAdapter, adapt_product_image, DEFAULTS)


def make_test_image(w=400, h=500, dark_edge=True):
    """合成商品图：主体 + 可选深色边缘（模拟实拍缩略图）。"""
    img = Image.new("RGBA", (w, h), (40, 70, 130, 255))
    d = ImageDraw.Draw(img)
    d.ellipse([w // 4, h // 4, 3 * w // 4, 3 * h // 4], fill=(225, 185, 95, 255))
    if dark_edge:
        d.rectangle([0, 0, w - 1, 7], fill=(12, 12, 16, 255))
        d.rectangle([0, h - 8, w - 1, h - 1], fill=(12, 12, 16, 255))
        d.rectangle([0, 0, 7, h - 1], fill=(12, 12, 16, 255))
        d.rectangle([w - 8, 0, w - 1, h - 1], fill=(12, 12, 16, 255))
    return img


class TestProductImageAdapter(unittest.TestCase):

    def test_modes_all_run(self):
        """四种模式均可运行且产出正确画布。"""
        img = make_test_image()
        for mode in ("card", "blend", "extend", "collage"):
            res = adapt_product_image(img, (540, 960), {"mode": mode})
            self.assertEqual(res.canvas.size, (540, 960))
            self.assertEqual(res.meta["mode"], mode)
            self.assertIn("subject_box", res.meta)
            self.assertIn("config", res.meta)

    def test_margin_respected(self):
        """留白配置生效（容差内）。"""
        img = make_test_image()
        res = adapt_product_image(img, (540, 960),
                                  {"mode": "blend", "margin_px": 80})
        x0, y0, x1, y1 = res.meta["subject_box"]
        self.assertGreaterEqual(x0, 80 - 3)
        self.assertGreaterEqual(y0, 80 - 3)
        self.assertLessEqual(x1, 540 - 80 + 3)
        self.assertLessEqual(y1, 960 - 80 + 3)

    def test_canvas_sizes(self):
        """适配不同封面尺寸。"""
        img = make_test_image()
        for size in ((1080, 1920), (720, 1280), (540, 960), (1200, 1200)):
            res = adapt_product_image(img, size, {"mode": "blend"})
            self.assertEqual(res.canvas.size, size)

    def test_custom_background(self):
        """自定义背景图注入（适配不同背景）。"""
        img = make_test_image()
        bg = Image.new("RGB", (540, 960), (200, 30, 30))
        adapter = ProductImageAdapter({"mode": "blend"})
        res3 = adapter.adapt(img, (540, 960), background=bg)
        self.assertEqual(res3.meta["background_type"], "custom")
        px = np.array(res3.canvas.convert("RGB"))
        # 左上角应是红色背景（留白区）
        self.assertGreater(px[10, 10, 0], 150)
        self.assertLess(px[10, 10, 1], 80)

    def test_feather_alpha_profile(self):
        """羽化开启时 alpha 剖面单调过渡；关闭时为硬边。"""
        img = make_test_image()
        res = adapt_product_image(img, (540, 960),
                                  {"mode": "blend", "feather_px": 40,
                                   "margin_px": 60})
        fg = np.array(res.foreground.split()[3])
        x0, y0, x1, y1 = res.meta["subject_box"]
        cols = slice(x0 + (x1 - x0) // 3, x0 + 2 * (x1 - x0) // 3)
        # 羽化：前景自身顶行应近透明，向内 40px 应不透明
        edge_row = float(np.mean(fg[y0, cols] / 255.0))
        inner = float(np.mean(fg[y0 + 40:y0 + 60, cols] / 255.0))
        self.assertLess(edge_row, 0.35, f"羽化边行应近透明，实测 {edge_row:.3f}")
        self.assertGreater(inner, 0.95, f"内侧应不透明，实测 {inner:.3f}")
        # 单调性：从边缘向内 alpha 不应下降
        prof = [float(np.mean(fg[y0 + d, cols] / 255.0)) for d in range(0, 80, 8)]
        self.assertTrue(all(p2 >= p1 - 0.06 for p1, p2 in zip(prof, prof[1:])),
                        f"alpha 剖面应单调非降: {[round(p, 2) for p in prof]}")

        res_hard = adapt_product_image(img, (540, 960),
                                       {"mode": "blend", "feather_px": 0,
                                        "margin_px": 60})
        fg_h = np.array(res_hard.foreground.split()[3])
        edge_h = float(np.mean(fg_h[y0, cols] / 255.0))
        self.assertGreater(edge_h, 0.9, "硬边时前景边缘行应完全不透明")
        outside_h = float(np.mean(fg_h[max(0, y0 - 1), cols] / 255.0))
        self.assertLess(outside_h, 0.05, "硬边时前景外侧应全透明")

    def test_remove_white_background(self):
        """白底去除：边界连通白底被移除，主体（含内部浅色区）完整保留。"""
        img = make_test_image()
        cut, meta = ProductImageAdapter.remove_white_background(
            img, feather_sigma=2.0, erode_px=1)
        a = np.array(cut.split()[3])
        h, w = a.shape
        # 四边应透明（白底+深色外框已去除）
        self.assertLess(int(a[:3].max()), 60)
        self.assertLess(int(a[-3:].max()), 60)
        self.assertLess(int(a[:, :3].max()), 60)
        self.assertLess(int(a[:, -3:].max()), 60)
        # 中心主体不透明
        self.assertGreater(int(a[h // 2, w // 2]), 200)
        # 主体保留率 > 90%
        self.assertGreater(meta["subject_retention"], 0.90)
        # 内部浅色连通区保护：主体上放一个白色小块（不触边）
        img2 = make_test_image()
        d = ImageDraw.Draw(img2)
        d.ellipse([w // 2 - 20, h // 2 - 20, w // 2 + 20, h // 2 + 20],
                  fill=(250, 250, 250, 255))
        cut2, meta2 = ProductImageAdapter.remove_white_background(
            img2, feather_sigma=2.0, erode_px=0)
        a2 = np.array(cut2.split()[3])
        self.assertGreater(int(a2[h // 2, w // 2]), 200,
                           "主体内部白色区域被误删")

    def test_invalid_config_rejected(self):
        """非法配置被拒绝。"""
        with self.assertRaises(ValueError):
            ProductImageAdapter({"mode": "nope"})
        with self.assertRaises(ValueError):
            ProductImageAdapter({"background": {"type": "nope"}})

    def test_verifier_catches_hard_edge(self):
        """验证器能捕获硬边（反向测试）。"""
        from src.tools.verify_cover_adaptation import check_v3_hard_edge
        img = make_test_image(dark_edge=False)
        res = adapt_product_image(img, (540, 960),
                                  {"mode": "blend", "feather_px": 0,
                                   "margin_px": 60})
        canvas_rgb = np.array(res.canvas.convert("RGB"))
        fg_alpha = np.array(res.foreground.split()[3])
        r = check_v3_hard_edge(canvas_rgb, fg_alpha,
                               tuple(res.meta["subject_box"]), 0)
        self.assertEqual(r["status"], "BLOCK",
                         f"硬边应被 V3 捕获: {r['detail']}")

    def test_verifier_passes_feathered(self):
        """羽化后验证器通过（正向测试）。"""
        from src.tools.verify_cover_adaptation import check_v3_hard_edge
        img = make_test_image(dark_edge=False)
        res = adapt_product_image(img, (540, 960),
                                  {"mode": "blend", "feather_px": 48,
                                   "margin_px": 60})
        canvas_rgb = np.array(res.canvas.convert("RGB"))
        fg_alpha = np.array(res.foreground.split()[3])
        r = check_v3_hard_edge(canvas_rgb, fg_alpha,
                               tuple(res.meta["subject_box"]), 48)
        self.assertIn(r["status"], ("PASS", "WARN"),
                      f"羽化边应通过 V3: {r['detail']}")

    def test_no_halo_dark_edge_on_light_bg(self):
        """深色边缘商品图 + 浅色背景：去晕后无 halo。"""
        from src.tools.verify_cover_adaptation import check_v4_halo
        img = make_test_image(dark_edge=True)
        res = adapt_product_image(img, (540, 960),
                                  {"mode": "blend", "feather_px": 48,
                                   "margin_px": 60,
                                   "background": {"type": "color",
                                                  "color": "#F5EFE8"},
                                   "dehalo": True})
        canvas_rgb = np.array(res.canvas.convert("RGB"))
        fg_rgb = np.array(res.foreground.convert("RGB"))
        fg_alpha = np.array(res.foreground.split()[3])
        r = check_v4_halo(canvas_rgb, fg_rgb, fg_alpha,
                          tuple(res.meta["subject_box"]), 48)
        self.assertEqual(r["status"], "PASS",
                         f"去晕后不应有 halo: {r['detail']}")


if __name__ == "__main__":
    unittest.main()
