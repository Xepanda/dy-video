#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
detect_price.py - 中文数字价格检测器 (chinese_numeral_v1)
==========================================================
决策 3 落地工具：封面与视频字幕零具体价格。

三级防护中的第一级（检测原语），供三个环节复用：
  1. build_plan 选段排除（clip 级）
  2. 字幕生成过滤（subtitle 级）
  3. qc_audit 成片断言（QC 级，assert_zero）

检测规则（按优先级）：
  R1 数字串 >= 2 位            : 309, 299, 39.9, 1080（商品编号场景需白名单兜底）
  R2 货币符号 + 数字           : ￥299 / ¥39.9 / $29
  R3 数字 + 价格单位           : 29块, 39.9米, 299元, 五毛
  R4 中文数字(可带"多")+价格单位 : 三百多块, 二十九米, 两百
  R5 价格关键词 + 数字         : 到手价299 / 只要三百 / 立减50 / 原价八百
  R6 价格关键词 + 中文数字     : 秒杀价 / 特价 / 捡漏 / 破价 等 + 中文数字

白名单（不误杀合法内容）：
  - 数字 + 非价格单位（码/号/厘米/cm/斤/kg/岁/年/月/日/点/层/度/% 等）
  - 中文数字 + "码"（三码/拍小码）
  - 纯长度/重量/温度语境由白名单单位覆盖

CLI:
  python src/tools/detect_price.py --text "今天到手价299"
  python src/tools/detect_price.py --segments whisper.json --out-json price_report.json
退出码: 0=未检出价格, 1=检出价格, 2=用法/文件错误
"""

import os
import sys
import json
import re
import argparse
from typing import List, Dict, Any, Tuple, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------- 规则定义

# 价格单位（R3/R4）
PRICE_UNITS = ["块", "米", "元", "钱", "毛", "圆"]
PRICE_UNIT_ALT = "|".join(map(re.escape, PRICE_UNITS))

# 中文数字字符（含"两"与"多"）
CN_NUM_CHARS = "一二三四五六七八九十百千零两"
CN_NUM_RE = f"[{CN_NUM_CHARS}]+多?"

# 价格关键词（R5/R6）
PRICE_KEYWORDS = [
    "到手价", "到手", "只要", "立减", "原价", "降价", "调价", "特价",
    "秒杀价", "秒杀", "捡漏", "破价", "清仓价", "活动价", "福利价", "白菜价",
]

RULES: List[Tuple[str, "re.Pattern[str]"]] = [
    # R1: 两位及以上数字串（含小数 39.9）
    ("R1_digit_sequence", re.compile(r"(?<![\d.])\d{2,}(?:\.\d+)?(?![\d.])")),
    # R2: 货币符号 + 数字
    ("R2_currency_symbol", re.compile(r"[￥$€¥]\s*\d+(?:\.\d+)?")),
    # R3: 数字 + 价格单位
    ("R3_digit_price_unit", re.compile(rf"\d+(?:\.\d+)?\s*(?:{PRICE_UNIT_ALT})")),
    # R4: 中文数字(可带多) + 价格单位
    ("R4_cn_num_price_unit", re.compile(rf"{CN_NUM_RE}\s*(?:{PRICE_UNIT_ALT})")),
    # R5: 价格关键词 + 数字（间隔不超过 6 字符）
    ("R5_keyword_digit", re.compile(
        rf"(?:{'|'.join(map(re.escape, PRICE_KEYWORDS))})[^，。！？,.!?\d]{{0,6}}\d+(?:\.\d+)?")),
    # R6: 价格关键词 + 中文数字
    ("R6_keyword_cn_num", re.compile(
        rf"(?:{'|'.join(map(re.escape, PRICE_KEYWORDS))})[^，。！？,.!?\d]{{0,6}}{CN_NUM_RE}")),
]

# 白名单单位（R1 数字串后跟这些单位 → 合法，如 165码 / 1080P / 3M）
# 注意：不含"秒/分钟/小时"——避免"299秒杀"被误豁免（秒杀是强价格信号）
WHITELIST_UNITS = [
    "码", "号", "厘米", "cm", "CM", "斤", "kg", "KG", "岁", "年", "月", "日",
    "点", "层", "楼", "度", "%", "％", "p", "P", "M", "m", "L", "l", "XL", "xl",
    "色", "件", "条", "版", "期", "集", "页", "身高", "体重", "年纪", "年龄", "公分", "寸",
]
# 注意：件/条/色 等是库存语境而非价格语境，R1 命中后跟这些单位时豁免（库存由黑名单另行处理）

_WHITELIST_TAIL = "|".join(map(re.escape, WHITELIST_UNITS))
_WHITELIST_PATTERNS = [
    # 数字 + 白名单单位（允许"的"间隔，如 165的身高 / 160码）
    re.compile(rf"\d+(?:\.\d+)?\s*的?\s*(?:{_WHITELIST_TAIL})(?![块米元钱毛])"),
    # 中文数字 + "码"（三码 / 拍小码）
    re.compile(rf"{CN_NUM_RE}?\s*码"),
    # 常见非价格技术数字（版本号/型号/分辨率）
    re.compile(r"\d+(?:\.\d+)*\s*(?:版|型|代|系|Pro|pro|MAX|max|Plus|plus)"),
    # —— 尺码/体重语境（2026-10-21 泛化验收修正）——
    # 裸码数在直播口语里常见（"从125穿到150斤""从101开始穿""105你看穿出来"），
    # 数字本身不是价格；数字邻近"穿/尺/体重/身高"等语境词时豁免。
    re.compile(r"\d+(?:\.\d+)?\s*(?:开始)?\s*.{0,4}?(?:穿|尺|体重|身高)"),
    re.compile(r"(?:从|推|是|到)\s*\d+(?:\.\d+)?\s*(?:开始)?\s*(?:穿|码|尺|斤|体重|身高)?"),
    # 毛衣/毛衫/毛料/毛呢/毛线/毛圈/毛领/毛毯/毛巾/毛重/毛利/一毛一样/毛病 等非货币词汇
    re.compile(rf"{CN_NUM_RE}?\s*毛(?:衣|衫|呢|料|线|领|圈|纺|织|毯|巾|坯|重|利|边|孔|病|一样)"),
]

# 裸 3 位数字落在服装码数区间（95~135）且未带货币单位 → 视为码数而非价格。
# 依据：直播口语中 95~135 的裸数字几乎都是"按体重/身高选码"，
# 真价格通常带单位（"299块""105元"）或落在该区间外。
SIZE_BARE_RANGE = (95, 135)

DETECTOR_VERSION = "chinese_numeral_v1"


def _whitelisted(text: str, start: int, end: int) -> bool:
    """判断 [start, end) 的价格命中是否落在白名单语境内。"""
    # 向前看 2 字符、向后看 4 字符构造上下文
    ctx = text[max(0, start - 2): min(len(text), end + 4)]
    for pat in _WHITELIST_PATTERNS:
        for m in pat.finditer(ctx):
            # 白名单匹配需要覆盖住价格命中的核心区域
            if m.start() <= (start - max(0, start - 2)) + 1 and m.end() >= (end - max(0, start - 2)) - 1:
                return True
    return False


def _is_bare_size_number(text: str, start: int, end: int) -> bool:
    """裸 3 位数字落在服装码数区间（95~135）且邻近无货币单位 → 判为码数而非价格。"""
    raw = text[start:end]
    if not raw.isdigit() or len(raw) != 3:
        return False
    val = int(raw)
    if not (SIZE_BARE_RANGE[0] <= val <= SIZE_BARE_RANGE[1]):
        return False
    # 邻近货币单位/关键词 → 仍视为价格
    near = text[max(0, start - 3): min(len(text), end + 3)]
    if re.search(r"[块米元钱毛]|到手|只要|原价|秒杀|价", near):
        return False
    return True


def find_price_matches(text: str) -> List[Dict[str, Any]]:
    """返回文本中所有价格命中的结构化列表（已过白名单）。"""
    if not text:
        return []
    matches: List[Dict[str, Any]] = []
    for rule_name, pattern in RULES:
        for m in pattern.finditer(text):
            if _whitelisted(text, m.start(), m.end()):
                continue
            if rule_name == "R1_digit_sequence" and _is_bare_size_number(text, m.start(), m.end()):
                continue
            matches.append({
                "rule": rule_name,
                "span": [m.start(), m.end()],
                "text": m.group(0),
            })
    # 按位置排序并去重（同一区域只保留最先命中的规则）
    matches.sort(key=lambda x: (x["span"][0], x["span"][1]))
    deduped: List[Dict[str, Any]] = []
    for m in matches:
        if deduped and m["span"][0] < deduped[-1]["span"][1]:
            continue
        deduped.append(m)
    return deduped


def contains_price(text: str) -> Tuple[bool, List[Dict[str, Any]]]:
    """便捷接口：是否包含价格表达。"""
    matches = find_price_matches(text)
    return (len(matches) > 0), matches


# ---------------------------------------------------------------- 批量接口

def audit_segments(segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对 ASR 片段列表做价格审计，返回结构化报告。"""
    hits = []
    clean = []
    for i, s in enumerate(segments):
        text = s.get("text", "")
        matches = find_price_matches(text)
        if matches:
            hits.append({
                "index": i,
                "start": s.get("start"),
                "end": s.get("end"),
                "text": text,
                "matches": matches,
            })
        else:
            clean.append(s)
    return {
        "detector": DETECTOR_VERSION,
        "total_segments": len(segments),
        "price_segment_count": len(hits),
        "price_hits": hits,
        "clean_segments": clean,
    }


def audit_ass_file(ass_path: str) -> Dict[str, Any]:
    """对 .ass 字幕文件做价格审计（QC 断言用）。"""
    with open(ass_path, "r", encoding="utf-8-sig", errors="ignore") as f:
        content = f.read()
    hits = []
    for lineno, line in enumerate(content.splitlines(), 1):
        if not line.startswith("Dialogue:"):
            continue
        # Dialogue 行第 10 个字段为 Text
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        text = parts[9]
        # 去掉 ASS 覆盖代码 {\c&H...&}
        plain = re.sub(r"\{[^}]*\}", "", text)
        matches = find_price_matches(plain)
        if matches:
            hits.append({"line": lineno, "text": plain, "matches": matches})
    return {
        "detector": DETECTOR_VERSION,
        "ass_path": os.path.abspath(ass_path),
        "price_dialogue_count": len(hits),
        "price_hits": hits,
    }


# ---------------------------------------------------------------- CLI

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="中文数字价格检测器 (chinese_numeral_v1)")
    parser.add_argument("--text", help="直接检测单条文本")
    parser.add_argument("--segments", help="ASR 片段 JSON 文件路径（[{start,end,text}]）")
    parser.add_argument("--ass", help=".ass 字幕文件路径")
    parser.add_argument("--out-json", help="审计报告输出路径（JSON）")
    args = parser.parse_args(argv)

    if not any([args.text, args.segments, args.ass]):
        parser.print_help()
        return 2

    report: Dict[str, Any] = {}
    exit_code = 0

    if args.text is not None:
        has, matches = contains_price(args.text)
        report = {"detector": DETECTOR_VERSION, "text": args.text,
                  "has_price": has, "matches": matches}
        exit_code = 1 if has else 0
        status = "检出价格" if has else "未检出价格"
        print(f"[PRICE] {status}: {matches if matches else ''}")

    if args.segments:
        if not os.path.exists(args.segments):
            print(f"[PRICE][ERROR] 文件不存在: {args.segments}")
            return 2
        with open(args.segments, "r", encoding="utf-8") as f:
            segments = json.load(f)
        rep = audit_segments(segments)
        report = rep
        exit_code = 1 if rep["price_segment_count"] > 0 else 0
        print(f"[PRICE] 片段审计: {rep['total_segments']} 段中检出价格 {rep['price_segment_count']} 段")
        for h in rep["price_hits"]:
            print(f"        @{h.get('start')}s {h['text']} -> {h['matches']}")

    if args.ass:
        if not os.path.exists(args.ass):
            print(f"[PRICE][ERROR] 文件不存在: {args.ass}")
            return 2
        rep = audit_ass_file(args.ass)
        report = rep
        exit_code = 1 if rep["price_dialogue_count"] > 0 else 0
        print(f"[PRICE] 字幕审计: 检出含价格 Dialogue {rep['price_dialogue_count']} 行")

    if args.out_json:
        os.makedirs(os.path.dirname(os.path.abspath(args.out_json)), exist_ok=True)
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"[PRICE] 审计报告已落盘: {args.out_json}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
