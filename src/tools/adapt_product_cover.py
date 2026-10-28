#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
adapt_product_cover.py - 商品图封面适配 CLI（2026-10-28 新增）
=================================================================
把商品图适配为不显突兀的封面底图：背景延展 / 边缘羽化 / 渐变融合 / 阴影 / 圆角，
处理强度、边缘样式、留白全部可配置，适配任意封面尺寸与背景。

用法：
  # 最简：用 constraints 默认配置（blend 模式 + 渐变背景）
  python src/tools/adapt_product_cover.py --image product.png --out cover.jpg

  # 沉浸式：商品图色彩延展为全幅背景
  python src/tools/adapt_product_cover.py --image product.png --out cover.jpg \
      --mode extend --size 1080x1920

  # 全参数覆盖（JSON 片段，深合并进默认配置）
  python src/tools/adapt_product_cover.py --image product.png --out cover.jpg \
      --set '{"margin_px":140,"feather_px":72,"background":{"type":"color","color":"#FFFFFF"}}'

  # 自定义背景图（适配不同背景）
  python src/tools/adapt_product_cover.py --image product.png --bg bg.jpg --out cover.jpg

  # 适配后再叠加 MIXCITY 品牌文字（生成完整封面）
  python src/tools/adapt_product_cover.py --image product.png --out cover.jpg \
      --with-text --spec job_spec.json

输出：
  <out>                     适配后的封面（JPEG）
  <out_meta>.json           几何 meta（主体框/羽化/阴影/背景类型，供校验）
"""

import os
import sys
import json
import argparse
from typing import Optional, Dict, Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from PIL import Image


def _parse_size(s: str):
    try:
        w, h = s.lower().split("x")
        return (int(w), int(h))
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError(
            f"尺寸格式应为 WxH（如 1080x1920），得到 {s}")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="商品图封面适配器 CLI")
    parser.add_argument("--image", required=True, help="商品图路径（PNG/JPG，RGBA 均可）")
    parser.add_argument("--out", required=True, help="输出封面路径（JPEG）")
    parser.add_argument("--mode", default=None,
                        choices=["card", "blend", "extend", "collage"],
                        help="适配模式（缺省用 constraints 配置）")
    parser.add_argument("--size", default=None, type=_parse_size,
                        help="输出画布 WxH（缺省 1080x1920）")
    parser.add_argument("--bg", default=None,
                        help="自定义背景图路径（覆盖配置的背景类型）")
    parser.add_argument("--set", default=None, dest="set_json",
                        help="JSON 配置片段，深合并进默认配置")
    parser.add_argument("--with-text", action="store_true",
                        help="适配后再叠加 MIXCITY 品牌文字（生成完整封面）")
    parser.add_argument("--spec", default=None,
                        help="--with-text 时的任务契约（默认 job_spec.json）")
    parser.add_argument("--meta-out", default=None,
                        help="meta JSON 输出路径（缺省 <out>_meta.json）")
    args = parser.parse_args(argv)

    # 1) 配置组装：constraints 默认 ← --set 覆盖 ← --mode/--size
    from src.graphics.product_image_adapter import ProductImageAdapter, _load_constraints
    constraints = _load_constraints()
    config: Dict[str, Any] = {}
    if args.set_json:
        try:
            config = json.loads(args.set_json)
        except json.JSONDecodeError as e:
            print(f"[FATAL] --set JSON 解析失败: {e}")
            return 2
    if args.mode:
        config["mode"] = args.mode
    if args.size:
        config["canvas_size"] = list(args.size)

    # 2) 执行适配
    adapter = ProductImageAdapter(config, constraints)
    bg = args.bg if args.bg else None
    result = adapter.adapt(args.image,
                           canvas_size=args.size,
                           background=bg)

    # 3) 输出
    out_path = result.save(args.out)
    meta_path = args.meta_out or (
        os.path.splitext(args.out)[0] + "_adapt_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(result.meta, f, ensure_ascii=False, indent=2)

    m = result.meta
    print(f"[ADAPTER] 模式 {m['mode']} | 画布 {m['canvas_size'][0]}x{m['canvas_size'][1]} | "
          f"主体框 {m['subject_box']} ({m['subject_size'][0]}x{m['subject_size'][1]})")
    print(f"[ADAPTER] 留白 {m['margin_px']}px | 羽化 {m['feather_px_effective']:.0f}px | "
          f"圆角 {m['corner_radius']}px | 背景 {m['background_type']} | "
          f"阴影 {'on' if m['shadow'].get('enabled') else 'off'}")
    print(f"[ADAPTER] 封面已生成: {out_path}")
    print(f"[ADAPTER] meta: {meta_path}")

    # 4) 可选：叠加品牌文字（复用既有封面渲染器的文字层）
    if args.with_text:
        spec_path = args.spec or os.path.join(PROJECT_ROOT, "job_spec.json")
        if not os.path.exists(spec_path):
            print(f"[WARN] --with-text 需要 spec 文件: {spec_path}（跳过文字叠加）")
            return 0
        with open(spec_path, "r", encoding="utf-8") as f:
            spec = json.load(f)
        from src.graphics.cover_renderer import MixCityCoverRenderer
        renderer = MixCityCoverRenderer(constraints)
        text_out = os.path.splitext(args.out)[0] + "_with_text.jpg"
        # 以适配结果为底图；清空 item_images 避免重复贴商品图
        spec_for_text = json.loads(json.dumps(spec))
        spec_for_text.setdefault("product", {})["item_images"] = []
        renderer.render_cover_with_meta(
            base_frame_path=out_path,
            brand_info=spec_for_text.get("brand", {}),
            product_info=spec_for_text.get("product", {}),
            output_path=text_out,
            meta_output_path=os.path.splitext(text_out)[0] + "_render_meta.json",
        )
        print(f"[ADAPTER] 含品牌文字的完整封面: {text_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
