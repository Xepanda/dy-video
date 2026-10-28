#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
product_image_adapter.py - 商品图封面适配器（可复用 · 反馈驱动 2026-10-28）
=================================================================================
**要解决的问题**：商品图（尤其是带深色边缘的实拍缩略图）直接作为封面或封面大图
时，边缘会出现生硬的矩形边界（"贴纸感"），与封面整体风格割裂。

**方案**：对商品图四周做自然过渡处理，全部强度/样式/留白可配置：
  1. 背景延展（extend）  —— 商品图放大 + 镜像延展 + 高斯模糊，让商品图的色彩
                           场溢出填满画布，封面底色由商品图自身派生；
  2. 边缘羽化（feather）  —— 距离变换生成软 alpha 边（圆角同步软化），宽度可配；
  3. 渐变融合（blend）    —— 羽化带内 RGB 向背景色插值 + 边缘去晕（de-halo），
                           根除深色商品图贴在浅色封面上的"脏边/暗 halo"；
  4. 阴影（shadow）       —— 高斯模糊圆角投影，参数（模糊/偏移/浓度/色）可配；
  5. 圆角（corner）       —— 圆角半径可配（0=直角）。

**四种模式**（mode）：
  card    —— 圆角卡片 + 描边 + 阴影（兼容既有贴图行为，用于小尺寸商品贴图）；
  blend   —— 商品图柔和融入纯色/渐变背景（推荐：商品图作为封面主体）；
  extend  —— 商品图色彩延展为全幅背景 + 商品图主体柔边居中（沉浸式封面）；
  collage —— 大尺寸商品图重羽化 + 延展背景（杂志拼贴风）。

**适配能力**：任意输出尺寸（canvas_size）、任意背景（color/gradient/extend）、
任意商品图尺寸（自动 LANCZOS 缩放），原有 Alpha 通道受尊重。

**输出**：AdaptationResult（background / foreground / canvas 三层 + meta），
供 cover_renderer 继续叠加品牌文字，或直接作为封面底图。

**配置来源**：contracts/constraints.yaml 的 product_image_adapter 段（v1.6），
本模块 DEFAULTS 仅作缺省。
"""

import os
import sys
import json
import math
from typing import List, Dict, Any, Optional, Tuple, Union

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------- 默认配置

DEFAULTS: Dict[str, Any] = {
    "mode": "blend",                 # card | blend | extend | collage
    "canvas_size": [1080, 1920],     # 输出画布（宽, 高）
    # —— 尺寸与留白 -—
    "margin_px": 96,                 # 商品图四周留白（封面安全边距）
    "scale_mode": "fit",             # fit（完整放入）| fill（裁切填充）| stretch（拉伸）
    "max_subject_ratio": 0.72,       # 商品图主体区占画布短边的比例上限
    # —— 边缘样式 -—
    "edge_style": "gradient",        # gradient（渐变融合）| hard（硬边）| rounded（仅圆角）
    "corner_radius": 28,             # 圆角半径 px（0=直角）
    "feather_px": 48,                # 边缘羽化宽度 px（0=硬边）
    "blend_ratio": 1.0,              # 渐变融合强度 0~1（缩放羽化宽度）
    "dehalo": True,                  # 边缘去晕（深色商品图贴浅背景的脏边清除）
    "dehalo_blur_ratio": 0.5,        # 去晕模糊半径 = feather_px × 此比例
    # —— 阴影 -—
    "shadow": {
        "enabled": True,
        "blur": 24,                  # 阴影模糊半径 px
        "offset_x": 0,
        "offset_y": 10,              # 垂直偏移 px（光源在上方）
        "alpha": 90,                 # 阴影不透明度 0~255
        "color": "#1A0D07",          # 阴影色（深褐，与品牌描边同源）
    },
    # —— 背景 -—
    "background": {
        "type": "gradient",          # color | gradient | extend
        "color": "#F5EFE8",          # type=color
        "gradient": ["#F5EFE8", "#E8D9C8"],   # type=gradient（顶→底）
        # type=extend（背景延展）专用：
        "extend_method": "mirror",   # mirror（镜像延展）| replicate（边缘复制）
        "extend_scale": 1.25,        # 延展放大系数（>1 保证模糊后无露边）
        "extend_blur": 60,           # 延展背景的高斯模糊半径 px
        "darken": 0.12,              # 延展背景压暗比例（0~0.5，提升文字可读性）
        "saturate": 1.05,            # 延展背景饱和度微增
    },
    # —— collage 模式专用 -—
    "collage": {
        "subject_ratio": 0.88,       # 商品图占画布短边比例（大图）
        "feather_px": 90,            # 重羽化
        "rotate_deg": 0.0,           # 轻微旋转（杂志感，0=不旋转）
    },
    # —— 描边（card 模式默认细描边，其余默认无）——
    "border": {
        "width": 0,                  # 描边宽度 px（0=无描边）
        "color": "#E2E8F0",          # 描边色
    },
}

VALID_MODES = ("card", "blend", "extend", "collage")

# mode 驱动的形态默认层（2026-10-28）：
# 优先级 DEFAULTS < constraints < MODE_DEFAULTS[mode] < 用户 config。
# mode 是调用方的显式形态意图，应能覆盖 constraints 的通用缺省；
# 用户 config（--set / API 参数）始终最高。
MODE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    # 圆角商品卡：较小主体 + 轻羽化 + 大圆角 + 描边 + 阴影（经典贴图感）
    "card": {
        "max_subject_ratio": 0.60,
        "feather_px": 20,
        "corner_radius": 36,
        "shadow": {"enabled": True, "blur": 28, "offset_y": 12, "alpha": 100},
        "border": {"width": 3, "color": "#E2E8F0"},
    },
    # 柔边融入：商品图作为封面主体，中度羽化融入渐变/纯色背景（推荐默认）
    "blend": {
        "max_subject_ratio": 0.72,
        "feather_px": 48,
        "corner_radius": 28,
        "shadow": {"enabled": True, "blur": 24, "offset_y": 10, "alpha": 90},
        "border": {"width": 0, "color": "#E2E8F0"},
    },
    # 沉浸延展：商品图色彩延展为全幅背景 + 主体重羽化居中，无阴影（背景即延展）
    "extend": {
        "background": {"type": "extend"},
        "max_subject_ratio": 0.64,
        "feather_px": 64,
        "corner_radius": 40,
        "shadow": {"enabled": False},
        "border": {"width": 0, "color": "#E2E8F0"},
    },
    # 杂志拼贴：大尺寸重羽化，无阴影无描边
    "collage": {
        "max_subject_ratio": 0.88,
        "feather_px": 90,
        "corner_radius": 60,
        "shadow": {"enabled": False},
        "border": {"width": 0, "color": "#E2E8F0"},
    },
}
VALID_EDGE_STYLES = ("gradient", "hard", "rounded")
VALID_SCALE_MODES = ("fit", "fill", "stretch")
VALID_BG_TYPES = ("color", "gradient", "extend")


def _hex_to_rgb(h: str) -> Tuple[int, int, int]:
    h = (h or "").strip().lstrip("#")
    if len(h) == 6:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (245, 239, 232)


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并配置（override 优先）。"""
    out = json.loads(json.dumps(base))
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_constraints(path: str = None) -> Dict[str, Any]:
    path = path or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "contracts", "constraints.yaml")
    if not os.path.exists(path):
        return {}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------- 核心适配器

class AdaptationResult:
    """适配结果：三层输出 + 几何 meta。"""

    def __init__(self, background: Image.Image, foreground: Image.Image,
                 canvas: Image.Image, meta: Dict[str, Any]):
        self.background = background    # 全幅 RGBA 背景层（延展/纯色/渐变）
        self.foreground = foreground    # 全幅 RGBA 前景层（适配后的商品图）
        self.canvas = canvas            # 合成成品（RGBA）
        self.meta = meta

    def save(self, path: str, quality: int = 95) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.canvas.convert("RGB").save(path, "JPEG", quality=quality)
        return path


class ProductImageAdapter:
    """商品图封面适配器（可复用、全参数可配置）。"""

    def __init__(self, config: Dict[str, Any] = None,
                 constraints: Dict[str, Any] = None):
        # 合并层次：DEFAULTS < constraints < MODE_DEFAULTS[mode] < 用户 config
        # （mode 先由 用户>constraints>DEFAULTS 解析，再以 MODE_DEFAULTS 覆盖
        #   constraints 的通用缺省——mode 是显式形态意图）
        user_cfg = config or {}
        cons_cfg = (constraints or {}).get("product_image_adapter", {}) or {}
        mode = (user_cfg.get("mode") or cons_cfg.get("mode")
                or DEFAULTS["mode"])
        if mode not in VALID_MODES:
            raise ValueError(f"mode 必须是 {VALID_MODES}，得到 {mode}")
        merged = _deep_merge(DEFAULTS, cons_cfg)
        merged = _deep_merge(merged, MODE_DEFAULTS.get(mode, {}))
        merged["mode"] = mode
        self.cfg = _deep_merge(merged, user_cfg)
        self.cfg["mode"] = mode
        # 参数校验与收敛
        if self.cfg["mode"] not in VALID_MODES:
            raise ValueError(f"mode 必须是 {VALID_MODES}，得到 {self.cfg['mode']}")
        if self.cfg["edge_style"] not in VALID_EDGE_STYLES:
            raise ValueError(f"edge_style 必须是 {VALID_EDGE_STYLES}")
        if self.cfg["scale_mode"] not in VALID_SCALE_MODES:
            raise ValueError(f"scale_mode 必须是 {VALID_SCALE_MODES}")
        bg_type = self.cfg["background"]["type"]
        if bg_type not in VALID_BG_TYPES:
            raise ValueError(f"background.type 必须是 {VALID_BG_TYPES}")
        self.cfg["blend_ratio"] = float(min(max(self.cfg["blend_ratio"], 0.0), 1.0))
        self.cfg["feather_px"] = float(max(0.0, self.cfg["feather_px"]))
        self.cfg["margin_px"] = int(max(0, self.cfg["margin_px"]))

    # ------------------------------------------------------------ 公开入口

    def adapt(self, product_image: Union[str, Image.Image],
              canvas_size: Tuple[int, int] = None,
              background: Union[str, Image.Image] = None) -> AdaptationResult:
        """执行适配。

        product_image: 商品图路径或 PIL Image（RGBA/RGB 均可）。
        canvas_size:   输出画布（缺省用配置）。
        background:    可选自定义背景图/路径（覆盖配置的背景类型，
                       用于"适配不同封面尺寸与背景"）。
        """
        W, H = int(canvas_size[0]) if canvas_size else int(self.cfg["canvas_size"][0]), \
            int(canvas_size[1]) if canvas_size else int(self.cfg["canvas_size"][1])
        mode = self.cfg["mode"]

        img = self._load_image(product_image)
        bg_layer = self._build_background(img, (W, H), background)

        # —— 商品图主体框（按模式与留白计算）——
        box = self._subject_box(img, (W, H))
        fg_rgba = self.render_foreground(img, box, bg_layer)

        # —— 阴影层（card/blend/extend 默认有；collage 重羽化可省）——
        shadow_layer = self._render_shadow(box, (W, H))

        # —— 合成 -—
        canvas = bg_layer.copy()
        if shadow_layer is not None:
            canvas.alpha_composite(shadow_layer, (0, 0))
        canvas.alpha_composite(fg_rgba, (0, 0))

        meta = {
            "mode": mode,
            "canvas_size": [W, H],
            "subject_box": list(box),
            "subject_size": [box[2] - box[0], box[3] - box[1]],
            "margin_px": self.cfg["margin_px"],
            "feather_px_effective": self._effective_feather(),
            "corner_radius": self.cfg["corner_radius"],
            "edge_style": self.cfg["edge_style"],
            "blend_ratio": self.cfg["blend_ratio"],
            "shadow": dict(self.cfg["shadow"], enabled=shadow_layer is not None),
            "background_type": ("custom" if background is not None
                                else self.cfg["background"]["type"]),
            "source_image_size": list(img.size),
            # 完整生效配置（验证工具据此复现适配，保证可审计）
            "config": self.cfg,
        }
        return AdaptationResult(bg_layer, fg_rgba, canvas, meta)

    # ------------------------------------------------------------ 图像装载

    @staticmethod
    def _load_image(src: Union[str, Image.Image]) -> Image.Image:
        if isinstance(src, Image.Image):
            img = src
        else:
            if not os.path.exists(src):
                raise FileNotFoundError(f"商品图不存在: {src}")
            img = Image.open(src)
        img = img.convert("RGBA")
        # 统一方向信息（EXIF）在 PIL 中默认不自动应用，封面场景保持原样即可
        return img

    # ------------------------------------------------------------ 背景构建

    def _build_background(self, img: Image.Image, canvas_size: Tuple[int, int],
                          custom: Union[str, Image.Image] = None) -> Image.Image:
        """构建全幅背景层：自定义 > extend（延展）> gradient > color。"""
        W, H = canvas_size
        bgc = self.cfg["background"]

        # 1) 自定义背景（适配不同背景的直接注入点）
        if custom is not None:
            bg = custom if isinstance(custom, Image.Image) else Image.open(custom)
            bg = bg.convert("RGBA")
            return self._cover_resize(bg, (W, H))

        btype = bgc["type"]
        if btype == "color":
            return Image.new("RGBA", (W, H), _hex_to_rgb(bgc["color"]) + (255,))

        if btype == "gradient":
            return self._gradient_background(bgc["gradient"], (W, H))

        # btype == "extend"：商品图背景延展
        return self._extend_background(img, (W, H))

    def _gradient_background(self, colors: List[str],
                             canvas_size: Tuple[int, int]) -> Image.Image:
        """线性垂直渐变（顶→底），色标可扩展。"""
        W, H = canvas_size
        stops = [_hex_to_rgb(c) for c in (colors or ["#F5EFE8", "#E8D9C8"])]
        if len(stops) == 1:
            return Image.new("RGBA", (W, H), stops[0] + (255,))
        grad = np.zeros((H, W, 3), dtype=np.uint8)
        for y in range(H):
            t = y / max(1, H - 1)
            seg = t * (len(stops) - 1)
            i = min(int(seg), len(stops) - 2)
            f = seg - i
            c0, c1 = np.array(stops[i], dtype=float), np.array(stops[i + 1], dtype=float)
            grad[y, :, :] = (c0 + (c1 - c0) * f).astype(np.uint8)
        return Image.fromarray(grad, "RGB").convert("RGBA")

    def _extend_background(self, img: Image.Image,
                           canvas_size: Tuple[int, int]) -> Image.Image:
        """背景延展：商品图放大 → 边缘镜像延展 → 高斯模糊 → 可选压暗/增饱和。

        原理：先放大到超过画布（extend_scale），超出部分用镜像/复制延展填满，
        再大半径高斯模糊——商品图的色彩场自然溢出填满画布，且延展处无接缝。
        """
        W, H = canvas_size
        bgc = self.cfg["background"]
        method = bgc.get("extend_method", "mirror")
        scale = float(bgc.get("extend_scale", 1.25))
        blur = float(bgc.get("extend_blur", 60))

        # 1) 覆盖式放大到超过画布
        tw, th = int(W * scale), int(H * scale)
        cover = self._cover_resize(img, (tw, th))

        # 2) 镜像/复制延展：把 cover 的四边像素向外复制，保证模糊后无露边
        pad = int(max(tw, th) * 0.5) + 8
        ext = self._extend_edges(cover, pad, method)

        # 3) 大半径模糊（先缩放加速：模糊在 1/4 尺寸上做再放大，视觉无损）
        small = ext.resize((max(1, ext.width // 4), max(1, ext.height // 4)),
                           Image.LANCZOS)
        small = small.filter(ImageFilter.GaussianBlur(max(1.0, blur / 4.0)))
        ext = small.resize(ext.size, Image.LANCZOS)

        # 4) 裁回画布尺寸（居中）
        left = (ext.width - W) // 2
        top = (ext.height - H) // 2
        bg = ext.crop((left, top, left + W, top + H))

        # 5) 饱和度微增 + 压暗（文字可读性）
        sat = float(bgc.get("saturate", 1.05))
        if sat and abs(sat - 1.0) > 1e-3:
            arr = np.array(bg.convert("RGB")).astype(float)
            gray = arr.mean(axis=2, keepdims=True)
            arr = np.clip(gray + (arr - gray) * sat, 0, 255)
            bg = Image.fromarray(arr.astype(np.uint8), "RGB").convert("RGBA")
        darken = float(bgc.get("darken", 0.0))
        if darken > 0:
            bg = self._darken(bg, darken)
        return bg

    @staticmethod
    def _extend_edges(img: Image.Image, pad: int, method: str) -> Image.Image:
        """边缘延展：把 img 四边的连续带向外延伸 pad 像素。

        method=mirror    —— 取边缘邻近带翻转后拉伸（内容方向的镜像延续，
                           色彩地壳自然延续）；
        method=replicate —— 边缘 1px 条带直接拉伸（纯色块延续）。
        四角用角落像素色填充（模糊后不可见）。
        全程 PIL 原生操作，无 numpy 广播形状陷阵。
        """
        w, h = img.size
        pad = int(max(1, pad))
        out = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
        out.paste(img, (pad, pad))

        def _band_v(src_img, y0, y1, target_h):
            """水平带：取 [y0,y1) 行区域，按 method 处理后拉伸到 target_h。"""
            band = src_img.crop((0, y0, w, y1))
            if method == "mirror":
                band = band.transpose(Image.FLIP_TOP_BOTTOM)
            return band.resize((w, max(1, target_h)), Image.LANCZOS)

        def _band_h(src_img, x0, x1, target_w):
            band = src_img.crop((x0, 0, x1, h))
            if method == "mirror":
                band = band.transpose(Image.FLIP_LEFT_RIGHT)
            return band.resize((max(1, target_w), h), Image.LANCZOS)

        # 四边带（内容方向取带：mirror 时翻转后是“向外镜像”）
        top_band = _band_v(img, 0, min(h, pad), pad)
        bot_band = _band_v(img, max(0, h - min(h, pad)), h, pad)
        left_band = _band_h(img, 0, min(w, pad), pad)
        right_band = _band_h(img, max(0, w - min(w, pad)), w, pad)
        out.paste(top_band, (pad, 0))
        out.paste(bot_band, (pad, pad + h))
        out.paste(left_band, (0, pad))
        out.paste(right_band, (pad + w, pad))
        # 四角：角落像素色
        corners = {
            (0, 0): img.getpixel((0, 0)),
            (w + pad, 0): img.getpixel((w - 1, 0)),
            (0, h + pad): img.getpixel((0, h - 1)),
            (w + pad, h + pad): img.getpixel((w - 1, h - 1)),
        }
        for (cx, cy), color in corners.items():
            cw = pad if cx == 0 else pad
            ch = pad if cy == 0 else pad
            patch = Image.new("RGBA", (cw, ch), color)
            out.paste(patch, (cx, cy))
        return out

    @staticmethod
    def _darken(img: Image.Image, ratio: float) -> Image.Image:
        arr = np.array(img.convert("RGB")).astype(float)
        arr = arr * (1.0 - float(ratio))
        return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB").convert("RGBA")

    # ------------------------------------------------------------ 主体框

    def _subject_box(self, img: Image.Image,
                     canvas_size: Tuple[int, int]) -> Tuple[int, int, int, int]:
        """计算商品图主体框（留白 + 模式比例 + 缩放模式）。"""
        W, H = canvas_size
        mode = self.cfg["mode"]
        margin = self.cfg["margin_px"]
        if mode == "collage":
            ratio = float(self.cfg["collage"]["subject_ratio"])
        else:
            ratio = float(self.cfg["max_subject_ratio"])
        # 可用区域
        avail_w = max(1, W - margin * 2)
        avail_h = max(1, H - margin * 2)
        # 目标短边占比
        target_short = int(min(W, H) * ratio)
        iw, ih = img.size
        if self.cfg["scale_mode"] == "stretch":
            bw, bh = avail_w, avail_h
        elif self.cfg["scale_mode"] == "fill":
            s = max(target_short / min(iw, ih), 1.0) if min(iw, ih) > 0 else 1.0
            s = max(s, avail_w / iw, avail_h / ih)
            bw, bh = int(iw * s), int(ih * s)
        else:  # fit：完整放入，受短边比例约束
            s = min(target_short / max(1, min(iw, ih)),
                    avail_w / max(1, iw), avail_h / max(1, ih))
            s = max(s, 0.01)
            bw, bh = max(1, int(iw * s)), max(1, int(ih * s))
        # 居中
        x0 = (W - bw) // 2
        y0 = (H - bh) // 2
        return (int(x0), int(y0), int(x0 + bw), int(y0 + bh))

    # ------------------------------------------------------------ 前景渲染

    def _effective_feather(self) -> float:
        """有效羽化宽度（collage 用自带值；blend_ratio 缩放）。"""
        mode = self.cfg["mode"]
        if mode == "collage":
            base = float(self.cfg["collage"].get("feather_px", 90))
        elif self.cfg["edge_style"] == "hard":
            base = 0.0
        else:
            base = float(self.cfg["feather_px"])
        return base * float(self.cfg["blend_ratio"])

    def render_foreground(self, img: Image.Image, box: Tuple[int, int, int, int],
                          bg_layer: Image.Image,
                          place_at: Tuple[int, int] = None) -> Image.Image:
        """渲染全幅前景层：缩放 →（旋转）→ 圆角 → 去晕 → 羽化 → 放置。

        place_at: 显式指定前景左上角坐标（缺省居中）。
                  供封面右侧贴图等非居中场景复用。
        """
        W, H = bg_layer.size
        x0, y0, x1, y1 = box
        bw, bh = x1 - x0, y1 - y0
        feather = self._effective_feather()
        radius = float(self.cfg["corner_radius"])

        # 1) 缩放到主体框
        fg = self._resize_to(img, (bw, bh))

        # 2) collage 可选旋转
        rot = float(self.cfg["collage"].get("rotate_deg", 0.0))
        if self.cfg["mode"] == "collage" and abs(rot) > 0.01:
            fg = fg.rotate(rot, resample=Image.BICUBIC, expand=True)

        # 3) 去晕：软化商品图自身生硬的深色/亮色边框（先于羽化）
        if self.cfg.get("dehalo", True) and feather > 2:
            fg = self._soften_edges(fg, feather)

        # 4) 圆角 + 羽化 alpha 掩码（距离变换 → 平滑软边）
        mask = self._edge_mask(fg.size, radius, feather)

        # 5) 渐变融合：羽化带内 RGB 向背景色插值（防 halo 双保险）
        if self.cfg["edge_style"] == "gradient" and feather > 2 and self.cfg["blend_ratio"] > 0:
            fg = self._blend_edges_to_bg(fg, mask, bg_layer, box)

        # 6) 描边（card 模式默认；随羽化边缘自然淡化）
        border_c = self.cfg.get("border", {}) or {}
        bw = int(border_c.get("width", 0) or 0)
        if bw > 0:
            bd = ImageDraw.Draw(fg)
            inset = max(1, bw // 2)
            bd.rounded_rectangle(
                [inset, inset, fg.width - 1 - inset, fg.height - 1 - inset],
                radius=max(0, int(radius) - inset),
                outline=_hex_to_rgb(border_c.get("color", "#E2E8F0")) + (255,),
                width=bw)

        # 7) 组装全幅前景层（place_at 支持非居中放置）
        fg.putalpha(mask)
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if place_at is not None:
            px, py = int(place_at[0]), int(place_at[1])
        else:
            px = (W - fg.width) // 2
            py = (H - fg.height) // 2
        layer.alpha_composite(fg, (px, py))
        return layer

    @staticmethod
    def _cover_resize(img: Image.Image, size: Tuple[int, int]) -> Image.Image:
        """CSS object-fit: cover（裁切填充，不变形）。"""
        W, H = size
        s = max(W / img.width, H / img.height)
        nw, nh = max(1, int(img.width * s)), max(1, int(img.height * s))
        r = img.resize((nw, nh), Image.LANCZOS)
        left = (nw - W) // 2
        top = (nh - H) // 2
        return r.crop((left, top, left + W, top + H))

    @staticmethod
    def remove_white_background(img_rgba: Image.Image,
                                white_thresh: int = 205,
                                sat_thresh: int = 42,
                                ring_px: int = 3,
                                feather_sigma: float = 4.0,
                                erode_px: int = 1
                                ) -> Tuple["Image.Image", Dict[str, Any]]:
        """白底商品图去底（2026-10-28 反馈驱动）。

        需求：商品图贴到封面右侧时去除四周白边，主体完整、边缘柔和。
        算法（flood fill 连通域语义，保护主体）：
          1. 候选背景 = 近白像素（高亮度 + 低饱和度）
             ∪ 外框深色环（仅最外 ring_px 像素，防止深色商品内部被误伤）；
          2. 仅移除与图像边界**连通**的候选分量
             ——主体内部的淡色/白色区域绝对保留；
          3. alpha 轻微腐蚀（去光晕）→ 高斯羽化；
          4. 圆角由调用方（封面渲染器）叠加。
        返回 (RGBA 图, matte meta)。
        """
        import numpy as np
        from scipy.ndimage import label, binary_erosion, gaussian_filter

        arr = np.array(img_rgba.convert("RGBA"))
        rgb = arr[..., :3].astype(np.int32)
        lum = (0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1]
               + 0.0722 * rgb[..., 2])
        sat = rgb.max(axis=2) - rgb.min(axis=2)
        h, w = lum.shape

        white = (lum > float(white_thresh)) & (sat < float(sat_thresh))
        # 外框深色环（缩略图拉伸遗留的深色边）：
        # 只把最多 ring_px 像素内的暗色像素视为背景候选
        ring = np.zeros((h, w), dtype=bool)
        rp = int(max(1, ring_px))
        ring[:rp, :] = ring[-rp:, :] = True
        ring[:, :rp] = ring[:, -rp:] = True
        dark_ring = (lum < 70.0) & ring
        candidate = white | dark_ring

        # flood fill 语义：仅保留边界连通分量
        lab, n = label(candidate)
        if n > 0:
            border_labels = np.unique(np.concatenate([
                lab[0, :], lab[-1, :], lab[:, 0], lab[:, -1]]))
            border_labels = border_labels[border_labels > 0]
            bg = np.isin(lab, border_labels)
        else:
            bg = np.zeros((h, w), dtype=bool)

        fg = ~bg
        n_src = int(fg.sum())
        if erode_px and erode_px > 0:
            fg = binary_erosion(fg, iterations=int(erode_px))
        n_eroded = int(fg.sum())
        alpha = gaussian_filter(fg.astype(np.float32) * 255.0,
                                sigma=float(feather_sigma))
        out = arr.copy()
        out[..., 3] = np.clip(alpha, 0, 255).astype(np.uint8)
        meta = {
            "white_thresh": white_thresh,
            "sat_thresh": sat_thresh,
            "ring_px": rp,
            "feather_sigma": round(float(feather_sigma), 2),
            "erode_px": int(erode_px or 0),
            "bg_removed_ratio": round(float(bg.mean()), 4),
            "subject_px_before_erode": n_src,
            "subject_px_after_erode": n_eroded,
            "subject_retention": (round(n_eroded / max(1, n_src), 4)
                                  if n_src else 0.0),
            "subject_coverage": round(float(n_eroded) / max(1, h * w), 4),
        }
        return Image.fromarray(out, "RGBA"), meta

    @staticmethod
    def _resize_to(img: Image.Image, size: Tuple[int, int]) -> Image.Image:
        nw, nh = max(1, int(size[0])), max(1, int(size[1]))
        r = img.resize((nw, nh), Image.LANCZOS)
        # 重放大场景键化：源图远小于目标框（>3x）时 LANCZOS 会偏软，
        # 补一步轻度 UnsharpMask 恢复商品细节（钩花/织纹）。
        factor = max(nw / max(1, img.width), nh / max(1, img.height))
        if factor > 3.0:
            r = r.filter(ImageFilter.UnsharpMask(radius=2, percent=55, threshold=2))
        return r

    @staticmethod
    def _soften_edges(img: Image.Image, feather: float) -> Image.Image:
        """边缘去晕：把商品图外圈（宽度=羽化带）替换为轻模糊版本。

        实拍商品图外圈常有深色描边/白边/压缩黑边，直接羽化会把深色像素
        半透明地带到浅色背景上形成"脏边"。先把外圈轻模糊（半径=0.5×羽化），
        使边缘像素与内容自然过渡，再羽化即无 halo。
        """
        band = max(2, int(feather))
        if band >= min(img.size) // 2:
            return img
        blurred = img.filter(ImageFilter.GaussianBlur(max(1.0, feather * 0.5)))
        arr = np.array(img).astype(np.uint8)
        barr = np.array(blurred).astype(np.uint8)
        h, w = arr.shape[:2]
        # 构造边缘带掩码（外圈 band 宽）
        edge = np.zeros((h, w), dtype=bool)
        edge[:band, :] = True
        edge[-band:, :] = True
        edge[:, :band] = True
        edge[:, -band:] = True
        arr[edge] = barr[edge]
        return Image.fromarray(arr, "RGBA")

    def _edge_mask(self, size: Tuple[int, int], radius: float,
                   feather: float) -> Image.Image:
        """生成 alpha 掩码：圆角矩形 + 距离变换软边。

        距离变换 EDT：掩码内每像素到边界的距离 → 归一化到 [0,1] 的软边带。
        相比线性渐变，EDT 在圆角处同样平滑（无折角硬纹）。
        """
        w, h = size
        base = Image.new("L", (w, h), 0)
        d = ImageDraw.Draw(base)
        if radius > 0:
            d.rounded_rectangle([0, 0, w - 1, h - 1], radius=int(radius), fill=255)
        else:
            d.rectangle([0, 0, w - 1, h - 1], fill=255)
        if feather <= 0.5:
            return base
        arr = np.array(base)
        # ⚠️ 关键：距离变换只看数组内部的 False 像素。若不加外框，
        # 矩形顶边行本身在矩形内（距最近的 False——四角圆角区很远），
        # 导致边缘行 alpha≈1，羽化完全不起作用。
        # 解决：外圈一周 False 边框，顶边行距离=1 → alpha 从数组边缘开始渐变。
        padded = np.zeros((arr.shape[0] + 2, arr.shape[1] + 2), dtype=arr.dtype)
        padded[1:-1, 1:-1] = arr
        dist = _distance_transform_edt(padded > 127)[1:-1, 1:-1]
        alpha = np.clip(dist / max(feather, 0.5), 0.0, 1.0)
        return Image.fromarray((alpha * 255).astype(np.uint8), "L")

    def _blend_edges_to_bg(self, fg: Image.Image, mask: Image.Image,
                           bg_layer: Image.Image,
                           box: Tuple[int, int, int, int]) -> Image.Image:
        """渐变融合：把前景在**软边带内**的 RGB 向背景色插值。

        与纯 alpha 合成互补：alpha 处理"透明度"，这里处理"颜色"——
        软边带内的商品图像素逐渐变成背景色，彻底消除 hairline 色差。
        """
        feather = self._effective_feather()
        if feather <= 2:
            return fg
        fw, fh = fg.size
        # 背景在商品图区域的采样（取 box 对应区域缩放）
        bx0, by0, bx1, by1 = box
        bg_crop = bg_layer.crop((max(0, bx0), max(0, by0),
                                 min(bg_layer.width, bx1), min(bg_layer.height, by1)))
        if bg_crop.width < 2 or bg_crop.height < 2:
            return fg
        bg_res = bg_crop.resize((fw, fh), Image.LANCZOS)
        fg_arr = np.array(fg.convert("RGB")).astype(float)
        bg_arr = np.array(bg_res.convert("RGB")).astype(float)
        m = np.array(mask).astype(float) / 255.0
        # 软边带权重：alpha 在 (0,1) 的位置才做颜色插值
        band = (m > 0.02) & (m < 0.98)
        if not band.any():
            return fg
        blend_w = (1.0 - m)[..., None]
        out = fg_arr.copy()
        out[band] = fg_arr[band] * (1 - blend_w[band]) + bg_arr[band] * blend_w[band]
        return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB").convert("RGBA")

    # ------------------------------------------------------------ 阴影

    def _render_shadow(self, box: Tuple[int, int, int, int],
                       canvas_size: Tuple[int, int]) -> Optional[Image.Image]:
        """投影层：与前景同状的圆角矩形 + 高斯模糊（偏移可配）。

        canvas_size 必须传入实际画布尺寸（adapt() 参数优先于配置）。"""
        sc = self.cfg["shadow"]
        if not sc.get("enabled", True):
            return None
        if self.cfg["mode"] == "collage":
            return None   # 重羽化拼贴不需要投影
        W, H = int(canvas_size[0]), int(canvas_size[1])
        x0, y0, x1, y1 = box
        bw, bh = x1 - x0, y1 - y0
        blur = float(sc.get("blur", 24))
        ox = int(sc.get("offset_x", 0))
        oy = int(sc.get("offset_y", 10))
        alpha = int(sc.get("alpha", 90))
        color = _hex_to_rgb(sc.get("color", "#1A0D07"))

        pad = int(blur * 3)
        layer = Image.new("RGBA", (bw + pad * 2, bh + pad * 2), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        radius = int(self.cfg["corner_radius"])
        d.rounded_rectangle([pad, pad, pad + bw - 1, pad + bh - 1],
                            radius=radius, fill=color + (alpha,))
        layer = layer.filter(ImageFilter.GaussianBlur(max(1.0, blur)))
        # 偏移放置：以画布中心对齐 box 中心
        full = Image.new("RGBA", (max(W, layer.width), max(H, layer.height)),
                         (0, 0, 0, 0))
        cx = (x0 + x1) // 2 + ox
        cy = (y0 + y1) // 2 + oy
        full.alpha_composite(layer, (cx - layer.width // 2, cy - layer.height // 2))
        return full.crop((0, 0, W, H))


# ---------------------------------------------------------------- 距离变换

def _distance_transform_edt(mask: np.ndarray) -> np.ndarray:
    """欧氏距离变换（优先 scipy；缺失时用 PIL 近似梯度）。

    返回与 mask 同形的 float 数组：每像素到最近 False 像素的距离。
    """
    try:
        from scipy.ndimage import distance_transform_edt
        return distance_transform_edt(mask)
    except Exception:
        # 退化实现：两次可分离盒式扩散近似（封面场景足够平滑）
        h, w = mask.shape
        dist = np.where(mask, 1e6, 0.0).astype(float)
        for _ in range(int(max(h, w) ** 0.5) + 2):
            new = dist.copy()
            new[1:, :] = np.minimum(new[1:, :], dist[:-1, :] + 1)
            new[:-1, :] = np.minimum(new[:-1, :], dist[1:, :] + 1)
            new[:, 1:] = np.minimum(new[:, 1:], dist[:, :-1] + 1)
            new[:, :-1] = np.minimum(new[:, :-1], dist[:, 1:] + 1)
            if np.array_equal(new, dist):
                break
            dist = new
        return dist


# ---------------------------------------------------------------- 便捷函数

def adapt_product_image(product_image: Union[str, Image.Image],
                        canvas_size: Tuple[int, int] = (1080, 1920),
                        config: Dict[str, Any] = None) -> AdaptationResult:
    """一行式适配入口。"""
    return ProductImageAdapter(config).adapt(product_image, canvas_size)


if __name__ == "__main__":
    # 最小自测：生成合成商品图并适配
    demo = Image.new("RGBA", (400, 500), (30, 60, 120, 255))
    d = ImageDraw.Draw(demo)
    d.ellipse([120, 120, 280, 280], fill=(220, 180, 90, 255))
    res = adapt_product_image(demo, (540, 960),
                              {"mode": "blend", "margin_px": 60})
    res.save("data/tmp_frames/adapter_demo.jpg")
    print("[ADAPTER] demo ->", res.meta["subject_box"], res.meta["feather_px_effective"])
