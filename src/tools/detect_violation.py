#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
detect_violation.py - 抖音电商违规词检测器 (violation_words_v1)
===============================================================
反馈驱动（2026-10-27）：接入飞书《违规整合》极限词库，对成片全链路检测。

三级检测（与 detect_price 同构）：
  L1 选段级  —— build_plan 对 ASR 台词扫描，命中段降权/剔除（可选）；
  L2 字幕级  —— 成片字幕（ASS Dialogue 行）扫描，命中词屏蔽为 ***；
  L3 画面级 —— OCR 扫描分镜抽帧，画面可见命中词打码（boxblur）。

词库：contracts/violation_words.yaml（数据驱动，增删词条不改代码）。
  words   —— 精确子串匹配（长词，低误报）
  patterns—— 正则族匹配（短词根），配 allow 白名单消歧
  severity—— BLOCK（必须处理）| WARN（仅记录）

CLI:
  python src/tools/detect_violation.py --text "这件是全网最受欢迎的"
  python src/tools/detect_violation.py --segments whisper.json --out-json vreport.json
  python src/tools/detect_violation.py --ass subtitles.ass
退出码: 0=未检出, 1=检出违规, 2=用法/文件错误
"""

import os
import sys
import json
import re
import argparse
from typing import List, Dict, Any, Tuple, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_LIBRARY = os.path.join(PROJECT_ROOT, "contracts", "violation_words.yaml")
DEFAULT_CONSTRAINTS = os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")

MASK_TOKEN = "***"


# ---------------------------------------------------------------- 词库加载

def load_violation_library(library_path: str = None) -> Dict[str, Any]:
    """加载违规词库 YAML；不存在时返回空库（检测器降级为直通）。"""
    path = library_path or DEFAULT_LIBRARY
    if not os.path.exists(path):
        print(f"[VIOLATION] 词库不存在: {path}（检测降级为直通）")
        return {"categories": [], "global_allow": []}
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        lib = yaml.safe_load(f) or {}
    lib.setdefault("categories", [])
    lib.setdefault("global_allow", [])
    return lib


def _constraints_from_disk() -> Dict[str, Any]:
    """读取 constraints.yaml（violation_words 段的默认来源）。"""
    try:
        import yaml
        if os.path.exists(DEFAULT_CONSTRAINTS):
            with open(DEFAULT_CONSTRAINTS, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}


class ViolationDetector:
    """违规词检测器：词库驱动，支持文本/台词段/字幕/OCR 框四类输入。

    2026-10-28 接线（此前为死键）：
      - violation_words.library_path：未显式指定库路径时按 constraints 加载
        （相对路径以项目根为基准）；
      - violation_words.detect.{asr_segments, subtitles, cover_ocr}：
        通道开关，关闭后对应扫描方法直接返回空列表（不影响 scan_text）。
    """

    def __init__(self, library: Dict[str, Any] = None,
                 library_path: str = None,
                 constraints: Dict[str, Any] = None):
        cons = constraints if constraints is not None else _constraints_from_disk()
        vw_c = (cons or {}).get("violation_words", {}) or {}
        # —— 词库路径（约束驱动）——
        if library_path is None:
            lib_rel = str(vw_c.get("library_path") or "").strip()
            if lib_rel:
                library_path = (lib_rel if os.path.isabs(lib_rel)
                                else os.path.join(PROJECT_ROOT, lib_rel))
        # —— 通道开关（约束驱动）——
        detect_c = vw_c.get("detect", {}) or {}
        self.channels = {
            "asr_segments": bool(detect_c.get("asr_segments", True)),
            "subtitles": bool(detect_c.get("subtitles", True)),
            "cover_ocr": bool(detect_c.get("cover_ocr", True)),
        }
        self.library = library or load_violation_library(library_path)
        self.global_allow = self._compile_allow(self.library.get("global_allow", []))
        self.categories = []
        for cat in self.library.get("categories", []):
            self.categories.append({
                "id": cat.get("id", ""),
                "name": cat.get("name", cat.get("id", "")),
                "severity": cat.get("severity", "BLOCK"),
                "words": [w for w in (cat.get("words") or []) if w],
                "patterns": [re.compile(p) for p in (cat.get("patterns") or []) if p],
                "allow": self._compile_allow(cat.get("allow", [])),
            })

    @staticmethod
    def _compile_allow(rules: List[str]):
        """白名单规则 → 编译后的正则列表（空则返回空列表）。"""
        out = []
        for r in rules or []:
            try:
                out.append(re.compile(r))
            except re.error:
                continue
        return out

    def _is_allowed(self, text: str, start: int, end: int) -> bool:
        """命中区间若被任一白名单规则覆盖 → 判合法。"""
        for rule in self.global_allow + []:
            for m in rule.finditer(text):
                if m.start() <= start and m.end() >= end:
                    return True
        return False

    def _is_allowed_for(self, cat: Dict[str, Any], text: str,
                        start: int, end: int) -> bool:
        for rule in list(self.global_allow) + list(cat["allow"]):
            for m in rule.finditer(text):
                if m.start() <= start and m.end() >= end:
                    return True
        return False

    # ------------------------------------------------------------ 核心扫描

    def scan_text(self, text: str) -> List[Dict[str, Any]]:
        """扫描单段文本，返回命中列表（去重、按出现位置排序）。"""
        if not text:
            return []
        hits: List[Dict[str, Any]] = []
        seen = set()
        for cat in self.categories:
            # 1) 精确词匹配
            for w in cat["words"]:
                start = 0
                while True:
                    idx = text.find(w, start)
                    if idx < 0:
                        break
                    end = idx + len(w)
                    start = end
                    if self._is_allowed_for(cat, text, idx, end):
                        continue
                    key = (idx, end, cat["id"])
                    if key in seen:
                        continue
                    seen.add(key)
                    hits.append({
                        "word": w, "category": cat["id"],
                        "category_name": cat["name"], "severity": cat["severity"],
                        "start": idx, "end": end,
                    })
            # 2) 正则族匹配
            for pat in cat["patterns"]:
                for m in pat.finditer(text):
                    s, e = m.start(), m.end()
                    if self._is_allowed_for(cat, text, s, e):
                        continue
                    key = (s, e, cat["id"])
                    if key in seen:
                        continue
                    seen.add(key)
                    hits.append({
                        "word": m.group(0), "category": cat["id"],
                        "category_name": cat["name"], "severity": cat["severity"],
                        "start": s, "end": e,
                    })
        hits.sort(key=lambda h: (h["start"], h["end"]))
        # 重叠命中保留最长者（同一区间被多族命中时取覆盖最长）
        merged: List[Dict[str, Any]] = []
        for h in hits:
            if merged and h["start"] < merged[-1]["end"]:
                if h["end"] > merged[-1]["end"]:
                    merged[-1] = h
                continue
            merged.append(h)
        return merged

    def scan_segments(self, segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """扫描 ASR 台词段，命中附带时间戳与来源文件。"""
        if not self.channels.get("asr_segments", True):
            return []   # 通道关闭（violation_words.detect.asr_segments=false）
        hits = []
        for seg in segments:
            text = seg.get("text", "") or ""
            for h in self.scan_text(text):
                hits.append({
                    **h,
                    "t_start": seg.get("start"),
                    "t_end": seg.get("end"),
                    "source_file": seg.get("source_file", ""),
                    "segment_text": text,
                })
        return hits

    def scan_subtitle_events(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """扫描字幕事件列表（assembler 产出的 sub_events 结构）。"""
        if not self.channels.get("subtitles", True):
            return []   # 通道关闭（violation_words.detect.subtitles=false）
        hits = []
        for ev in events:
            for h in self.scan_text(ev.get("text", "") or ""):
                hits.append({
                    **h,
                    "t_start": ev.get("t_start"),
                    "t_end": ev.get("t_end"),
                    "segment_text": ev.get("text", ""),
                })
        return hits

    def scan_ass_file(self, ass_path: str) -> List[Dict[str, Any]]:
        """扫描 .ass 字幕文件（QC 级复测）。"""
        if not os.path.exists(ass_path):
            return []
        hits = []
        with open(ass_path, "r", encoding="utf-8-sig") as f:
            for ln, line in enumerate(f, 1):
                if not line.startswith("Dialogue:"):
                    continue
                parts = line.split(",", 9)
                if len(parts) < 10:
                    continue
                text = parts[9].rstrip("\r\n")
                for h in self.scan_text(text):
                    hits.append({**h, "line": ln, "ass_text": text})
        return hits

    def scan_ocr_boxes(self, boxes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """扫描 OCR 文字框（画面可见文字 → 打码通道）。"""
        if not self.channels.get("cover_ocr", True):
            return []   # 通道关闭（violation_words.detect.cover_ocr=false）
        hits = []
        for b in boxes:
            for h in self.scan_text(b.get("text", "") or ""):
                hits.append({**h, "box": b.get("box"), "ocr_text": b.get("text", "")})
        return hits

    # ------------------------------------------------------------ 屏蔽处理

    def mask_text(self, text: str) -> Tuple[str, List[Dict[str, Any]]]:
        """字幕屏蔽：命中词替换为 ***。返回 (屏蔽后文本, 命中列表)。"""
        hits = self.scan_text(text)
        if not hits:
            return text, []
        out = text
        for h in sorted(hits, key=lambda x: x["start"], reverse=True):
            out = out[:h["start"]] + MASK_TOKEN + out[h["end"]:]
        return out, hits


# ---------------------------------------------------------------- 便捷函数

def mask_text_action(text: str, words: List[str],
                     action: str = "delete") -> Tuple[str, List[str]]:
    """按动作处理字幕文本（2026-10-30 用户要求：只删词不显示）。

    action="delete" —— 直接从字幕中删除命中词（并收敛残留标点）；
    action="mask"   —— 替换为 ***（兼容旧行为）。
    返回 (处理后文本, 实际命中的词列表)。长词优先，避免子串重复替换。
    """
    if not text or not words:
        return text, []
    hits: List[str] = []
    out = text
    for w in sorted({w for w in words if w}, key=len, reverse=True):
        if w in out:
            hits.append(w)
            out = out.replace(w, "***" if action == "mask" else "")
    if action != "mask" and hits:
        out = re.sub(r"[，。、！？；：,]{2,}", lambda m: m.group(0)[0], out)
        out = re.sub(r"^[，。、！？；：,\s]+", "", out)
        out = re.sub(r"\s{2,}", " ", out).strip()
    return out, hits


def find_violation_matches(text: str, library_path: str = None) -> List[Dict[str, Any]]:
    """单文本检测（模块级便捷入口，对齐 detect_price.find_price_matches）。"""
    return ViolationDetector(library_path=library_path).scan_text(text)


def contains_violation(text: str, library_path: str = None) -> Tuple[bool, List[Dict[str, Any]]]:
    det = ViolationDetector(library_path=library_path)
    hits = det.scan_text(text)
    return (len(hits) > 0), hits


def audit_segments(segments: List[Dict[str, Any]],
                   library_path: str = None) -> Dict[str, Any]:
    """ASR 台词段全量审计（L1/L2 处理依据）。"""
    det = ViolationDetector(library_path=library_path)
    hits = det.scan_segments(segments)
    by_cat: Dict[str, int] = {}
    for h in hits:
        by_cat[h["category"]] = by_cat.get(h["category"], 0) + 1
    return {
        "total_hits": len(hits),
        "by_category": by_cat,
        "hits": hits,
        "clean": len(hits) == 0,
    }


def audit_ass_file(ass_path: str, library_path: str = None) -> Dict[str, Any]:
    """成片字幕审计（QC 断言用）。"""
    det = ViolationDetector(library_path=library_path)
    hits = det.scan_ass_file(ass_path)
    return {
        "total_hits": len(hits),
        "hits": hits,
        "clean": len(hits) == 0,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="抖音电商违规词检测器")
    parser.add_argument("--text", help="直接检测单段文本")
    parser.add_argument("--segments", help="ASR 台词段 JSON 路径")
    parser.add_argument("--ass", help=".ass 字幕文件路径")
    parser.add_argument("--library", default=None, help="词库路径（默认 contracts/violation_words.yaml）")
    parser.add_argument("--out-json", default=None)
    args = parser.parse_args(argv)

    det = ViolationDetector(library_path=args.library)
    result = None
    if args.text is not None:
        hits = det.scan_text(args.text)
        result = {"mode": "text", "total_hits": len(hits), "hits": hits}
        for h in hits:
            print(f"[HIT] {h['word']} ({h['category_name']}) @{h['start']}-{h['end']}")
        if not hits:
            print("[CLEAN] 未检出违规词")
    elif args.segments and os.path.exists(args.segments):
        with open(args.segments, "r", encoding="utf-8") as f:
            segments = json.load(f)
        result = audit_segments(segments, args.library)
        print(f"[SEGMENTS] 检出 {result['total_hits']} 处违规词 "
              f"(分类: {result['by_category']})")
    elif args.ass and os.path.exists(args.ass):
        result = audit_ass_file(args.ass, args.library)
        print(f"[ASS] 检出 {result['total_hits']} 处违规词")
        for h in result["hits"][:20]:
            print(f"  [HIT] line {h.get('line')}: {h['word']} ({h['category_name']})")
    else:
        parser.print_help()
        return 2

    if args.out_json and result is not None:
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[OUT] {args.out_json}")
    return 1 if (result and result.get("total_hits")) else 0


if __name__ == "__main__":
    sys.exit(main())
