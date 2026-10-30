#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""normalize_zh_script.py — 繁简字形归一（2026-10-30 反馈驱动）
=================================================================
背景（用户实测问题）：
  faster-whisper 的 zh 词表含港台繁体字幕，language="zh" 只限定语种、
  不约束字形，"褲/個/類/麼/嗎/覺" 等高频字概率性输出繁体
  （"那款褲子""搭個啥""類型""感覺""你知道嗎"）。
  - 既有 asr_agent_verifier 只有 8 条硬编码简繁对，覆盖不足；
  - 违规词库为简体字面匹配，繁体还会造成漏检（"褲"匹配不到"裤"）。

设计原则：
  1. 确定性优先：OpenCC t2s（词组级，"頭髮→头发"，避免单字歧义）；
     OpenCC 不可用时退化为内置高频字表（仅覆盖常用字，精度略低）。
  2. 时间戳绝对守恒：只改 text，start/end/duration 原样拷贝（R-S301A）。
  3. 幂等：简体输入转换结果不变，可重复执行（渲染前二次兜底安全）。
  4. 商品术语校验：转换后核对契约术语写法，缺失则在报告中标记。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ---------------------------------------------------------------- 转换器

_CONVERTER = None
_CONVERTER_NAME = ""

# 内置高频繁体字表（OpenCC 缺失时的兜底；覆盖直播带货常见字）
_BUILTIN_T2S = {
    "褲": "裤", "個": "个", "類": "类", "麼": "么", "嗎": "吗", "覺": "觉",
    "這": "这", "說": "说", "沒": "没", "後": "后", "時": "时", "會": "会",
    "來": "来", "為": "为", "對": "对", "開": "开", "關": "关", "門": "门",
    "東": "东", "車": "车", "長": "长", "問": "问", "題": "题", "樣": "样",
    "點": "点", "錢": "钱", "價": "价", "買": "买", "賣": "卖", "貨": "货",
    "質": "质", "絲": "丝", "絨": "绒", "線": "线", "條": "条", "種": "种",
    "還": "还", "進": "进", "過": "过", "裡": "里", "裏": "里", "邊": "边",
    "號": "号", "碼": "码", "碼": "码", "餘": "余", "雙": "双", "單": "单",
    "襯": "衬", "領": "领", "袖": "袖", "擺": "摆", "寬": "宽", "鬆": "松",
    "緊": "紧", "軟": "软", "彈": "弹", "暖": "暖", "溫": "温", "層": "层",
    "織": "织", "紋": "纹", "細": "细", "膩": "腻", "麗": "丽", "華": "华",
    "貴": "贵", "輕": "轻", "飄": "飘", "顯": "显", "瘦": "瘦", "氣": "气",
    "質": "质", "顏": "颜", "色": "色", "藍": "蓝", "綠": "绿", "紅": "红",
    "黃": "黄", "黑": "黑", "白": "白", "銀": "银", "裝": "装", "飾": "饰",
    "寶": "宝", "貝": "贝", "專": "专", "業": "业", "級": "级", "極": "极",
    "萬": "万", "與": "与", "專": "专", "們": "们", "妳": "你", "臺": "台",
    "灣": "湾", "髮": "发", "鬚": "须", "濃": "浓", "霧": "雾", "塵": "尘",
    "淨": "净", "潔": "洁", "養": "养", "護": "护", "膚": "肤", "體": "体",
    "適": "适", "暢": "畅", "風": "风", "飄": "飘", "順": "顺", "滑": "滑",
    "膚": "肤", "觸": "触", "壓": "压", "縮": "缩", "漲": "涨", "彈": "弹",
}


def _get_converter(prefer: str = "opencc"):
    """返回 (convert_fn, name)。OpenCC 可用时优先，否则内置表。"""
    global _CONVERTER, _CONVERTER_NAME
    if _CONVERTER is not None:
        return _CONVERTER, _CONVERTER_NAME
    if prefer != "builtin":
        try:
            import opencc  # type: ignore
            cc = opencc.OpenCC("t2s")
            _CONVERTER = (lambda s: cc.convert(s))
            _CONVERTER_NAME = "opencc-t2s"
            return _CONVERTER, _CONVERTER_NAME
        except Exception:
            pass
    table = _BUILTIN_T2S

    def _builtin(s: str) -> str:
        return "".join(table.get(ch, ch) for ch in s)

    _CONVERTER = _builtin
    _CONVERTER_NAME = "builtin-table"
    return _CONVERTER, _CONVERTER_NAME


def to_simplified(text: str, prefer: str = "opencc") -> str:
    """繁体 → 简体（幂等：简体输入原样返回）。"""
    if not text:
        return text
    fn, _ = _get_converter(prefer)
    try:
        return fn(text)
    except Exception:
        return text


def contains_traditional(text: str, prefer: str = "opencc") -> bool:
    """基于回环比对判定是否含繁体（t2s(x) != x ⇒ 含繁体）。"""
    if not text:
        return False
    return to_simplified(text, prefer) != text


def converter_name(prefer: str = "opencc") -> str:
    """当前生效的转换器名：opencc-t2s | builtin-table。

    ⚠️ contains_traditional 是回环比对（同一个转换器转一遍，变了就算繁体），
    内置表仅覆盖 120 个高频繁体字——一旦降级到 builtin-table，生僻繁体既
    转不掉、也检不出，A11 门禁与 QC 断言会静默变弱。调用方应据此记 WARN。
    """
    _, name = _get_converter(prefer)
    return name


def normalize_segments(
    segments: List[Dict[str, Any]],
    protect_terms: Optional[List[str]] = None,
    prefer: str = "opencc",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """归一化台词段文本，时间戳与其余字段绝对守恒。

    返回 (new_segments, report)。new_segments 为新列表（不修改入参对象），
    每段浅拷贝原 dict，仅替换 text。report 含替换明细与术语校验结果。
    """
    fn_name = _get_converter(prefer)[1]
    out: List[Dict[str, Any]] = []
    changes: List[Dict[str, Any]] = []
    n_chars_before = 0
    n_chars_changed = 0

    for i, s in enumerate(segments or []):
        seg = dict(s)
        text = (seg.get("text") or "")
        fixed = to_simplified(text, prefer)
        if fixed != text:
            diff = sum(1 for a, b in zip(text, fixed) if a != b)
            n_chars_changed += diff
            changes.append({
                "index": i,
                "t_start": seg.get("start"),
                "before": text,
                "after": fixed,
                "changed_chars": diff,
            })
            seg["text"] = fixed
            seg["script_normalized"] = True
        n_chars_before += len(text)
        out.append(seg)

    residual = [i for i, s in enumerate(out)
                if contains_traditional(s.get("text") or "", prefer)]

    term_check = {}
    for term in (protect_terms or []):
        if not term:
            continue
        hit = any(term in (s.get("text") or "") for s in out)
        term_check[term] = "present" if hit else "absent"

    report = {
        "converter": fn_name,
        "n_segments": len(out),
        "n_changed_segments": len(changes),
        "n_changed_chars": n_chars_changed,
        "changed_ratio": round(n_chars_changed / max(1, n_chars_before), 4),
        "residual_traditional_segments": residual,
        "product_terms": term_check,
        "changes": changes[:200],
        "timestamps_preserved": True,
    }
    return out, report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="繁体 → 简体字形归一")
    ap.add_argument("--segments", required=True, help="输入台词 JSON（list）")
    ap.add_argument("--out", help="输出归一后 JSON（缺省覆盖输入）")
    ap.add_argument("--report", help="归一报告输出路径")
    ap.add_argument("--terms", nargs="*", default=[], help="商品术语（校验用）")
    ap.add_argument("--converter", default="opencc", choices=("opencc", "builtin"))
    args = ap.parse_args(argv)

    segs = json.load(open(args.segments, "r", encoding="utf-8"))
    new_segs, report = normalize_segments(segs, protect_terms=args.terms,
                                          prefer=args.converter)
    out_path = args.out or args.segments
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(new_segs, f, ensure_ascii=False, indent=2)
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[ZH-NORM] {report['converter']} | 改动 {report['n_changed_segments']} 段 / "
          f"{report['n_changed_chars']} 字 | 残留繁体段 {len(report['residual_traditional_segments'])}"
          f" | 输出 {out_path}")
    return 0 if not report["residual_traditional_segments"] else 2


if __name__ == "__main__":
    sys.exit(main())
