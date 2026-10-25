#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
cover_renderer.py - MIXCITY 风格参数化高定品牌封面排版引擎
============================================================
严格遵循《会议纪要》与示例图的几何栅格标准：
1. 顶部品牌区 (y=120~300):
   - 英文主标 (MIXCITY): 72pt, 金色立体双层字, 3D 投影 + 深褐描边
   - 中文副标 (深圳高端女装): 54pt, 纯白填充 + 深褐描边
2. 右侧浮动贴图区 (N=0~2 张):
   - 正方形 280x280px, 圆角 20px, 淡灰边框 #E2E8F0 + 柔和阴影
   - 靠右固定边距 a=32px; 垂直中心严格居中于 y=960
3. 底部商品名 (y=1520~1720):
   - 双行堆叠 (材质卖点 + 品类), 64pt 自适应缩放
   - 右倾仿斜体 (Shear=-0.12), 白字深褐描边, 离底保留 >200px 避让小黄车
"""

import os
import sys
import json
import math
from typing import List, Dict, Any, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUT_W = 1080
OUT_H = 1920

FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc" if os.name == "nt" else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_HEAVY = "C:/Windows/Fonts/simhei.ttf" if os.name == "nt" else FONT_BOLD

# 配色常数 (1:1 像素级复刻 MIXCITY 样例图)
COLOR_BRAND_STROKE = (56, 30, 17, 255)       # 深咖啡深褐描边 #381E11
COLOR_BRAND_SHADOW = (26, 13, 7, 210)        # 3D 投影深棕 #1A0D07
COLOR_GOLD_TEXT = (255, 235, 170, 255)       # 暖金字芯 #FFEBAA
COLOR_WHITE = (255, 255, 255, 255)
COLOR_BORDER_GRAY = (226, 232, 240, 255)     # #E2E8F0
COLOR_PRODUCT_STROKE = (66, 33, 21, 255)     # 底部商品名深褐描边 #422115


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
    
    # 1. 绘制阴影层
    pad = shadow_blur * 2
    shadow_img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    s_draw = ImageDraw.Draw(shadow_img)
    s_draw.rounded_rectangle(
        [pad, pad, pad + w, pad + h],
        radius=radius,
        fill=(0, 0, 0, shadow_alpha)
    )
    shadow_img = shadow_img.filter(ImageFilter.GaussianBlur(shadow_blur))
    
    # 合成阴影到主画布
    sx = x0 - pad + shadow_offset[0]
    sy = y0 - pad + shadow_offset[1]
    canvas.alpha_composite(shadow_img, (sx, sy))


def render_sheared_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    fill_color: Tuple[int, int, int, int],
    stroke_color: Tuple[int, int, int, int],
    stroke_width: int = 4,
    shear: float = -0.12
) -> Image.Image:
    """渲染带微斜体 (Shear) 的时尚艺术字条"""
    bbox = font.getbbox(text)
    tw = bbox[2] - bbox[0] + 40
    th = bbox[3] - bbox[1] + 40
    
    strip = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    d = ImageDraw.Draw(strip)
    # 文字先居中渲染在条带中
    d.text((20, 20), text, font=font, fill=fill_color,
           stroke_width=stroke_width, stroke_fill=stroke_color)
    
    # 仿射变换实现向右微倾斜 (x' = x + shear * y)
    extra_w = int(abs(shear) * th)
    sheared = strip.transform(
        (tw + extra_w, th),
        Image.AFFINE,
        (1, shear, 0, 0, 1, 0),
        resample=Image.BICUBIC
    )
    return sheared


class MixCityCoverRenderer:
    def __init__(self):
        self.font_h1 = ImageFont.truetype(FONT_HEAVY, 74)
        self.font_sub = ImageFont.truetype(FONT_BOLD, 52)
        self.font_prod_base = ImageFont.truetype(FONT_BOLD, 64)

    def render_cover(
        self,
        base_frame_path: str,
        brand_info: Dict[str, str],
        product_info: Dict[str, Any],
        output_path: str
    ) -> str:
        """主入口：生成标准 1080x1920 MIXCITY 封面"""
        if not os.path.exists(base_frame_path):
            raise FileNotFoundError(f"底图文件未找到: {base_frame_path}")
            
        base = Image.open(base_frame_path).convert("RGBA")
        
        # 1. 规整画幅至 1080x1920 (居中裁切或等比放大)
        if base.size != (OUT_W, OUT_H):
            # 若源帧为 1088x1920，做中心 1080 裁切
            if base.size[1] == OUT_H and base.size[0] > OUT_W:
                left = (base.size[0] - OUT_W) // 2
                base = base.crop((left, 0, left + OUT_W, OUT_H))
            else:
                base = base.resize((OUT_W, OUT_H), Image.LANCZOS)
                
        overlay = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        
        # ---------------------------------------------------------------------
        # 2. 顶部品牌区域渲染 (y = 120 ~ 290)
        # ---------------------------------------------------------------------
        brand_en = brand_info.get("title_en", "MIXCITY").strip()
        brand_cn = brand_info.get("subtitle_cn", "深圳高端女装").strip()
        
        # 2.1 英文主标 (3D 投影 + 金色双层字)
        y_en = 125
        # 投影层 (先画偏移)
        draw.text((OUT_W // 2 + 2, y_en + 4), brand_en, font=self.font_h1,
                  fill=COLOR_BRAND_SHADOW, anchor="mt", stroke_width=4, stroke_fill=COLOR_BRAND_SHADOW)
        # 主字体层 (深褐粗描边 + 金黄字芯)
        draw.text((OUT_W // 2, y_en), brand_en, font=self.font_h1,
                  fill=COLOR_GOLD_TEXT, anchor="mt", stroke_width=4.5, stroke_fill=COLOR_BRAND_STROKE)
                  
        # 2.2 中文副标 (白芯深褐描边)
        y_cn = y_en + 96
        draw.text((OUT_W // 2 + 1, y_cn + 3), brand_cn, font=self.font_sub,
                  fill=COLOR_BRAND_SHADOW, anchor="mt", stroke_width=3, stroke_fill=COLOR_BRAND_SHADOW)
        draw.text((OUT_W // 2, y_cn), brand_cn, font=self.font_sub,
                  fill=COLOR_WHITE, anchor="mt", stroke_width=3.5, stroke_fill=COLOR_BRAND_STROKE)
                  
        # ---------------------------------------------------------------------
        # 3. 浮动圆角商品贴图渲染 (右侧居中排列)
        # ---------------------------------------------------------------------
        item_images = [img for img in product_info.get("item_images", []) if img and os.path.exists(img)]
        num_cards = min(len(item_images), 2)
        
        card_size = 280
        margin_a = 32
        x_card = OUT_W - card_size - margin_a  # 1080 - 280 - 32 = 768px
        
        card_y_positions = []
        if num_cards == 1:
            # 单张贴图垂直全局居中 (y=960)
            card_y_positions.append(OUT_H // 2 - card_size // 2)
        elif num_cards == 2:
            # 双张贴图上下对称居中，间距保持为 a=32px
            y1 = (OUT_H // 2) - card_size - (margin_a // 2)  # 960 - 280 - 16 = 664
            y2 = (OUT_H // 2) + (margin_a // 2)              # 960 + 16 = 976
            card_y_positions.extend([y1, y2])
            
        for idx, (img_path, y_pos) in enumerate(zip(item_images[:num_cards], card_y_positions)):
            # 绘制外发光与柔和阴影
            card_box = (x_card, y_pos, x_card + card_size, y_pos + card_size)
            draw_rounded_rectangle_with_shadow(
                overlay, card_box, radius=20,
                shadow_offset=(0, 6), shadow_blur=12, shadow_alpha=80
            )
            
            # 读取商品平铺图并裁切为圆角正方形
            prod_img = Image.open(img_path).convert("RGBA")
            prod_img = prod_img.resize((card_size, card_size), Image.LANCZOS)
            
            mask = Image.new("L", (card_size, card_size), 0)
            m_draw = ImageDraw.Draw(mask)
            m_draw.rounded_rectangle([0, 0, card_size, card_size], radius=20, fill=255)
            
            # 贴合商品图
            overlay.paste(prod_img, (x_card, y_pos), mask=mask)
            
            # 绘制 1.5px 极细优雅淡灰描边 #E2E8F0
            c_draw = ImageDraw.Draw(overlay)
            c_draw.rounded_rectangle([x_card, y_pos, x_card + card_size, y_pos + card_size],
                                     radius=20, outline=COLOR_BORDER_GRAY, width=2)
                                     
        # ---------------------------------------------------------------------
        # 4. 底部商品名称渲染 (y = 1520 ~ 1720, 动感斜体 + 自适应)
        # ---------------------------------------------------------------------
        t1 = product_info.get("title_line1", "桑蚕丝羊绒羊毛").strip()
        t2 = product_info.get("title_line2", "渐变针织衫").strip()
        
        # 根据字符长度自适应调节字号
        max_len = max(len(t1), len(t2), 1)
        font_size = 64
        if max_len > 8:
            font_size = max(48, int(920 / (max_len * 1.15)))
        f_prod = ImageFont.truetype(FONT_BOLD, font_size)
        
        # 渲染第 1 行 (微斜体)
        strip1 = render_sheared_text(t1, f_prod, COLOR_WHITE, COLOR_PRODUCT_STROKE, stroke_width=4, shear=-0.12)
        pos1_x = (OUT_W - strip1.width) // 2
        pos1_y = 1530
        overlay.alpha_composite(strip1, (pos1_x, pos1_y))
        
        # 渲染第 2 行 (微斜体)
        strip2 = render_sheared_text(t2, f_prod, COLOR_WHITE, COLOR_PRODUCT_STROKE, stroke_width=4, shear=-0.12)
        pos2_x = (OUT_W - strip2.width) // 2
        pos2_y = pos1_y + font_size + 24
        overlay.alpha_composite(strip2, (pos2_x, pos2_y))
        
        # ---------------------------------------------------------------------
        # 5. 合成最终输出并保存
        # ---------------------------------------------------------------------
        final_img = Image.alpha_composite(base, overlay).convert("RGB")
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        final_img.save(output_path, "JPEG", quality=95)
        print(f"[COVER] MIXCITY 风格封面已生成: {output_path}")
        return output_path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", default="job_spec.json", help="任务契约文件路径")
    parser.add_argument("--bg", help="背景底图路径 (留空则从视频提取)")
    parser.add_argument("--out", default="data/test_cover.jpg", help="输出封面路径")
    args = parser.parse_args()
    
    spec_data = json.load(open(args.spec, encoding="utf-8")) if os.path.exists(args.spec) else {}
    renderer = MixCityCoverRenderer()
    
    # 缺省测试背景底图
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
