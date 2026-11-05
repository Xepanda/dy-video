#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_clip_product_alignment.py - 分镜片段品类与语义对齐核验工具
================================================================
背景与价值（2026-11 总结）：
  原流水线 S1.5 仅对素材池「全量转录文本」做一致性统计，S5 质检仅针对格式/分辨率/响度等物理指标；
  导致 S2 编排出的分镜可能混入非本品（如上衣装配中混入皮裤、石墨烯加绒裤、可拆卸外套等话术），
  或者包含口误数字（如'3030多点羊毛'被误判为价格）与违规极限词（如结尾'最大'）。

本工具实现分镜级的语义与规则对齐门禁：
  1. 商品品类一致性（排除负向品类与异物词）
  2. 口误价格风险筛查（如'3030'等非预期数字）
  3. 边界违规词切片核查（如切片截断处残留极限词）
  4. 漏斗占比合规性校验（面料 ≤25%，Hook ≥10%，Fit ≥15%，Styling ≥25%，Closing ≥10%）
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 通用服饰他品类负向词库（当商品为上装/毛衣时排除下装/外套/鞋包）
CATEGORY_NEGATIVE_TERMS = {
    "top": [
        "裤", "库长", "裤长", "毛呢裤", "毛泥裤", "裤腿", "西裤", "卫裤", "皮裤",
        "石墨烯", "加绒裤", "棉服", "填充物", "羽绒", "鞋", "靴"
    ],
    "bottom": [
        "上衣", "毛衣", "针织衫", "衬衫", "吊带", "外套", "大衣", "风衣"
    ]
}

# 常见口误价格/数字正则（如 '3030多点', '1999', '299元'，排除 100斤, 115斤, S码, 30% 等体型与成分单位）
PRICE_STUTTER_REGEX = re.compile(r"(\d{3,4}(?![斤码米克分秒度%])|\d+块|\d+元|\d+多点)")

# 常见违禁/极限词列表
DEFAULT_VIOLATION_WORDS = ["最大", "最好", "第一", "国家级", "顶级", "极品", "万能"]


def infer_product_category(title: str) -> str:
    """根据商品名推断品类大类（top / bottom / other）。"""
    title_lower = title.lower()
    if any(k in title_lower for k in ("上衣", "毛衣", "针织", "衫", "t恤", "卫衣", "开衫")):
        return "top"
    if any(k in title_lower for k in ("裤", "裙", "半裙", "短裤", "长裤")):
        return "bottom"
    return "other"


def verify_clip_alignment(
    plan_data: Dict[str, Any],
    spec_data: Optional[Dict[str, Any]] = None,
    constraints: Optional[Dict[str, Any]] = None,
    custom_negative_terms: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """对 plan_data 中的 clips 执行深度语义与规则核验。"""
    spec_data = spec_data or {}
    constraints = constraints or {}
    clips = plan_data.get("clips", [])

    product_info = spec_data.get("product", {})
    title = f"{product_info.get('title_line1', '')} {product_info.get('title_line2', '')}".strip()
    if not title:
        title = spec_data.get("title", "")

    cat = infer_product_category(title)
    neg_terms = list(custom_negative_terms or CATEGORY_NEGATIVE_TERMS.get(cat, []))

    checks = []
    violations = []

    # 1. 负向品类词排查
    neg_hits = []
    for c in clips:
        cid = c.get("id", "unknown")
        text = str(c.get("desc") or "") + " " + " ".join(c.get("spoken_lines", []))
        for t in neg_terms:
            if t in text:
                neg_hits.append({"clip_id": cid, "term": t, "text_snippet": text[:40]})
    
    if neg_hits:
        violations.append(f"检出 {len(neg_hits)} 处非本品负向词: {neg_hits[:3]}")
        checks.append({
            "name": "商品品类负向词排查",
            "status": "FAIL",
            "detail": f"命中负向词: {[h['term'] for h in neg_hits]}"
        })
    else:
        checks.append({
            "name": "商品品类负向词排查",
            "status": "PASS",
            "detail": "0 处异品类词，100% 聚焦本品"
        })

    # 2. 语音数字/口误价格残留排查
    price_hits = []
    for c in clips:
        cid = c.get("id", "unknown")
        text = str(c.get("desc") or "") + " " + " ".join(c.get("spoken_lines", []))
        m = PRICE_STUTTER_REGEX.findall(text)
        if m:
            price_hits.append({"clip_id": cid, "matched": m})

    if price_hits:
        violations.append(f"检出疑似价格/数字残留: {price_hits}")
        checks.append({
            "name": "语音价格与口误数字排查",
            "status": "WARN",
            "detail": f"疑似数字残留: {price_hits}"
        })
    else:
        checks.append({
            "name": "语音价格与口误数字排查",
            "status": "PASS",
            "detail": "0 处价格表达残留"
        })

    # 3. 边缘违规词排查
    violation_hits = []
    for c in clips:
        cid = c.get("id", "unknown")
        text = str(c.get("desc") or "")
        for w in DEFAULT_VIOLATION_WORDS:
            if w in text:
                violation_hits.append({"clip_id": cid, "word": w})

    if violation_hits:
        violations.append(f"检出违规极限词: {violation_hits}")
        checks.append({
            "name": "极限违规词排查",
            "status": "FAIL",
            "detail": f"检出违规词: {violation_hits}"
        })
    else:
        checks.append({
            "name": "极限违规词排查",
            "status": "PASS",
            "detail": "0 处极限违禁词"
        })

    # 4. 漏斗占比核算
    total_dur = sum(float(c.get("dur", 0.0)) for c in clips)
    if total_dur > 0:
        cat_durs: Dict[str, float] = {}
        for c in clips:
            k = c.get("category", "other")
            cat_durs[k] = cat_durs.get(k, 0.0) + float(c.get("dur", 0.0))

        fabric_ratio = cat_durs.get("fabric", 0.0) / total_dur
        hook_ratio = cat_durs.get("hook", 0.0) / total_dur
        fit_ratio = cat_durs.get("fit", 0.0) / total_dur
        styling_ratio = cat_durs.get("styling", 0.0) / total_dur
        closing_ratio = cat_durs.get("decision_closing", cat_durs.get("closing", 0.0)) / total_dur

        funnel_status = "PASS"
        funnel_notes = []
        if fabric_ratio > 0.2501:
            funnel_status = "FAIL"
            funnel_notes.append(f"面料占比 {fabric_ratio:.1%} > 上限 25.0%")
        if hook_ratio < 0.0999:
            funnel_status = "FAIL"
            funnel_notes.append(f"Hook 占比 {hook_ratio:.1%} < 下限 10.0%")
        if fit_ratio < 0.1499:
            funnel_status = "FAIL"
            funnel_notes.append(f"版型占比 {fit_ratio:.1%} < 下限 15.0%")
        if styling_ratio < 0.2499:
            funnel_status = "FAIL"
            funnel_notes.append(f"搭配占比 {styling_ratio:.1%} < 下限 25.0%")
        if closing_ratio < 0.0999:
            funnel_status = "FAIL"
            funnel_notes.append(f"促单占比 {closing_ratio:.1%} < 下限 10.0%")

        checks.append({
            "name": "漏斗时长与配比合规性",
            "status": funnel_status,
            "detail": f"总时长 {total_dur:.2f}s | 面料={fabric_ratio:.1%}, Hook={hook_ratio:.1%}, 版型={fit_ratio:.1%}, 搭配={styling_ratio:.1%}, 促单={closing_ratio:.1%}" + (f" ({', '.join(funnel_notes)})" if funnel_notes else "")
        })
        if funnel_status == "FAIL":
            violations.extend(funnel_notes)

    passed = len([c for c in checks if c["status"] == "FAIL"]) == 0
    return {
        "passed": passed,
        "checks": checks,
        "violations": violations,
        "total_clips": len(clips),
        "total_duration": total_dur,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="分镜片段品类与语义对齐核验工具")
    parser.add_argument("--plan", required=True, help="plan.json 路径")
    parser.add_argument("--spec", default=None, help="job_spec.json 路径")
    args = parser.parse_args()

    with open(args.plan, "r", encoding="utf-8") as f:
        plan = json.load(f)

    spec = {}
    if args.spec and os.path.exists(args.spec):
        with open(args.spec, "r", encoding="utf-8") as f:
            spec = json.load(f)

    res = verify_clip_alignment(plan, spec)
    print(f"\n[CLIP-ALIGNMENT] 审核结果: {'PASS' if res['passed'] else 'FAIL'}")
    for c in res["checks"]:
        print(f"  [{c['status']}] {c['name']}: {c['detail']}")
    if res["violations"]:
        print(f"\n违规项:")
        for v in res["violations"]:
            print(f"  - {v}")
    sys.exit(0 if res["passed"] else 1)


if __name__ == "__main__":
    main()
