#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
apply_violation_handling.py - 违规词三通道处理准备器（反馈驱动 2026-10-27）
=================================================================================
对齐飞书《违规整合》要求：命中词必须**彻底处理**，不残留于最终视频。

三通道（与 constraints.violation_words.handling 对应）：
  通道 1 字幕屏蔽 —— detector.mask_text 把字幕命中词替换为 ***（assembler 执行）；
  通道 2 语音消音 —— 本工具计算每个分镜内的相对消音时段（assembler 执行 volume 表达式）；
  通道 3 视频打码 —— 本工具 OCR 扫描分镜抽帧，定位画面可见命中词并生成马赛克区域
                   （assembler 执行 crop+pixelate+overlay）。

产出 violation_handling.json（assembler 与 QC 的共同依据）：
  {
    "clips": { "<id>": {"audio_mute": [[a,b]...], "video_mosaic": [...],
                        "asr_hits": [...] } },
    "subtitle_hits": [...],
    "summary": {...}
  }

CLI:
  python src/tools/apply_violation_handling.py --plan plan.json \
      --segments whisper_segments_verified.json --work-dir <dir>
"""

import os
import sys
import json
import argparse
import re
from typing import List, Dict, Any, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

import cv2
from src.tools.detect_violation import ViolationDetector
from src.tools.detect_ocr import detect_text_boxes

OUT_W, OUT_H = 1080, 1920
# 消音时段向左右各外扩的秒数（词边界 ASR 段粒度粗，外扩保底）
MUTE_PAD_SEC = 0.12
# 无 LLM 时的最小兜底：仅"强宣传语境"才删字幕（用户要求整体宽松）
STRONG_CONTEXT = [
    re.compile(r"全网\s*(最|第一)"),
    re.compile(r"销量\s*(第一|冠军)"),
    re.compile(r"(行业|国内|全球)\s*(第一|领先|顶级)"),
    re.compile(r"(第一|唯一|顶级)\s*(品牌|选择)"),
    re.compile(r"(百分之百|100%)\s*(有效|见效|瘦|安全)"),
    re.compile(r"(根治|治愈|治疗|药效|消炎|抗癌)"),
]
# OCR 命中帧的马赛克时间窗（相对帧时刻）
MOSAIC_PRE_SEC = 0.6
MOSAIC_POST_SEC = 1.2


def _load_constraints(path: str = None) -> Dict[str, Any]:
    path = path or os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
    if not os.path.exists(path):
        return {}
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _overlap(a0: float, a1: float, b0: float, b1: float) -> Optional[Tuple[float, float]]:
    """两个区间的重叠部分；无重叠返回 None。"""
    lo, hi = max(a0, b0), min(a1, b1)
    return (lo, hi) if hi - lo > 0.01 else None


def _clip_canvas_map(src_w: int, src_h: int, effect: str) -> Tuple[float, float, float]:
    """源帧坐标 → 分镜画布坐标的线性映射参数。
    返回 (scale, x_off, mirrored)。assemble 的 crop 为居中 1080 宽、无垂直裁剪。"""
    scale = OUT_W / float(src_w) if src_w else 1.0
    x_off = (src_w - OUT_W) / 2.0 if src_w > OUT_W else 0.0
    return scale, x_off, (effect == "hflip")


def _hit_context(h: Dict[str, Any], window: int = 24) -> str:
    """命中词所在句（ASR 段文本）——Agent 复核的最小语境。"""
    seg_text = h.get("segment_text") or ""
    s, e = h.get("start"), h.get("end")
    if isinstance(s, int) and isinstance(e, int) and seg_text:
        return seg_text[max(0, s - window): min(len(seg_text), e + window)]
    return seg_text


def _review_request_payload(hits: List[Dict[str, Any]],
                            work_dir: str) -> Dict[str, Any]:
    """构造 agent 复核请求（流水线只出题，判定由 agent 完成）。"""
    import time
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "purpose": "违规词复核：判断每条候选在当前语境中是否真需从字幕删除",
        "policy": (
            "从宽判定：尺码（最大码/最高码）、序数时间（第一眼/第一次）、口语、"
            "否定、转述、非排他表述一律放行；仅语义确属虚假宣传/医疗功效才删词。"
            "处置仅限从字幕删除（不改音频、不画面打码）。"
        ),
        "candidates": [{
            "index": i,
            "word": h.get("word"),
            "category": h.get("category"),
            "category_name": h.get("category_name"),
            "context": _hit_context(h),
            "t_start": h.get("t_start"),
            "t_end": h.get("t_end"),
            "source_file": h.get("source_file", ""),
        } for i, h in enumerate(hits)],
        "respond_to": os.path.join(os.path.abspath(work_dir),
                                   "violation_decisions.json"),
        "response_schema": {
            "items": [{"word": "命中词", "t_start": 0.0,
                       "action": "delete_subtitle | none",
                       "reason": "不超过30字"}],
            "delete_words": ["可选：全局删除词，按词匹配"],
        },
    }


def _load_agent_decisions(work_dir: str) -> Tuple[Dict[Tuple[str, float], str], Dict[str, str]]:
    """读取 agent 回写的决策（violation_decisions.json）。

    返回 ({(word, t_start): action}, {word: action})。
    文件缺失/非法 → 空 dict（调用方走宽松兜底，不阻断流程）。
    """
    path = os.path.join(work_dir, "violation_decisions.json")
    if not os.path.exists(path):
        return {}, {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[VIOLATION] agent 决策文件解析失败（走兜底）: {e}")
        return {}, {}
    by_ts: Dict[Tuple[str, float], str] = {}
    by_word: Dict[str, str] = {}
    for it in (data.get("items") or []):
        w = str(it.get("word") or "").strip()
        action = str(it.get("action") or "").strip()
        if not w or action not in ("delete_subtitle", "none"):
            continue
        try:
            ts = round(float(it.get("t_start", -1)), 2)
        except (TypeError, ValueError):
            ts = -1.0
        if ts >= 0:
            by_ts[(w, ts)] = action
        by_word.setdefault(w, action)
    for w in (data.get("delete_words") or []):
        w = str(w).strip()
        if w:
            by_word[w] = "delete_subtitle"
    return by_ts, by_word


def prepare_violation_handling(
    plan: Dict[str, Any],
    segments: List[Dict[str, Any]],
    work_dir: str,
    constraints: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """生成处置参数（2026-10-30 简化版）。

    用户明确：真实违禁词**只从字幕里删掉**（不显示即可），声音继续、画面不打码。
    判定由 Agent 一轮复核完成；LLM 不可用时仅兜底硬违规类目（其余放行）。

    产出 violation_handling.json：
      subtitle_delete_words —— assembler 从字幕文本中删除的词
      decisions             —— 每条候选的裁决（action/依据/理由），供 QC 与人审
      clips                 —— 消音/打码（默认关闭，配置开启时才生成）
    """
    constraints = constraints or _load_constraints()
    vw_c = constraints.get("violation_words", {}) or {}
    detect_c = vw_c.get("detect", {}) or {}
    handling_c = vw_c.get("handling", {}) or {}
    review_c = vw_c.get("review", {}) or {}
    scan_frames = int(detect_c.get("scan_frames", 6) or 6)
    pad_px = int(handling_c.get("mosaic_pad_px", 24) or 24)

    det = ViolationDetector(constraints=constraints)
    hits = det.scan_segments(segments)

    max_candidates = int(review_c.get("max_candidates", 40) or 40)
    # 2026-10-30：复核由 agent 完成（流水线不内置 LLM，不依赖 API Key）。
    # 流水线职责 = 产出复核请求；agent 审阅后回写 violation_decisions.json。
    req_path = None
    if review_c.get("enabled", True) and hits:
        req = _review_request_payload(hits[:max_candidates], work_dir)
        req_path = os.path.join(work_dir, "violation_review_request.json")
        try:
            os.makedirs(work_dir, exist_ok=True)
            with open(req_path, "w", encoding="utf-8") as f:
                json.dump(req, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[VIOLATION] 复核请求落盘失败: {e}")
    by_ts, by_word = _load_agent_decisions(work_dir)
    review_engine = ("agent-file" if (by_ts or by_word)
                     else "fallback (agent review pending)")

    hard_cats = tuple(review_c.get("fallback_hard_categories",
                                    ["medical_claim", "false_absolutism"]) or ())
    decisions: List[Dict[str, Any]] = []
    for i, h in enumerate(hits):
        key = (str(h.get("word") or ""), round(float(h.get("t_start") or 0.0), 2))
        action = by_ts.get(key) or by_word.get(key[0])
        if action is not None:
            violation = (action == "delete_subtitle")
            basis, conf = "agent", 0.95
            reason = "agent 复核：删除字幕" if violation else "agent 复核：放行"
        else:
            strong = next((p.pattern for p in STRONG_CONTEXT
                           if p.search(_hit_context(h))), None)
            violation = (h.get("category") in hard_cats) or bool(strong)
            basis = ("fallback-hard" if h.get("category") in hard_cats
                     else ("fallback-strong" if strong else "fallback-lenient"))
            conf = 0.8 if violation else 0.3
            reason = ("硬违规类目兜底" if h.get("category") in hard_cats
                      else (f"强宣传语境兜底（{strong}）" if strong
                            else "宽松放行（未交 Agent）"))
        decisions.append({
            "word": h.get("word"),
            "category": h.get("category"),
            "category_name": h.get("category_name"),
            "context": _hit_context(h),
            "t_start": h.get("t_start"),
            "t_end": h.get("t_end"),
            "source_file": h.get("source_file", ""),
            "action": "delete_subtitle" if violation else "none",
            "confidence": round(float(conf), 2),
            "basis": basis,
            "reason": reason,
        })

    delete_words = sorted({d["word"] for d in decisions
                           if d["action"] == "delete_subtitle"})

    clips_out: Dict[str, Any] = {}
    n_mute_clips = 0
    n_mosaic_clips = 0
    mute_on = bool(handling_c.get("audio_mute", False))
    mosaic_on = bool(handling_c.get("video_mosaic", False))

    for c in plan.get("clips", []):
        cid = c["id"]
        src = c.get("source_file") or plan.get("raw_video_path")
        st, end = float(c.get("st", 0)), float(c.get("end", 0))
        dur = float(c.get("dur", end - st))
        effect = c.get("effect", "none")
        zoom = float(c.get("zoom", 1.0) or 1.0)

        clip_hits = []
        for d in decisions:
            if d["action"] != "delete_subtitle":
                continue
            if d.get("source_file") and src and \
                    os.path.abspath(d["source_file"]) != os.path.abspath(src):
                continue
            ov = _overlap(st, end, d.get("t_start") or 0, d.get("t_end") or 0)
            if ov is None:
                continue
            clip_hits.append({**d, "rel_range": [
                round(max(0.0, ov[0] - st - MUTE_PAD_SEC), 3),
                round(min(dur, ov[1] - st + MUTE_PAD_SEC), 3)]})

        audio_mute: List[List[float]] = []
        if mute_on and clip_hits:
            ranges = sorted([h["rel_range"] for h in clip_hits], key=lambda r: r[0])
            for r in ranges:
                if audio_mute and r[0] <= audio_mute[-1][1] + 0.05:
                    audio_mute[-1][1] = max(audio_mute[-1][1], r[1])
                else:
                    audio_mute.append(list(r))
            n_mute_clips += 1

        video_mosaic: List[Dict[str, Any]] = []
        if mosaic_on and src and os.path.exists(src) and zoom <= 1.0:
            video_mosaic = _scan_clip_frames_for_violation(
                src, st, dur, effect, det, scan_frames, pad_px)
            if video_mosaic:
                n_mosaic_clips += 1

        clips_out[cid] = {
            "audio_mute": audio_mute,
            "video_mosaic": video_mosaic,
            "asr_hits": [{k: h.get(k) for k in
                          ("word", "category", "category_name", "t_start", "t_end")}
                         for h in clip_hits],
        }

    n_delete = sum(1 for d in decisions if d["action"] == "delete_subtitle")
    result = {
        "clips": clips_out,
        "subtitle_delete_words": delete_words,
        "decisions": decisions,
        "subtitle_hits": [{
            "word": d["word"], "category": d["category_name"],
            "t_start": d["t_start"], "t_end": d["t_end"],
            "action": d["action"], "reason": d["reason"],
        } for d in decisions],
        "summary": {
            "n_candidates": len(decisions),
            "n_delete_subtitle": n_delete,
            "n_none": len(decisions) - n_delete,
            "n_hits": len(hits),
            "review_engine": review_engine,
            "review_request": req_path,
            "n_clips_with_mute": n_mute_clips,
            "n_clips_with_mosaic": n_mosaic_clips,
            "mute_pad_sec": MUTE_PAD_SEC,
            "channels": {
                "subtitle_action": "delete",
                "audio_mute": mute_on,
                "video_mosaic": mosaic_on,
            },
        },
        "applied": {},
    }
    os.makedirs(work_dir, exist_ok=True)
    out_path = os.path.join(work_dir, "violation_handling.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    s = result["summary"]
    print(f"[VIOLATION] 处置参数已生成: {out_path}")
    print(f"  候选 {s['n_candidates']} → 字幕删除 {s['n_delete_subtitle']} / "
          f"放行 {s['n_none']} | 复核引擎 {s['review_engine']} | "
          f"消音 {'on' if mute_on else 'off'} / 打码 {'on' if mosaic_on else 'off'}")
    return result


def _scan_clip_frames_for_violation(
    src: str, st: float, dur: float, effect: str,
    det: ViolationDetector, scan_frames: int, pad_px: int,
) -> List[Dict[str, Any]]:
    """OCR 扫描分镜抽帧，返回画面可见违规词的马赛克区域列表（画布坐标）。"""
    if dur <= 0.5:
        return []
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        return []
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    scale, x_off, mirrored = _clip_canvas_map(src_w, src_h, effect)

    regions: List[Dict[str, Any]] = []
    times = [st + dur * (i + 0.5) / max(1, scan_frames)
             for i in range(scan_frames)]
    seen_words = set()
    for t in times:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ret, frame = cap.read()
        if not ret or frame is None:
            continue
        try:
            boxes = detect_text_boxes(frame)
        except Exception:
            continue
        for b in boxes:
            text = (b.get("text") or "").strip()
            if not text:
                continue
            for h in det.scan_text(text):
                key = (h["word"], round(h["start"] / 2))
                if key in seen_words:
                    continue
                seen_words.add(key)
                bx = b.get("box") or []
                if len(bx) < 4:
                    continue
                x0, y0, x1, y1 = [float(v) for v in bx[:4]]
                # 源帧 → 画布坐标（含镜像翻转）
                cx0 = (x0 - x_off) * scale
                cx1 = (x1 - x_off) * scale
                if mirrored:
                    cx0, cx1 = OUT_W - cx1, OUT_W - cx0
                cy0, cy1 = y0 * (OUT_H / src_h if src_h else 1.0), \
                    y1 * (OUT_H / src_h if src_h else 1.0)
                cx0 = max(0, int(cx0) - pad_px)
                cy0 = max(0, int(cy0) - pad_px)
                cx1 = min(OUT_W, int(cx1) + pad_px)
                cy1 = min(OUT_H, int(cy1) + pad_px)
                if cx1 - cx0 < 8 or cy1 - cy0 < 8:
                    continue
                regions.append({
                    "box": [cx0, cy0, cx1, cy1],
                    "t0": round(max(0.0, t - st - MOSAIC_PRE_SEC), 3),
                    "t1": round(min(dur, t - st + MOSAIC_POST_SEC), 3),
                    "word": h["word"],
                    "category": h["category_name"],
                })
    cap.release()
    return regions


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="违规词处置准备（agent 复核契约）")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--segments", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--decisions",
                        help="agent 回写的 violation_decisions.json（缺省读 work-dir 下同名文件）")
    parser.add_argument("--emit-only", action="store_true",
                        help="仅产出复核请求（供 agent 审阅），不生成处置参数")
    args = parser.parse_args(argv)

    with open(args.plan, "r", encoding="utf-8") as f:
        plan = json.load(f)
    with open(args.segments, "r", encoding="utf-8") as f:
        segments = json.load(f)

    if args.decisions and os.path.abspath(args.decisions) != os.path.abspath(
            os.path.join(args.work_dir, "violation_decisions.json")):
        import shutil
        shutil.copyfile(args.decisions,
                        os.path.join(args.work_dir, "violation_decisions.json"))

    if args.emit_only:
        from src.tools.detect_violation import ViolationDetector
        det = ViolationDetector()
        hits = det.scan_segments(segments)
        req = _review_request_payload(hits, args.work_dir)
        path = os.path.join(args.work_dir, "violation_review_request.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(req, f, ensure_ascii=False, indent=2)
        print(f"[VIOLATION] 复核请求已产出（{len(hits)} 条候选）: {path}")
        return 0

    result = prepare_violation_handling(plan, segments, args.work_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
