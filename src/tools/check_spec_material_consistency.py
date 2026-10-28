#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_spec_material_consistency.py - 素材-契约一致性预检（反馈驱动 2026-10-27）
=================================================================================
背景（2026-10-26 教训）：5 段直播素材实拍为黑色马甲，而 job_spec.json 契约商品
为渐变针织衫，成片口播与封面商品不符——正是抖音「虚假宣传-商品信息不一致」
违规类型。本工具在 Phase 1.5 执行预检，把错配暴露在组装之前。

检查项：
  C1 规范关键词覆盖率 —— spec.options.highlight_keywords 在 ASR 台词中的命中比例；
  C2 商品名词覆盖率   —— 契约商品名（title_line1/2）核心词在台词中的命中比例；
  C3 衣着一致性       —— 复用 detect_garment_match（直方图降级），与人气封面帧对比；
  C4 违禁词扫描       —— detect_violation 全量扫描（登记，不拦截）。

输出 consistency_report.json：
  {passed, checks: [{id, name, level, status, measured, threshold, detail}], warnings[]}

CLI:
  python src/tools/check_spec_material_consistency.py --spec job_spec.json \
      --segments whisper_segments_verified.json --cover-frame cand.jpg \
      --out-json consistency_report.json
退出码: 0=通过, 1=有 WARN, 2=BLOCK 失败
"""

import os
import sys
import json
import argparse
from typing import List, Dict, Any, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.tools.detect_violation import ViolationDetector
from src.core.path_resolver import resolve_path


def _load_constraints(path: str = None) -> Dict[str, Any]:
    path = path or os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
    if not os.path.exists(path):
        return {}
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _product_core_words(spec: Dict[str, Any]) -> List[str]:
    """从商品名提取核心词（长度 >= 2 的分词）。"""
    product = spec.get("product", {}) or {}
    words = []
    for key in ("title_line1", "title_line2"):
        t = (product.get(key) or "").strip()
        if not t:
            continue
        # 轻量切分：按常见品类/材质词表匹配，其次按 2 字滑窗
        for kw in ("针织衫", "桑蚕丝", "羊绒", "羊毛", "渐变", "开衫", "毛衣",
                   "卫衣", "外套", "羽绒服", "马甲", "衬衫", "T恤", "裤子"):
            if kw in t:
                words.append(kw)
        if not words:
            for i in range(0, max(0, len(t) - 1)):
                words.append(t[i:i + 2])
    seen, out = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:8]


def check_spec_material_consistency(
    spec: Dict[str, Any],
    segments: List[Dict[str, Any]],
    cover_frame_path: str = None,
    constraints: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """执行素材-契约一致性预检。"""
    constraints = constraints or _load_constraints()
    cc = constraints.get("consistency_check", {}) or {}
    if not cc.get("enabled", True):
        return {"passed": True, "checks": [], "warnings": ["一致性预检已禁用"]}

    kw_warn = float(cc.get("keyword_coverage_warn", 0.34))
    pw_warn = float(cc.get("product_word_warn", 0.5))
    gm_warn = float(cc.get("garment_match_warn", 0.15))
    enforce = str(cc.get("enforce_level", "WARN")).upper()

    all_text = "".join((s.get("text") or "") for s in segments)
    checks: List[Dict[str, Any]] = []
    warnings: List[str] = []

    # C1 规范关键词覆盖率
    keywords = (spec.get("options", {}) or {}).get("highlight_keywords", []) or []
    if keywords:
        hit_kws = [k for k in keywords if k in all_text]
        cov = len(hit_kws) / len(keywords)
        status = "PASS" if cov >= kw_warn else "WARN"
        checks.append({
            "id": "C1", "name": "规范关键词覆盖率", "level": "WARN",
            "status": status, "measured": round(cov, 3),
            "threshold": kw_warn,
            "detail": f"{len(hit_kws)}/{len(keywords)} 命中: {', '.join(hit_kws) or '无'}",
        })
        if status == "WARN":
            warnings.append(
                f"规范关键词覆盖率 {cov:.0%} < {kw_warn:.0%}"
                f"（命中 {len(hit_kws)}/{len(keywords)}）——主播话术可能未覆盖契约卖点")

    # C2 商品名词覆盖率
    core_words = _product_core_words(spec)
    if core_words:
        hit_words = [w for w in core_words if w in all_text]
        cov = len(hit_words) / len(core_words)
        status = "PASS" if cov >= pw_warn else "WARN"
        checks.append({
            "id": "C2", "name": "商品名词覆盖率", "level": "WARN",
            "status": status, "measured": round(cov, 3),
            "threshold": pw_warn,
            "detail": f"{len(hit_words)}/{len(core_words)} 命中: {', '.join(hit_words) or '无'}",
        })
        if status == "WARN":
            warnings.append(
                f"商品名核心词覆盖率 {cov:.0%} < {pw_warn:.0%}"
                f"（命中 {len(hit_words)}/{len(core_words)}）——素材商品可能与契约商品不一致")

    # C3 衣着一致性（封面帧 vs 商品图）
    product = spec.get("product", {}) or {}
    item_images = []
    for img in (product.get("item_images") or []):
        rp = resolve_path(img, PROJECT_ROOT)
        if rp and os.path.exists(rp):
            item_images.append(rp)
    if cover_frame_path and os.path.exists(cover_frame_path) and item_images:
        try:
            from src.tools.detect_garment_match import garment_match_score
            score = garment_match_score(cover_frame_path, item_images[0])
            val = float(score.get("score", 0.0) if isinstance(score, dict) else score)
            status = "PASS" if val >= gm_warn else "WARN"
            checks.append({
                "id": "C3", "name": "衣着一致性（封面帧 vs 商品图）", "level": "WARN",
                "status": status, "measured": round(val, 4),
                "threshold": gm_warn, "detail": str(score)[:160],
            })
            if status == "WARN":
                warnings.append(
                    f"衣着一致性 {val:.3f} < {gm_warn}——封面人像衣着与契约商品图可能不一致")
        except Exception as e:
            checks.append({
                "id": "C3", "name": "衣着一致性（封面帧 vs 商品图）", "level": "WARN",
                "status": "WARN", "measured": None, "threshold": gm_warn,
                "detail": f"检测失败（不拦截）: {str(e)[:100]}",
            })

    # C4 违禁词扫描（登记）
    det = ViolationDetector()
    v_hits = det.scan_segments(segments)
    checks.append({
        "id": "C4", "name": "违规词扫描（ASR 台词）", "level": "INFO",
        "status": "PASS" if not v_hits else "INFO",
        "measured": len(v_hits), "threshold": 0,
        "detail": (f"命中 {len(v_hits)} 处，将三通道处理"
                   if v_hits else "未检出违规词"),
    })

    n_warn = sum(1 for c in checks if c["status"] == "WARN")
    passed = not (enforce == "BLOCK" and n_warn > 0)
    return {
        "passed": passed,
        "enforce_level": enforce,
        "checks": checks,
        "warnings": warnings,
        "summary": {"n_checks": len(checks), "n_warnings": n_warn},
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="素材-契约一致性预检")
    parser.add_argument("--spec", default="job_spec.json")
    parser.add_argument("--segments", required=True)
    parser.add_argument("--cover-frame", default=None)
    parser.add_argument("--out-json", default=None)
    args = parser.parse_args(argv)

    with open(args.spec, "r", encoding="utf-8") as f:
        spec = json.load(f)
    with open(args.segments, "r", encoding="utf-8") as f:
        segments = json.load(f)

    result = check_spec_material_consistency(
        spec, segments, cover_frame_path=args.cover_frame)
    print(f"[CONSISTENCY] 预检完成: {'PASS' if result['passed'] else 'BLOCK'}"
          f"（{result['summary']['n_warnings']} 条 WARN）")
    for w in result["warnings"]:
        print(f"  [WARN] {w}")
    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[OUT] {args.out_json}")
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
