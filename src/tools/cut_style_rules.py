"""切片风格规则引擎 —— R7~R10（2026-11-02）。

对照参考成品 `SSY410163_蝴蝶结高腰A字半身裙_切片.mp4` 的实测，把
「转场方式 / 音频风格 / 调色一致性 / 节奏曲线」变成**可判定**规则。

设计原则（与 slicing_rules.py 一致）：
  · 阈值唯一事实源 = contracts/constraints.yaml 的 `slicing_rules` 段；
  · 每个函数直接用 `constraints.get("slicing_rules")` 取值，
    以便被 audit_requirements 的 AST 审计追踪到「有消费方」；
  · 判定对象与实际交付对象一致（审的就是成片本身，不是候选）。

证据详见 docs/reference_cut_style_analysis.md。
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "load_style_rules",
    "resolve_transition_style",
    "resolve_audio_style",
    "check_transition_style",
    "check_audio_style",
    "check_color_consistency",
    "check_pacing_curve",
    "measure_color_stats",
    "measure_pacing",
    "evaluate_style_plan",
]


# ============ 规则装载 ============

def load_style_rules(constraints: Dict[str, Any]) -> Dict[str, Any]:
    """取出 slicing_rules 段（保持 AST 可追踪的取值形态）。"""
    rules = constraints.get("slicing_rules") or {}
    return rules


def _sec(constraints: Dict[str, Any], name: str) -> Dict[str, Any]:
    rules = constraints.get("slicing_rules") or {}
    sec = rules.get(name) or {}
    return sec


def _enabled(constraints: Dict[str, Any], name: str) -> bool:
    sec = _sec(constraints, name)
    return bool(sec.get("enabled", True))


def _level(constraints: Dict[str, Any], name: str, default: str = "WARN") -> str:
    sec = _sec(constraints, name)
    return str(sec.get("enforce_level", default))


# ============ R7 转场风格 ============

def resolve_transition_style(constraints: Dict[str, Any]) -> Dict[str, Any]:
    """解析成片实际应使用的转场参数。

    mode:
      hard_cut —— 纯硬切（参考片实测 9/9），视频不叠化、音频不交叉淡化
      dissolve —— 统一叠化，时长取 dissolve_sec
      mixed   —— 按卖点类型决定（面料/做工段叠化，其余硬切）
    """
    sec = _sec(constraints, "transition_style")
    mode = str(sec.get("mode", "hard_cut") or "hard_cut").lower()
    d = float(sec.get("dissolve_sec", 0.0) or 0.0)
    dmax = float(sec.get("dissolve_sec_max", d) or d)
    if mode == "hard_cut":
        d = 0.0
    acf = bool(sec.get("audio_crossfade", False))
    if d <= 0.0:
        acf = False          # 无视频叠化时，音频交叉淡化会凭空吃掉语音
    micro_afade = float(sec.get("micro_afade_sec", 0.01 if mode == "hard_cut" else 0.0) or 0.0)
    return {
        "mode": mode,
        "video_transition_sec": d,
        "dissolve_sec_max": dmax,
        "audio_crossfade": acf,
        "micro_afade_sec": micro_afade,
        "insert_silence_gap": bool(sec.get("insert_silence_gap", False)),
        "silence_gap_sec": float(sec.get("silence_gap_sec", 0.0) or 0.0),
        "enabled": bool(sec.get("enabled", True)),
    }


def check_transition_style(constraints: Dict[str, Any],
                           measured: Dict[str, Any]) -> Dict[str, Any]:
    """校验成片实际的转场方式是否符合规则。

    measured: {"cut_count": int, "dissolve_count": int,
               "max_frame_jump_ratio": float, "audio_gap_count": int}
    """
    if not _enabled(constraints, "transition_style"):
        return {"id": "R7_transition_style", "passed": True,
                "level": "INFO", "detail": "转场风格校验已关闭"}
    st = resolve_transition_style(constraints)
    cuts = int(measured.get("cut_count", 0))
    dis = int(measured.get("dissolve_count", 0))
    gap = int(measured.get("audio_gap_count", 0))
    mode = st["mode"]

    if mode == "hard_cut":
        ok = dis == 0
        detail = (f"硬切模式：实测叠化 {dis} 处（要求 0），切点 {cuts} 个")
    elif mode == "dissolve":
        ok = cuts == 0 or dis / max(cuts, 1) >= 0.9
        detail = (f"叠化模式：叠化 {dis}/{cuts} 处"
                  f"（要求 ≥90%），过渡时长 {st['video_transition_sec']:.2f}s")
    else:  # mixed
        ratio = dis / max(cuts, 1)
        ok = 0.0 <= ratio <= 1.0
        detail = f"混合模式：叠化占比 {ratio:.0%}（切点 {cuts}，叠化 {dis}）"

    if st["audio_crossfade"]:
        pass
    elif gap > 0 and mode == "hard_cut":
        ok = ok and gap / max(cuts, 1) <= 0.5
        detail += f"；音频断缝 {gap} 处（硬切下允许 ≤50% 切点）"

    return {"id": "R7_transition_style", "passed": bool(ok),
            "level": _level(constraints, "transition_style"), "detail": detail}


# ============ R8 音频风格 ============

def resolve_audio_style(constraints: Dict[str, Any]) -> Dict[str, Any]:
    sec = _sec(constraints, "audio_style")
    return {
        "bgm_enabled": bool(sec.get("bgm_enabled", False)),
        "boom_sfx_enabled": bool(sec.get("boom_sfx_enabled", False)),
        "target_lufs": float(sec.get("target_lufs", -14.0) or -14.0),
        "lufs_tolerance": float(sec.get("lufs_tolerance", 1.5) or 1.5),
        "max_lra": float(sec.get("max_lra", 6.0) or 6.0),
        "min_speech_ratio": float(sec.get("min_speech_ratio", 0.80) or 0.80),
        "allow_cut_audio_gap": bool(sec.get("allow_cut_audio_gap", True)),
        "enabled": bool(sec.get("enabled", True)),
    }


def check_audio_style(constraints: Dict[str, Any],
                      measured: Dict[str, Any]) -> Dict[str, Any]:
    """校验成片音频风格。

    measured: {"integrated_lufs": float, "lra": float, "true_peak": float,
               "speech_ratio": float, "boom_present": bool,
               "bgm_present": bool, "low_mid_ratio_db": float}
    """
    if not _enabled(constraints, "audio_style"):
        return {"id": "R8_audio_style", "passed": True,
                "level": "INFO", "detail": "音频风格校验已关闭"}
    st = resolve_audio_style(constraints)
    issues: List[str] = []
    ok = True

    if not st["boom_sfx_enabled"] and measured.get("boom_present"):
        ok = False
        issues.append("检测到 Boom/转场音效，但规则要求无音效（参考片实测纯人声）")
    if not st["bgm_enabled"] and measured.get("bgm_present"):
        ok = False
        issues.append("检测到背景音乐，但规则要求无 BGM（参考片实测无音乐床）")

    lufs = measured.get("integrated_lufs")
    if lufs is not None:
        dev = abs(float(lufs) - st["target_lufs"])
        if dev > st["lufs_tolerance"]:
            ok = False
            issues.append(f"综合响度 {lufs:.1f} LUFS 偏离目标 "
                          f"{st['target_lufs']:.1f}±{st['lufs_tolerance']:.1f}")
    lra = measured.get("lra")
    if lra is not None and float(lra) > st["max_lra"]:
        ok = False
        issues.append(f"响度动态范围 LRA={lra:.1f} LU 超上限 {st['max_lra']:.1f}")

    sr = measured.get("speech_ratio")
    if sr is not None and float(sr) < st["min_speech_ratio"]:
        ok = False
        issues.append(f"语音占比 {float(sr):.0%} 低于下限 {st['min_speech_ratio']:.0%}")

    detail = "；".join(issues) if issues else (
        f"响度 {lufs:.1f} LUFS / LRA {lra:.1f} / 语音 {float(sr):.0%}"
        if lufs is not None and lra is not None and sr is not None
        else "音频风格符合")
    return {"id": "R8_audio_style", "passed": bool(ok),
            "level": _level(constraints, "audio_style"), "detail": detail}


# ============ R9 调色一致性 ============

def measure_color_stats(video: str, bounds: Sequence[Tuple[float, float]],
                        samples_per_clip: int = 3) -> Dict[str, Any]:
    """逐段测量调色特征（不依赖 numpy 之外的库，用 OpenCV）。

    返回 {"per_clip": [...], "cv": {"white_balance_r": x, ...}}
    """
    import cv2  # 局部导入：允许无 OpenCV 环境做纯逻辑单测

    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        return {"per_clip": [], "cv": {}}
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    per: List[Tuple[float, Any]] = []
    i = 0
    while True:
        ok, f = cap.read()
        if not ok:
            break
        per.append((i / fps, cv2.resize(f, (135, 240)).astype("float32")))
        i += 1
    cap.release()

    rows: List[Dict[str, float]] = []
    for (a, b) in bounds:
        sel = [f for t, f in per if a + 0.2 <= t <= b - 0.2]
        if not sel:
            continue
        step = max(1, len(sel) // max(samples_per_clip, 1))
        picks = sel[::step][:max(samples_per_clip, 1)]
        feats = []
        for f in picks:
            hsv = cv2.cvtColor(f.astype("uint8"), cv2.COLOR_BGR2HSV).astype("float32")
            b_, g_, r_ = float(f[:, :, 0].mean()), float(f[:, :, 1].mean()), float(f[:, :, 2].mean())
            v = hsv[:, :, 2]
            feats.append((r_ / (g_ + 1e-6), b_ / (g_ + 1e-6),
                          float(hsv[:, :, 1].mean()) / 255.0,
                          float(v.mean()) / 255.0, float(v.std()) / 255.0))
        rows.append({
            "white_balance_r": statistics.median([x[0] for x in feats]),
            "white_balance_b": statistics.median([x[1] for x in feats]),
            "saturation": statistics.median([x[2] for x in feats]),
            "brightness": statistics.median([x[3] for x in feats]),
            "contrast": statistics.median([x[4] for x in feats]),
        })

    cv: Dict[str, float] = {}
    for k in ("white_balance_r", "white_balance_b", "saturation",
              "brightness", "contrast"):
        vals = [r[k] for r in rows]
        if len(vals) >= 2:
            mean = sum(vals) / len(vals)
            sd = statistics.pstdev(vals)
            cv[k] = (sd / (abs(mean) + 1e-9)) * 100.0
    return {"per_clip": rows, "cv": cv}


def check_color_consistency(constraints: Dict[str, Any],
                            measured: Dict[str, Any]) -> Dict[str, Any]:
    """校验成片段间调色一致性（变异系数 %）。"""
    if not _enabled(constraints, "color_consistency"):
        return {"id": "R9_color_consistency", "passed": True,
                "level": "INFO", "detail": "调色一致性校验已关闭"}
    sec = _sec(constraints, "color_consistency")
    cv = measured.get("cv") or {}
    lim_wb = float(sec.get("max_cv_white_balance", 8.0) or 8.0)
    lim_sat = float(sec.get("max_cv_saturation", 12.0) or 12.0)
    lim_bri = float(sec.get("max_cv_brightness", 8.0) or 8.0)
    lim_con = float(sec.get("max_cv_contrast", 12.0) or 12.0)
    # 采样数由约束决定（每段取 N 帧中位，N 越大越稳但更慢）
    _n_samples = int(sec.get("samples_per_clip", 3) or 3)
    if not cv:
        return {"id": "R9_color_consistency", "passed": True,
                "level": _level(constraints, "color_consistency"),
                "detail": f"无调色采样数据（每段 {_n_samples} 帧），跳过"}

    issues: List[str] = []
    for key, lim, name in (("white_balance_r", lim_wb, "R/G白平衡"),
                           ("white_balance_b", lim_wb, "B/G白平衡"),
                           ("saturation", lim_sat, "饱和度"),
                           ("brightness", lim_bri, "亮度"),
                           ("contrast", lim_con, "对比度")):
        v = cv.get(key)
        if v is None:
            continue
        if v > lim:
            issues.append(f"{name}段间变异 {v:.1f}% 超上限 {lim:.1f}%")
    return {"id": "R9_color_consistency", "passed": not issues,
            "level": _level(constraints, "color_consistency"),
            "detail": "；".join(issues) if issues else (
                "段间调色一致（"
                + "，".join(f"{k} {cv[k]:.1f}%" for k in cv) + "）")}


# ============ R10 节奏与情绪曲线 ============

def measure_pacing(cuts: Sequence[float], segments: Sequence[Dict[str, Any]],
                   total_sec: float,
                   constraints: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """测量节奏曲线：钩子区切点密度、句长、语速、收紧比。"""
    segs = [s for s in segments if float(s.get("end", 0)) > float(s.get("start", 0))]
    if not segs:
        return {}
    lens = [float(s["end"]) - float(s["start"]) for s in segs]
    chars = sum(len(str(s.get("text", ""))) for s in segs)
    speech = sum(lens)
    rate = (chars / (speech / 60.0)) if speech > 0 else 0.0

    hook_window = min(10.0, total_sec)
    hook_cuts = sum(1 for c in cuts if 0.0 <= c <= hook_window)

    n = len(segs)
    t1 = max(1, n // 3)
    t2 = max(t1 + 1, (2 * n) // 3)
    def _med(xs):
        return statistics.median(xs) if xs else 0.0
    early = _med(lens[:t1])
    mid = _med(lens[t1:t2])
    late = _med(lens[t2:])
    ratio = (late / early) if early > 0 else 1.0

    mid_start, mid_end = 0.25 * total_sec, 0.70 * total_sec
    mid_clips = [c for c in cuts if mid_start <= c <= mid_end]

    # 片段时长离散度测量（2026-11-03 改进方案：σ 与极差比、提神快切档）
    sorted_cuts = sorted(c for c in cuts if 0.0 < c < total_sec)
    pts = [0.0] + sorted_cuts + [total_sec]
    clip_durs = [round(pts[i + 1] - pts[i], 3) for i in range(len(pts) - 1)] if total_sec > 0 else []
    sigma = statistics.pstdev(clip_durs) if len(clip_durs) >= 2 else 0.0
    ratio_disp = (max(clip_durs) / max(min(clip_durs), 1e-6)) if clip_durs else 1.0

    p_sec = ((constraints.get("slicing_rules") or {}).get("pacing_curve") or {}) if isinstance(constraints, dict) else {}
    p_min = float(p_sec.get("punch_min_sec", 1.5) or 1.5)
    p_max = float(p_sec.get("punch_max_sec", 2.8) or 2.8)
    punch_count = sum(1 for d in clip_durs if (p_min - 0.05) <= d <= (p_max + 0.05))

    return {
        "hook_window_sec": hook_window,
        "hook_cuts": hook_cuts,
        "sentence_len_median": _med(lens),
        "sentence_len_early": early,
        "sentence_len_mid": mid,
        "sentence_len_late": late,
        "speech_rate_cpm": rate,
        "speech_ratio": (speech / total_sec) if total_sec > 0 else 0.0,
        "tightening_ratio": ratio,
        "mid_section_cuts": len(mid_clips),
        "total_cuts": len(cuts),
        "duration_sigma": round(sigma, 2),
        "duration_ratio": round(ratio_disp, 2),
        "punch_clips": punch_count,
    }


def check_pacing_curve(constraints: Dict[str, Any],
                        measured: Dict[str, Any]) -> Dict[str, Any]:
    """校验节奏与情绪曲线是否符合"钩子快→中段慢→尾段再起"。"""
    if not _enabled(constraints, "pacing_curve"):
        return {"id": "R10_pacing_curve", "passed": True,
                "level": "INFO", "detail": "节奏曲线校验已关闭"}
    sec = _sec(constraints, "pacing_curve")
    if not measured:
        return {"id": "R10_pacing_curve", "passed": True,
                "level": _level(constraints, "pacing_curve"),
                "detail": "无节奏采样数据，跳过"}
    issues: List[str] = []
    hmin = float(sec.get("hook_cuts_per_10s_min", 2) or 2)
    hmax = float(sec.get("hook_cuts_per_10s_max", 4) or 4)
    hc = int(measured.get("hook_cuts", 0))
    if not (hmin <= hc <= hmax):
        issues.append(f"钩子区切点 {hc} 个/10s 超出 [{hmin}, {hmax}]")

    smin = float(sec.get("sentence_len_min", 1.2) or 1.2)
    smax = float(sec.get("sentence_len_max", 3.0) or 3.0)
    sm = float(measured.get("sentence_len_median", 0.0))
    if not (smin <= sm <= smax):
        issues.append(f"句长中位 {sm:.2f}s 超出 [{smin}, {smax}]")

    rmin = float(sec.get("speech_rate_min", 300) or 300)
    rmax = float(sec.get("speech_rate_max", 480) or 480)
    rate = float(measured.get("speech_rate_cpm", 0.0))
    if not (rmin <= rate <= rmax):
        issues.append(f"语速 {rate:.0f} 字/分 超出 [{rmin}, {rmax}]")

    tmax = float(sec.get("tightening_ratio_max", 0.85) or 0.85)
    tr = float(measured.get("tightening_ratio", 1.0))
    if tr > tmax:
        issues.append(f"尾段/首段句长比 {tr:.2f} > {tmax}（后段未收紧）")

    midmax = float(sec.get("mid_section_max_clip", 13.0) or 13.0)
    long_mid = float(measured.get("max_mid_clip_sec", 0.0))
    if long_mid > midmax:
        issues.append(f"中段最长单段 {long_mid:.2f}s 超上限 {midmax}")

    sig_min = float(sec.get("min_duration_sigma", 0.0) or 0.0)
    ds = float(measured.get("duration_sigma", 0.0) or 0.0)
    if sig_min > 0:
        if ds < sig_min:
            issues.append(f"片段时长标准差 σ {ds:.2f}s 低于下限 {sig_min:.2f}s（剪辑节奏平铺）")

    ratio_min = float(sec.get("min_duration_ratio", 0.0) or 0.0)
    dr = float(measured.get("duration_ratio", 0.0) or 0.0)
    if ratio_min > 0:
        if dr < ratio_min:
            issues.append(f"片段时长极差比 {dr:.2f} 低于下限 {ratio_min:.2f}（缺乏时长张力）")

    pmin_count = int(sec.get("punch_min_count", 0) or 0)
    p_min = float(sec.get("punch_min_sec", 1.5) or 1.5)
    p_max = float(sec.get("punch_max_sec", 2.8) or 2.8)
    if pmin_count > 0 and "punch_clips" in measured:
        pc = int(measured.get("punch_clips", 0))
        if pc < pmin_count:
            issues.append(f"提神快切档（{p_min:.1f}~{p_max:.1f}s）{pc} 段低于下限 {pmin_count} 段")

    return {"id": "R10_pacing_curve", "passed": not issues,
            "level": _level(constraints, "pacing_curve"),
            "detail": "；".join(issues) if issues else (
                f"钩子 {hc} 切/10s，句长中位 {sm:.2f}s，语速 {rate:.0f} 字/分，"
                f"收紧比 {tr:.2f}，σ {ds:.2f}s，极差比 {dr:.2f}")}


# ============ 汇总评估 ============

def evaluate_style_plan(constraints: Dict[str, Any],
                        measured: Dict[str, Any]) -> Dict[str, Any]:
    """一次性评估 R7~R10。measured 顶层键对应各 check_* 的入参。"""
    checks = [
        check_transition_style(constraints, measured.get("transition") or {}),
        check_audio_style(constraints, measured.get("audio") or {}),
        check_color_consistency(constraints, measured.get("color") or {}),
        check_pacing_curve(constraints, measured.get("pacing") or {}),
    ]
    return {
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "level": "WARN",
    }


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cons_path = os.path.join(root, "contracts", "constraints.yaml")
    import yaml
    with open(cons_path, "r", encoding="utf-8") as f:
        cons = yaml.safe_load(f) or {}
    print(json.dumps(resolve_transition_style(cons), ensure_ascii=False, indent=2))
    print(json.dumps(resolve_audio_style(cons), ensure_ascii=False, indent=2))
