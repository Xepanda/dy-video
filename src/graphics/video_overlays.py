#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
video_overlays.py - 视频画面叠加元素渲染器
============================================
2026-10-24 新增。为视频（区别于封面）提供四类叠加元素：

1. `generate_platform_bar` —— 顶部平台/店铺账号标识条（常驻水印区）
2. `generate_stage_sticker` —— 分镜挂角利益点徽标（带出现/消失时序）
3. `generate_occlusion_patch` —— 镜像后背景反向文字的遮挡贴纸（SKILL 2.1）
4. `generate_split_labels` —— 分屏左右两侧的独立胶囊标签（SKILL 2.3）

所有阈值/尺寸/配色来自 contracts/constraints.yaml，本模块不硬编码业务参数。
"""
import os
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

# 字体候选（Windows 优先，缺失时回退 PIL 默认位图字体）
_FONT_CANDIDATES_BOLD = [
    "C:/Windows/Fonts/msyhbd.ttc",   # 微软雅黑 Bold
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
]
_FONT_CANDIDATES_REGULAR = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]


def _load_font(candidates: List[str], size: int) -> ImageFont.FreeTypeFont:
    """加载第一个可用的 TrueType 字体；都没有则回退默认字体（不抛异常）。"""
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _hex_to_rgb(hex_color: str, default=(255, 255, 255)) -> Tuple[int, int, int]:
    h = (hex_color or "").strip().lstrip("#")
    if len(h) == 6:
        try:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        except ValueError:
            return default
    return default


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> Tuple[int, int]:
    if not text:
        return (0, 0)
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return (bbox[2] - bbox[0], bbox[3] - bbox[1])
    except AttributeError:            # 老版本 PIL
        bbox = draw.textsize(text, font=font)
        return (bbox[0], bbox[1])


# ---------------------------------------------------------------- 1. 顶部平台标识条

def generate_platform_bar(
    out_path: str,
    bar_c: Dict[str, Any],
    shop_name: str = "",
    account: str = "",
) -> str:
    """生成视频顶部平台/店铺账号标识条（常驻水印区）。

    布局（1080 宽）：
      [圆形图标] │ 店铺名（白）        @账号（金色，靠右）
    整条深色半透明底，y=0 起算，高度取 constraints.platform_bar.height。
    """
    width = 1080
    height = int(bar_c.get("height", 88) or 88)
    pad_x = int(bar_c.get("padding_x", 28) or 28)
    font_size = int(bar_c.get("font_size", 34) or 34)
    show_icon = bool(bar_c.get("show_icon", True))
    icon_size = int(bar_c.get("icon_size", 44) or 44)

    bg = _hex_to_rgb(bar_c.get("background", "#101014"), (16, 16, 20))
    alpha = int(bar_c.get("background_alpha", 150) or 150)
    text_rgb = _hex_to_rgb(bar_c.get("text_color", "#FFFFFF"), (255, 255, 255))
    accent = _hex_to_rgb(bar_c.get("accent_color", "#E2C98A"), (226, 201, 138))

    img = Image.new("RGBA", (width, height), bg + (alpha,))
    d = ImageDraw.Draw(img)
    f = _load_font(_FONT_CANDIDATES_BOLD, font_size)

    x = pad_x
    if show_icon:
        # 圆形店铺图标（强调色描边 + 深色填充）
        cy = height // 2
        r = icon_size // 2
        d.ellipse([x, cy - r, x + icon_size, cy + r], fill=accent + (255,))
        # 图标内的首字（品牌色）
        if shop_name:
            ch = shop_name[0]
            tw, th = _text_size(d, ch, f)
            d.text((x + (icon_size - tw) // 2, cy - th // 2 - 2), ch,
                   font=f, fill=bg + (255,))
        x += icon_size + 16

    # 店铺名（白）
    if shop_name:
        d.text((x, height // 2), shop_name, font=f, fill=text_rgb + (255,),
               anchor="lm")
    # 账号（强调色，靠右）
    if account:
        d.text((width - pad_x, height // 2), account, font=f, fill=accent + (255,),
               anchor="rm")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------- 2. 挂角利益点徽标

def generate_stage_sticker(
    text: str,
    out_path: str,
    sticker_c: Dict[str, Any],
) -> str:
    """生成分镜挂角利益点徽标（正方形金底胶囊文字）。

    与封面商品贴图区分：这是**视频内的卖点贴纸**（如"显瘦""三M新雪丽"），
    按 SKILL 的"阶段式利益点挂角徽标"定位在画面右上/左上的空白区。
    """
    size = int(sticker_c.get("size", 220) or 220)
    font_size = int(sticker_c.get("font_size", 40) or 40)
    bg = _hex_to_rgb(sticker_c.get("bg_color", "#FFE9A8"), (255, 233, 168))
    border = _hex_to_rgb(sticker_c.get("border_color", "#E8A91E"), (232, 169, 30))
    text_rgb = _hex_to_rgb(sticker_c.get("text_color", "#3D1F0E"), (61, 31, 14))

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 金底圆角方块 + 深色描边
    d.rounded_rectangle([2, 2, size - 2, size - 2], radius=24,
                        fill=bg + (255,), outline=border + (255,), width=4)
    f = _load_font(_FONT_CANDIDATES_BOLD, font_size)
    if text:
        # 逐字换行居中
        lines, cur = [], ""
        for ch in text:
            trial = cur + ch
            if _text_size(d, trial, f)[0] > size - 28 and cur:
                lines.append(cur)
                cur = ch
            else:
                cur = trial
        if cur:
            lines.append(cur)
        lines = lines[:4]
        lh = font_size + 6
        y0 = (size - lh * len(lines)) // 2
        for i, ln in enumerate(lines):
            tw, th = _text_size(d, ln, f)
            d.text(((size - tw) // 2, y0 + i * lh), ln, font=f,
                   fill=text_rgb + (255,))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------- 3. 镜像防反遮挡贴纸

def generate_occlusion_patch(
    out_path: str,
    width: int,
    height: int,
    regions: List[Tuple[int, int, int, int]],
    patch_c: Dict[str, Any],
) -> str:
    """生成遮挡贴纸：覆盖镜像后反向的背景文字区域（SKILL 2.1）。

    regions: [(x0, y0, x1, y1), ...] 像素坐标（成片坐标系 1080x1920）。
    用深色圆角块覆盖，视觉上表现为"品名遮罩"，避免出现反向文字。
    """
    color = _hex_to_rgb(patch_c.get("patch_color", "#1A1A1E"), (26, 26, 30))
    alpha = int(patch_c.get("patch_alpha", 235) or 235)
    radius = int(patch_c.get("patch_radius", 14) or 14)

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for (x0, y0, x1, y1) in regions:
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(width, int(x1)), min(height, int(y1))
        if x1 - x0 < 8 or y1 - y0 < 8:
            continue
        d.rounded_rectangle([x0, y0, x1, y1], radius=radius,
                            fill=color + (alpha,))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------- 4. 分屏双侧标签

def generate_split_labels(
    out_path: str,
    left_text: str,
    right_text: str,
    split_c: Dict[str, Any],
    half_w: int = 540,
    height: int = 1920,
) -> str:
    """生成分屏左右两侧的独立胶囊标签（SKILL 2.3）。

    输出一张 1080x1920 的透明 PNG：左半屏画左标签、右半屏画右标签，
    位置在中缝分割线两侧各留 label_gap。
    """
    font_size = int(split_c.get("label_font_size", 30) or 30)
    text_rgb = _hex_to_rgb(split_c.get("label_text_color", "#3D1F0E"), (61, 31, 14))
    bg = _hex_to_rgb(split_c.get("label_bg_color", "#FFE9A8"), (255, 233, 168))
    padding = int(split_c.get("label_padding", 14) or 14)
    gap = int(split_c.get("label_gap", 24) or 24)
    divider_w = int(split_c.get("divider_width", 8) or 8)

    img = Image.new("RGBA", (half_w * 2, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = _load_font(_FONT_CANDIDATES_BOLD, font_size)

    mid = half_w
    # 左标签：右对齐到分割线左侧
    if left_text:
        tw, th = _text_size(d, left_text, f)
        bw, bh = tw + padding * 2, th + padding
        bx1 = mid - divider_w // 2 - gap
        by0 = 120
        d.rounded_rectangle([bx1 - bw, by0, bx1, by0 + bh], radius=bh // 2,
                            fill=bg + (255,))
        d.text((bx1 - bw + padding, by0 + padding // 2), left_text, font=f,
               fill=text_rgb + (255,))
    # 右标签：左对齐到分割线右侧
    if right_text:
        tw, th = _text_size(d, right_text, f)
        bw, bh = tw + padding * 2, th + padding
        bx0 = mid + divider_w // 2 + gap
        by0 = 120
        d.rounded_rectangle([bx0, by0, bx0 + bw, by0 + bh], radius=bh // 2,
                            fill=bg + (255,))
        d.text((bx0 + padding, by0 + padding // 2), right_text, font=f,
               fill=text_rgb + (255,))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------- 放大镜圆圈（2026-10-27 反馈驱动）

def generate_magnifier_mask(out_path: str, w: int, h: int,
                            cx: int, cy: int, r: int) -> str:
    """生成全幅圆形 alpha 掩码（黑底白圈，圈外全透明）。

    与 zoompan 推镜区别：本掩码使放大内容只在圈内显示，
    主画面保持完整构图（真正的「放大镟」）。
    """
    img = Image.new("L", (int(w), int(h)), 0)
    d = ImageDraw.Draw(img)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


def generate_magnifier_ring(out_path: str, r: int, ring_width: int = 8,
                            ring_color: str = "#E8A91E") -> Tuple[str, int]:
    """生成放大镟金属环 PNG（透明底 + 圈环 + 右下手柄）。

    返回 (png 路径, PNG 边长)；边长 = 2r + 2*pad，超出圈体的部分用于
    环与手柄，超出部分全透明（overlay 时不影响画面）。
    """
    rgb = tuple(int(ring_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    pad = int(ring_width * 3 + 44)
    size = 2 * r + 2 * pad
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = size // 2
    # 主金环（外）
    d.ellipse([c - r - ring_width, c - r - ring_width,
               c + r + ring_width, c + r + ring_width],
              outline=rgb + (255,), width=ring_width)
    # 内部细亮线（片层教感）
    d.ellipse([c - r - 3, c - r - 3, c + r + 3, c + r + 3],
              outline=(255, 240, 200, 150), width=2)
    # 右下 45° 手柄
    h0x, h0y = c + int(r * 0.70), c + int(r * 0.70)
    h1x, h1y = c + r + pad - 10, c + r + pad - 10
    d.line([h0x, h0y, h1x, h1y], fill=rgb + (255,), width=ring_width + 8)
    d.ellipse([h1x - (ring_width + 8) // 2, h1y - (ring_width + 8) // 2,
               h1x + (ring_width + 8) // 2, h1y + (ring_width + 8) // 2],
              fill=rgb + (255,))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path, "PNG")
    return out_path, size
