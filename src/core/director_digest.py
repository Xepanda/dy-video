# -*- coding: utf-8 -*-
"""
director_digest.py - V3.3 视觉导演简报与高清联系表生成器（V33-04 核心）
=======================================================================
负责解决外部多模态 Agent 的两大痛点：
  1. 上下文爆炸：将数百条滑窗候选按语义簇去重 + 漏斗配额压缩为 Top 40~60 精选目录
     （外加最多 12 条高口播分灰区候选），生成 analysis/director_brief.md & .json；
  2. 盲人导片：自动生成 4×4 分镜高清联系表（analysis/storyboards/sheet_01.jpg ...）
     与封面候选对比拼板（analysis/storyboards/cover_sheet.jpg），让多模态 Agent
     看 2~3 张图即可用“眼睛”完成选片、封面审美与灰区 SKU 肉眼终审。
"""

import os
import json
import math
from typing import Dict, Any, List, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont

from src.core.category_profile import load_category_profile


def _load_cjk_font(size: int) -> ImageFont.ImageFont:
    """跨平台加载中文字体（Windows 微软雅黑粗体 / Linux 文泉驿或 NotoCJK），兜底默认字体。"""
    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for fp in candidates:
        if os.path.isfile(fp):
            try:
                return ImageFont.truetype(fp, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _fit_cover_crop(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """等比缩放并居中裁切填满目标单元格（保持人像上半身略偏上 0.35 锚点，避免切头）。"""
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        return Image.new("RGB", (target_w, target_h), (28, 28, 32))
    scale = max(target_w / float(src_w), target_h / float(src_h))
    nw = max(target_w, int(round(src_w * scale)))
    nh = max(target_h, int(round(src_h * scale)))
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    x0 = max(0, (nw - target_w) // 2)
    y0 = max(0, int((nh - target_h) * 0.28))
    return resized.crop((x0, y0, x0 + target_w, y0 + target_h))


def _pick_representative_frame(cand: Dict[str, Any], work_dir: str) -> Optional[str]:
    """选取候选最适合展示在联系表中的关键帧（优先中点 p50 帧）。"""
    refs = cand.get("frame_refs") or []
    valid = [p for p in refs if isinstance(p, str) and os.path.isfile(p) and os.path.getsize(p) > 0]
    if valid:
        return valid[len(valid) // 2]
    fids = cand.get("frame_ids") or []
    fdir = os.path.join(work_dir, "candidate_frames")
    for fid in fids:
        p = os.path.join(fdir, str(fid))
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
    return None


def _select_digest_candidates(
    candidates: List[Dict[str, Any]],
    scores: List[Dict[str, Any]],
    product_profile: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按语义簇去重 + 时间防重 + 漏斗配额，选出精选候选与高价值灰区候选。"""
    cat_profile = load_category_profile(product_profile)
    brief_cfg = cat_profile.get("director_brief") or {}
    max_featured = int(brief_cfg.get("max_featured_candidates", 54))
    max_gray = int(brief_cfg.get("max_gray_zone_candidates", 12))
    role_quotas: Dict[str, int] = dict(brief_cfg.get("role_quotas") or {
        "HOOK": 10, "FABRIC": 14, "FIT": 12, "STYLING": 10, "COLOR": 4, "CLOSING": 4, "PROOF": 8,
    })

    score_map = {s["candidate_id"]: s for s in scores if isinstance(s, dict) and s.get("candidate_id")}

    # 1. 每个语义簇仅留 1 条综合分最高代表，避免同一句话的滑窗变体霸屏
    best_by_cluster: Dict[str, Dict[str, Any]] = {}
    for c in candidates:
        cid = c.get("candidate_id")
        if not cid:
            continue
        sc = score_map.get(cid, {})
        rel = sc.get("product_relation") or c.get("product_relation", "UNCERTAIN")
        if rel == "OTHER_SKU":
            continue
        cl_id = sc.get("duplicate_cluster_id") or f"cl_{cid}"
        comp = float(c.get("composite_score", 0.0) or 0.0)
        elig = bool(sc.get("eligible") if "eligible" in sc else c.get("eligible", False))
        # 优先 eligible=True，其次综合分高
        rank_tuple = (1 if elig else 0, comp, -abs((c.get("source_end_ms", 0) - c.get("source_start_ms", 0)) - 5000))
        prev = best_by_cluster.get(cl_id)
        if prev is None or rank_tuple > prev["_rank_tuple"]:
            enriched = dict(c)
            enriched["_role"] = str(sc.get("narrative_role") or "PROOF").upper()
            enriched["_eligible"] = elig
            enriched["_relation"] = rel
            enriched["_rejection_reasons"] = list(sc.get("rejection_reasons") or c.get("rejection_reasons") or [])
            enriched["_rank_tuple"] = rank_tuple
            best_by_cluster[cl_id] = enriched

    deduped = list(best_by_cluster.values())

    # 时间防高度重叠辅助（同一源中重叠 > 60% 仅留高分者）
    def _is_heavy_overlap(cand: Dict[str, Any], chosen: List[Dict[str, Any]]) -> bool:
        sid = cand.get("source_id")
        st = int(cand.get("source_start_ms", 0))
        en = int(cand.get("source_end_ms", 0))
        dur = max(1, en - st)
        for ex in chosen:
            if ex.get("source_id") != sid:
                continue
            ov = min(en, int(ex.get("source_end_ms", 0))) - max(st, int(ex.get("source_start_ms", 0)))
            if ov > 0 and (ov / float(min(dur, max(1, int(ex.get("source_end_ms", 0)) - int(ex.get("source_start_ms", 0)))))) > 0.6:
                return True
        return False

    # 2. 精选合格池：先按漏斗角色配额选，再全局补满 max_featured
    eligible_pool = [c for c in deduped if c["_eligible"]]
    eligible_pool.sort(key=lambda x: x["_rank_tuple"], reverse=True)

    featured: List[Dict[str, Any]] = []
    featured_ids = set()

    for role_name, quota in role_quotas.items():
        picked = 0
        for c in eligible_pool:
            if picked >= quota or len(featured) >= max_featured:
                break
            if c["_role"] != role_name or c["candidate_id"] in featured_ids:
                continue
            if _is_heavy_overlap(c, featured):
                continue
            featured.append(c)
            featured_ids.add(c["candidate_id"])
            picked += 1

    # 全局高分补足配额空缺
    for c in eligible_pool:
        if len(featured) >= max_featured:
            break
        if c["candidate_id"] in featured_ids:
            continue
        if _is_heavy_overlap(c, featured):
            continue
        featured.append(c)
        featured_ids.add(c["candidate_id"])

    # 3. 灰区抢救池：未入选 featured，但原关系为 UNCERTAIN、无断句/价格/违规词硬拒因的片段
    gray_pool = []
    for c in deduped:
        cid = c["candidate_id"]
        if cid in featured_ids:
            continue
        if c["_relation"] != "UNCERTAIN":
            continue
        # 剔除有非视觉硬拒因（如断句/价格/违规词/叫卖）的候选，仅留真正纯视觉灰区
        hard_non_vis = [
            r for r in c["_rejection_reasons"]
            if not ("UNCERTAIN" in str(r) or "未通过身份核实" in str(r) or "缺失有效视觉证据" in str(r))
        ]
        if hard_non_vis:
            continue
        if "INCOMPLETE_SENTENCE" in (c.get("risks") or []):
            continue
        gray_pool.append(c)

    gray_pool.sort(
        key=lambda x: (
            1 if x.get("visual_gray_zone") else 0,
            float(x.get("vision_similarity", 0.0) or 0.0),
            float(x.get("composite_score", 0.0) or 0.0),
        ),
        reverse=True,
    )

    gray_selected: List[Dict[str, Any]] = []
    for c in gray_pool:
        if len(gray_selected) >= max_gray:
            break
        if _is_heavy_overlap(c, featured + gray_selected):
            continue
        gray_selected.append(c)

    return featured, gray_selected


def render_candidate_contact_sheets(
    candidates: List[Dict[str, Any]],
    work_dir: str,
    out_dir: str,
    grid_cols: int = 4,
    grid_rows: int = 4,
    cell_size: int = 480,
) -> Tuple[List[str], Dict[str, str]]:
    """把候选列表渲染为多张 4×4 高清联系表（1920×1920），返回 (sheet路径列表, {cid: 'S01-#04'})。"""
    os.makedirs(out_dir, exist_ok=True)
    per_sheet = grid_cols * grid_rows
    sheet_paths: List[str] = []
    slot_map: Dict[str, str] = {}

    if not candidates:
        return sheet_paths, slot_map

    font_top = _load_cjk_font(20)
    font_bot = _load_cjk_font(18)

    num_sheets = int(math.ceil(len(candidates) / float(per_sheet)))
    for s_idx in range(num_sheets):
        chunk = candidates[s_idx * per_sheet: (s_idx + 1) * per_sheet]
        canvas_w = grid_cols * cell_size
        canvas_h = grid_rows * cell_size
        canvas = Image.new("RGB", (canvas_w, canvas_h), (18, 18, 22))
        draw = ImageDraw.Draw(canvas)

        sheet_name = f"sheet_{s_idx + 1:02d}.jpg"
        sheet_rel = f"analysis/storyboards/{sheet_name}"

        for i, c in enumerate(chunk):
            r = i // grid_cols
            col = i % grid_cols
            x0 = col * cell_size
            y0 = r * cell_size
            slot_label = f"S{s_idx + 1:02d}-#{i + 1:02d}"
            cid = str(c.get("candidate_id", ""))
            slot_map[cid] = f"{sheet_rel}#{i + 1:02d}"

            frame_p = _pick_representative_frame(c, work_dir)
            if frame_p and os.path.isfile(frame_p):
                try:
                    with Image.open(frame_p) as im:
                        tile = _fit_cover_crop(im.convert("RGB"), cell_size - 4, cell_size - 4)
                    canvas.paste(tile, (x0 + 2, y0 + 2))
                except Exception:
                    draw.rectangle([x0 + 2, y0 + 2, x0 + cell_size - 2, y0 + cell_size - 2], fill=(40, 40, 46))
            else:
                draw.rectangle([x0 + 2, y0 + 2, x0 + cell_size - 2, y0 + cell_size - 2], fill=(40, 40, 46))

            # 顶部标签条（格号 + ID + 时长 + 角色）
            rel = str(c.get("_relation") or c.get("product_relation", "UNCERTAIN"))
            if rel in ("SAME_SKU", "SAME_SKU_VARIANT"):
                badge_color = (22, 145, 72)
            elif rel == "ACCESSORY_OR_STYLING":
                badge_color = (30, 118, 195)
            elif rel == "OTHER_SKU":
                badge_color = (195, 42, 42)
            else:
                badge_color = (205, 145, 20)

            draw.rectangle([x0 + 2, y0 + 2, x0 + cell_size - 2, y0 + 54], fill=(12, 12, 16))
            draw.rectangle([x0 + 2, y0 + 2, x0 + 12, y0 + 54], fill=badge_color)

            dur_s = max(0.0, (int(c.get("source_end_ms", 0)) - int(c.get("source_start_ms", 0))) / 1000.0)
            role = str(c.get("_role") or c.get("narrative_role") or "PROOF")
            top_line1 = f"{slot_label} | {dur_s:.1f}s | {role} | {c.get('shot_scale', '?')}"
            top_line2 = cid[:34]
            draw.text((x0 + 18, y0 + 4), top_line1, fill=(255, 255, 255), font=font_top)
            draw.text((x0 + 18, y0 + 29), top_line2, fill=(195, 215, 255), font=font_bot)

            # 底部信息条（SKU状态 + 相似度 + 口播前16字）
            draw.rectangle([x0 + 2, y0 + cell_size - 62, x0 + cell_size - 2, y0 + cell_size - 2], fill=(12, 12, 16))
            sim = c.get("vision_similarity")
            sim_str = f"sim={float(sim):.2f}" if sim is not None else "sim=?"
            bot_line1 = f"{rel} ({sim_str}) score={float(c.get('composite_score', 0) or 0):.0f}"
            txt_snip = str(c.get("text", "")).replace("\n", " ").strip()[:18]
            draw.text((x0 + 12, y0 + cell_size - 58), bot_line1, fill=badge_color if badge_color != (22, 145, 72) else (110, 240, 155), font=font_bot)
            draw.text((x0 + 12, y0 + cell_size - 32), txt_snip, fill=(240, 240, 240), font=font_bot)

            # 格边框
            draw.rectangle([x0 + 1, y0 + 1, x0 + cell_size - 2, y0 + cell_size - 2], outline=(70, 70, 82), width=2)

        out_p = os.path.join(out_dir, sheet_name)
        canvas.save(out_p, quality=88)
        sheet_paths.append(out_p)

    return sheet_paths, slot_map


def render_cover_contact_sheet(
    cover_candidates: List[Dict[str, Any]],
    out_path: str,
    cell_w: int = 420,
    cell_h: int = 740,
) -> Optional[str]:
    """把全部封面候选渲染为同屏对比拼板 cover_sheet.jpg，方便外部 Agent 一眼定封面。"""
    if not cover_candidates:
        return None
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    items = cover_candidates[:8]
    cols = min(4, max(1, len(items)))
    rows = int(math.ceil(len(items) / float(cols)))

    canvas = Image.new("RGB", (cols * cell_w, rows * cell_h), (18, 18, 22))
    draw = ImageDraw.Draw(canvas)
    font_top = _load_cjk_font(20)
    font_bot = _load_cjk_font(17)

    for idx, cov in enumerate(items):
        r = idx // cols
        col = idx % cols
        x0 = col * cell_w
        y0 = r * cell_h
        img_p = cov.get("image_path") or ""
        if img_p and os.path.isfile(img_p):
            try:
                with Image.open(img_p) as im:
                    tile = _fit_cover_crop(im.convert("RGB"), cell_w - 6, cell_h - 6)
                canvas.paste(tile, (x0 + 3, y0 + 3))
            except Exception:
                draw.rectangle([x0 + 3, y0 + 3, x0 + cell_w - 3, y0 + cell_h - 3], fill=(40, 40, 46))
        else:
            draw.rectangle([x0 + 3, y0 + 3, x0 + cell_w - 3, y0 + cell_h - 3], fill=(40, 40, 46))

        draw.rectangle([x0 + 3, y0 + 3, x0 + cell_w - 3, y0 + 62], fill=(12, 12, 16))
        draw.text((x0 + 12, y0 + 6), f"#{idx + 1} | {str(cov.get('id', ''))[:26]}", fill=(255, 225, 110), font=font_top)
        strat = str(cov.get("strategy", "frontal"))
        draw.text((x0 + 12, y0 + 34), f"score={float(cov.get('frontal_score', 0) or 0):.1f} | {strat}", fill=(215, 225, 240), font=font_bot)

        framing = cov.get("framing") if isinstance(cov.get("framing"), dict) else {}
        face_info = cov.get("face") if isinstance(cov.get("face"), dict) else {}
        gm_raw = cov.get("garment_match")
        if isinstance(gm_raw, dict):
            gm = gm_raw.get("score", "?")
        elif isinstance(gm_raw, (int, float)):
            gm = round(float(gm_raw), 2)
        else:
            gm = "?"
        sharp = framing.get("post_scale_sharpness", cov.get("sharpness", "?"))
        wr = framing.get("achieved_width_ratio")
        wr_s = f"{float(wr):.2f}" if isinstance(wr, (int, float)) else "?"
        yaw = face_info.get("yaw_deg", "?")
        draw.rectangle([x0 + 3, y0 + cell_h - 46, x0 + cell_w - 3, y0 + cell_h - 3], fill=(12, 12, 16))
        draw.text((x0 + 10, y0 + cell_h - 38), f"sh={sharp} wr={wr_s} yaw={yaw} gm={gm}", fill=(160, 240, 180), font=font_bot)
        draw.rectangle([x0 + 2, y0 + 2, x0 + cell_w - 3, y0 + cell_h - 3], outline=(90, 90, 105), width=2)

    canvas.save(out_path, quality=88)
    return out_path


def build_director_digest(
    work_dir: str,
    candidates: List[Dict[str, Any]],
    scores: List[Dict[str, Any]],
    cover_candidates: List[Dict[str, Any]],
    product_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """生成完整的视觉导演简报（director_brief.md / .json）与联系表拼图（storyboards/*）。"""
    analysis_dir = os.path.join(work_dir, "analysis")
    sb_dir = os.path.join(analysis_dir, "storyboards")
    os.makedirs(sb_dir, exist_ok=True)

    featured, gray_zone = _select_digest_candidates(candidates, scores, product_profile=product_profile)
    combined_for_sheets = featured + gray_zone

    sheet_paths, slot_map = render_candidate_contact_sheets(combined_for_sheets, work_dir=work_dir, out_dir=sb_dir)
    cover_sheet_path = render_cover_contact_sheet(
        cover_candidates, out_path=os.path.join(sb_dir, "cover_sheet.jpg")
    )

    def _compact_entry(c: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(c.get("candidate_id", ""))
        st_ms = int(c.get("source_start_ms", 0))
        en_ms = int(c.get("source_end_ms", 0))
        return {
            "candidate_id": cid,
            "slot_ref": slot_map.get(cid, ""),
            "source_id": c.get("source_id", ""),
            "start_s": round(st_ms / 1000.0, 2),
            "end_s": round(en_ms / 1000.0, 2),
            "duration_s": round(max(0, en_ms - st_ms) / 1000.0, 2),
            "role": c.get("_role", "PROOF"),
            "eligible": bool(c.get("_eligible", False)),
            "product_relation": c.get("_relation", c.get("product_relation", "UNCERTAIN")),
            "vision_similarity": c.get("vision_similarity"),
            "vision_engine": c.get("vision_engine", ""),
            "shot_scale": c.get("shot_scale", "UNKNOWN"),
            "composite_score": c.get("composite_score", 0.0),
            "selling_points": c.get("selling_point_evidence_refs") or [],
            "pause_margin_ms": c.get("pause_margin_ms"),
            "risks": c.get("risks") or [],
            "text": c.get("text", ""),
        }

    featured_entries = [_compact_entry(c) for c in featured]
    gray_entries = [_compact_entry(c) for c in gray_zone]

    brief_json = {
        "schema_version": "3.3",
        "total_raw_candidates": len(candidates),
        "featured_count": len(featured_entries),
        "gray_zone_count": len(gray_entries),
        "storyboard_sheets": sheet_paths,
        "cover_sheet_path": cover_sheet_path,
        "featured_candidates": featured_entries,
        "gray_zone_candidates": gray_entries,
    }
    brief_json_path = os.path.join(analysis_dir, "director_brief.json")
    with open(brief_json_path, "w", encoding="utf-8") as f:
        json.dump(brief_json, f, ensure_ascii=False, indent=2)

    # 渲染极易阅读的 Markdown 导演简报
    prod_name = (product_profile or {}).get("name", "当前商品")
    lines = [
        f"# V3.3 视觉导演简报 — {prod_name}",
        "",
        f"- **原始候选总数**：`{len(candidates)}` → **去重精选**：`{len(featured_entries)}` | **灰区待肉眼复核**：`{len(gray_entries)}`",
        f"- **封面对比拼板**：`{cover_sheet_path or '无'}`",
        "- **分镜视觉联系表**：",
    ]
    for sp in sheet_paths:
        lines.append(f"  - `{sp}`")
    lines += [
        "",
        "> **导演工作指引**：先用 `read_file` 查看上方 `cover_sheet.jpg` 与 `sheet_*.jpg` 联系表，结合下方精选台词编排 45–65s 漏斗叙事；若某条灰区候选画面确认是本品，可在 `sequence_proposal.json` 的 `visual_overrides` 中引用对应 `slot_ref` 与肉眼特征说明放行。",
        "",
        "## 一、精选合格候选池（按漏斗角色分组）",
        "",
    ]

    role_order = ("HOOK", "FABRIC", "FIT", "STYLING", "COLOR", "CLOSING", "PROOF")
    for role_name in role_order:
        grp = [e for e in featured_entries if e["role"] == role_name]
        if not grp:
            continue
        lines.append(f"### [{role_name}]（{len(grp)} 条）")
        lines.append("| 格号 | Candidate ID | 时长 | SKU/相似度 | 景别 | 得分 | 卖点 | 完整口播 |")
        lines.append("| :--- | :--- | :---: | :---: | :---: | :---: | :--- | :--- |")
        for e in grp:
            sim_txt = f"{e['vision_similarity']:.2f}" if isinstance(e["vision_similarity"], (int, float)) else "?"
            sp_txt = ",".join(e["selling_points"][:3]) if e["selling_points"] else "-"
            txt = str(e["text"]).replace("|", "/").replace("\n", " ")
            slot_short = e["slot_ref"].split("/")[-1] if e["slot_ref"] else "-"
            lines.append(
                f"| `{slot_short}` | `{e['candidate_id']}` | {e['duration_s']}s | {e['product_relation']}({sim_txt}) | {e['shot_scale']} | {e['composite_score']} | {sp_txt} | {txt} |"
            )
        lines.append("")

    if gray_entries:
        lines += [
            "## 二、灰区候选（UNCERTAIN，可看拼图后通过 `visual_overrides` 肉眼放行）",
            "",
            "| 格号 | Candidate ID | 时长 | 相似度 | 景别 | 角色 | 完整口播 |",
            "| :--- | :--- | :---: | :---: | :---: | :---: | :--- |",
        ]
        for e in gray_entries:
            sim_txt = f"{e['vision_similarity']:.2f}" if isinstance(e["vision_similarity"], (int, float)) else "?"
            txt = str(e["text"]).replace("|", "/").replace("\n", " ")
            slot_short = e["slot_ref"].split("/")[-1] if e["slot_ref"] else "-"
            lines.append(
                f"| `{slot_short}` | `{e['candidate_id']}` | {e['duration_s']}s | {sim_txt} | {e['shot_scale']} | {e['role']} | {txt} |"
            )
        lines.append("")

    brief_md_path = os.path.join(analysis_dir, "director_brief.md")
    with open(brief_md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return {
        "director_brief_md": brief_md_path,
        "director_brief_json": brief_json_path,
        "storyboard_sheets": sheet_paths,
        "cover_sheet_path": cover_sheet_path,
        "featured_count": len(featured_entries),
        "gray_zone_count": len(gray_entries),
    }
