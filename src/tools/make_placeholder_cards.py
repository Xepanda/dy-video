#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
make_placeholder_cards.py - 商品图占位示例图生成器
=====================================================
背景（2026-10-21 需求 1）：当前全部素材均缺真实商品图。
本轮暂不接入真实图片，但需为每个商品位生成一张**明确标注为占位**的示例图，
保证封面版面布局与视觉完整性，后续拿到真实商品图后直接替换即可（同名同尺寸）。

设计约束：
  - 尺寸与真实贴图位一致（constraints.sticker.size，默认 280x280）；
  - 视觉上必须"一眼看出是占位"——斜纹底+ 虚线框 + "商品图占位"主标
    + "PLACEHOLDER" 副标 + 待替换说明，杜绝误当真实商品图发布；
  - 生成后写入 data/placeholders/，并自动登记到各spec 的 item_images，
    使cover_renderer 走正常贴图渲染路径（C11~C14 几何/QC 断言得以真实验证）。

CLI:
  python src/tools/make_placeholder_cards.py --out-dir data/placeholders \
      --specs job_spec.json specs/*.json [--size 280]
"""
import os
import sys
import json
import glob
import argparse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from PIL import Image, ImageDraw, ImageFont

FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc" if os.name == "nt" \
    else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "C:/Windows/Fonts/msyh.ttc" if os.name == "nt" \
    else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# 占位视觉配色（低饱和灰蓝，明确区别于真实商品图的暖色调）
BG_TOP = (226, 232, 240)
BG_BOTTOM = (203, 213, 225)
STRIPE = (186, 199, 214)
BORDER = (148, 163, 184)
TEXT_MAIN = (71, 85, 105)
TEXT_SUB = (113, 128, 150)


def _draw_diagonal_stripes(d: ImageDraw.ImageDraw, size: int, gap: int = 18) -> None:
    """斜纹底：占位图的通用视觉语言，一眼可辨。"""
    for x in range(-size, size * 2, gap):
        d.line([(x, 0), (x + size, size)], fill=STRIPE, width=6)


def make_placeholder_card(size: int = 280, title: str = "商品图占位") -> str:
    """生成单张占位商品图，返回 PNG 路径。"""
    img = Image.new("RGB", (size, size), BG_TOP)
    d = ImageDraw.Draw(img)

    # 斜纹底
    _draw_diagonal_stripes(d, size)
    # 竖向渐变（自上而下加深，强化"这不是照片"的观感）
    for y in range(size):
        t = y / max(size - 1, 1)
        c = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3))
        d.line([(0, y), (size, y)], fill=c)
    _draw_diagonal_stripes(d, size)

    # 虚线边框
    dash, gap = 14, 9
    inset = 8
    for x in range(inset, size - inset, dash + gap):
        d.line([(x, inset), (min(x + dash, size - inset), inset)], fill=BORDER, width=3)
        d.line([(x, size - inset), (min(x + dash, size - inset), size - inset)],
               fill=BORDER, width=3)
    for y in range(inset, size - inset, dash + gap):
        d.line([(inset, y), (inset, min(y + dash, size - inset))], fill=BORDER, width=3)
        d.line([(size - inset, y), (size - inset, min(y + dash, size - inset))],
               fill=BORDER, width=3)

    # 主标：商品图占位
    f_main = ImageFont.truetype(FONT_BOLD, max(28, size // 8))
    txt = title
    bbox = d.textbbox((0, 0), txt, font=f_main)
    d.text(((size - (bbox[2] - bbox[0])) / 2, size * 0.40), txt,
           font=f_main, fill=TEXT_MAIN)

    # 副标：PLACEHOLDER
    f_sub = ImageFont.truetype(FONT_BOLD, max(18, size // 13))
    sub = "PLACEHOLDER"
    bbox = d.textbbox((0, 0), sub, font=f_sub)
    d.text(((size - (bbox[2] - bbox[0])) / 2, size * 0.56), sub,
           font=f_sub, fill=TEXT_SUB)

    # 底部说明：待替换为真实商品图
    f_note = ImageFont.truetype(FONT_REG, max(14, size // 18))
    note = "待替换为真实商品图"
    bbox = d.textbbox((0, 0), note, font=f_note)
    d.text(((size - (bbox[2] - bbox[0])) / 2, size * 0.68), note,
           font=f_note, fill=TEXT_SUB)

    return img


def ensure_placeholder_cards(spec: dict, project_id: str, out_dir: str = None,
                              n_cards: int = 2, size: int = 280) -> list:
    """确保 spec 的商品位上有可用图片：优先真实商品图，缺失时生成占位示例图。

    返回最终用于 product.item_images 的路径列表（可能为空）。
    - 已有真实商品图 → 原样返回，不做任何改动；
    - 缺失 → 生成 n_cards 张占位图并写回 spec（打上 placeholder 标记）。
    幂等：重复调用不会重复生成（已登记的占位图直接复用）。
    """
    out_dir = out_dir or os.path.join(PROJECT_ROOT, "data", "placeholders")
    os.makedirs(out_dir, exist_ok=True)

    existing = [i for i in (spec.get("product", {}) or {}).get("item_images", [])
                if i and os.path.exists(i)]
    if existing:
        print(f"[PLACEHOLDER] {project_id}: 已有 {len(existing)} 张商品图，跳过占位生成")
        return existing

    paths = []
    for i in range(n_cards):
        title = "商品图占位" + (f" {i + 1}" if n_cards > 1 else "")
        card = make_placeholder_card(size, title)
        out = os.path.join(out_dir, f"{project_id}_placeholder_{i + 1}.png")
        card.save(out, "PNG")
        paths.append(out.replace("\\", "/"))
    spec.setdefault("product", {})["item_images"] = paths
    spec["product"]["placeholder_images"] = True
    spec["product"]["placeholder_note"] = (
        "当前为占位示例图，仅用于保证封面版面完整；"
        "拿到真实商品图后替换 product.item_images 同名文件即可")
    print(f"[PLACEHOLDER] {project_id}: 生成 {len(paths)} 张占位图并登记到 product.item_images")
    return paths


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="商品图占位示例图生成器")
    ap.add_argument("--out-dir", default=os.path.join(PROJECT_ROOT, "data", "placeholders"))
    ap.add_argument("--specs", nargs="*", default=None,
                    help="要登记的 spec 文件；缺省为 job_spec.json + specs/*.json")
    ap.add_argument("--size", type=int, default=None,
                    help="占位图边长；缺省读 constraints.sticker.size")
    args = ap.parse_args(argv)

    size = args.size
    if size is None:
        size = 280
        cpath = os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
        if os.path.exists(cpath):
            import yaml
            with open(cpath, "r", encoding="utf-8") as f:
                size = int((yaml.safe_load(f) or {}).get("sticker", {}).get("size", 280))

    os.makedirs(args.out_dir, exist_ok=True)

    spec_paths = args.specs or (
        [os.path.join(PROJECT_ROOT, "job_spec.json")]
        + sorted(glob.glob(os.path.join(PROJECT_ROOT, "specs", "job_spec*.json"))))
    spec_paths = [p for p in spec_paths if os.path.exists(p)]
    if not spec_paths:
        print("[PLACEHOLDER] 未找到任何 spec 文件，仅生成通用占位图")
        spec_paths = []

    made: dict = {}
    for sp in spec_paths:
        with open(sp, "r", encoding="utf-8") as f:
            spec = json.load(f)
        pid = spec.get("project_id") or os.path.splitext(os.path.basename(sp))[0]
        # 已有真实商品图的素材跳过（占位仅用于补齐空位）
        existing = [i for i in (spec.get("product", {}) or {}).get("item_images", [])
                    if i and os.path.exists(i)]
        if existing:
            print(f"[PLACEHOLDER] {pid}: 已有 {len(existing)} 张真实商品图，跳过")
            continue

        n_cards = int((spec.get("options", {}) or {}).get("placeholder_cards", 2))
        paths = []
        for i in range(n_cards):
            title = "商品图占位" + (f" {i + 1}" if n_cards > 1 else "")
            card = make_placeholder_card(size, title)
            out = os.path.join(args.out_dir, f"{pid}_placeholder_{i + 1}.png")
            card.save(out, "PNG")
            paths.append(out.replace("\\", "/"))
            print(f"[PLACEHOLDER] 生成 {os.path.basename(out)} ({size}x{size})")
        spec.setdefault("product", {})["item_images"] = paths
        spec["product"]["placeholder_images"] = True
        spec["product"]["placeholder_note"] = (
            "当前为占位示例图，仅用于保证封面版面完整；"
            "拿到真实商品图后替换 product.item_images 同名文件即可")
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False, indent=2)
        made[pid] = paths
        print(f"[PLACEHOLDER] {pid}: 登记 {len(paths)} 张占位图到 product.item_images")

    print(f"\n[PLACEHOLDER] 完成：{len(made)} 个项目已补齐占位商品图 -> {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())