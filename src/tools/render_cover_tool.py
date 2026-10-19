#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
render_cover_tool.py - MIXCITY 高定品牌封面参数化渲染工具
=========================================================
向 Agent 提供确定性的高定封面排版渲染能力:
1. 输入由 Agent 决策选定的候选正身底图 (或候选帧 ID);
2. 结合品牌预设与商品信息, 执行高定排版:
   - 顶部金色 3D 浮雕立体字 (品牌名称)
   - 底部 Shear 动感右倾斜体 (品类副标)
   - 右侧 280x280 商品圆角卡片居中
3. 严格遵循 1080x1920 竖屏安全视域规范。
"""

import os
import sys
import json
import argparse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.graphics.cover_renderer import MixCityCoverRenderer


def render_cover_tool(
    base_frame_path: str,
    spec_data: dict,
    output_path: str
) -> str:
    """渲染封面并返回成图路径"""
    if not os.path.exists(base_frame_path):
        raise FileNotFoundError(f"底图文件不存在: {base_frame_path}")

    brand_info = spec_data.get("brand", {
        "title_en": "MIXCITY",
        "subtitle_cn": "深圳高端女装"
    })
    product_info = spec_data.get("product", {
        "title_line1": "半岛棉镂空钩花",
        "title_line2": "气质显瘦针织衫"
    })

    renderer = MixCityCoverRenderer()
    out = renderer.render_cover(
        base_frame_path=base_frame_path,
        brand_info=brand_info,
        product_info=product_info,
        output_path=output_path
    )
    print(f"[COVER-TOOL] 高定封面渲染完成: {out}")
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MIXCITY 高定品牌封面渲染工具")
    parser.add_argument("--candidate-img", required=True, help="Agent 选定的底图路径")
    parser.add_argument("--spec", default="job_spec.json", help="任务配置文件")
    parser.add_argument("--output", required=True, help="输出封面路径")
    args = parser.parse_args()

    with open(args.spec, "r", encoding="utf-8") as f:
        spec = json.load(f)

    render_cover_tool(args.candidate_img, spec, args.output)
