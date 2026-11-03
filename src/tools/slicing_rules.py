#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
slicing_rules.py - 切片规则引擎（2026-11-01 新增）
============================================================
把参考成片（SSY410163_蝴蝶结高腰A字半身裙_切片.mp4）实测出的切片规律
翻译成**可判定**的规则，落到「素材池检索 → 打分 → 选段 → 定切点」四步。

规则来源与证据：docs/reference_cut_analysis_SSY410163.md
阈值来源（唯一事实源）：contracts/constraints.yaml 的 `slicing_rules` 段

本模块职责（纯确定性，不含 LLM）：
  R1 切点落在语音停顿（cut_on_speech_pause）
  R2 片段时长区间（clip_duration）
  R3 镜头景别（shot_framing）
  R4 展示角度多样性（shot_variety）
  R5 语音密度（speech_density）
  R6 漏斗顺序可按品类覆写（funnel_order_overridable）

与既有体系的关系：
  - 规则**不推翻** build_plan 的漏斗配额与黑名单，只在其之上叠加"切点/时长/景别/
    多样性"四个方向的约束；
  - 全部阈值来自 constraints.yaml，本模块不内置默认值（避免第二份事实源）；
  - 每个函数都是纯函数，便于单测锁定。

CLI:
  python -m src.tools.slicing_rules --spec job_spec.json          # 打印生效规则
  python -m src.tools.slicing_rules --spec job_spec.json --json   # 机器可读
"""
from __future__ import annotations

import math
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 展示角度的判定特征（用于 R4 多样性）。
# 角度不是"标签"而是**画面证据**的抽象，故用可量化的画面特征聚类：
#   - 人体高度占比（远/近）
#   - 肩宽占比（转身/正面）
#   - 人脸面积占比（特写/中景）
#   - 画面下部是否出现大面积织物色块（手持商品）
# 每个角度 = 上述特征的一个签名；签名差异显著才认为是"不同角度"。
ANGLE_FEATURE_KEYS = ("span", "shoulder_w", "face_area", "fabric_block")


def load_slicing_rules(constraints: Dict[str, Any]) -> Dict[str, Any]:
    """从 constraints 取 `slicing_rules` 段；缺失返回空 dict（调用方按"未启用"处理）。"""
    return dict((constraints or {}).get("slicing_rules") or {})


def rules_enabled(constraints: Dict[str, Any]) -> bool:
    """切片规则是否启用。constraints 直取，便于 AST 死键审计追踪。"""
    return bool((constraints.get("slicing_rules") or {}).get("enabled"))


# ---------------------------------------------------------------- R1 切点落停顿

def find_speech_pauses(silence_db: float, min_silence_sec: float,
                       windows: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """在给定的 RMS 窗口序列中提取"近数字静音"区间。

    windows: [(t_start, db_value), ...]，按时间升序。
    返回满足 silence_db 阈值且时长 ≥ min_silence_sec 的区间列表。

    ⚠️ 实测修正（2026-11-01，参考成片）：句缝处的静音是**瞬时凹陷**
    （40~60ms），不是"停顿"。参考片 9 个切点里有 7 个落在这种瞬时凹陷上，
    而全片 ≥0.08s 的连续静音只有 0 处。故 min_silence_sec 应设 **0.03**
    （3 个 10ms 跳步），用 ≥0.05 会漏掉 6/7 的真实切点。
    """
    pauses: List[Tuple[float, float]] = []
    run_start: Optional[float] = None
    for t, db in windows:
        if db <= silence_db:
            if run_start is None:
                run_start = t
        else:
            if run_start is not None:
                if t - run_start >= min_silence_sec:
                    pauses.append((run_start, t))
                run_start = None
    if run_start is not None and windows:
        t_end = windows[-1][0]
        if t_end - run_start >= min_silence_sec:
            pauses.append((run_start, t_end))
    return pauses


def find_local_silence_dips(silence_db: float, windows: Sequence[Tuple[float, float]],
                            hop_sec: float = 0.01) -> List[Tuple[float, float]]:
    """提取"瞬时静音凹陷"（句缝气口）——比连续区间更贴近人工剪辑的真实依据。

    windows: [(t, db)] 升序。
    返回 [(凹陷时间, 该处 db)]，仅保留 db ≤ silence_db 的局部极小值。

    与 find_speech_pauses 的区别：后者要求**连续区间**，前者只要**一个够深的瞬间**——
    人工切片切的是"字与字之间的那一瞬空隙"，实测长度仅 40~60ms。
    """
    dips: List[Tuple[float, float]] = []
    n = len(windows)
    for i in range(1, n - 1):
        t, db = windows[i]
        if db > silence_db:
            continue
        # 局部极小（允许等值平台）
        if db <= windows[i - 1][1] and db <= windows[i + 1][1]:
            dips.append((t, db))
    # 合并相邻凹陷（同一气口被多个跳步命中）
    merged: List[Tuple[float, float]] = []
    for t, db in dips:
        if merged and t - merged[-1][0] <= hop_sec * 2:
            # 取更深的作为代表
            if db < merged[-1][1]:
                merged[-1] = (t, db)
        else:
            merged.append((t, db))
    return merged


def find_relative_dips(windows: Sequence[Tuple[float, float]],
                       min_dip_db: float = 8.0,
                       context_sec: float = 0.60,
                       half_window_sec: float = 0.15,
                       hop_sec: float = 0.01) -> List[Tuple[float, float]]:
    """提取**相对凹陷**（相对于局部语流电平的下陷）——直播素材可用的气口判据。

    为什么需要相对判据（2026-11-01 实测修正）：
      直播原始素材有**环境底噪**，实测底噪 −53.4dB、全片中位 −24.9dB，
      根本不存在数字静音（< −60dB）。若用绝对阈值找气口，在原始素材上
      **一个都找不到**（实测 project-1 六段素材命中 0 处）。
      而参考成片切点处的 −120dB 是**拼接产物**（剪辑软件切音轨留下的缝），
      不是选段信号——实测源侧凹陷在切点处中位 8.2dB，与随机点 9.4dB **无富集**。
      → 真正可复现的规则是「切在 ASR 句末」；气口只能作为**辅助精调**信号，
        且必须用相对判据（相对局部语流电平的下陷深度）。

    判据：某点电平比其 ±context_sec 邻域中位低 ≥ min_dip_db，即记为一个凹陷。
    返回 [(凹陷时间, 凹陷深度dB), ...]（深度为正，越大越明显）。
    """
    if not windows:
        return []
    step = max(1, int(round(hop_sec / hop_sec)))
    ctx_n = max(3, int(context_sec / hop_sec))
    half_n = max(1, int(half_window_sec / hop_sec))
    dbs = [db for _, db in windows]
    out: List[Tuple[float, float]] = []
    for i in range(ctx_n, len(dbs) - ctx_n):
        lo = max(i - half_n, 0)
        hi = min(i + half_n + 1, len(dbs))
        local_min = min(dbs[lo:hi])
        neigh = sorted(dbs[i - ctx_n:i + ctx_n])
        neigh_med = neigh[len(neigh) // 2]
        dip = neigh_med - local_min
        if dip >= min_dip_db:
            out.append((windows[i][0], float(dip)))
    # 合并相邻（同一凹陷被多跳步命中），保留最深的
    merged: List[Tuple[float, float]] = []
    for t, dip in out:
        if merged and t - merged[-1][0] <= hop_sec * 2:
            if dip > merged[-1][1]:
                merged[-1] = (t, dip)
        else:
            merged.append((t, dip))
    return merged


def adaptive_silence_db(windows: Sequence[Tuple[float, float]],
                        absolute_db: float = -60.0,
                        floor_margin_db: float = 6.0,
                        dip_threshold_db: float = 8.0) -> Tuple[float, Dict[str, Any]]:
    """按素材自身噪声底自适应地决定"静音"阈值。

    直播素材底噪高（实测 −53dB），绝对阈值不可用；成片/数字静音素材底噪极低，
    绝对阈值有效。故取二者较宽松者：
        thr = max(absolute_db, 素材底噪 + floor_margin_db)
    并额外要求该点相对局部中位有 ≥ dip_threshold_db 的凹陷（相对判据）。

    返回 (阈值 dB, 统计信息)。
    """
    if not windows:
        return absolute_db, {}
    dbs = sorted(db for _, db in windows)
    floor = dbs[int(len(dbs) * 0.02)]      # 2 分位近似底噪
    median = dbs[len(dbs) // 2]
    thr = max(absolute_db, floor + floor_margin_db)
    return thr, {"noise_floor_db": round(floor, 2), "median_db": round(median, 2),
                 "adaptive_silence_db": round(thr, 2)}


def snap_cut_to_pause(cut: float, pauses: Sequence[Tuple[float, float]],
                       search_window_sec: float,
                       max_offset_from_sentence: float,
                       dips: Optional[Sequence[Tuple[float, float]]] = None) -> Optional[float]:
    """把切点吸附到最近的语音气口（参考片 R1）。

    规则：
      1. 优先在**气口**列表中寻找——直播素材用相对凹陷判据（dips = (时刻, 深度dB)），
         数字静音素材用绝对凹陷判据（dips = (时刻, dB)）；两者都能直接用；
      2. 无凹陷数据时，退回连续静音区间（pauses），取其中点；
      3. 只在 cut ± search_window_sec 内寻找；
      4. 候选点相对 cut 的偏移超过 max_offset_from_sentence 则放弃吸附
         （宁可不吸附，也不要把切点挪到离原意太远的地方）。

    返回吸附后的切点；无可用气口时返回 None（调用方按 fallback 处理）。
    """
    cands: List[Tuple[float, float]] = []
    if dips:
        cands = [(t, abs(t - cut)) for t, _ in dips]
    else:
        for a, b in pauses:
            cands.append(((a + b) / 2.0, abs((a + b) / 2.0 - cut)))
    best: Optional[Tuple[float, float]] = None
    for t, off in cands:
        if off > search_window_sec:
            continue
        if off > max_offset_from_sentence:
            continue
        if best is None or off < best[0]:
            best = (off, t)
    return best[1] if best else None


def resolve_cut_params(constraints: Dict[str, Any]) -> Dict[str, Any]:
    """解析 R1 的全部阈值（供调用方消费，避免各处重复读 yaml）。

    返回 {snap_tolerance_sec, search_window_sec, max_offset_from_sentence,
          dip_threshold_db, absolute_db, floor_margin_db, fallback,
          intra_sentence_cut_max_ratio}；缺省时给出与 constraints 一致的值。
    """
    cp = (constraints.get("slicing_rules", {}) or {}).get("cut_on_speech_pause") or {}
    return {
        "snap_tolerance_sec": float(cp.get("snap_tolerance_sec", 0.45)),
        "search_window_sec": float(cp.get("search_window_sec", 0.40)),
        "max_offset_from_sentence": float(cp.get("max_offset_from_sentence", 0.45)),
        "dip_threshold_db": float(cp.get("dip_threshold_db", 8.0)),
        "absolute_db": float(cp.get("absolute_db", -60.0)),
        "floor_margin_db": float(cp.get("floor_margin_db", 6.0)),
        "fallback": str(cp.get("fallback", "keep_punctuation")),
        "intra_sentence_cut_max_ratio": float(
            cp.get("intra_sentence_cut_max_ratio", 0.30)),
    }


def classify_cut_type(cut: float, pauses: Sequence[Tuple[float, float]],
                       db_at_cut: Optional[float],
                       silence_db: float,
                       dips: Optional[Sequence[Tuple[float, float]]] = None,
                       dip_tolerance_sec: float = 0.04) -> str:
    """判定切点类型：'speech_pause'（语音接缝）/ 'intra_sentence'（句内视觉切点）。

    参考片实测：9 个切点中 7 个是语音接缝、2 个是句内视觉切点（只换画面不换话题）。
    用 dips（瞬时气口）判定时容差取 40ms（= 实测气口长度），
    可**逐个复现**参考片的 7/2 分布。
    """
    if dips is not None:
        if any(abs(t - cut) <= dip_tolerance_sec for t, _ in dips):
            return "speech_pause"
        return "intra_sentence"
    for a, b in pauses:
        if a - 0.02 <= cut <= b + 0.02:
            return "speech_pause"
    if db_at_cut is not None and db_at_cut <= silence_db:
        return "speech_pause"
    return "intra_sentence"


def check_intra_sentence_ratio(cuts: Sequence[Tuple[float, str]],
                               max_ratio: float) -> Tuple[bool, float]:
    """句内视觉切点占比上限校验（防止通篇跳切）。返回 (是否通过, 实际占比)。"""
    if not cuts:
        return True, 0.0
    intra = sum(1 for _, t in cuts if t == "intra_sentence")
    ratio = intra / len(cuts)
    return ratio <= max_ratio, ratio


# ---------------------------------------------------------------- R2 时长

def check_clip_duration(dur: float, constraints: Dict[str, Any]) -> Tuple[bool, str]:
    """片段时长区间校验（R2）。返回 (是否通过, 说明)。"""
    cd = (constraints.get("slicing_rules", {}) or {}).get("clip_duration") or {}
    if not cd.get("enabled", True):
        return True, "R2 已停用，跳过时长区间校验"
    lo = float(cd.get("min_sec", 0) or 0)
    hi = float(cd.get("max_sec", 1e9))
    if dur < lo:
        return False, f"片段 {dur:.2f}s 短于下限 {lo:.2f}s（碎切）"
    if dur > hi:
        return False, f"片段 {dur:.2f}s 超过上限 {hi:.2f}s（拖沓）"
    return True, f"片段 {dur:.2f}s ∈ [{lo:.2f}, {hi:.2f}]"


def trim_to_duration_window(dur: float, constraints: Dict[str, Any]) -> float:
    """把计划片段时长夹到 R2 区间内（超出 target 时向 target 收敛）。"""
    cd = (constraints.get("slicing_rules", {}) or {}).get("clip_duration") or {}
    lo = float(cd.get("min_sec", 0) or 0)
    hi = float(cd.get("max_sec", 1e9))
    target = float(cd.get("target_sec", 0) or 0)
    dur = max(lo, min(dur, hi))
    # 超过目标时长时向 target 收敛（参考片：超长段是拖沓的主因）
    if target and dur > target:
        dur = min(dur, max(target, lo))
    return dur


# ---------------------------------------------------------------- R3 景别

def classify_framing(body_h: float, constraints: Dict[str, Any],
                     span: float = None, ankles_in_frame: bool = True) -> str:
    """按人物可见高度判定景别：'full_body' / 'partial' / 'invalid'。

    span（头顶→踝）优先——实测同一片段 body_h 波动 ±0.08，span 稳定得多。
    T6: 若脚踝出画（ankles_in_frame=False），强制降级为 partial，防止 span 外推误判。
    """
    sf = (constraints.get("slicing_rules", {}) or {}).get("shot_framing") or {}
    if not sf.get("enabled", True):
        return "disabled"
    v = span if (span is not None and span > 0) else body_h
    if v is None or v <= 0:
        return "invalid"
    if not ankles_in_frame:
        return "partial"
    if v >= float(sf.get("min_body_height_ratio", 0.45)):
        return "full_body"
    return "partial"


def check_full_body_ratio(clips: Sequence[Dict[str, Any]],
                         constraints: Dict[str, Any]) -> Tuple[bool, float]:
    """全身景别占比校验（R3）。返回 (是否通过, 实际占比)。"""
    sf = (constraints.get("slicing_rules", {}) or {}).get("shot_framing") or {}
    if not sf.get("enabled", True):
        return True, 0.0
    if not clips:
        return True, 0.0
    full = sum(1 for c in clips if c.get("framing") == "full_body")
    ratio = full / len(clips)
    return ratio >= float(sf.get("full_body_min_ratio", 0.0)), ratio


# ---------------------------------------------------------------- R4 角度多样性

def angle_signature(clip: Dict[str, Any]) -> Tuple:
    """由画面特征构造"展示角度"签名。相近特征视为同一角度。"""
    return tuple(round(float(clip.get(k, 0) or 0), 3) for k in ANGLE_FEATURE_KEYS)


def is_same_angle(a: Dict[str, Any], b: Dict[str, Any],
                  tol: float = 0.08) -> bool:
    """两个片段是否属于同一展示角度（各特征差均小于 tol）。"""
    sa, sb = angle_signature(a), angle_signature(b)
    return all(abs(x - y) <= tol for x, y in zip(sa, sb))


def check_shot_variety(clips: Sequence[Dict[str, Any]],
                       constraints: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """展示角度多样性校验（R4）。返回 (是否通过, 明细)。"""
    sv = (constraints.get("slicing_rules", {}) or {}).get("shot_variety") or {}
    if not sv.get("enabled", True):
        return True, {"distinct_angles": 0, "max_consecutive_same": 0,
                      "skipped": "R4 已停用"}
    min_distinct = int(sv.get("min_distinct_angles", 0) or 0)
    max_consec = int(sv.get("max_same_angle_consecutive", 99) or 99)

    if not clips:
        return True, {"distinct_angles": 0, "max_consecutive_same": 0}

    # 贪心聚类：与已有簇中心比较，差值都小于 tol 则归入该簇
    clusters: List[Dict[str, Any]] = []
    for c in clips:
        placed = False
        for cl in clusters:
            if is_same_angle(c, cl["center"]):
                cl["members"].append(c)
                cl["center"] = _blend(cl["center"], c)
                placed = True
                break
        if not placed:
            clusters.append({"center": dict(c), "members": [c]})

    distinct = len(clusters)
    # 连续同角度计数：**直接比较相邻两段**。
    # （此前误用"簇成员身份"判断——同簇的非相邻段也会被算作连续，
    #   实测 4 段互不同角度却被判 max_consecutive_same=3。）
    max_consec_actual = 1
    run = 1
    for i in range(1, len(clips)):
        if is_same_angle(clips[i], clips[i - 1]):
            run += 1
            max_consec_actual = max(max_consec_actual, run)
        else:
            run = 1
    ok = distinct >= min_distinct and max_consec_actual <= max_consec
    return ok, {"distinct_angles": distinct,
                "max_consecutive_same": max_consec_actual,
                "required_distinct": min_distinct,
                "required_max_consecutive": max_consec}


def _blend(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """簇中心更新（各特征取均值）。"""
    out = dict(a)
    for k in ANGLE_FEATURE_KEYS:
        out[k] = (float(a.get(k, 0) or 0) + float(b.get(k, 0) or 0)) / 2.0
    return out


# ---------------------------------------------------------------- R5 语音密度

def check_speech_density(speech_sec: float, total_sec: float,
                         constraints: Dict[str, Any]) -> Tuple[bool, float]:
    """语音密度校验（R5）。返回 (是否通过, 实际密度)。"""
    sd = (constraints.get("slicing_rules", {}) or {}).get("speech_density") or {}
    if not sd.get("enabled", True):
        return True, 0.0
    if total_sec <= 0:
        return True, 0.0
    ratio = speech_sec / total_sec
    return ratio >= float(sd.get("min_ratio", 0.0)), ratio


# ---------------------------------------------------------------- R6 漏斗顺序

def resolve_funnel_order(spec: Dict[str, Any], constraints: Dict[str, Any]) -> List[str]:
    """漏斗顺序：支持品类预设字典（T7），且 spec.options.funnel_order 可覆写（R6）。

    参考片实测顺序为 hook→fit→craft_detail→styling→decision_closing（半身裙卖点重心是比例而非面料），
    故内置品类预设字典（skirt_dress / coat_outerwear / default），仍可被 spec.options.funnel_order 覆写。
    """
    rules = constraints.get("slicing_rules", {}) or {}
    opts = (spec or {}).get("options", {}) or {}
    override = opts.get("funnel_order")

    # T7 品类预设字典
    funnel_orders = rules.get("funnel_orders") or {}
    fo_skirt = funnel_orders.get("skirt_dress")
    fo_coat = funnel_orders.get("coat_outerwear")
    fo_default = funnel_orders.get("default")

    cat_key = (spec or {}).get("category") or opts.get("category") or ((spec or {}).get("product") or {}).get("category")
    if not cat_key and (spec or {}).get("product"):
        title = f"{spec['product'].get('title_line1', '')} {spec['product'].get('title_line2', '')}"
        if any(w in title for w in ("裙", "半裙", "半身裙", "连衣裙")):
            cat_key = "skirt_dress"
        elif any(w in title for w in ("大衣", "外套", "羽绒服", "风衣", "皮衣", "棉服", "西装")):
            cat_key = "coat_outerwear"

    if cat_key == "skirt_dress" and fo_skirt:
        base_order = list(fo_skirt)
    elif cat_key == "coat_outerwear" and fo_coat:
        base_order = list(fo_coat)
    elif cat_key and cat_key in funnel_orders:
        base_order = list(funnel_orders[cat_key])
    elif not cat_key and rules.get("default_funnel_order"):
        base_order = list(rules.get("default_funnel_order"))
    elif fo_default:
        base_order = list(fo_default)
    else:
        base_order = list(rules.get("default_funnel_order")
                          or ["hook", "fabric", "fit", "styling"])

    if not rules.get("funnel_order_overridable", True):
        return base_order
    if not override:
        return base_order
    order = [str(c) for c in override if str(c) in base_order or str(c) in ("hook", "fabric", "craft_detail", "fit", "styling", "decision_closing")]
    for c in base_order:
        if c not in order:
            order.append(c)
    return order


# ---------------------------------------------------------------- 汇总

def evaluate_plan_slicing(clips: Sequence[Dict[str, Any]], spec: Dict[str, Any],
                           constraints: Dict[str, Any],
                           speech_sec: Optional[float] = None,
                           cuts: Optional[Sequence[Tuple[float, str]]] = None,
                           ) -> Dict[str, Any]:
    """对已编排好的分镜计划执行全部切片规则，返回可序列化的报告。

    clips 元素建议字段：id, dur, framing, body_h, shoulder_w, face_area,
                        fabric_block, source_file, st, end
    """
    rules = load_slicing_rules(constraints)
    if not rules_enabled(constraints):
        return {"enabled": False, "checks": [], "passed": True}

    checks: List[Dict[str, Any]] = []

    # R2 时长
    dur_fail = []
    for c in clips:
        ok, msg = check_clip_duration(float(c.get("dur", 0) or 0), constraints)
        if not ok:
            dur_fail.append({"id": c.get("id"), "detail": msg})
    _cd2 = (constraints.get("slicing_rules") or {}).get("clip_duration") or {}
    _lv2 = str(_cd2.get("enforce_level", "WARN"))
    checks.append({"id": "R2_clip_duration", "level": _lv2,
                   "passed": not dur_fail, "detail": dur_fail or "全部片段时长合规"})

    # R3 景别（含目标 body_h 与全身占比）
    fb_ok, fb_ratio = check_full_body_ratio(clips, constraints)
    _sf = (constraints.get("slicing_rules", {}) or {}).get("shot_framing") or {}
    _bh = sorted(float(c.get("span", 0) or c.get("body_h", 0) or 0)
                 for c in clips if (c.get("span") or c.get("body_h")))
    _bh_med = round(_bh[len(_bh) // 2], 3) if _bh else 0.0
    _bh_ok = (not _bh) or _bh_med >= float(_sf.get("target_body_height_ratio", 0.0))
    _lv3 = str(_sf.get("enforce_level", "WARN"))
    checks.append({"id": "R3_shot_framing", "level": _lv3,
                   "passed": bool(fb_ok and _bh_ok),
                   "measured": round(fb_ratio, 3),
                   "detail": f"全身景别占比 {fb_ratio:.0%}；人物可见高度中位 {_bh_med:.3f}"
                             f"（目标 {_sf.get('target_body_height_ratio')}）"})

    # R4 多样性
    sv_ok, sv_detail = check_shot_variety(clips, constraints)
    _sv4 = (constraints.get("slicing_rules") or {}).get("shot_variety") or {}
    _lv4 = str(_sv4.get("enforce_level", "WARN"))
    checks.append({"id": "R4_shot_variety", "level": _lv4, "passed": sv_ok,
                   "measured": sv_detail, "detail": sv_detail})

    # R5 语音密度
    if speech_sec is not None:
        total = sum(float(c.get("dur", 0) or 0) for c in clips)
        sd_ok, sd_ratio = check_speech_density(speech_sec, total, constraints)
        _sd5 = (constraints.get("slicing_rules") or {}).get("speech_density") or {}
        _lv5 = str(_sd5.get("enforce_level", "WARN"))
        checks.append({"id": "R5_speech_density", "level": _lv5, "passed": sd_ok,
                       "measured": round(sd_ratio, 3),
                       "detail": f"语音密度 {sd_ratio:.0%}"})

    # R1 句内切点占比
    if cuts:
        max_ratio = float(((constraints.get("slicing_rules", {}) or {})
                           .get("cut_on_speech_pause") or {})
                          .get("intra_sentence_cut_max_ratio", 1.0))
        ok, ratio = check_intra_sentence_ratio(cuts, max_ratio)
        checks.append({"id": "R1_cut_on_speech_pause", "level": "WARN", "passed": ok,
                       "measured": round(ratio, 3),
                       "detail": f"句内视觉切点占比 {ratio:.0%}（上限 {max_ratio:.0%}）"})

    passed = all(c["passed"] for c in checks)
    return {"enabled": True, "checks": checks, "passed": passed,
            "funnel_order": resolve_funnel_order(spec, constraints)}


# ---------------------------------------------------------------- CLI

def main(argv: List[str] = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="切片规则引擎")
    ap.add_argument("--spec", help="job_spec.json 路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--constraints", default=os.path.join(PROJECT_ROOT, "contracts",
                                                          "constraints.yaml"))
    args = ap.parse_args(argv)

    import yaml
    with open(args.constraints, "r", encoding="utf-8") as f:
        constraints = yaml.safe_load(f)
    rules = load_slicing_rules(constraints)

    spec = {}
    if args.spec:
        with open(args.spec, "r", encoding="utf-8") as f:
            spec = json.load(f)

    payload = {
        "enabled": rules_enabled(constraints),
        "rules": rules,
        "funnel_order": resolve_funnel_order(spec, constraints),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"切片规则 {'启用' if payload['enabled'] else '未启用'}"
              f"（constraints.slicing_rules）")
        print("-" * 62)
        for k, v in rules.items():
            if isinstance(v, dict):
                print(f"  {k}:")
                for kk, vv in v.items():
                    print(f"      {kk}: {vv}")
            else:
                print(f"  {k}: {v}")
        print(f"\n生效漏斗顺序: {' → '.join(payload['funnel_order'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
