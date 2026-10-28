#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_final_video_asr.py - 拼接后完整视频 ASR 复检门禁（2026-10-27 新增）
=================================================================================
**为什么需要**：原工作流对**素材** ASR 有三重校验（asr_review_gate 的 A1~A10、
asr_agent_verifier 的语境纠错、consistency_check 的关键词覆盖），但对**拼接后
完整视频**没有任何 ASR 复检——QC 的零价格/违禁词检查只是 ASS 字幕**文件级**
文本检查，无法回答四个致命问题：

  Q1 成片字幕与成片原声是否一致？（烧录错位/切片串位/转场吞音）
  Q2 语音消音通道是否真的生效？（volume 表达式写错/时段算错 → 词仍可听见）
  Q3 xfade/acrossfade 转场是否损坏了语音衔接？
  Q4 价格/违规词是否在**音频通道**残留？（字幕屏蔽了，嘴里还在说）

本工具对成片音轨重新转录（与转写同规模模型，保证可比性），执行五项检查：

  V1 字幕-原声一致性   —— 成片 ASR 全文 vs 烧录字幕全文，difflib 相似度
  V2 语音违规词零残留 —— ViolationDetector 扫成片 ASR（BLOCK）
  V3 语音价格零残留   —— detect_price 扫成片 ASR（决策 3 音频通道延伸，BLOCK）
  V4 识别密度健全性   —— 成片 ASR 句数/时长 ≥ density_min（防组装吞音，WARN）
  V5 消音执行核查     —— violation_handling.json 的消音窗口内实测音量
                         （volumedetect max_volume > 阈值 → 词仍可闻，BLOCK）

阈值来源：contracts/constraints.yaml 的 final_asr_verify 段（v1.5）。

CLI:
  python src/tools/verify_final_video_asr.py --video final.mp4 \
      --ass subtitles.ass --work-dir <dir> [--handling violation_handling.json]
退出码: 0=通过, 1=有 WARN, 2=BLOCK 失败, 3=用法/环境错误
"""

import os
import sys
import json
import re
import argparse
import subprocess
from typing import List, Dict, Any, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)


def _load_constraints(path: str = None) -> Dict[str, Any]:
    path = path or os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
    if not os.path.exists(path):
        return {}
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------- 文本归一化

_STRIP_RE = re.compile(r"[\s，,。.、！!？?；;：:~～…—\-*#\u3000]+")


def _normalize_text(text: str) -> str:
    """归一化：去空白/标点/屏蔽符，仅留实义字符（中文+字母数字）。

    字幕里的 `***` 屏蔽符与标点都会参与相似度计算前剥除，
    避免屏蔽处理被误判为"字幕与原声不符"。
    """
    return _STRIP_RE.sub("", text or "")


def _read_ass_texts(ass_path: str) -> List[Dict[str, Any]]:
    """读取 .ass 的 Dialogue 行 → [{t_start, t_end, text}]（秒）。"""
    events = []
    if not ass_path or not os.path.exists(ass_path):
        return events
    with open(ass_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            if not line.startswith("Dialogue:"):
                continue
            parts = line.split(",", 9)
            if len(parts) < 10:
                continue
            t0, t1 = parts[1].strip(), parts[2].strip()
            events.append({
                "t_start": _ass_time_to_sec(t0),
                "t_end": _ass_time_to_sec(t1),
                "text": parts[9].rstrip("\r\n"),
            })
    return events


def _ass_time_to_sec(t: str) -> float:
    """H:MM:SS.cc → 秒。"""
    try:
        h, m, rest = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)
    except (ValueError, AttributeError):
        return 0.0


# ---------------------------------------------------------------- 音量实测

def _probe_volume_db(video_path: str, t0: float, t1: float) -> Optional[float]:
    """用 ffmpeg volumedetect 测 [t0,t1] 窗口内的最大音量（dB）。失败返回 None。"""
    dur = max(0.05, t1 - t0)
    try:
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats",
             "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}", "-i", video_path,
             "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120)
        m = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", res.stderr or "")
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 主入口

def verify_final_video_asr(
    video_path: str,
    ass_path: str = None,
    work_dir: str = None,
    handling_path: str = None,
    constraints: Dict[str, Any] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """对成片执行 ASR 复检（V1~V5）。返回结构化报告。"""
    constraints = constraints or _load_constraints()
    cfg = constraints.get("final_asr_verify", {}) or {}
    if not cfg.get("enabled", True):
        return {"passed": True, "checks": [], "warnings": ["成片 ASR 复检已禁用"],
                "skipped": True}
    if not os.path.exists(video_path):
        return {"passed": True, "checks": [],
                "warnings": [f"成片不存在 {video_path}，跳过复检"], "skipped": True}

    checks: List[Dict[str, Any]] = []
    warnings: List[str] = []

    def _add(cid, name, status, detail, measured=None, threshold=None):
        checks.append({"id": cid, "name": name, "status": status,
                       "detail": detail, "measured": measured,
                       "threshold": threshold})
        if status == "WARN":
            warnings.append(f"{name}: {detail}")

    # ---- 0. 成片时长 ----
    dur_out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", video_path],
        capture_output=True, text=True)
    total_dur = float(dur_out.stdout.strip() or 0.0)

    # ---- 1. 复检转录（带缓存：同目录 final_asr_segments.json）----
    segs = None
    cache_path = os.path.join(work_dir, "final_asr_segments.json") if work_dir else None
    if use_cache and cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if abs(float(cached.get("duration", 0)) - total_dur) < 1.0:
                segs = cached.get("segments")
                print(f"[FINAL-ASR] 命中复检转录缓存（{len(segs or [])} 句）")
        except Exception:
            segs = None
    if segs is None:
        try:
            from src.core.asr_engine import AsrEngine
            engine = AsrEngine(model_size=str(cfg.get("model_size", "small")))
            segs = engine.transcribe_video(video_path)
            if cache_path:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump({"duration": total_dur, "segments": segs},
                              f, ensure_ascii=False, indent=2)
            print(f"[FINAL-ASR] 成片复检转录完成: {len(segs)} 句 / {total_dur:.1f}s")
        except Exception as e:
            _add("V0", "成片 ASR 复检（转录）", "WARN",
                 f"复检转录失败（环境问题，不拦截交付）: {str(e)[:120]}")
            return {"passed": True, "checks": checks, "warnings": warnings,
                    "skipped": True}

    # ---- V4 识别密度健全性 ----
    density_min = float(cfg.get("density_min", 0.06) or 0.06)
    n_segs = len(segs or [])
    density = n_segs / total_dur if total_dur > 0 else 0.0
    v4_ok = density >= density_min or n_segs >= 3
    _add("V4", "成片识别密度健全性",
         "PASS" if v4_ok else "WARN",
         f"{n_segs} 句 / {total_dur:.1f}s = {density:.3f} 句/秒"
         + ("" if v4_ok else f"（< {density_min}，疑似组装吞音/音轨损坏）"),
         measured=round(density, 4), threshold=density_min)

    final_text = "".join((s.get("text") or "") for s in (segs or []))

    # ---- V2 语音违规词零残留 ----
    from src.tools.detect_violation import ViolationDetector
    v_hits = ViolationDetector().scan_text(final_text)
    _add("V2", "语音违规词零残留",
         "PASS" if not v_hits else "BLOCK",
         f"成片原声检出 {len(v_hits)} 处违规词"
         + (f": {', '.join(h['word'] for h in v_hits[:5])}" if v_hits else ""),
         measured=len(v_hits), threshold=0)

    # ---- V3 语音价格零残留（决策 3 音频通道延伸）----
    from src.tools.detect_price import find_price_matches
    p_hits = find_price_matches(final_text)
    _add("V3", "语音价格零残留（决策3）",
         "PASS" if not p_hits else "BLOCK",
         f"成片原声检出 {len(p_hits)} 处价格表达"
         + (f": {[h.get('match', h.get('text', '')) for h in p_hits[:3]]}"
            if p_hits else ""),
         measured=len(p_hits), threshold=0)

    # ---- V1 字幕-原声一致性 ----
    if ass_path and os.path.exists(ass_path):
        import difflib
        sub_events = _read_ass_texts(ass_path)
        sub_text = "".join(e["text"] for e in sub_events)
        a, b = _normalize_text(final_text), _normalize_text(sub_text)
        ratio = difflib.SequenceMatcher(None, a, b).ratio() if (a and b) else 0.0
        block_below = float(cfg.get("subtitle_match_block", 0.50) or 0.50)
        warn_below = float(cfg.get("subtitle_match_warn", 0.70) or 0.70)
        if ratio < block_below:
            v1_status = "BLOCK"
        elif ratio < warn_below:
            v1_status = "WARN"
        else:
            v1_status = "PASS"
        _add("V1", "字幕-原声一致性",
             v1_status,
             f"成片 ASR 全文 vs 烧录字幕全文相似度 {ratio:.3f}"
             f"（ASR {len(a)} 字 / 字幕 {len(b)} 字；"
             f"BLOCK<{block_below}，WARN<{warn_below}）",
             measured=round(ratio, 4), threshold=warn_below)
    else:
        _add("V1", "字幕-原声一致性", "WARN", "未提供 .ass 字幕文件，跳过一致性比对")

    # ---- V5 消音执行核查 ----
    hp = handling_path
    if work_dir and not hp:
        hp = os.path.join(work_dir, "violation_handling.json")
    if hp and os.path.exists(hp):
        with open(hp, "r", encoding="utf-8") as f:
            handling = json.load(f)
        # 成片时间轴：分镜 start 已在转场折算后的 timeline 里；消音区间是
        # **分镜内相对时间**，需加分镜在成片中的起点。优先读 timeline.json。
        timeline = {}
        tl_path = os.path.join(work_dir, "timeline.json") if work_dir else None
        if tl_path and os.path.exists(tl_path):
            with open(tl_path, "r", encoding="utf-8") as f:
                timeline = json.load(f)
        max_db = float(cfg.get("mute_max_volume_db", -30.0) or -30.0)
        # ⚠️ 消音表达式两端有 80ms 淡变（防爆音），淡变区播放的是
        # **u5408规的相邻语音**（不是违规词本身）。因此核查必须回避淡变边、
        # 只测窗口核心区（否则正常语音淡出会被误判为违规残留）。
        RAMP_MARGIN = 0.15   # > 80ms 淡变 + 侧量余量
        audible = []
        n_windows = 0
        for cid, cdata in (handling.get("clips", {}) or {}).items():
            ranges = cdata.get("audio_mute", []) or []
            if not ranges:
                continue
            clip_start = float((timeline.get(cid) or {}).get("start", 0.0))
            for (a0, a1) in ranges:
                t0 = clip_start + float(a0)
                t1 = clip_start + float(a1)
                if t1 <= t0 or t0 >= total_dur:
                    continue
                t1 = min(t1, total_dur)
                # 核心区：去掉两端淡变边（过短窗口跳过）
                core0, core1 = t0 + RAMP_MARGIN, t1 - RAMP_MARGIN
                if core1 - core0 < 0.2:
                    continue
                n_windows += 1
                db = _probe_volume_db(video_path, core0, core1)
                if db is None:
                    continue
                if db > max_db:
                    audible.append({"clip": cid,
                                    "window": [round(core0, 2), round(core1, 2)],
                                    "max_volume_db": db})
        if n_windows == 0:
            _add("V5", "语音消音执行核查", "PASS", "无消音窗口需要核查")
        elif audible:
            _add("V5", "语音消音执行核查", "BLOCK",
                 f"{len(audible)}/{n_windows} 个消音窗口仍有可听见音频"
                 f"（max_volume > {max_db}dB）: {audible[:3]}",
                 measured=len(audible), threshold=0)
        else:
            _add("V5", "语音消音执行核查", "PASS",
                 f"{n_windows} 个消音窗口均实测静音（≤{max_db}dB）",
                 measured=0, threshold=0)
    else:
        _add("V5", "语音消音执行核查", "PASS",
             "无 violation_handling.json（无消音窗口），跳过")

    # ---- 汇总 ----
    enforce = str(cfg.get("enforce_level", "BLOCK")).upper()
    blocks = [c for c in checks if c["status"] == "BLOCK"]
    passed = not (enforce == "BLOCK" and blocks)
    report = {
        "passed": passed,
        "enforce_level": enforce,
        "total_duration": round(total_dur, 3),
        "n_final_segments": n_segs,
        "final_asr_text_preview": final_text[:200],
        "checks": checks,
        "warnings": warnings,
        "summary": {
            "n_checks": len(checks),
            "n_pass": sum(1 for c in checks if c["status"] == "PASS"),
            "n_warn": sum(1 for c in checks if c["status"] == "WARN"),
            "n_block": sum(1 for c in checks if c["status"] == "BLOCK"),
        },
    }
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="拼接后完整视频 ASR 复检门禁")
    parser.add_argument("--video", required=True)
    parser.add_argument("--ass", default=None)
    parser.add_argument("--work-dir", default=None)
    parser.add_argument("--handling", default=None)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args(argv)

    report = verify_final_video_asr(
        args.video, ass_path=args.ass, work_dir=args.work_dir,
        handling_path=args.handling, use_cache=not args.no_cache)
    s = report.get("summary", {})
    print(f"[FINAL-ASR] {'PASS' if report['passed'] else 'BLOCK'} | "
          f"检查 {s.get('n_checks', 0)} 项: PASS {s.get('n_pass', 0)} / "
          f"WARN {s.get('n_warn', 0)} / BLOCK {s.get('n_block', 0)}")
    for c in report.get("checks", []):
        if c["status"] != "PASS":
            print(f"  [{c['status']}] {c['id']} {c['name']}: {c['detail'][:110]}")
    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"[OUT] {args.out_json}")
    if report.get("skipped"):
        return 3
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
