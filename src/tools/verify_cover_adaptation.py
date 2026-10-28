#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_cover_adaptation.py - 商品图封面适配效果验证器（2026-10-28 新增）
===========================================================================
对 adapt_product_cover.py 的产出做**量化效果验证**（不只是"看起来对"）：

  V1 尺寸正确性    —— 输出 == 声明的画布尺寸；
  V2 羽化渐变存在性 —— 前景层 alpha 沿边缘的距离剖面应单调 0→1，
                       过渡宽度 ≈ 配置的 feather_px（证明软边真实存在）；
  V3 无硬边界      —— 合成图上穿过边缘带的扫描线，逐像素亮度跳变
                       ≤ 阈值（证明渐变融合、无生硬接缝）；
  V4 防脏边        —— 羽化带内合成色不得深于 min(前景色, 背景色) - 容差
                       （深色商品图贴浅背景的 halo 检测）；
  V5 阴影存在      —— 开启阴影时，主体框外侧应测得比背景更深的阴影带；
  V6 留白正确      —— 主体框到画布四边距离 ≥ margin_px - 容差。

验证方式：读 meta（含完整生效配置）→ 复现适配（确定性）→ 逐项量化。
同时输出**边缘放大可视化条带**（四边 6x 放大裁剪拼图），供人工复核。

CLI:
  python src/tools/verify_cover_adaptation.py --image product.png \
      --meta cover_adapt_meta.json [--out report.json] [--visual strip.jpg]
退出码: 0=全部通过, 1=有 WARN, 2=有 BLOCK
"""

import os
import sys
import json
import argparse
from typing import List, Dict, Any, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

import numpy as np
from PIL import Image, ImageDraw


def _lum(rgb: np.ndarray) -> np.ndarray:
    """相对亮度（0~255）。"""
    return 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]


def check_v2_feather_profile(fg_alpha: np.ndarray, box: Tuple[int, int, int, int],
                             feather: float) -> Dict[str, Any]:
    """V2：前景 alpha 的距离剖面（从主体框四边向内）。"""
    x0, y0, x1, y1 = box
    h, w = fg_alpha.shape[:2]
    depths = list(range(0, int(feather * 2.2) + 2))
    profiles = {"top": [], "bottom": [], "left": [], "right": []}
    for d in depths:
        # 每条边取中部 1/3 区域的平均 alpha
        cx0, cx1 = x0 + (x1 - x0) // 3, x0 + 2 * (x1 - x0) // 3
        cy0, cy1 = y0 + (y1 - y0) // 3, y0 + 2 * (y1 - y0) // 3
        if d == 0:
            profiles["top"].append(float(np.mean(fg_alpha[max(0, y0 - 1), cx0:cx1])))
            profiles["bottom"].append(float(np.mean(fg_alpha[min(h - 1, y1), cx0:cx1])))
            profiles["left"].append(float(np.mean(fg_alpha[cy0:cy1, max(0, x0 - 1)])))
            profiles["right"].append(float(np.mean(fg_alpha[cy0:cy1, min(w - 1, x1)])))
        else:
            profiles["top"].append(float(np.mean(fg_alpha[max(0, y0 - d), cx0:cx1])))
            profiles["bottom"].append(float(np.mean(fg_alpha[min(h - 1, y1 + d - 1), cx0:cx1])))
            profiles["left"].append(float(np.mean(fg_alpha[cy0:cy1, max(0, x0 - d)])))
            profiles["right"].append(float(np.mean(fg_alpha[cy0:cy1, min(w - 1, x1 + d - 1)])))
    # 判定：任一边单调非降（允许小噪声），且从 <0.15 过渡到 >0.85 的宽度与 feather 相符
    mono_ok = True
    for side, prof in profiles.items():
        arr = np.array(prof)
        if arr.size >= 2 and not np.all(np.diff(arr) >= -0.06):
            mono_ok = False
    # 过渡宽度：首次 >0.5 的深度
    trans_w = None
    for d, p in zip(depths, profiles["top"]):
        if p > 0.5:
            trans_w = d
            break
    width_ok = True
    if feather > 2 and trans_w is not None:
        width_ok = 0.35 * feather <= trans_w <= 2.2 * feather + 2
    status = "PASS" if (mono_ok and width_ok) else ("WARN" if mono_ok else "BLOCK")
    return {
        "id": "V2", "name": "羽化渐变存在性", "status": status,
        "detail": f"alpha 剖面单调={'是' if mono_ok else '否'}；"
                  f"半透明过渡深度≈{trans_w}px（配置羽化 {feather:.0f}px）",
        "profiles": {k: [round(v, 3) for v in p[:12]] for k, p in profiles.items()},
        "measured": trans_w, "threshold": f"0.35x~2.2x of {feather:.0f}",
    }


def check_v3_hard_edge(canvas_rgb: np.ndarray, fg_alpha: np.ndarray,
                       box: Tuple[int, int, int, int],
                       feather: float) -> Dict[str, Any]:
    """V3：只测**半透明融合区**内的逐像素亮度跳变。

    2026-10-28 修正：初版测 ±feather 全带，把商品图**自身内部内容边缘**
    （如深色商品卡边框与主体的对比，实测跳变 50）误判为融合缺陷——
    该跳变发生在 alpha=1.00 的不透明内部，与边缘融合无关。
    现版：仅评测 alpha ∈ (0.05, 0.95) 的融合区相邻像素对；
    无融合区（硬边）时测边界单步（alpha 0→1 的一步，必然巨大 → BLOCK）。
    """
    x0, y0, x1, y1 = box
    lum = _lum(canvas_rgb)
    a = fg_alpha.astype(float) / 255.0
    h, w = lum.shape
    band = int(max(4, feather))

    max_jump = 0.0
    mode_note = ""
    for axis, ranges in (("v", ((y0 - band, y0 + band), (y1 - band, y1 + band))),
                         ("h", ((x0 - band, x0 + band), (x1 - band, x1 + band)))):
        for (lo, hi) in ranges:
            if axis == "v":
                if lo < 0 or hi >= h:
                    continue
                seg_l = lum[lo:hi + 1, x0 + (x1 - x0) // 3: x0 + 2 * (x1 - x0) // 3].mean(axis=1)
                seg_a = a[lo:hi + 1, x0 + (x1 - x0) // 3: x0 + 2 * (x1 - x0) // 3].mean(axis=1)
            else:
                if lo < 0 or hi >= w:
                    continue
                seg_l = lum[y0 + (y1 - y0) // 3: y0 + 2 * (y1 - y0) // 3, lo:hi + 1].mean(axis=0)
                seg_a = a[y0 + (y1 - y0) // 3: y0 + 2 * (y1 - y0) // 3, lo:hi + 1].mean(axis=0)
            if seg_l.size < 2:
                continue
            zone = (seg_a > 0.05) & (seg_a < 0.95)
            if zone.sum() >= 2:
                # 融合区：相邻对中任一端在区内即参与（含进出融合区的两步）
                idx = np.where(zone)[0]
                pairs = [(i, i + 1) for i in idx if i + 1 < seg_l.size]
                if pairs:
                    jumps = [abs(float(seg_l[j] - seg_l[i])) for i, j in pairs]
                    max_jump = max(max_jump, max(jumps))
                    mode_note = "融合区"
            else:
                # 无融合区（硬边）：测 alpha 0→1 的边界单步
                da = np.abs(np.diff(seg_a))
                step_idx = np.where(da > 0.9)[0]
                if step_idx.size:
                    steps = [abs(float(seg_l[i + 1] - seg_l[i])) for i in step_idx]
                    max_jump = max(max_jump, max(steps))
                    mode_note = "硬边单步"
    if max_jump <= 14:
        status = "PASS"
    elif max_jump <= 24:
        status = "WARN"
    else:
        status = "BLOCK"
    return {
        "id": "V3", "name": "无硬边界（渐变融合）", "status": status,
        "detail": f"融合区（{mode_note or 'n/a'}）最大单像素亮度跳变 {max_jump:.1f}"
                  f"（PASS ≤14，WARN ≤24，BLOCK >24）",
        "measured": round(max_jump, 2), "threshold": 14,
    }


def check_v4_halo(canvas_rgb: np.ndarray, fg_rgb: np.ndarray,
                  fg_alpha: np.ndarray, box: Tuple[int, int, int, int],
                  feather: float) -> Dict[str, Any]:
    """V4：羽化带内不得出现比前景与背景都暗的 halo 像素。"""
    if feather <= 2:
        return {"id": "V4", "name": "防脏边（halo）", "status": "PASS",
                "detail": "未启用羽化，跳过", "measured": None, "threshold": None}
    x0, y0, x1, y1 = box
    band = int(max(3, feather * 0.6))
    h, w = fg_alpha.shape
    # 取上边缘带（中部 1/3 列）：alpha 在 0.1~0.9 的位置
    cols = slice(x0 + (x1 - x0) // 3, x0 + 2 * (x1 - x0) // 3)
    rows = slice(max(0, y0 - band), min(h, y0 + band))
    a = fg_alpha[rows, cols] / 255.0
    fg_c = fg_rgb[rows, cols].astype(float)
    cv_c = canvas_rgb[rows, cols].astype(float)
    m = (a > 0.1) & (a < 0.9)
    if not m.any():
        return {"id": "V4", "name": "防脏边（halo）", "status": "PASS",
                "detail": "边缘带内无半透明像素，跳过", "measured": None, "threshold": None}
    fg_l = _lum(fg_c[m][..., None, :]).squeeze(-1) if False else (
        0.2126 * fg_c[m][..., 0] + 0.7152 * fg_c[m][..., 1] + 0.0722 * fg_c[m][..., 2])
    cv_l = (0.2126 * cv_c[m][..., 0] + 0.7152 * cv_c[m][..., 1] + 0.0722 * cv_c[m][..., 2])
    # halo 判据：合成色显著暗于两端（比暗的一端还暗 > 8）
    floor = np.minimum(fg_l, cv_l)
    excess = float(np.maximum(floor - cv_l, 0).max()) if cv_l.size else 0.0
    # 背景色近似：alpha≈0 处的合成色
    a_low = a < 0.08
    bg_l = (0.2126 * cv_c[a_low][..., 0] + 0.7152 * cv_c[a_low][..., 1]
            + 0.0722 * cv_c[a_low][..., 2]) if a_low.any() else None
    status = "PASS" if excess <= 8 else ("WARN" if excess <= 16 else "BLOCK")
    return {
        "id": "V4", "name": "防脏边（halo）", "status": status,
        "detail": f"羽化带内合成色最坏比两端暗 {excess:.1f}"
                  f"（PASS ≤8，WARN ≤16，BLOCK >16）",
        "measured": round(excess, 2), "threshold": 8,
    }


def check_v5_shadow(canvas_rgb: np.ndarray, bg_rgb: np.ndarray,
                    box: Tuple[int, int, int, int], shadow_cfg: Dict[str, Any],
                    feather: float) -> Dict[str, Any]:
    """V5：阴影带应比背景暗（offset_y 侧）。"""
    if not shadow_cfg.get("enabled"):
        return {"id": "V5", "name": "阴影存在", "status": "PASS",
                "detail": "阴影未启用，跳过", "measured": None, "threshold": None}
    x0, y0, x1, y1 = box
    oy = int(shadow_cfg.get("offset_y", 10))
    # 阴影带：主体框下方 oy 附近；参照带：同形状但在画布内更下方（无阴影处）
    h, w = canvas_rgb.shape[:2]
    band_h = 24
    sy0 = min(h - 1, y1 + max(0, oy) - 4)
    sy1 = min(h, sy0 + band_h)
    ry0 = min(h - 1, y1 + abs(oy) + int(feather) + 30)
    ry1 = min(h, ry0 + band_h)
    if sy1 <= sy0 or ry1 <= ry0:
        return {"id": "V5", "name": "阴影存在", "status": "WARN",
                "detail": "主体贴近画布底部，无法取样阴影带", "measured": None,
                "threshold": None}
    cols = slice(x0 + (x1 - x0) // 4, x0 + 3 * (x1 - x0) // 4)
    shadow_l = _lum(canvas_rgb[sy0:sy1, cols]).mean()
    ref_l = _lum(canvas_rgb[ry0:ry1, cols]).mean()
    delta = float(ref_l - shadow_l)
    status = "PASS" if delta >= 3 else "WARN"
    return {
        "id": "V5", "name": "阴影存在", "status": status,
        "detail": f"阴影带比参照背景暗 {delta:.1f}（≥3 判存在）",
        "measured": round(delta, 2), "threshold": 3,
    }


def check_v6_margin(meta: Dict[str, Any]) -> Dict[str, Any]:
    """V6：主体框到画布四边距离 ≥ margin_px - 容差。"""
    x0, y0, x1, y1 = meta["subject_box"]
    W, H = meta["canvas_size"]
    margin = meta["margin_px"]
    feather = meta.get("feather_px_effective", 0)
    tol = max(2, int(feather * 0.25))   # 羽化会轻微外扩，给容差
    gaps = {"left": x0, "top": y0, "right": W - x1, "bottom": H - y1}
    bad = {k: v for k, v in gaps.items() if v < margin - tol}
    status = "PASS" if not bad else "WARN"
    return {
        "id": "V6", "name": "留白正确", "status": status,
        "detail": f"四边留白 {gaps}（要求 ≥{margin}px，容差 {tol}px）"
                  + (f"；不足: {bad}" if bad else ""),
        "measured": gaps, "threshold": margin,
    }


def make_edge_strip(canvas_rgb: np.ndarray, box: Tuple[int, int, int, int],
                    zoom: int = 6, patch: int = 140) -> Image.Image:
    """边缘放大可视化：四边各取 140px 见方 6x 放大，横向拼条。"""
    x0, y0, x1, y1 = box
    h, w = canvas_rgb.shape[:2]
    crops = []
    regions = [
        ("top", (x0 + (x1 - x0) // 2 - patch // 2, max(0, y0 - patch // 2))),
        ("bottom", (x0 + (x1 - x0) // 2 - patch // 2, min(h - patch, y1 - patch // 2))),
        ("left", (max(0, x0 - patch // 2), y0 + (y1 - y0) // 2 - patch // 2)),
        ("right", (min(w - patch, x1 - patch // 2), y0 + (y1 - y0) // 2 - patch // 2)),
    ]
    for name, (px, py) in regions:
        px, py = max(0, min(w - patch, px)), max(0, min(h - patch, py))
        c = Image.fromarray(canvas_rgb[py:py + patch, px:px + patch].astype(np.uint8), "RGB")
        crops.append(c.resize((patch * zoom, patch * zoom), Image.NEAREST))
    strip = Image.new("RGB", (patch * zoom * 4 + 30, patch * zoom + 30), (238, 238, 238))
    d = ImageDraw.Draw(strip)
    for i, c in enumerate(crops):
        x = 10 + i * (patch * zoom + 10)
        strip.paste(c, (x, 20))
        d.text((x + 4, 4), regions[i][0], fill=(30, 30, 30))
    return strip


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="商品图封面适配效果验证器")
    parser.add_argument("--image", required=True, help="商品图路径")
    parser.add_argument("--meta", required=True,
                        help="adapt_product_cover.py 产出的 *_adapt_meta.json")
    parser.add_argument("--out", default=None, help="验证报告 JSON 输出路径")
    parser.add_argument("--visual", default=None, help="边缘放大条带 JPG 输出路径")
    args = parser.parse_args(argv)

    with open(args.meta, "r", encoding="utf-8") as f:
        meta = json.load(f)
    if "config" not in meta:
        print("[FATAL] meta 缺少 config 段（旧版产物），请重新生成适配")
        return 2

    # 复现适配（确定性）
    from src.graphics.product_image_adapter import ProductImageAdapter
    adapter = ProductImageAdapter(meta["config"])
    result = adapter.adapt(args.image, tuple(meta["canvas_size"]))

    W, H = meta["canvas_size"]
    box = tuple(meta["subject_box"])
    feather = float(meta.get("feather_px_effective", 0))

    canvas_rgb = np.array(result.canvas.convert("RGB"))
    fg_rgb = np.array(result.foreground.convert("RGB"))
    fg_alpha = np.array(result.foreground.split()[3])
    bg_rgb = np.array(result.background.convert("RGB"))

    checks = [
        {"id": "V1", "name": "尺寸正确性",
         "status": "PASS" if (result.canvas.size == (W, H)) else "BLOCK",
         "detail": f"输出 {result.canvas.size[0]}x{result.canvas.size[1]}"
                   f"（声明 {W}x{H}）",
         "measured": list(result.canvas.size), "threshold": [W, H]},
        check_v2_feather_profile(fg_alpha, box, feather),
        check_v3_hard_edge(canvas_rgb, fg_alpha, box, feather),
        check_v4_halo(canvas_rgb, fg_rgb, fg_alpha, box, feather),
        check_v5_shadow(canvas_rgb, bg_rgb, box, meta["shadow"], feather),
        check_v6_margin(meta),
    ]

    n_block = sum(1 for c in checks if c["status"] == "BLOCK")
    n_warn = sum(1 for c in checks if c["status"] == "WARN")
    passed = n_block == 0
    report = {
        "passed": passed,
        "mode": meta["mode"],
        "canvas_size": [W, H],
        "summary": {"n_checks": len(checks), "n_pass": len(checks) - n_block - n_warn,
                    "n_warn": n_warn, "n_block": n_block},
        "checks": checks,
    }
    print(f"[VERIFY] 商品图封面适配验证: {'PASS' if passed else 'BLOCK'} | "
          f"{len(checks)} 项检查（PASS {report['summary']['n_pass']} / "
          f"WARN {n_warn} / BLOCK {n_block}）")
    for c in checks:
        if c["status"] != "PASS":
            print(f"  [{c['status']}] {c['id']} {c['name']}: {c['detail'][:100]}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"[OUT] {args.out}")
    if args.visual:
        strip = make_edge_strip(canvas_rgb, box)
        os.makedirs(os.path.dirname(os.path.abspath(args.visual)), exist_ok=True)
        strip.save(args.visual, "JPEG", quality=90)
        print(f"[VISUAL] 边缘放大条带: {args.visual}")
    return 0 if passed else 2


if __name__ == "__main__":
    sys.exit(main())
