#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_clip_product_alignment.py - 分镜片段品类与语义对齐核验工具
================================================================
依据 2026-11 架构复核重构（解决 B3 与 B5 缺陷）：
  1. 完整文本通道：优先读取分镜完整台词（full_text / whisper_segments 关联），
     绝不局限于 24 字截断的 desc。
  2. 唯一定义复用：复用 detect_price.PriceDetector（防误报 100斤/S码）与
     detect_violation.ViolationDetector（统一消费 violation_words.yaml）。
  3. 契约动态对齐：严格读取 constraints.yaml 的 funnel 秒数区间与
     funnel_enforce_level（默认 WARN，避免类目稀薄素材被一票否决）。
  4. 语义状态解耦：区分 BLOCK 级 violations 与 WARN 级 warnings，
     passed 严格表达是否存在阻断性违规。
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

from src.tools.detect_price import find_price_matches
from src.tools.detect_violation import find_violation_matches

# 服饰跨品类负向词库（作为未显式提供 anchor 时基于推断品类的兜底）
CATEGORY_NEGATIVE_TERMS = {
    "top": [
        "裤", "库长", "裤长", "毛呢裤", "毛泥裤", "裤腿", "西裤", "卫裤", "皮裤",
        "石墨烯", "加绒裤", "棉服", "填充物", "舔充物", "羽绒", "鞋", "靴"
    ],
    "bottom": [
        "上衣", "毛衣", "针织衫", "衬衫", "吊带", "外套", "大衣", "风衣"
    ]
}

# 常见结巴连读数字模式（如 '3030多点'，排除 '100斤'、'S码'、'30%' 等合法度量单位与年份）
STUTTER_NUMBER_REGEX = re.compile(r"(?<!\d)(\d{2,4})\1+多?(?![年月日号版码斤米克分秒度%])")


def _load_yaml_constraints() -> Dict[str, Any]:
    """读取 contracts/constraints.yaml 默认配置。"""
    try:
        import yaml
        cp = os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
        if os.path.exists(cp):
            with open(cp, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}


def infer_product_category(title: str) -> str:
    """根据商品名推断品类大类（top / bottom / other）。"""
    title_lower = title.lower()
    if any(k in title_lower for k in ("上衣", "毛衣", "针织", "衫", "t恤", "卫衣", "开衫")):
        return "top"
    if any(k in title_lower for k in ("裤", "裙", "半裙", "短裤", "长裤")):
        return "bottom"
    return "other"


def _extract_clip_full_text(
    clip: Dict[str, Any],
    segments_by_source: Optional[Dict[str, List[Dict[str, Any]]]] = None
) -> str:
    """获取分镜的完整口播文本。优先读 full_text/desc，未截断时直接使用。"""
    txt = clip.get("full_text") or clip.get("text")
    if txt:
        return str(txt)

    desc = str(clip.get("desc") or "")
    if desc and not desc.endswith("…"):
        return desc

    # 降级：仅当 desc 带有省略号且无 full_text 时，才按中点落在分镜内的 ASR 句反查
    if segments_by_source:
        src = clip.get("source_file") or clip.get("source_tag") or ""
        st = float(clip.get("st", 0.0))
        end = float(clip.get("end", 0.0))
        segs = segments_by_source.get(src) or segments_by_source.get(os.path.basename(src)) or []
        overlapping = [
            s.get("text", "") for s in segs
            if st <= (float(s.get("start", 0)) + float(s.get("end", 0))) / 2.0 <= end
        ]
        if overlapping:
            return "，".join(overlapping)

    return desc


def verify_clip_alignment(
    plan_data: Dict[str, Any],
    spec_data: Optional[Dict[str, Any]] = None,
    constraints: Optional[Dict[str, Any]] = None,
    work_dir: Optional[str] = None,
    custom_negative_terms: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """对 plan_data 中的 clips 执行深度语义与规则核验。"""
    spec_data = spec_data or {}
    constraints = constraints or _load_yaml_constraints()
    clips = plan_data.get("clips", [])

    # 加载 ASR 段索引（用于反查分镜全量口播）
    segments_by_source = {}
    if work_dir and os.path.exists(work_dir):
        for candidate in ("whisper_segments_verified.json", "whisper_segments.json"):
            sp = os.path.join(work_dir, candidate)
            if os.path.exists(sp):
                try:
                    with open(sp, "r", encoding="utf-8") as f:
                        all_segs = json.load(f)
                    for s in all_segs:
                        src_key = s.get("source_file") or s.get("source_tag") or "default"
                        segments_by_source.setdefault(src_key, []).append(s)
                        segments_by_source.setdefault(os.path.basename(src_key), []).append(s)
                    break
                except Exception:
                    pass

    product_info = spec_data.get("product", {})
    title = f"{product_info.get('title_line1', '')} {product_info.get('title_line2', '')}".strip()
    if not title:
        title = spec_data.get("title", "")

    cat = infer_product_category(title)
    # 负向词优选：用户指定 > spec 指定 > 品类兜底
    neg_terms = custom_negative_terms
    if not neg_terms:
        neg_terms = spec_data.get("options", {}).get("negative_keywords")
    if not neg_terms:
        neg_terms = CATEGORY_NEGATIVE_TERMS.get(cat, [])

    checks = []
    violations = []
    warnings = []

    # 1. 负向品类词排查（BLOCK 级）
    neg_hits = []
    for c in clips:
        cid = c.get("id", "unknown")
        full_text = _extract_clip_full_text(c, segments_by_source)
        for t in neg_terms:
            if t in full_text:
                neg_hits.append({"clip_id": cid, "term": t, "text_snippet": full_text[:40]})

    if neg_hits:
        msg = f"检出 {len(neg_hits)} 处非本品负向词: {neg_hits[:3]}"
        violations.append(msg)
        checks.append({
            "name": "商品品类负向词排查",
            "status": "FAIL",
            "detail": msg
        })
    else:
        checks.append({
            "name": "商品品类负向词排查",
            "status": "PASS",
            "detail": "0 处异品类词，100% 聚焦本品"
        })

    # 2. 价格检测与口误数字排查（复用 find_price_matches 单一真值源）
    price_hits = []
    stutter_hits = []
    for c in clips:
        cid = c.get("id", "unknown")
        full_text = _extract_clip_full_text(c, segments_by_source)
        # 检测结巴重复数字（如 3030）
        stutter_m = []
        stutter_texts = set()
        for sm in STUTTER_NUMBER_REGEX.finditer(full_text):
            stutter_m.append(sm.group(0))
            stutter_texts.add(sm.group(0))
            digits_only = re.sub(r"\D", "", sm.group(0))
            if digits_only:
                stutter_texts.add(digits_only)

        if stutter_m:
            stutter_hits.append({"clip_id": cid, "stutter": stutter_m})

        # 检测明确价格表达（剔除口误数字，口误走 WARN 预警）
        detected_prices = find_price_matches(full_text)
        real_prices = [p for p in detected_prices if p.get("text") not in stutter_texts]
        if real_prices:
            price_hits.append({"clip_id": cid, "prices": [p["text"] for p in real_prices]})

    if price_hits:
        msg = f"检出明确价格表达残留: {price_hits}"
        violations.append(msg)
        checks.append({
            "name": "语音价格零残留排查",
            "status": "FAIL",
            "detail": msg
        })
    elif stutter_hits:
        msg = f"检出疑似口误连读数字: {stutter_hits}"
        warnings.append(msg)
        checks.append({
            "name": "语音价格零残留排查",
            "status": "WARN",
            "detail": msg
        })
    else:
        checks.append({
            "name": "语音价格零残留排查",
            "status": "PASS",
            "detail": "0 处价格或口误数字表达"
        })

    # 3. 极限违规词排查（复用 find_violation_matches 单一真值源）
    violation_hits = []
    for c in clips:
        cid = c.get("id", "unknown")
        full_text = _extract_clip_full_text(c, segments_by_source)
        v_matches = find_violation_matches(full_text)
        if v_matches:
            violation_hits.append({"clip_id": cid, "hits": [h["word"] for h in v_matches]})

    if violation_hits:
        msg = f"检出违规极限词: {violation_hits}"
        violations.append(msg)
        checks.append({
            "name": "极限违规词排查",
            "status": "FAIL",
            "detail": msg
        })
    else:
        checks.append({
            "name": "极限违规词排查",
            "status": "PASS",
            "detail": "0 处极限违禁词"
        })

    # 4. 漏斗时长与配比合规性（契约动态核验，遵循 funnel_enforce_level）
    funnel_cfg = constraints.get("funnel", {}) or {}
    enforce_level = str(constraints.get("funnel_enforce_level", "WARN")).upper()
    total_dur = sum(float(c.get("dur", 0.0)) for c in clips)

    cat_durs: Dict[str, float] = {}
    for c in clips:
        k = c.get("category", "other")
        cat_durs[k] = cat_durs.get(k, 0.0) + float(c.get("dur", 0.0))

    funnel_notes = []
    # 核对秒数区间
    for cat_name, interval in funnel_cfg.items():
        if isinstance(interval, dict):
            c_min = float(interval.get("min", 0))
            c_max = float(interval.get("max", 9999))
            act = cat_durs.get(cat_name, 0.0)
            if act < c_min:
                funnel_notes.append(f"{cat_name}={act:.1f}s < 下限 {c_min}s")
            elif act > c_max:
                funnel_notes.append(f"{cat_name}={act:.1f}s > 上限 {c_max}s")

    # 兜底核对：面料占比防堆砌上限 25%
    fabric_ratio = cat_durs.get("fabric", 0.0) / max(0.1, total_dur)
    if fabric_ratio > 0.2501:
        funnel_notes.append(f"面料占比 {fabric_ratio:.1%} > 上限 25.0%")

    funnel_status = "PASS"
    if funnel_notes:
        funnel_status = "FAIL" if enforce_level == "BLOCK" else "WARN"
        note_str = f"漏斗配额提示: {', '.join(funnel_notes)}"
        if funnel_status == "FAIL":
            violations.append(note_str)
        else:
            warnings.append(note_str)

    checks.append({
        "name": "漏斗时长与配比合规性",
        "status": funnel_status,
        "detail": f"总时长 {total_dur:.2f}s | 面料={fabric_ratio:.1%} (各段时长: {cat_durs})" + (f" ({', '.join(funnel_notes)})" if funnel_notes else "")
    })

    passed = (len(violations) == 0)
    return {
        "passed": passed,
        "checks": checks,
        "violations": violations,
        "warnings": warnings,
        "total_clips": len(clips),
        "total_duration": total_dur,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="分镜片段品类与语义对齐核验工具")
    parser.add_argument("--plan", required=True, help="plan.json 路径")
    parser.add_argument("--spec", default=None, help="job_spec.json 路径")
    parser.add_argument("--work-dir", default=None, help="工作目录（存放 whisper_segments）")
    args = parser.parse_args()

    with open(args.plan, "r", encoding="utf-8") as f:
        plan = json.load(f)

    spec = {}
    if args.spec and os.path.exists(args.spec):
        with open(args.spec, "r", encoding="utf-8") as f:
            spec = json.load(f)

    res = verify_clip_alignment(plan, spec, work_dir=args.work_dir)
    print(f"\n[CLIP-ALIGNMENT] 审核结果: {'PASS' if res['passed'] else 'FAIL'}")
    for c in res["checks"]:
        print(f"  [{c['status']}] {c['name']}: {c['detail']}")
    if res["warnings"]:
        print("\n提示信息 (WARN):")
        for w in res["warnings"]:
            print(f"  - {w}")
    if res["violations"]:
        print("\n违规项 (BLOCK):")
        for v in res["violations"]:
            print(f"  - {v}")
    sys.exit(0 if res["passed"] else 1)


if __name__ == "__main__":
    main()
