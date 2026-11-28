# -*- coding: utf-8 -*-
"""
inspect.py - V3.3 导演按需钻取与成片接缝回看 Critic 工具（V33-04 & V33-07）
===========================================================================
1. stage_inspect_candidate: 按 ID 或条件钻取候选详情、相邻上下文台词与 3 点原帧路径；
2. stage_inspect_render: 抽取成片全部 N-1 个接缝的前/中/后 3 帧对照条拼成 seams_review.jpg，
   并运行 ContinuityAuditor + CutOptimizer 给出局部微调建议（只读不改文件）。
"""

import os
import json
from typing import Dict, Any, List, Optional
import cv2
from PIL import Image, ImageDraw

from src.core.director_digest import _load_cjk_font, _fit_cover_crop
from src.qc.continuity_qc import ContinuityAuditor
from src.core.cut_optimizer import CutOptimizer
from src.runtime.stages.common import resolve_run_dir
from src.runtime.stages.security import resolve_trusted_paths


def stage_inspect_candidate(
    run_arg: str,
    ids: Optional[List[str]] = None,
    role: Optional[str] = None,
    keyword: Optional[str] = None,
    gray_zone_only: bool = False,
    top: int = 10,
) -> Dict[str, Any]:
    """按 ID 或筛选条件钻取候选详情、相邻台词上下文与 3 点原帧路径（避免裸读 797 条大 JSON）。"""
    work_dir = resolve_run_dir(run_arg)
    candidates_path = os.path.join(work_dir, "analysis", "candidates.json")
    scores_path = os.path.join(work_dir, "analysis", "scores.json")
    if not os.path.isfile(candidates_path):
        raise FileNotFoundError(f"未找到 candidates.json: {candidates_path}，请先执行 analyze 阶段")

    with open(candidates_path, "r", encoding="utf-8") as f:
        all_candidates = json.load(f)

    score_map: Dict[str, Dict[str, Any]] = {}
    if os.path.isfile(scores_path):
        try:
            with open(scores_path, "r", encoding="utf-8") as sf:
                raw_s = json.load(sf)
            s_list = raw_s.get("scores", []) if isinstance(raw_s, dict) else (raw_s if isinstance(raw_s, list) else [])
            score_map = {s["candidate_id"]: s for s in s_list if isinstance(s, dict) and s.get("candidate_id")}
        except Exception:
            pass

    # 按源建立时间排序索引，用于提供前后相邻上下文
    by_src: Dict[str, List[Dict[str, Any]]] = {}
    for c in all_candidates:
        by_src.setdefault(str(c.get("source_id", "")), []).append(c)
    for arr in by_src.values():
        arr.sort(key=lambda x: (int(x.get("source_start_ms", 0)), int(x.get("source_end_ms", 0))))

    def _neighbor_texts(cand: Dict[str, Any]) -> Dict[str, str]:
        arr = by_src.get(str(cand.get("source_id", "")), [])
        st = int(cand.get("source_start_ms", 0))
        en = int(cand.get("source_end_ms", 0))
        prev_txt, next_txt = "", ""
        for item in arr:
            if int(item.get("source_end_ms", 0)) <= st + 200:
                prev_txt = str(item.get("text", ""))
            elif int(item.get("source_start_ms", 0)) >= en - 200 and not next_txt:
                next_txt = str(item.get("text", ""))
                break
        return {"prev_context": prev_txt, "next_context": next_txt}

    matched = []
    id_set = {x.strip() for x in (ids or []) if x and x.strip()}
    role_upper = str(role or "").strip().upper()
    kw = str(keyword or "").strip()

    for c in all_candidates:
        cid = str(c.get("candidate_id", ""))
        sc = score_map.get(cid, {})
        c_role = str(sc.get("narrative_role") or "PROOF").upper()
        if id_set and cid not in id_set:
            continue
        if role_upper and c_role != role_upper:
            continue
        if kw and kw not in str(c.get("text", "")):
            continue
        if gray_zone_only and not (c.get("visual_gray_zone") or (sc.get("product_relation") or c.get("product_relation")) == "UNCERTAIN"):
            continue
        matched.append((c, sc, c_role))

    if not id_set:
        matched.sort(key=lambda pair: float(pair[0].get("composite_score", 0.0) or 0.0), reverse=True)
        matched = matched[: max(1, int(top))]

    items = []
    for c, sc, c_role in matched:
        nb = _neighbor_texts(c)
        items.append({
            "candidate_id": c.get("candidate_id"),
            "source_id": c.get("source_id"),
            "source_file": c.get("source_file"),
            "source_start_ms": c.get("source_start_ms"),
            "source_end_ms": c.get("source_end_ms"),
            "duration_s": round(max(0, int(c.get("source_end_ms", 0)) - int(c.get("source_start_ms", 0))) / 1000.0, 2),
            "narrative_role": c_role,
            "eligible": sc.get("eligible") if "eligible" in sc else c.get("eligible"),
            "product_relation": sc.get("product_relation") or c.get("product_relation"),
            "relation_confidence": c.get("relation_confidence"),
            "vision_engine": c.get("vision_engine"),
            "vision_similarity": c.get("vision_similarity"),
            "vision_sim_stats": c.get("vision_sim_stats"),
            "visual_gray_zone": bool(c.get("visual_gray_zone", False)),
            "shot_scale": c.get("shot_scale", "UNKNOWN"),
            "roi_mode": c.get("roi_mode", ""),
            "pause_margin_ms": c.get("pause_margin_ms"),
            "composite_score": c.get("composite_score"),
            "selling_point_evidence_refs": c.get("selling_point_evidence_refs") or [],
            "risks": c.get("risks") or [],
            "rejection_reasons": sc.get("rejection_reasons") or c.get("rejection_reasons") or [],
            "frame_refs": c.get("frame_refs") or [],
            "text": c.get("text", ""),
            **nb,
        })

    return {
        "schema_version": "3.3",
        "status": "OK",
        "work_dir": work_dir,
        "matched_count": len(items),
        "candidates": items,
    }


def stage_inspect_render(run_arg: str) -> Dict[str, Any]:
    """成片接缝回看 Critic 工具（只读）：
    抽取 final.mp4 中全部 N-1 个物理接缝的 [t-0.25s, t, t+0.25s] 三帧拼成 seams_review.jpg，
    结合 ContinuityAuditor 与 CutOptimizer 产出局部微调建议。
    """
    work_dir = resolve_run_dir(run_arg)
    context_path = os.path.join(work_dir, "run_context.json")
    if not os.path.isfile(context_path):
        raise FileNotFoundError(f"未找到 run_context.json: {context_path}")
    with open(context_path, "r", encoding="utf-8") as f:
        context = json.load(f)

    paths = resolve_trusted_paths(work_dir, context)
    output_dir = paths.output_dir

    final_video = os.path.join(output_dir, "final.mp4")
    if not os.path.isfile(final_video):
        raise FileNotFoundError(f"未找到成片文件: {final_video}，请先执行 render 阶段")

    plan_path = os.path.join(work_dir, "validated_plan.json")
    if not os.path.isfile(plan_path):
        raise FileNotFoundError(f"未找到 validated_plan.json: {plan_path}")
    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)

    clips = plan.get("clips", [])
    cand_map: Dict[str, Dict[str, Any]] = {}
    cand_path = os.path.join(work_dir, "analysis", "candidates.json")
    if os.path.isfile(cand_path):
        try:
            with open(cand_path, "r", encoding="utf-8") as cf:
                for item in json.load(cf):
                    if isinstance(item, dict) and item.get("candidate_id"):
                        cand_map[item["candidate_id"]] = item
        except Exception:
            pass
    for ov in (plan.get("agent_visual_overrides") or []):
        cid = ov.get("candidate_id")
        if cid in cand_map:
            c_copy = dict(cand_map[cid])
            c_copy["product_relation"] = ov.get("override_relation", c_copy.get("product_relation"))
            cand_map[cid] = c_copy

    continuity_review = ContinuityAuditor().audit_sequence_seams(clips, cand_map)
    seams = continuity_review.get("seams", [])

    # 计算 CutOptimizer 建议（仅在副本上运行，绝不改动 validated_plan.json）
    _, optimizer_actions = CutOptimizer().optimize_cuts(clips, continuity_review)
    suggested_clip_overrides: Dict[str, Dict[str, Any]] = {}
    for s in seams:
        if s.get("suggested_action") == "SHIFT_CUT" and int(s.get("suggested_time_shift_ms", 0) or 0) != 0:
            lcid = s.get("left_candidate_id")
            if lcid:
                suggested_clip_overrides[lcid] = {
                    "cut_adjust_end_ms": int(s["suggested_time_shift_ms"]),
                    "reason": s.get("notes", "接缝微调"),
                }

    # 计算成片时间轴上的物理接缝点（若 timeline.json 存在优先采用实测时间，否则累加 dur）
    seam_times: List[float] = []
    timeline_path = os.path.join(work_dir, "timeline.json")
    if os.path.isfile(timeline_path):
        try:
            with open(timeline_path, "r", encoding="utf-8") as tf:
                tl = json.load(tf)
            tl_clips = tl if isinstance(tl, list) else (tl.get("clips") or [])
            if len(tl_clips) == len(clips) and len(tl_clips) >= 2:
                for item in tl_clips[:-1]:
                    if "timeline_end" in item:
                        seam_times.append(float(item["timeline_end"]))
                    elif "out_end" in item:
                        seam_times.append(float(item["out_end"]))
        except Exception:
            seam_times = []

    if len(seam_times) != max(0, len(clips) - 1):
        seam_times = []
        acc = 0.0
        for c in clips[:-1]:
            dur = float(c.get("dur", max(0.0, float(c.get("end", 0)) - float(c.get("st", 0)))))
            acc += dur
            seam_times.append(round(acc, 3))

    seams_review_path = None
    if seam_times:
        cell_w, cell_h = 360, 460
        header_h = 54
        row_h = cell_h + header_h
        canvas = Image.new("RGB", (3 * cell_w, len(seam_times) * row_h), (18, 18, 22))
        draw = ImageDraw.Draw(canvas)
        font_hdr = _load_cjk_font(19)
        font_sub = _load_cjk_font(16)

        cap = cv2.VideoCapture(final_video)
        try:
            for idx, t_seam in enumerate(seam_times):
                y0 = idx * row_h
                seam_meta = seams[idx] if idx < len(seams) else {}
                sev = str(seam_meta.get("severity", "PASS"))
                sev_color = (200, 45, 45) if sev == "BLOCK" else ((215, 155, 25) if sev == "WARN" else (30, 150, 80))

                left_c = clips[idx]
                right_c = clips[idx + 1]
                l_tail = str(left_c.get("text", ""))[-10:]
                r_head = str(right_c.get("text", ""))[:10]
                hdr_txt = (
                    f"Seam #{idx + 1} @ {t_seam:.2f}s [{sev}] | "
                    f"{left_c.get('candidate_id', left_c.get('id'))} -> {right_c.get('candidate_id', right_c.get('id'))}"
                )
                sub_txt = f"左尾: '...{l_tail}'  ==>  右首: '{r_head}...' | {seam_meta.get('notes', '')}"

                draw.rectangle([0, y0, 3 * cell_w, y0 + header_h], fill=(14, 14, 18))
                draw.rectangle([0, y0, 10, y0 + header_h], fill=sev_color)
                draw.text((16, y0 + 5), hdr_txt, fill=(255, 255, 255), font=font_hdr)
                draw.text((16, y0 + 30), sub_txt, fill=(200, 210, 225), font=font_sub)

                offsets = [(-0.25, "t - 0.25s (左段尾)"), (0.0, "t = 0.00s (切点)"), (0.25, "t + 0.25s (右段首)")]
                for col_i, (dt, label) in enumerate(offsets):
                    t_q = max(0.0, t_seam + dt)
                    x0 = col_i * cell_w
                    tile = None
                    if cap.isOpened():
                        cap.set(cv2.CAP_PROP_POS_MSEC, t_q * 1000.0)
                        ok, frame = cap.read()
                        if ok and frame is not None:
                            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                            tile = _fit_cover_crop(Image.fromarray(rgb), cell_w - 4, cell_h - 4)
                    if tile is not None:
                        canvas.paste(tile, (x0 + 2, y0 + header_h + 2))
                    else:
                        draw.rectangle([x0 + 2, y0 + header_h + 2, x0 + cell_w - 2, y0 + row_h - 2], fill=(38, 38, 44))
                    draw.rectangle([x0 + 2, y0 + row_h - 32, x0 + cell_w - 2, y0 + row_h - 2], fill=(12, 12, 16))
                    draw.text((x0 + 10, y0 + row_h - 27), f"{label} ({t_q:.2f}s)", fill=(235, 235, 240), font=font_sub)
                    draw.rectangle([x0 + 1, y0 + header_h + 1, x0 + cell_w - 2, y0 + row_h - 2], outline=(75, 75, 88), width=2)
        finally:
            cap.release()

        seams_review_path = os.path.join(output_dir, "seams_review.jpg")
        canvas.save(seams_review_path, quality=88)

    # V3.4 (借鉴 VideoAgent Adaptive Self-Reflection): 额外生成成品叠加层视觉自检拼图 overlay_review.jpg
    # 抽取每段真实成片中点帧（含贴纸/Hook/CTA/字幕），让导演交付前亲眼核验最终画面排版与错别字
    overlay_review_path = None
    if clips and os.path.isfile(final_video):
        sample_points: List[Dict[str, Any]] = []
        acc_t = 0.0
        for idx, c in enumerate(clips):
            dur = float(c.get("dur", max(0.0, float(c.get("end", 0)) - float(c.get("st", 0)))))
            if idx == 0:
                sample_points.append({
                    "t": min(1.2, max(0.2, dur * 0.35)),
                    "label": f"C01 Hook ({c.get('sticker_text') or '无贴纸'})",
                })
            sample_points.append({
                "t": acc_t + dur * 0.5,
                "label": f"C{idx + 1:02d} {c.get('category', '')} ({c.get('sticker_text') or '无贴纸'})",
            })
            acc_t += dur
        if acc_t > 2.0:
            sample_points.append({"t": max(0.2, acc_t - 1.1), "label": "结尾 CTA 帧"})

        ov_cols = 4
        ov_rows = (len(sample_points) + ov_cols - 1) // ov_cols
        ov_w, ov_h = 360, 640
        ov_hdr = 34
        ov_canvas = Image.new("RGB", (ov_cols * ov_w, ov_rows * (ov_h + ov_hdr)), (18, 18, 22))
        ov_draw = ImageDraw.Draw(ov_canvas)
        ov_font = _load_cjk_font(16)
        cap_ov = cv2.VideoCapture(final_video)
        try:
            for si, sp in enumerate(sample_points):
                r_i = si // ov_cols
                c_i = si % ov_cols
                x0 = c_i * ov_w
                y0 = r_i * (ov_h + ov_hdr)
                ov_draw.rectangle([x0, y0, x0 + ov_w, y0 + ov_hdr], fill=(14, 14, 18))
                ov_draw.text((x0 + 8, y0 + 7), f"{sp['label']} @ {sp['t']:.1f}s"[:30], fill=(245, 220, 120), font=ov_font)
                tile = None
                if cap_ov.isOpened():
                    cap_ov.set(cv2.CAP_PROP_POS_MSEC, max(0.0, sp["t"]) * 1000.0)
                    ok, fr = cap_ov.read()
                    if ok and fr is not None:
                        rgb = cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)
                        tile = Image.fromarray(rgb).resize((ov_w - 4, ov_h - 4), Image.Resampling.LANCZOS)
                if tile is not None:
                    ov_canvas.paste(tile, (x0 + 2, y0 + ov_hdr + 2))
                ov_draw.rectangle([x0 + 1, y0 + 1, x0 + ov_w - 2, y0 + ov_h + ov_hdr - 2], outline=(75, 75, 88), width=2)
        finally:
            cap_ov.release()
        overlay_review_path = os.path.join(output_dir, "overlay_review.jpg")
        ov_canvas.save(overlay_review_path, quality=88)

    return {
        "schema_version": "3.4",
        "status": "INSPECTED",
        "run_id": context.get("run_id"),
        "final_video": final_video,
        "cover_path": os.path.join(output_dir, "cover.jpg"),
        "seams_review_path": seams_review_path,
        "overlay_review_path": overlay_review_path,
        "seams_count": len(seam_times),
        "continuity_summary": continuity_review.get("summary", ""),
        "seams": seams,
        "optimizer_actions": optimizer_actions,
        "suggested_clip_overrides": suggested_clip_overrides,
        "next_step": f"python -m src.cli qc --run {work_dir}",
    }
