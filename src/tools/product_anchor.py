#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
product_anchor.py — 商品锚点驱动的选段守卫（project-4 v2 规范 P0 实现）
=====================================================================
依据 docs/project-4_clip_selection_spec_v2.md 与
contracts/product_anchor_project-4.yaml（唯一真值源）。

解决的问题（G-01~G-06）：
  1. 原 build_blocks 把 729 句合并成 27 个 30~100s 的巨型块，一个块里混合
     多种卖点甚至夹带他品话术 → 粒度太粗，无法做"关键片段"判定。
  2. 商品主体从未进入选段目标函数；他品块只扣分不排除。
  3. 仓库从未定义"必备卖点清单"，选段器收敛到最容易命中关键词的一小段。

本模块提供：
  load_anchor(spec)                 从 spec.options.product_anchor 加载契约
  label_text(text, anchor)          句级 本品/他品/中性 三分类 + 卖点标签
  build_eligible_windows(...)       句级准入 → 3~12s 窗口白名单 eligible_windows
  enforce_selection(...)            卖点清单驱动 + 单源占比上限 + Hook 主体明确
  audit_plan(...)                   成片级可验证指标 M01/M03/M05/M10/M11

设计约束：
  * 唯一真值源 = YAML 契约；不内置第二套词表。
  * 他品硬排除（含时间邻域扩展），绝不用"改字幕"代替"改选段"。
  * 仅在 spec.options.product_anchor 存在时启用；未启用时行为零变化。
  * 窗口边界落在 ASR 句末，天然满足 slicing_rules R1 的 ±0.45s 容差。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_ANCHOR_REL = "contracts/product_anchor_project-4.yaml"

# 句间最大间隔（与 build_plan.MERGE_GAP_SEC 一致）：超过则断开窗口
MERGE_GAP_SEC = 0.6
# 主体明确词：Hook 首镜 MUST 命中其一（有质感无主体不合格，K-06）
SUBJECT_TERMS = ("毛衣", "针织", "上衣", "开衫", "羊毛衫")


def load_anchor(spec_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """加载商品锚点契约。spec.options.product_anchor 未设置时返回 None（不启用）。"""
    opts = (spec_data or {}).get("options", {}) or {}
    rel = opts.get("product_anchor")
    if not rel:
        return None
    path = rel if os.path.isabs(rel) else os.path.join(PROJECT_ROOT, rel)
    if not os.path.exists(path):
        print(f"[PRODUCT-ANCHOR][WARN] 契约不存在，退回原选段逻辑: {path}")
        return None
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            anchor = yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001
        print(f"[PRODUCT-ANCHOR][WARN] 契约解析失败，退回原选段逻辑: {e}")
        return None
    if not anchor.get("product") or not anchor.get("selling_points"):
        print("[PRODUCT-ANCHOR][WARN] 契约缺 product/selling_points，退回原选段逻辑")
        return None
    anchor["_path"] = os.path.abspath(path)
    return anchor


def label_text(text: str, anchor: Dict[str, Any]) -> Dict[str, Any]:
    """句级三分类 + 必备卖点标签（F-01/F-02、K-02）。"""
    prod = anchor["product"]
    txt = str(text or "")
    other = [str(w) for w in (prod.get("negative_terms") or []) if str(w) in txt]
    hero = [str(w) for w in (prod.get("positive_terms") or []) if str(w) in txt]
    points = [
        str(k) for k, v in (anchor.get("selling_points") or {}).items()
        if any(str(w) in txt for w in (v or {}).get("terms", []))
    ]
    if other:
        tag = "他品"
    elif hero:
        tag = "本品"
    else:
        tag = "中性"
    return {
        "product_tag": tag,
        "other_hits": other,
        "hero_hits": hero,
        "selling_points": points,
        "subject_clear": any(w in txt for w in SUBJECT_TERMS),
    }


def _window_reason(lab: Dict[str, Any], text: str) -> str:
    bits = []
    if lab["selling_points"]:
        bits.append("卖点:" + "/".join(lab["selling_points"]))
    if lab["hero_hits"]:
        bits.append("本品词:" + "/".join(lab["hero_hits"][:3]))
    if lab["subject_clear"]:
        bits.append("主体明确")
    return "；".join(bits) or "本品上下文"


def build_eligible_windows(
    segments: Sequence[Dict[str, Any]],
    anchor: Dict[str, Any],
    work_dir: str = None,
    min_win_sec: float = 3.0,
    target_win_sec: float = 7.0,
    max_win_sec: float = 12.0,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """句级准入 → 3~12s 候选窗口白名单。

    返回 (windows, meta)。windows 结构兼容 build_plan 的 block 契约
    （start/end/dur/text/segments/source_file/source_tag），并额外携带
    `_anchor` 字段供下游 clip_schema / QC 消费。

    硬规则：
      F-01/F-02 命中 negative_terms 的句子判「他品」并直接剔除；
      F-04     命中他品的句子前后各扩展 other_gap_expand_sec 一并剔除；
      K-02     合并后跨句产生他品词的窗口整窗丢弃；
      K-04     窗口 3~12s，起止落在 ASR 句末（R1 ±0.45s）。
    """
    gates = anchor.get("gates", {}) or {}
    expand = float(gates.get("other_gap_expand_sec", 5) or 5)
    clip_min, clip_max = (gates.get("clip_duration_sec") or [3, 12])[:2]
    min_win_sec = max(min_win_sec, float(clip_min))
    max_win_sec = min(max_win_sec, float(clip_max))

    # 价格零容忍（price.subtitle_assert_zero / 决策网关 plan_selection）：
    # 原 build_blocks 用 is_blacklisted→find_price_matches 在句级剔除，
    # 锚点模式绕过了 build_blocks，必须补回同款过滤，否则「31 个绵羊毛」
    # 这类成分比例中的裸数字会被网关当成价格表达拦截（实测 R1_digit_sequence）。
    try:
        from src.tools.detect_price import find_price_matches as _price_fn
    except Exception:  # noqa: BLE001
        _price_fn = None

    # —— 视觉通道硬排除（V-01 / 2026-11-05 2s 网格 + 34 窗全覆盖复核）——
    # 有些他款「同为上装」：口播仍含毛衣/上衣等本品词，文本通道无法区分。
    # 目视复核结论写入 forbidden_windows：无 time_ranges = 整源硬排除；
    # 有 time_ranges = 只排除该源内 [start, end] 秒区间（源内相对时间，
    # 与逐源 ASR 时间戳同口径），避免为两段坏镜头误杀整个优质源。
    forbidden = ((anchor.get("product") or {}).get("forbidden_windows") or [])
    forbidden_ranges: List[Tuple[str, float, float]] = []
    for f in forbidden:
        if not isinstance(f, dict):
            continue
        mark = str(f.get("source_contains") or "")
        if not mark:
            continue
        ranges = f.get("time_ranges") or []
        if not ranges:
            forbidden_ranges.append((mark, -1.0, float("inf")))
            continue
        for rg in ranges:
            try:
                a, b = float(rg[0]), float(rg[1])
            except (TypeError, ValueError, IndexError):
                continue
            forbidden_ranges.append((mark, min(a, b), max(a, b)))
    excluded_rows = 0
    excluded_sources: Dict[str, int] = {}

    def _forbidden(src: str, st: float, en: float) -> bool:
        return any(mark in str(src) and en > a and st < b
                   for mark, a, b in forbidden_ranges)

    rows: List[Dict[str, Any]] = []
    for s in segments:
        src = str(s.get("source_file") or "")
        try:
            st = float(s.get("start", 0.0))
            en = float(s.get("end", st))
        except (TypeError, ValueError):
            continue
        if en <= st:
            en = st + 0.5
        if _forbidden(src, st, en):
            excluded_rows += 1
            k = os.path.basename(src)
            excluded_sources[k] = excluded_sources.get(k, 0) + 1
            continue
        txt = str(s.get("text") or "").strip()
        if not txt:
            continue
        if _price_fn and _price_fn(txt):
            continue
        lab = label_text(txt, anchor)
        rows.append({
            "text": txt, "start": st, "end": en,
            "source_file": src,
            "source_tag": str(s.get("source_tag") or ""),
            "seg": dict(s), "lab": lab,
        })

    # —— F-04 时间邻域扩展 ——
    # 逐句各扩 N 秒在密集 ASR 里会误杀全片（实测 72 个他品句 × 前后 5s
    # → 729 句中 651 句被剔除，白名单为空）。改为：先把同源、时间连续的
    # 强他品句聚成「换款区间」，只对区间首尾各扩 expand 秒；
    # 弱信号词（内里/退货率）只剔除该句及其紧邻 1s，不触发区间扩展。
    strong_markers = (
        "裤", "库长", "裤长", "毛呢裤", "毛泥裤", "膝盖", "裤腿", "西裤",
        "卫裤", "皮裤", "棉服", "棉浮", "填充物", "舔充物",
        "83848586", "加100条", "加一百条",
    )

    def _is_strong(r: Dict[str, Any]) -> bool:
        return any(any(m in h for m in strong_markers)
                   for h in r["lab"]["other_hits"])

    by_src: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_src.setdefault(r["source_file"], []).append(r)

    bad_intervals: List[Tuple[str, float, float]] = []
    for src, rs in by_src.items():
        rs.sort(key=lambda x: x["start"])
        cur_a = cur_b = None
        for r in rs:
            if r["lab"]["product_tag"] != "他品" or not _is_strong(r):
                continue
            if cur_a is None:
                cur_a, cur_b = r["start"], r["end"]
            elif r["start"] - cur_b <= 4.0:      # 4s 内视为同一换款段
                cur_b = r["end"]
            else:
                bad_intervals.append((src, cur_a - expand, cur_b + expand))
                cur_a, cur_b = r["start"], r["end"]
        if cur_a is not None:
            bad_intervals.append((src, cur_a - expand, cur_b + expand))
        for r in rs:
            if r["lab"]["product_tag"] == "他品" and not _is_strong(r):
                bad_intervals.append((src, r["start"] - 1.0, r["end"] + 1.0))

    def _in_bad(r: Dict[str, Any]) -> bool:
        return any(r["source_file"] == s and r["start"] < b and r["end"] > a
                   for s, a, b in bad_intervals)

    n_other = sum(1 for r in rows if r["lab"]["product_tag"] == "他品")
    # 句级只做「不在剔除区 + 非他品」；是否具备卖点由窗口级复查决定，
    # 避免把连续话术切碎导致窗口起止脱离句末。
    keep = [
        r for r in rows
        if not _in_bad(r) and r["lab"]["product_tag"] != "他品"
    ]

    # —— 按源聚合成 3~12s 窗口（绝不跨源合并） ——
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in keep:
        groups.setdefault(r["source_file"], []).append(r)

    windows: List[Dict[str, Any]] = []
    for src, rs in groups.items():
        rs.sort(key=lambda x: x["start"])
        cur: List[Dict[str, Any]] = []

        def _flush() -> None:
            nonlocal cur
            if not cur:
                return
            st, en = cur[0]["start"], cur[-1]["end"]
            dur = round(en - st, 3)
            cur, batch = [], cur
            if dur < min_win_sec:
                return
            if _forbidden(src, st, en):   # 窗口级复查：跨禁入区间边界整窗丢弃
                return
            text = "，".join(x["text"] for x in batch)
            if _price_fn and _price_fn(text):  # 跨句伪价格短语复查
                return
            lab = label_text(text, anchor)
            if lab["product_tag"] == "他品":   # K-02 跨句伪短语复查
                return
            # K-02：至少一通道强支持。本品词 MUST 命中——只有卖点词而无
            # 主体词的窗口（如「100 初酸的面料」）可能讲的是别的款，剔除。
            if not lab["hero_hits"]:
                return
            wid = "w%03d" % (len(windows) + 1)
            windows.append({
                "window_id": wid,
                "start": round(st, 3),
                "end": round(en, 3),
                "dur": dur,
                "text": text,
                "segments": [x["seg"] for x in batch],
                "source_file": src,
                "source_tag": rs[0].get("source_tag", ""),
                # 中性默认值：避免 build_plan 对数十个窗口逐个解码视频做
                # wrist_motion 探测（既不加权也不惩罚，保持打分稳定）。
                "wrist_motion": 0.05,
                "_anchor": {
                    "window_id": wid,
                    "product_tag": lab["product_tag"],
                    "selling_points": lab["selling_points"],
                    "hero_hits": lab["hero_hits"],
                    "other_hits": lab["other_hits"],
                    "subject_clear": lab["subject_clear"],
                    "reason": _window_reason(lab, text),
                },
            })

        for r in rs:
            if cur and r["start"] - cur[-1]["end"] > MERGE_GAP_SEC:
                _flush()
            cur.append(r)
            if cur[-1]["end"] - cur[0]["start"] >= target_win_sec:
                _flush()
        _flush()

    # 超长窗口按句边界再切（兜底，保证 ≤ max_win_sec）
    split: List[Dict[str, Any]] = []
    for w in windows:
        if w["dur"] <= max_win_sec:
            split.append(w)
            continue
        acc: List[Dict[str, Any]] = []
        for sg in w["segments"]:
            if acc and float(sg.get("end", 0)) - acc[0]["start"] > max_win_sec:
                piece = dict(w)
                piece.update({
                    "start": round(acc[0]["start"], 3),
                    "end": round(acc[-1]["end"], 3),
                    "dur": round(acc[-1]["end"] - acc[0]["start"], 3),
                    "text": "，".join(str(x.get("text", "")) for x in acc),
                    "segments": list(acc),
                })
                if piece["dur"] >= min_win_sec:
                    split.append(piece)
                acc = []
            acc.append(sg)
        if acc:
            piece = dict(w)
            piece.update({
                "start": round(acc[0]["start"], 3),
                "end": round(acc[-1]["end"], 3),
                "dur": round(acc[-1]["end"] - acc[0]["start"], 3),
                "text": "，".join(str(x.get("text", "")) for x in acc),
                "segments": list(acc),
            })
            if piece["dur"] >= min_win_sec:
                split.append(piece)
    windows = split
    for i, w in enumerate(windows):
        wid = "w%03d" % (i + 1)
        w["window_id"] = wid
        w["_anchor"]["window_id"] = wid

    by_point: Dict[str, int] = {}
    for w in windows:
        for p in w["_anchor"]["selling_points"]:
            by_point[p] = by_point.get(p, 0) + 1
    by_source: Dict[str, float] = {}
    for w in windows:
        k = os.path.basename(w["source_file"])
        by_source[k] = round(by_source.get(k, 0.0) + w["dur"], 2)

    meta = {
        "enabled": True,
        "anchor_path": anchor.get("_path", ""),
        "product": anchor["product"].get("name", ""),
        "total_sentences": len(rows),
        "other_sentences": n_other,
        "other_gap_expand_sec": expand,
        "excluded_by_neighborhood": len(rows) - n_other - len(keep),
        "eligible_windows": len(windows),
        "eligible_total_sec": round(sum(w["dur"] for w in windows), 2),
        "windows_by_selling_point": by_point,
        "windows_by_source_sec": by_source,
        "visual_forbidden_windows": [
            {"source_contains": str(f.get("source_contains") or ""),
             "time_ranges": f.get("time_ranges") or [],
             "reason": str(f.get("reason") or "")}
            for f in forbidden if isinstance(f, dict)
        ],
        "visual_excluded_sources": sorted(excluded_sources),
        "visual_excluded_sentences": excluded_rows,
        "clip_duration_sec": [min_win_sec, max_win_sec],
        "target_window_sec": target_win_sec,
    }

    if work_dir:
        os.makedirs(work_dir, exist_ok=True)
        out = os.path.join(work_dir, "eligible_windows.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({
                "project_id": anchor.get("product", {}).get("id", ""),
                "meta": meta,
                "windows": [
                    {k: v for k, v in w.items() if k != "segments"}
                    for w in windows
                ],
            }, f, ensure_ascii=False, indent=2)
        print(f"[PRODUCT-ANCHOR] 白名单已落盘: {out} "
              f"（{len(windows)} 窗 / {meta['eligible_total_sec']:.1f}s）")
    return windows, meta


def _points(blocks: Sequence[Dict[str, Any]]) -> set:
    out = set()
    for b in blocks:
        out.update((b.get("_anchor") or {}).get("selling_points", []))
    return out


def _source_ratio(blocks: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    total = sum(float(b.get("dur", 0) or 0) for b in blocks) or 1.0
    acc: Dict[str, float] = {}
    for b in blocks:
        k = os.path.basename(str(b.get("source_file") or "?"))
        acc[k] = acc.get(k, 0.0) + float(b.get("dur", 0) or 0)
    return {k: round(v / total, 4) for k, v in acc.items()}


def _legacy_enforce_selection(
    ordered: List[Dict[str, Any]],
    pool: Sequence[Dict[str, Any]],
    anchor: Dict[str, Any],
    log=print,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """S2 后置守卫：卖点清单驱动 + 单源占比上限 + Hook 主体明确。

    只做「等价替换」（时长相近、位置不变），因此不破坏配额/句末吸附/时长
    修剪等既有逻辑；替换后由 build_plan 重算 total 并走原有超限/不足分支。
    """
    gates = anchor.get("gates", {}) or {}
    sp_def = anchor.get("selling_points") or {}
    p0 = [k for k, v in sp_def.items() if (v or {}).get("level") == "P0"]
    max_ratio = float(gates.get("max_single_source_ratio", 0.40) or 0.40)
    min_sources = int(gates.get("min_source_count", 3) or 3)

    # ordered 中的块多为 dict(b, ...) 副本或修剪后的新对象，用对象 id 判重会失效。
    # 以「源 + 起始时刻」作为稳定键（修剪不改变起始时刻）。
    def _key(b: Dict[str, Any]):
        return (os.path.basename(str(b.get("source_file") or "")),
                round(float(b.get("start", 0) or 0), 2))

    used = {_key(b) for b in ordered}
    notes: List[str] = []

    def _candidates(must_point: str = None, must_subject: bool = False,
                    avoid_source: str = None) -> List[Dict[str, Any]]:
        out = []
        for b in pool:
            if _key(b) in used:
                continue
            a = b.get("_anchor") or {}
            if a.get("product_tag") == "他品":
                continue
            if must_point and must_point not in a.get("selling_points", []):
                continue
            if must_subject and not a.get("subject_clear"):
                continue
            if avoid_source and os.path.basename(str(b.get("source_file") or "")) == avoid_source:
                continue
            out.append(b)
        out.sort(key=lambda b: (
            len((b.get("_anchor") or {}).get("selling_points", [])),
            float(b.get("dur", 0) or 0),
        ), reverse=True)
        return out

    def _victim(ordered_: List[Dict[str, Any]], covered: set,
                near_dur: float, avoid_idx: set = None) -> Optional[int]:
        """挑一个「卖点全被别人覆盖」且时长最接近的块作为替换位。"""
        best, best_score = None, -1e9
        for i, b in enumerate(ordered_):
            if avoid_idx and i in avoid_idx:
                continue
            pts = set((b.get("_anchor") or {}).get("selling_points", []))
            if not pts:
                redundant = 0.5
            else:
                others = set()
                for j, o in enumerate(ordered_):
                    if j != i:
                        others.update((o.get("_anchor") or {}).get("selling_points", []))
                redundant = len(pts - others)
            # 冗余度越低越该被换掉；时长越接近越好
            score = -redundant * 10 - abs(float(b.get("dur", 0) or 0) - near_dur)
            if score > best_score:
                best, best_score = i, score
        return best

    # —— K-05：7 类必备卖点，P0 四类 MUST 各 ≥1 段 ——
    for point in list(sp_def.keys()):
        covered = _points(ordered)
        if point in covered:
            continue
        cands = _candidates(must_point=point)
        if not cands:
            notes.append(f"卖点 {point} 无可用白名单窗口（素材缺口）")
            continue
        cand = cands[0]
        idx = _victim(ordered, covered, float(cand.get("dur", 0) or 0)) if ordered else None
        if idx is None:
            ordered.append(cand)
        else:
            old = ordered[idx]
            used.discard(_key(old))
            ordered[idx] = cand
        used.add(_key(cand))
        notes.append(f"补齐卖点 {point} ← {cand.get('window_id')} "
                     f"({os.path.basename(str(cand.get('source_file') or ''))[:14]})")

    # —— M05：单源占比 ≤40%、源数 ≥3 ——
    for _ in range(8):
        ratios = _source_ratio(ordered)
        over = [(k, v) for k, v in ratios.items() if v > max_ratio]
        n_src = len(ratios)
        if not over and n_src >= min_sources:
            break
        if over:
            worst = max(over, key=lambda x: x[1])[0]
            same = [i for i, b in enumerate(ordered)
                    if os.path.basename(str(b.get("source_file") or "")) == worst]
            if not same:
                break
            # 换掉该源里卖点最冗余的一块
            cand_list = _candidates(avoid_source=worst)
            if not cand_list:
                break
            idx = _victim(ordered, _points(ordered),
                          float(ordered[same[0]].get("dur", 0) or 0),
                          avoid_idx=set(range(min(1, len(ordered))))) or same[0]
            old = ordered[idx]
            if os.path.basename(str(old.get("source_file") or "")) == worst:
                used.discard(_key(old))
                ordered[idx] = cand_list[0]
                used.add(_key(cand_list[0]))
                notes.append(f"单源 {worst} 占比 {ratios[worst]:.0%} 超限 → 换位 "
                             f"{cand_list[0].get('window_id')}")
                continue
        if n_src < min_sources:
            new_sources = {os.path.basename(str(b.get("source_file") or ""))
                           for b in ordered}
            cand = next((c for c in _candidates()
                         if os.path.basename(str(c.get("source_file") or "")) not in new_sources),
                        None)
            if not cand:
                break
            idx = _victim(ordered, _points(ordered), float(cand.get("dur", 0) or 0))
            if idx is None:
                break
            old = ordered[idx]
            used.discard(_key(old))
            ordered[idx] = cand
            used.add(_key(cand))
            notes.append(f"源数 {n_src}<{min_sources} → 引入 "
                         f"{os.path.basename(str(cand.get('source_file') or ''))[:14]}")

    # —— K-06：Hook 首镜主体明确 ——
    if ordered and not (ordered[0].get("_anchor") or {}).get("subject_clear"):
        cands = _candidates(must_subject=True)
        if cands:
            cand = cands[0]
            old = ordered[0]
            used.discard(_key(old))
            ordered[0] = cand
            used.add(_key(cand))
            notes.append(f"Hook 主体不明 → 换为 {cand.get('window_id')}"
                         f"（{cand.get('text', '')[:24]}）")
        else:
            notes.append("Hook 主体明确：无可用强主体窗口（素材缺口）")

    # —— C5：每个入选分镜 MUST 为本品（分镜级主体一致性 100%）——
    # 他品块、以及只有卖点词无主体词的中性块，都用白名单本品窗口等价替换，
    # 优先选时长最接近者，避免破坏总时长与配额。
    for i, b in enumerate(list(ordered)):
        if (b.get("_anchor") or {}).get("product_tag") == "本品":
            continue
        cands = [c for c in _candidates()
                 if (c.get("_anchor") or {}).get("product_tag") == "本品"
                 # 首镜替换不得破坏 K-06「Hook 主体明确」
                 and (i != 0 or (c.get("_anchor") or {}).get("subject_clear"))]
        if not cands:
            notes.append(f"C5 非本品块 {b.get('window_id', '?')} 无合规替换，"
                         f"保留由 QC 门禁拦截")
            continue
        orig_pts = set((b.get("_anchor") or {}).get("selling_points", []))

        def _replace_cost(c):
            kept = len(orig_pts & set(
                (c.get("_anchor") or {}).get("selling_points", [])))
            return (-kept,
                    abs(float(c.get("dur", 0) or 0)
                        - float(b.get("dur", 0) or 0)))

        cand = min(cands, key=_replace_cost)
        used.discard(_key(b))
        ordered[i] = cand
        used.add(_key(cand))
        notes.append(f"C5 非本品块 {b.get('window_id', '?')}"
                     f"({(b.get('_anchor') or {}).get('product_tag')}) "
                     f"→ 本品 {cand.get('window_id')}")

    ratios = _source_ratio(ordered)
    stats = {
        "selling_points_covered": sorted(_points(ordered)),
        "selling_points_total": len(sp_def),
        "p0_covered": sorted(set(p0) & _points(ordered)),
        "p0_total": len(p0),
        "source_ratios": ratios,
        "source_count": len(ratios),
        "max_single_source_ratio": max(ratios.values()) if ratios else 0.0,
        "hook_subject_clear": bool(ordered and
                                   (ordered[0].get("_anchor") or {}).get("subject_clear")),
        "notes": notes,
    }
    for n in notes:
        log(f"[PRODUCT-ANCHOR] {n}")
    return ordered, stats


def _is_param_block(b: Dict[str, Any]) -> bool:
    """参数类（面料/做工）窗口判定，口径与 QC「参数类内容占比复测」一致。"""
    cats = set(b.get("categories") or [])
    return bool(cats & {"fabric", "craft_detail"})


def _qc_category(b: Dict[str, Any]) -> str:
    """预测 build_plan 在无 _quota_cat 时给该块定的 category（QC 统计口径）。

    build_plan: 取 categories 中非 hook 的最高分类别；全为 hook 时取原表。
    """
    cats = list(b.get("categories") or [])
    pool = [c for c in cats if c != "hook"] or cats
    if not pool:
        return "styling"
    scores = b.get("scores") or {}
    return str(max(pool, key=lambda c: scores.get(c, 0)))


def _compose_by_selling_points(
    ordered: List[Dict[str, Any]],
    pool: Sequence[Dict[str, Any]],
    anchor: Dict[str, Any],
    log=print,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """按 7 类必备卖点清单重新组装分镜（v2 规范 §3 步 5）。

    取代「漏斗配额贪心 + 后置替换」——后者会让成片挤在同一卖点上、
    参数类内容超 25% BLOCK 上限、总时长溢出 65s。本函数直接以
    eligible_windows 白名单为唯一候选集，按卖点清单驱动组装：
      P0 四类各 ≥1 段 → Hook 主体强句置顶 → 余量按未用源/低重复补齐
      → 参数类占比 ≤25% → 单源 ≤40%、源数 ≥3 → 叙事顺序排序。
    """
    gates = anchor.get("gates", {}) or {}
    sp_def = anchor.get("selling_points") or {}
    max_ratio = float(gates.get("max_single_source_ratio", 0.40) or 0.40)
    min_sources = int(gates.get("min_source_count", 3) or 3)
    max_param_ratio = 0.25      # features.fabric_ratio=strict 的 BLOCK 上限
    target_total = 58.0
    hard_max = 61.0             # build_plan 的 dur_max = 62 - 0.5 安全余量

    def _a(b):
        return b.get("_anchor") or {}

    def _wid(b):
        return str(_a(b).get("window_id") or b.get("window_id") or "")

    def _key(b):
        # V-02：同一白名单窗口经「配额修剪余料」会产生多个不同 start 的
        # 副本。旧复合键把它们当成不同窗口，实测 c07_fit / c08_fit 同属
        # w002 且源时间窗 63.46-69.9 与 65.14-69.9 高度重叠，成片出现
        # 重复画面。以 window_id 为唯一身份；无 ID 时退回「源+起止」。
        wid = _wid(b)
        if wid:
            return "w:" + wid
        return "t:%s:%.2f:%.2f" % (
            os.path.basename(str(b.get("source_file") or "")),
            round(float(b.get("start", 0) or 0), 2),
            round(float(b.get("end", 0) or 0), 2))

    def _src(b):
        return os.path.basename(str(b.get("source_file") or ""))

    def _dur(b):
        return float(b.get("dur", 0) or 0)

    def _total(bs):
        return sum(_dur(b) for b in bs)

    # —— 候选集：本品白名单窗口，按 window_id 去重（pool 含修剪余料副本）——
    # 同一窗口保留「最完整」的副本（时长最长、起始最早），避免余料以
    # 另一个窗口身份二次入选造成重复画面。
    best: Dict[str, Dict[str, Any]] = {}
    for b in pool:
        a = _a(b)
        if a.get("product_tag") != "本品" or not a.get("hero_hits"):
            continue
        if not (2.5 <= _dur(b) <= 12.5):
            continue
        k = _key(b)
        old = best.get(k)
        if old is None or (_dur(b), -float(b.get("start", 0) or 0)) > (
                _dur(old), -float(old.get("start", 0) or 0)):
            best[k] = b
    cands = list(best.values())
    for b in cands:
        # 统一由内容关键词决定 category：清除配额贪心遗留的 _quota_cat，
        # 否则 QC 的参数类占比会受旧漏斗标签污染、不可预测。
        b.pop("_quota_cat", None)
        b["_is_param"] = _is_param_block(b)
        b["_qc_cat"] = _qc_category(b)
        b["_qc_fabric"] = str(b["_qc_cat"]).startswith("fabric")

    def _overlap_sec(x, y) -> float:
        """同源两个窗口的时间轴重叠秒数（跨源不算重复画面）。"""
        if _src(x) != _src(y):
            return 0.0
        return max(0.0,
                   min(float(x.get("end", 0) or 0), float(y.get("end", 0) or 0))
                   - max(float(x.get("start", 0) or 0),
                         float(y.get("start", 0) or 0)))

    def _conflicts(b, chosen_) -> bool:
        # 同源重叠 >0.5s 判定为重复画面（不同 window_id 的相邻/交叠窗口同理）
        return any(_overlap_sec(b, c) > 0.5 for c in chosen_)

    p0 = [k for k, v in sp_def.items() if (v or {}).get("level") == "P0"]
    p1 = [k for k, v in sp_def.items() if (v or {}).get("level") != "P0"]
    pick_order = p0 + p1
    narrative = ["composition", "color", "craft", "quality",
                 "fit", "size", "cta"]
    notes: List[str] = []

    def _primary(b):
        for p in narrative:
            if p in _a(b).get("selling_points", []):
                return p
        return ""

    chosen: List[Dict[str, Any]] = []
    used = set()

    def _pick(point: str, used_srcs: set):
        cc = [b for b in cands
              if point in _a(b).get("selling_points", [])
              and _key(b) not in used
              and not _conflicts(b, chosen)]
        if not cc:
            return None
        # 优先非参数类（保住 25% 硬上限）、优先新素材源、时长贴近 6s、
        # 卖点覆盖多的窗口优先（一段顶多类，节省时长）。
        cc.sort(key=lambda b: (
            1 if b["_is_param"] else 0,
            1 if _src(b) in used_srcs else 0,
            abs(_dur(b) - 6.0),
            -len(_a(b).get("selling_points", [])),
        ))
        return cc[0]

    # —— 1) 7 类必备卖点各 ≥1 段（P0 优先）——
    for p in pick_order:
        b = _pick(p, {_src(x) for x in chosen})
        if b is None:
            notes.append(f"卖点 {p} 无可用白名单窗口（素材缺口）")
            continue
        chosen.append(b)
        used.add(_key(b))
        notes.append(f"卖点 {p} ← {_a(b).get('window_id')} "
                     f"({_src(b)[:14]}, {_dur(b):.1f}s"
                     f"{', 参数类' if b['_is_param'] else ''})")

    # —— 2) Hook 首镜：主体明确（含品类词）的本品强句 ——
    hook = [b for b in cands
            if _a(b).get("subject_clear") and _key(b) not in used
            and not b["_is_param"] and not _conflicts(b, chosen)]
    hook.sort(key=lambda b: (abs(_dur(b) - 4.5),
                             -len(_a(b).get("selling_points", []))))
    if hook and (not chosen or not _a(chosen[0]).get("subject_clear")):
        h = hook[0]
        chosen.insert(0, h)
        used.add(_key(h))
        notes.append(f"Hook 主体强句 ← {_a(h).get('window_id')}: "
                     f"{str(h.get('text', ''))[:26]}")

    # —— 3) 余量补齐：优先未用源、非参数类、低重复 ——
    rest = [b for b in cands if _key(b) not in used]
    while _total(chosen) < target_total and rest:
        used_srcs = {_src(x) for x in chosen}
        # 补齐阶段只用非参数类窗口（池里非参数窗 84.4s 足够），
        # 否则参数类会挤过 features.fabric_ratio=strict 的 25% BLOCK 上限。
        # V-02：同源重叠窗口一律不补，宁可短一点也不出重复画面。
        rest = [r for r in rest if not _conflicts(r, chosen)]
        pick_from = [r for r in rest if not r["_is_param"]] or list(rest)
        if not pick_from:
            notes.append("无未重叠的剩余窗口，补齐提前结束")
            break
        pick_from.sort(key=lambda b: (
            1 if _src(b) in used_srcs else 0,
            abs(_dur(b) - 6.0),
        ))
        b = pick_from[0]
        if _total(chosen) + _dur(b) > hard_max:
            rest.remove(b)
            continue
        chosen.append(b)
        used.add(_key(b))
        rest.remove(b)
        notes.append(f"补齐 ← {_a(b).get('window_id')} ({_src(b)[:14]}, {_dur(b):.1f}s)")

    # —— 4) 参数类占比（按 QC 口径 category.startswith("fabric")）≤23% ——
    # 留 2pt 余量，防止 build_plan 的类别打分与实际输出有细微偏差。
    qc_target = 0.23

    def _qc_fabric_sec(bs):
        return sum(_dur(b) for b in bs if b.get("_qc_fabric"))

    for _ in range(8):
        tot = _total(chosen) or 1.0
        if _qc_fabric_sec(chosen) / tot <= qc_target:
            break
        victim, victim_dur = None, -1.0
        for i, b in enumerate(chosen):
            if not b.get("_qc_fabric"):
                continue
            pts = set(_a(b).get("selling_points", []))
            others = set()
            for j, o in enumerate(chosen):
                if j != i:
                    others.update(_a(o).get("selling_points", []))
            # 无卖点或卖点全冗余的面料块优先换掉
            if (not pts or not (pts - others)) and _dur(b) > victim_dur:
                victim, victim_dur = i, _dur(b)
        if victim is None:
            notes.append("参数类占比偏高但无冗余面料块可换（保住必备卖点）")
            break
        repl = None
        rest_wo_victim = [x for j, x in enumerate(chosen) if j != victim]
        for r in cands:
            if _key(r) in used or r.get("_qc_fabric") or r["_is_param"]:
                continue
            if _conflicts(r, rest_wo_victim):
                continue
            if _total(chosen) - _dur(chosen[victim]) + _dur(r) > hard_max:
                continue
            trial = rest_wo_victim + [r]
            if (len(_points(trial) & set(sp_def)) >= 6
                    and not (set(p0) - _points(trial))):
                repl = r
                break
        if repl is None:
            break
        old = chosen[victim]
        used.discard(_key(old))
        chosen[victim] = repl
        used.add(_key(repl))
        notes.append(f"参数占比超限 → {_a(old).get('window_id')} "
                     f"换成非参数窗 {_a(repl).get('window_id')}")

    # —— 5) 单源占比 ≤40%、源数 ≥3、总长 ≤61.5s ——
    # v4 修复：旧实现要求「替换后 ≤ hard_max(61s)」，但 7 类必备卖点 +
    # Hook 的基础组装合计已达 62.4s，任何同长度替换都不可能 ≤61，于是
    # repl=None 直接 break，单源 47.7% 被 QC C8 BLOCK。改为遍历全部可行
    # 替换，按「单源最大占比 → 总长」择优；每步都守住卖点覆盖、P0、
    # 参数占比、Hook 首镜、源数、同源无重叠，避免按下葫芦浮起瓢。
    def _worst_ratio(bs) -> float:
        t = _total(bs) or 1.0
        a: Dict[str, float] = {}
        for b in bs:
            a[_src(b)] = a.get(_src(b), 0.0) + _dur(b)
        return (max(a.values()) / t) if a else 1.0

    for _ in range(10):
        tot = _total(chosen) or 1.0
        acc: Dict[str, float] = {}
        for b in chosen:
            acc[_src(b)] = acc.get(_src(b), 0.0) + _dur(b)
        ratios = {k: v / tot for k, v in acc.items()}
        over = [k for k, v in ratios.items() if v > max_ratio + 1e-6]
        too_long = tot > 61.5
        if not over and len(ratios) >= min_sources and not too_long:
            break
        worst = max(over, key=lambda k: ratios[k]) if over else None
        cur_pts = _points(chosen) & set(sp_def)
        best = None
        for i, b in enumerate(chosen):
            if i == 0:                      # 保住 K-06 Hook 主体明确
                continue
            if worst and _src(b) != worst and not too_long:
                continue
            rest_i = [x for j, x in enumerate(chosen) if j != i]
            for r in cands:
                if _key(r) in used or _conflicts(r, rest_i):
                    continue
                if worst and _src(b) == worst and _src(r) == worst:
                    continue
                trial = rest_i + [r]
                t2 = _total(trial)
                if not (48.0 <= t2 <= 61.5):
                    continue
                if (len(_points(trial) & set(sp_def)) < len(cur_pts)
                        or (set(p0) - _points(trial))):
                    continue
                if _qc_fabric_sec(trial) / t2 > qc_target:
                    continue
                if len({_src(x) for x in trial}) < min_sources:
                    continue
                score = (_worst_ratio(trial), t2)
                if best is None or score < best[0]:
                    best = (score, i, r)
        if best is None:
            if len(ratios) < min_sources:
                new = next((r for r in cands
                            if _key(r) not in used and _src(r) not in ratios
                            and not _conflicts(r, chosen)), None)
                if new and 48.0 <= _total(chosen) + _dur(new) <= 61.5:
                    chosen.append(new)
                    used.add(_key(new))
                    notes.append(f"源数 {len(ratios)}<{min_sources} → 引入 {_src(new)[:14]}")
                    continue
            notes.append("单源/时长收敛：无满足全部约束的替换（保留现状由 QC 拦截）")
            break
        _, i, r = best
        old = chosen[i]
        used.discard(_key(old))
        chosen[i] = r
        used.add(_key(r))
        notes.append(f"源占比/时长收敛 → {_a(old).get('window_id')} "
                     f"({_src(old)[:12]}) 换 {_a(r).get('window_id')} ({_src(r)[:12]})")

    # —— 5.5) 最终把关：参数类占比（QC 口径）≤23% ——
    # 单源/源数调整可能把非参数窗换回面料窗，故所有调整结束后再守一次。
    # 只接受「整体占比下降 + 卖点覆盖不退化 + 时长仍在区间」的替换。
    for _ in range(10):
        tot = _total(chosen) or 1.0
        cur_ratio = _qc_fabric_sec(chosen) / tot
        if cur_ratio <= qc_target:
            break
        fabric_idx = [i for i, b in enumerate(chosen) if b.get("_qc_fabric")]
        if not fabric_idx:
            break
        best = None
        for i in fabric_idx:
            for r in cands:
                if _key(r) in used or r.get("_qc_fabric"):
                    continue
                rest_wo_i = [x for j, x in enumerate(chosen) if j != i]
                if _conflicts(r, rest_wo_i):
                    continue
                trial = rest_wo_i + [r]
                t2 = _total(trial)
                if not (48.0 <= t2 <= hard_max):
                    continue
                if (len(_points(trial) & set(sp_def)) < 6
                        or (set(p0) - _points(trial))):
                    continue
                ratio2 = (sum(_dur(x) for x in trial if x.get("_qc_fabric"))
                          / t2)
                if best is None or ratio2 < best[0]:
                    best = (ratio2, i, r)
        if best is None or best[0] >= cur_ratio - 1e-6:
            notes.append("参数类占比仍偏高：无更优替换（避免破坏卖点覆盖）")
            break
        _, i, r = best
        old = chosen[i]
        used.discard(_key(old))
        chosen[i] = r
        used.add(_key(r))
        notes.append(f"最终把关 参数占比 {cur_ratio:.0%} → "
                     f"{_a(old).get('window_id')} 换 {_a(r).get('window_id')}")

    # —— 6) 叙事排序：Hook 置顶 → 卖点递进 → CTA 收尾 ——
    head = chosen[0] if chosen and _a(chosen[0]).get("subject_clear") else None
    body = [b for b in chosen if b is not head]
    body.sort(key=lambda b: (
        1 if _primary(b) == "cta" else 0,
        narrative.index(_primary(b)) if _primary(b) in narrative else 99,
    ))
    chosen = ([head] if head else []) + body

    ratios_final = _source_ratio(chosen)
    tot = _total(chosen)
    pd = sum(_dur(b) for b in chosen if b["_is_param"])
    dup_pairs = []
    for i, x in enumerate(chosen):
        for y in chosen[i + 1:]:
            ov = _overlap_sec(x, y)
            if ov > 0.5:
                dup_pairs.append({"a": _wid(x), "b": _wid(y),
                                  "source": _src(x),
                                  "overlap_sec": round(ov, 2)})
    stats = {
        "strategy": "selling_point_driven_compose",
        "unique_windows": len({_key(b) for b in chosen}),
        "duplicate_overlap_pairs": dup_pairs,
        "visual_excluded_sources": [
            str(f.get("source_contains") or "")
            for f in ((anchor.get("product") or {}).get("forbidden_windows") or [])
            if isinstance(f, dict)
        ],
        "selling_points_covered": sorted(_points(chosen)),
        "selling_points_total": len(sp_def),
        "p0_covered": sorted(set(p0) & _points(chosen)),
        "p0_total": len(p0),
        "source_ratios": ratios_final,
        "source_count": len(ratios_final),
        "max_single_source_ratio": max(ratios_final.values()) if ratios_final else 0.0,
        "hook_subject_clear": bool(head),
        "total_duration_sec": round(tot, 2),
        "parameter_content_sec": round(pd, 2),
        "parameter_content_ratio": round(pd / tot, 4) if tot else 0.0,
        "qc_fabric_sec": round(_qc_fabric_sec(chosen), 2),
        "qc_fabric_ratio": round(_qc_fabric_sec(chosen) / tot, 4) if tot else 0.0,
        "parameter_ratio_limit": max_param_ratio,
        "notes": notes,
    }
    for n in notes:
        log(f"[PRODUCT-ANCHOR] {n}")
    return chosen, stats


def enforce_selection(
    ordered: List[Dict[str, Any]],
    pool: Sequence[Dict[str, Any]],
    anchor: Dict[str, Any],
    log=print,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """S2 守卫入口：卖点清单驱动组装（v2 规范 §3 步 5）。"""
    return _compose_by_selling_points(ordered, pool, anchor, log)


def audit_plan(plan: Dict[str, Any], anchor: Dict[str, Any],
               ass_text: str = "") -> Dict[str, Any]:
    """S6 成片级复算：M01/M02/M03/M05/M06/M10/M11。"""
    clips = plan.get("clips") or []
    neg = [str(w) for w in (anchor.get("product", {}).get("negative_terms") or [])]
    sp_def = anchor.get("selling_points") or {}
    gates = anchor.get("gates", {}) or {}
    tags = [str(c.get("product_tag") or "") for c in clips]
    pts = set()
    for c in clips:
        p = c.get("selling_point") or []
        pts.update(p if isinstance(p, list) else [p])
    ratios = _source_ratio(clips)
    other_hits = [w for w in neg if w in (ass_text or "")]
    p0 = [k for k, v in sp_def.items() if (v or {}).get("level") == "P0"]

    # M12 同源时间窗重叠（重复画面）/ M13 视觉禁入源零命中
    fw_ranges = []
    for f in ((anchor.get("product") or {}).get("forbidden_windows") or []):
        if not isinstance(f, dict):
            continue
        m = str(f.get("source_contains") or "")
        if not m:
            continue
        rgs = f.get("time_ranges") or []
        if not rgs:
            fw_ranges.append((m, -1.0, float("inf")))
            continue
        for rg in rgs:
            try:
                a, b = float(rg[0]), float(rg[1])
                fw_ranges.append((m, min(a, b), max(a, b)))
            except (TypeError, ValueError, IndexError):
                continue
    forbidden_hits = []
    for c in clips:
        sf = str(c.get("source_file") or "")
        st = float(c.get("st", c.get("start", 0)) or 0)
        en = float(c.get("end", 0) or 0)
        if any(m in sf and min(en, b) - max(st, a) > 0.5
               for m, a, b in fw_ranges):
            forbidden_hits.append(str(c.get("id")))
    dup_pairs = []
    for i, x in enumerate(clips):
        for y in clips[i + 1:]:
            if str(x.get("source_file") or "") != str(y.get("source_file") or ""):
                continue
            ov = (min(float(x.get("end", 0) or 0), float(y.get("end", 0) or 0))
                  - max(float(x.get("st", x.get("start", 0)) or 0),
                        float(y.get("st", y.get("start", 0)) or 0)))
            if ov > 0.5:
                dup_pairs.append(f"{x.get('id')}~{y.get('id')}({ov:.1f}s)")

    result = {
        "M12_同源重叠分镜对": dup_pairs,
        "M13_视觉禁入源命中分镜": forbidden_hits,
        "M01_他品误入选率": round(
            sum(1 for t in tags if t != "本品") / max(1, len(tags)), 4),
        "M02_分镜主体一致通过率": round(
            sum(1 for t in tags if t == "本品") / max(1, len(tags)), 4),
        "M03_必备卖点覆盖率": f"{len(pts & set(sp_def))}/{len(sp_def)}",
        "M03_P0覆盖": f"{len(pts & set(p0))}/{len(p0)}",
        "M05_源数": len(ratios),
        "M05_单源最大占比": max(ratios.values()) if ratios else 0.0,
        "M06_他品词命中": other_hits,
        "M10_Hook主体明确": bool(clips and clips[0].get("product_tag") == "本品"
                                and any(w in str(clips[0].get("desc", ""))
                                        for w in SUBJECT_TERMS)),
        "M11_选段可追溯率": round(
            sum(1 for c in clips if c.get("source_block_id")) / max(1, len(clips)), 4),
        "clips": len(clips),
        "total_duration_sec": round(
            sum(float(c.get("dur", 0) or 0) for c in clips), 2),
    }
    checks = {
        "M01": result["M01_他品误入选率"] == 0,
        "M02": result["M02_分镜主体一致通过率"] == 1.0,
        "M03": len(pts & set(sp_def)) >= 6 and len(pts & set(p0)) == len(p0),
        "M05": (len(ratios) >= int(gates.get("min_source_count", 3))
                and (max(ratios.values()) if ratios else 1.0)
                <= float(gates.get("max_single_source_ratio", 0.40))),
        "M06": not other_hits,
        "M11": result["M11_选段可追溯率"] == 1.0,
        "M12": not dup_pairs,
        "M13": not forbidden_hits,
    }
    result["checks"] = checks
    result["all_gates_passed"] = all(checks.values())
    return result
