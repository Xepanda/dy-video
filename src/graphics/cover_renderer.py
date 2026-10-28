#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
cover_renderer.py - MIXCITY 风格参数化高定品牌封面排版引擎（v2）
===============================================================
依据《会议纪要》+ 2026-10-20 决策重构：

1. 顶部品牌区 (y=120~290): 英文主标 3D 金字 + 中文副标白字褐描边；
   **新增自动换行**（C16）：名称过长时按 92% 画布宽换行并等比缩字；
2. 右侧/左侧浮动贴图区 (N=0~2 张): 正方形 280x280、圆角 20、淡灰描边 #E2E8F0；
   **新增左右侧可配**（C12：sticker_side = left | right）；
3. 底部商品名 (y=1530~): **决策 2：金色渐变 #FFE9A8→#E8A91E** + 深褐描边 + shear 斜体；
   **新增自动换行**（C9）：单行超 92% 画布宽触发，字号 64→48 下限，行距 1.35×；
4. **新增渲染几何 meta 输出**（render_cover_with_meta）：各元素 bbox 落盘 JSON，
   供 QC 门禁像素级复测（C8/C11/C13/C15）。

约束来源：contracts/constraints.yaml 的 text / sticker 段（缺省用内置 MIXCITY 预设）。
"""

import os
import sys
import json
import math
from typing import List, Dict, Any, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops

OUT_W = 1080
OUT_H = 1920

FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc" if os.name == "nt" else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_HEAVY = "C:/Windows/Fonts/simhei.ttf" if os.name == "nt" else FONT_BOLD

# 配色常数（1:1 复刻 MIXCITY 示例图，2026-10-20 采样校准）
COLOR_BRAND_STROKE = (56, 30, 17, 255)       # 深褐描边 #381E11
COLOR_BRAND_SHADOW = (26, 13, 7, 210)        # 3D 投影深棕 #1A0D07
COLOR_WHITE = (255, 255, 255, 255)
COLOR_BORDER_GRAY = (226, 232, 240, 255)     # #E2E8F0（贴图淡灰描边）
COLOR_PRODUCT_STROKE = (61, 31, 14, 255)     # 商品名深褐描边 #3D1F0E（决策 2）

# 品牌英文主标：青铜渐变（示例图采样 RGB(151,124,87) 校准）
BRAND_EN_GRADIENT_TOP = (201, 166, 107, 255)    # #C9A66B
BRAND_EN_GRADIENT_BOTTOM = (138, 101, 47, 255)  # #8A652F

# 决策 2：商品名金色渐变（顶 → 底）
PRODUCT_GRADIENT_TOP = (255, 233, 170, 255)    # #FFE9A8
PRODUCT_GRADIENT_BOTTOM = (232, 169, 30, 255)  # #E8A91E

DEFAULTS = {
    "text": {
        "wrap_width_ratio": 0.92,
        "line_spacing_ratio": 1.22,
        "font_size_base": 96,
        "font_size_min": 64,
        "brand_font_size_en": 74,
        "brand_font_size_cn": 52,
        "product_name_shear": -0.12,
        "product_name_stroke_width": 5,
        "product_name_top_ratio": 0.80,   # 名称区顶部（画幅比例）
    },
    "sticker": {
        "size": 280,
        "margin_a": 32,
        "gap_a": 32,
        "side": "right",
        "layout": "vertical",           # vertical（上下堆叠，靠右对齐）| horizontal（两张并排）
        "radius": 20,
        "border_color": "#E2E8F0",
        "border_width": 2,
        "vertical_center": 960,
    },
}


def _hex_to_rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _load_constraints(path: str = None) -> Dict[str, Any]:
    if path is None:
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "contracts", "constraints.yaml")
    if not os.path.exists(path):
        return {}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def apply_readability_scrims(canvas: Image.Image,
                            top_h: int = 430,
                            bottom_start_ratio: float = 0.70) -> Image.Image:
    """在完整底图上添加顶部/底部渐变遮罩（2026-10-27 反馈驱动）。

    背景：全幅底图新流程下，品牌文字与商品名直接叠在直播画面上，
    若底图背景复杂（吊衣架/人身/灯光）会降低文字可读性。
    方案：用渐变遮罩（scrim）而非硬黑边——顶部由深色渐变至透明，
    底部由透明渐变至深色，文字区域有阴影而画面中间保持纯净，
    视觉上文字与图片自然融为一体。
    """
    w, h = canvas.size
    scrim = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    s_draw = ImageDraw.Draw(scrim)
    # 顶部：(24, 14, 9, 120) → 透明（遮盖品牌文字区）
    top_h = min(top_h, h)
    for y in range(top_h):
        a = int(120 * (1.0 - y / max(1, top_h)) ** 1.3)
        if a <= 0:
            continue
        s_draw.line([(0, y), (w, y)], fill=(24, 14, 9, a))
    # 底部：透明 → (24, 14, 9, 165)（遮盖商品名区）
    y0 = int(h * bottom_start_ratio)
    span = max(1, h - y0)
    for y in range(y0, h):
        t = (y - y0) / span
        a = int(165 * t ** 1.2)
        s_draw.line([(0, y), (w, y)], fill=(24, 14, 9, a))
    return Image.alpha_composite(canvas, scrim)


def draw_rounded_rectangle_with_shadow(
    canvas: Image.Image,
    box: Tuple[int, int, int, int],
    radius: int = 20,
    shadow_offset: Tuple[int, int] = (0, 8),
    shadow_blur: int = 14,
    shadow_alpha: int = 70
) -> None:
    """绘制带柔和高斯模糊投影的外发光圆角矩形"""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    pad = shadow_blur * 2
    shadow_img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    s_draw = ImageDraw.Draw(shadow_img)
    s_draw.rounded_rectangle(
        [pad, pad, pad + w, pad + h], radius=radius, fill=(0, 0, 0, shadow_alpha))
    shadow_img = shadow_img.filter(ImageFilter.GaussianBlur(shadow_blur))
    canvas.alpha_composite(shadow_img, (x0 - pad + shadow_offset[0], y0 - pad + shadow_offset[1]))


def render_gradient_sheared_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    gradient_top: Tuple[int, int, int, int],
    gradient_bottom: Tuple[int, int, int, int],
    stroke_color: Tuple[int, int, int, int],
    stroke_width: int = 4,
    shear: float = -0.12
) -> Tuple[Image.Image, Tuple[int, int]]:
    """渲染金色渐变 + 深褐描边 + 微斜体的艺术字条。

    返回 (RGBA 字条, 原始未倾斜尺寸)，尺寸用于 QC bbox 复测。
    """
    bbox = font.getbbox(text)
    tw = bbox[2] - bbox[0] + 40
    th = bbox[3] - bbox[1] + 40

    # 1) 文字 alpha 掩码（字芯）
    mask_img = Image.new("L", (tw, th), 0)
    m_draw = ImageDraw.Draw(mask_img)
    m_draw.text((20, 20), text, font=font, fill=255)

    # 2) 垂直渐变（顶 → 底）
    gradient = Image.new("RGBA", (tw, th))
    g_draw = ImageDraw.Draw(gradient)
    for y in range(th):
        ratio = y / max(1, th - 1)
        color = tuple(int(gradient_top[i] + (gradient_bottom[i] - gradient_top[i]) * ratio)
                      for i in range(4))
        g_draw.line([(0, y), (tw, y)], fill=color)

    # 3) 渐变经掩码裁剪为字芯
    text_layer = Image.composite(gradient, Image.new("RGBA", (tw, th), (0, 0, 0, 0)), mask_img)

    # 4) 描边层（深褐）
    stroke_img = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    s_draw2 = ImageDraw.Draw(stroke_img)
    s_draw2.text((20, 20), text, font=font, fill=(0, 0, 0, 0),
                 stroke_width=stroke_width, stroke_fill=stroke_color)
    # 描边层只保留描边（去掉字芯）：用掩码反相扣掉字芯
    core_mask = mask_img.point(lambda v: 255 if v > 0 else 0)
    stroke_only = Image.composite(
        stroke_img, Image.new("RGBA", (tw, th), (0, 0, 0, 0)),
        core_mask.point(lambda v: 0 if v > 0 else 255))
    strip = Image.alpha_composite(stroke_only, text_layer)

    # 5) 仿射斜体（x' = x + shear * y）
    extra_w = int(abs(shear) * th)
    sheared = strip.transform(
        (tw + extra_w, th), Image.AFFINE, (1, shear, 0, 0, 1, 0),
        resample=Image.BICUBIC)
    return sheared, (tw, th)


def wrap_text_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    min_font_size: int = 48,
    base_size: int = 64
) -> Tuple[List[str], ImageFont.FreeTypeFont]:
    """自动换行 + 等比缩字（C9/C16）。

    单行超过 max_width 时优先按语义切分（标点/空格），仍超则按字符断行；
    全部行仍超宽时按比例缩小字号（不低于 min_font_size）。
    """
    size = base_size
    while size > min_font_size:
        f = font.font_variant(size=size) if hasattr(font, "font_variant") else \
            ImageFont.truetype(font.path, size)
        if draw.textlength(text, font=f) <= max_width:
            return [text], f
        size -= 2

    f = ImageFont.truetype(font.path, min_font_size) if hasattr(font, "path") else \
        ImageFont.truetype(FONT_BOLD, min_font_size)
    if draw.textlength(text, font=f) <= max_width:
        return [text], f

    # 按字符贪心断行
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=f) <= max_width:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines, f


class MixCityCoverRenderer:
    def __init__(self, constraints: Dict[str, Any] = None):
        constraints = constraints or _load_constraints()
        text_c = {**DEFAULTS["text"], **(constraints.get("text", {}) or {})}
        sticker_c = {**DEFAULTS["sticker"], **(constraints.get("sticker", {}) or {})}
        self.text_c = text_c
        self.sticker_c = sticker_c
        self.font_h1 = ImageFont.truetype(FONT_HEAVY, int(text_c["brand_font_size_en"]))
        self.font_sub = ImageFont.truetype(FONT_BOLD, int(text_c["brand_font_size_cn"]))
        self.font_prod_base = ImageFont.truetype(FONT_BOLD, int(text_c["font_size_base"]))
        # 商品名金色渐变（决策 2）
        self.gradient_top = PRODUCT_GRADIENT_TOP
        self.gradient_bottom = PRODUCT_GRADIENT_BOTTOM

    # ------------------------------------------------------------ 主入口

    def render_cover_with_meta(
        self,
        base_frame_path: str,
        brand_info: Dict[str, str],
        product_info: Dict[str, Any],
        output_path: str,
        meta_output_path: str = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """生成标准 1080x1920 MIXCITY 封面，并返回渲染几何 meta（供 QC 复测）。"""
        if not os.path.exists(base_frame_path):
            raise FileNotFoundError(f"底图文件未找到: {base_frame_path}")

        base = Image.open(base_frame_path).convert("RGBA")
        if base.size != (OUT_W, OUT_H):
            if base.size[1] == OUT_H and base.size[0] > OUT_W:
                left = (base.size[0] - OUT_W) // 2
                base = base.crop((left, 0, left + OUT_W, OUT_H))
            else:
                base = base.resize((OUT_W, OUT_H), Image.LANCZOS)

        overlay = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        meta: Dict[str, Any] = {"canvas": [OUT_W, OUT_H], "elements": {}}

        # -------------------------------------------------------------
        # 1. 顶部品牌区（自动换行 C16）
        # -------------------------------------------------------------
        brand_en = (brand_info.get("title_en") or "MIXCITY").strip()
        brand_cn = (brand_info.get("subtitle_cn") or "深圳高端女装").strip()
        max_text_w = OUT_W * self.text_c["wrap_width_ratio"]

        en_lines, f_en = wrap_text_to_width(
            draw, brand_en, self.font_h1, max_text_w,
            min_font_size=int(self.text_c["brand_font_size_cn"]),
            base_size=int(self.text_c["brand_font_size_en"]))
        y_en = 125
        line_h = int(self.text_c["brand_font_size_en"] * 1.15)
        for li, line in enumerate(en_lines):
            yy = y_en + li * line_h
            # 青铜渐变主标（示例图采样校准）+ 3D 投影
            strip, _ = render_gradient_sheared_text(
                line, f_en, BRAND_EN_GRADIENT_TOP, BRAND_EN_GRADIENT_BOTTOM,
                COLOR_BRAND_STROKE, stroke_width=4.5, shear=0.0)
            # 投影层
            shadow, _ = render_gradient_sheared_text(
                line, f_en, COLOR_BRAND_SHADOW, COLOR_BRAND_SHADOW,
                COLOR_BRAND_SHADOW, stroke_width=4, shear=0.0)
            pos_x = (OUT_W - strip.width) // 2
            overlay.alpha_composite(shadow, (pos_x + 2, yy + 4))
            overlay.alpha_composite(strip, (pos_x, yy))
        y_after_en = y_en + len(en_lines) * line_h

        cn_lines, f_cn = wrap_text_to_width(
            draw, brand_cn, self.font_sub, max_text_w,
            min_font_size=36, base_size=int(self.text_c["brand_font_size_cn"]))
        y_cn = y_after_en + 4
        for li, line in enumerate(cn_lines):
            yy = y_cn + li * int(self.text_c["brand_font_size_cn"] * 1.15)
            draw.text((OUT_W // 2 + 1, yy + 3), line, font=f_cn,
                      fill=COLOR_BRAND_SHADOW, anchor="mt", stroke_width=3,
                      stroke_fill=COLOR_BRAND_SHADOW)
            draw.text((OUT_W // 2, yy), line, font=f_cn,
                      fill=COLOR_WHITE, anchor="mt", stroke_width=3.5,
                      stroke_fill=COLOR_BRAND_STROKE)
        meta["elements"]["brand_en"] = {
            "lines": en_lines, "font_size": f_en.size,
            "top_y": y_en, "bottom_y": y_after_en,
            "center_x": OUT_W // 2,
        }
        meta["elements"]["brand_cn"] = {
            "lines": cn_lines, "font_size": f_cn.size,
            "top_y": y_cn, "bottom_y": y_cn + len(cn_lines) * int(self.text_c["brand_font_size_cn"] * 1.15),
            "center_x": OUT_W // 2,
        }

        # -------------------------------------------------------------
        # 2. 浮动圆角商品贴图（左右侧可配 C12；layout: horizontal 并排 | vertical 堆叠）
        # -------------------------------------------------------------
        item_images = [img for img in product_info.get("item_images", []) if img and os.path.exists(img)]
        num_cards = min(len(item_images), 2)
        if num_cards == 0:
            # 需求（2026-10-21）：素材缺少商品图时，仅保留直播画面，
            # 不插入任何商品图、不做补图、不留占位框。
            print("[COVER] 商品图为空：跳过贴图渲染，封面仅保留直播画面 + 品牌区 + 商品名")
        card_size = int(self.sticker_c["size"])
        margin_a = int(self.sticker_c["margin_a"])
        gap_a = int(self.sticker_c["gap_a"])
        side = product_info.get("sticker_side") or self.sticker_c.get("side", "right")
        layout = self.sticker_c.get("layout", "horizontal")
        v_center = int(self.sticker_c["vertical_center"])

        # 计算卡片位置列表（保持右侧靠边 a、整体垂直居中）
        card_boxes = []
        if num_cards >= 1:
            if layout == "horizontal" and num_cards == 2:
                # 并排：整体宽 = 2*size + gap，靠右 margin_a，纵向居中
                group_w = card_size * 2 + gap_a
                gx0 = OUT_W - margin_a - group_w if side == "right" else margin_a
                gy0 = v_center - card_size // 2
                card_boxes = [(gx0, gy0), (gx0 + card_size + gap_a, gy0)]
            else:
                # 堆叠（或单张）：整体高 = n*size + (n-1)*gap，靠右 margin_a，纵向居中
                group_h = card_size * num_cards + gap_a * (num_cards - 1)
                gy0 = v_center - group_h // 2
                gx0 = OUT_W - card_size - margin_a if side == "right" else margin_a
                for i in range(num_cards):
                    card_boxes.append((gx0, gy0 + i * (card_size + gap_a)))

        # 自然过渡贴图（2026-10-28：反馈——右侧商品图不得生硬“贴贴”感）
        # 复用 product_image_adapter 的边缘处理链：
        # 缩放+键化 → 去晕（dehalo）→ 圆角+羽毛 alpha → 向背景渐变融合。
        # 阴影/描边/meta 保持原样（QC 几何复测依赖 meta）。
        use_adapter_edges = bool(self.sticker_c.get("use_adapter_edges", True))
        # 贴图样式（2026-10-28 反馈：去除商品图四周白边）：
        #   cutout —— 去白底 + 圆角 + 羽化，商品主体直接融入直播画面（默认）；
        #   card    —— 保留白底卡片形态，仅做柔边处理（上一版行为）。
        sticker_style = str(self.sticker_c.get("sticker_style", "cutout"))
        if sticker_style not in ("cutout", "card"):
            sticker_style = "cutout"
        cutout_c = self.sticker_c.get("cutout", {}) or {}
        sticker_adapter = None
        if use_adapter_edges or sticker_style == "cutout":
            from src.graphics.product_image_adapter import ProductImageAdapter
            sticker_adapter = ProductImageAdapter({
                "mode": "card",
                "feather_px": float(self.sticker_c.get("sticker_feather_px", 14) or 14),
                "corner_radius": float(self.sticker_c.get("radius", 20)),
                "blend_ratio": 1.0,
                "dehalo": bool(self.sticker_c.get("sticker_dehalo", True)),
                "shadow": {"enabled": False},   # 阴影由下方传统阴影绘制（保持视觉一致）
                "border": {"width": 0},
            })

        for idx, (img_path, (cx, cy)) in enumerate(zip(item_images[:num_cards], card_boxes)):
            card_box = (cx, cy, cx + card_size, cy + card_size)
            prod_img = Image.open(img_path).convert("RGBA")

            if sticker_style == "cutout" and sticker_adapter is not None:
                # 去白底路径（2026-10-28 反馈：去除商品图四周白边，
                # 圆角 + 柔和羽化，主体清晰完整）：
                #   放大+锐化 → 去白底（flood fill，主体连通域保护）→
                #   圆角掩码（自身 2px 羽化）× matte alpha → 合成到直播画面
                prod = sticker_adapter._resize_to(prod_img,
                                                  (card_size, card_size))
                cut, matte_meta = ProductImageAdapter.remove_white_background(
                    prod,
                    white_thresh=int(cutout_c.get("white_thresh", 205) or 205),
                    sat_thresh=int(cutout_c.get("sat_thresh", 42) or 42),
                    feather_sigma=float(cutout_c.get("feather_sigma", 4.0) or 4.0),
                    erode_px=int(cutout_c.get("erode_px", 1) or 0),
                )
                radius_c = (int(cutout_c.get("corner_radius", 0) or 0)
                            or int(self.sticker_c["radius"]))
                corner = Image.new("L", (card_size, card_size), 0)
                cm_draw = ImageDraw.Draw(corner)
                cm_draw.rounded_rectangle([0, 0, card_size, card_size],
                                          radius=radius_c, fill=255)
                corner = corner.filter(ImageFilter.GaussianBlur(2))
                cut.putalpha(ImageChops.multiply(cut.split()[3], corner))

                # —— 贴纸描边（2026-10-28 反馈"商品贴图要贴上去"）——
                # 去底商品直接溶在背景里会读作"漂浮"而非"贴上"。
                # 沿剪影外扩一圈浅色描边（die-cut 贴纸的经典语言），
                # 让贴图明确读作贴在封面表面的元素。
                outline_c = cutout_c.get("outline", {}) or {}
                outline_applied = False
                if outline_c.get("enabled", True):
                    ow = max(1, int(outline_c.get("width", 3) or 3))
                    o_rgb = _hex_to_rgb(outline_c.get("color", "#FFFFFF"))
                    a_src = cut.split()[3]
                    dilated = a_src.filter(ImageFilter.MaxFilter(2 * ow + 1))
                    ring = ImageChops.subtract(dilated, a_src)
                    ring_layer = Image.new("RGBA", (card_size, card_size),
                                           o_rgb + (255,))
                    ring_layer.putalpha(ring)
                    overlay.alpha_composite(ring_layer, (cx, cy))
                    outline_applied = True

                # —— 剪影投影（跟随毛衣轮廓，而非矩形卡）——
                # 投影是"贴上去"最核心的视觉线索：拾起表面、悬浮于画面之上。
                sh_c = cutout_c.get("shadow", {}) or {}
                shadow_applied = False
                if sh_c.get("enabled", True):
                    off_y = int(sh_c.get("offset_y", 8) or 8)
                    blur = float(sh_c.get("blur", 7) or 7)
                    alpha_k = float(sh_c.get("alpha_scale", 0.42) or 0.42)
                    a_shift = Image.new("L", (card_size, card_size), 0)
                    a_shift.paste(cut.split()[3], (0, off_y))
                    a_blur = a_shift.filter(ImageFilter.GaussianBlur(blur))
                    a_sh = a_blur.point(lambda v: int(v * alpha_k))
                    sh_img = Image.new(
                        "RGBA", (card_size, card_size),
                        _hex_to_rgb(sh_c.get("color", "#1A0D07")) + (255,))
                    sh_img.putalpha(a_sh)
                    overlay.alpha_composite(sh_img, (cx, cy))
                    shadow_applied = True

                overlay.alpha_composite(cut, (cx, cy))
                meta["elements"][f"sticker_{idx+1}"] = {
                    "box": list(card_box), "side": side, "layout": layout,
                    "margin_a": margin_a, "size": card_size,
                    "style": "cutout",
                    "corner_radius": radius_c,
                    "outline": {"applied": outline_applied,
                                "width": int((cutout_c.get("outline", {}) or {}).get("width", 3) or 3)},
                    "silhouette_shadow": {"applied": shadow_applied,
                                          "offset_y": int((cutout_c.get("shadow", {}) or {}).get("offset_y", 8) or 8)},
                    "matte": {
                        "bg_removed_ratio": matte_meta["bg_removed_ratio"],
                        "subject_retention": matte_meta["subject_retention"],
                        "subject_coverage": matte_meta["subject_coverage"],
                        "feather_sigma": matte_meta["feather_sigma"],
                    },
                }
                continue

            # card / 旧路径：矩形投影 + 内容
            draw_rounded_rectangle_with_shadow(
                overlay, card_box, radius=int(self.sticker_c["radius"]),
                shadow_offset=(0, 6), shadow_blur=12, shadow_alpha=80)
            if sticker_adapter is not None:
                # 自然过渡路径：以当前合成预览为背景取色（渐变融合才准）
                preview = Image.alpha_composite(base, overlay)
                sticker_layer = sticker_adapter.render_foreground(
                    prod_img, card_box, preview, place_at=(cx, cy))
                overlay.alpha_composite(sticker_layer, (0, 0))
                # 描边（2026-10-28 反馈：浅色描边即"白边"，border_width=0 时关闭）
                if int(self.sticker_c.get("border_width", 0) or 0) > 0:
                    c_draw = ImageDraw.Draw(overlay)
                    border_rgb = _hex_to_rgb(self.sticker_c["border_color"])
                    inset = max(1, int(self.sticker_c["border_width"]) // 2 + 2)
                    c_draw.rounded_rectangle(
                        [cx + inset, cy + inset,
                         cx + card_size - 1 - inset, cy + card_size - 1 - inset],
                        radius=max(0, int(self.sticker_c["radius"]) - inset),
                        outline=border_rgb + (255,),
                        width=int(self.sticker_c["border_width"]))
            else:
                # 旧行为：硬圆角掩码直接贴（use_adapter_edges=false 时退化）
                prod_img = prod_img.resize((card_size, card_size), Image.LANCZOS)
                mask = Image.new("L", (card_size, card_size), 0)
                m_draw = ImageDraw.Draw(mask)
                m_draw.rounded_rectangle([0, 0, card_size, card_size],
                                         radius=int(self.sticker_c["radius"]), fill=255)
                overlay.paste(prod_img, (cx, cy), mask=mask)
                if int(self.sticker_c.get("border_width", 0) or 0) > 0:
                    c_draw = ImageDraw.Draw(overlay)
                    border_rgb = _hex_to_rgb(self.sticker_c["border_color"])
                    c_draw.rounded_rectangle(
                        [cx, cy, cx + card_size, cy + card_size],
                        radius=int(self.sticker_c["radius"]),
                        outline=border_rgb + (255,),
                        width=int(self.sticker_c["border_width"]))
            meta["elements"][f"sticker_{idx+1}"] = {
                "box": list(card_box), "side": side, "layout": layout,
                "margin_a": margin_a, "size": card_size,
            }

        # -------------------------------------------------------------
        # 3. 底部商品名（决策 2 金色渐变 + 名称区 80% 起 + 大字号复刻示例图）
        # -------------------------------------------------------------
        t1 = (product_info.get("title_line1") or "桑蚕丝羊绒羊毛").strip()
        t2 = (product_info.get("title_line2") or "渐变针织衫").strip()
        line_spacing = int(self.text_c["font_size_base"] * self.text_c["line_spacing_ratio"])
        stroke_w = int(self.text_c.get("product_name_stroke_width", 5))

        def render_prod_line(text: str, y_pos: int) -> int:
            """渲染一行金色渐变商品名，返回占用高度。"""
            lines, f = wrap_text_to_width(
                draw, text, self.font_prod_base, max_text_w,
                min_font_size=int(self.text_c["font_size_min"]),
                base_size=int(self.text_c["font_size_base"]))
            for li, line in enumerate(lines):
                strip, _ = render_gradient_sheared_text(
                    line, f, self.gradient_top, self.gradient_bottom,
                    COLOR_PRODUCT_STROKE, stroke_width=stroke_w,
                    shear=float(self.text_c["product_name_shear"]))
                pos_x = (OUT_W - strip.width) // 2
                overlay.alpha_composite(strip, (pos_x, y_pos + li * line_spacing))
            return len(lines) * line_spacing

        pos1_y = int(OUT_H * float(self.text_c.get("product_name_top_ratio", 0.80)))
        h1 = render_prod_line(t1, pos1_y)
        h2 = render_prod_line(t2, pos1_y + h1 + 8) if t2 else 0
        meta["elements"]["product_name"] = {
            "line1": t1, "line2": t2,
            "top_y": pos1_y, "bottom_y": pos1_y + h1 + h2,
            "line_spacing": line_spacing,
            "gradient": ["#%02X%02X%02X" % self.gradient_top[:3],
                         "#%02X%02X%02X" % self.gradient_bottom[:3]],
        }

        # -------------------------------------------------------------
        # 3.5 可读性渐变遮罩（2026-10-27：全幅底图下保证文字可读）
        # -------------------------------------------------------------
        base = apply_readability_scrims(base)

        # -------------------------------------------------------------
        # 4. 合成输出
        # -------------------------------------------------------------
        final_img = Image.alpha_composite(base, overlay).convert("RGB")
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        final_img.save(output_path, "JPEG", quality=95)

        if meta_output_path:
            os.makedirs(os.path.dirname(os.path.abspath(meta_output_path)), exist_ok=True)
            with open(meta_output_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

        print(f"[COVER v2] MIXCITY 风格封面已生成（金色渐变商品名）: {output_path}")
        return output_path, meta

    def render_cover(
        self,
        base_frame_path: str,
        brand_info: Dict[str, str],
        product_info: Dict[str, Any],
        output_path: str
    ) -> str:
        """兼容入口：仅返回路径。"""
        path, _ = self.render_cover_with_meta(
            base_frame_path, brand_info, product_info, output_path,
            meta_output_path=os.path.splitext(output_path)[0] + "_render_meta.json")
        return path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", default="job_spec.json")
    parser.add_argument("--bg", help="背景底图路径")
    parser.add_argument("--out", default="data/test_cover.jpg")
    args = parser.parse_args()

    spec_data = json.load(open(args.spec, encoding="utf-8")) if os.path.exists(args.spec) else {}
    renderer = MixCityCoverRenderer()
    bg_path = args.bg
    if not bg_path or not os.path.exists(bg_path):
        bg_path = "data/raw_samples/test_frame.jpg"
        if not os.path.exists(bg_path):
            test_bg = Image.new("RGBA", (OUT_W, OUT_H), (45, 52, 65, 255))
            test_bg.save(bg_path)
    renderer.render_cover(
        base_frame_path=bg_path,
        brand_info=spec_data.get("brand", {"title_en": "MIXCITY", "subtitle_cn": "深圳高端女装"}),
        product_info=spec_data.get("product", {
            "title_line1": "桑蚕丝羊绒羊毛",
            "title_line2": "渐变针织衫",
            "item_images": ["data/raw_samples/sample_product_card.png"]
        }),
        output_path=args.out
    )
