#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S6 商品锚点成片级复算（project-4 v2 规范 §5 可验证指标）。

用法：
  python scripts/audit_product_anchor.py \
      --plan data/work_dirs/project-4/plan.json \
      --ass  data/work_dirs/project-4/subtitles.ass \
      --out  output/project-4/product_anchor_audit.json

输出 M01/M02/M03/M05/M06/M10/M11/M12/M13 与 all_gates_passed：
  M12 同源重叠分镜对（重复画面）、M13 视觉禁入源/区间零命中
  （对应 QC 的 C10/C11 门禁，由 2s 网格目视复核结论驱动）。
任一 BLOCK 指标不达标时返回退出码 1，供交付签署前阻断。
"""

import argparse
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _ass_text(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    out = []
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        for line in f:
            if line.startswith("Dialogue:"):
                parts = line.split(",", 9)
                if len(parts) > 9:
                    out.append(parts[9])
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="S6 商品锚点成片级复算")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--ass", default=None)
    ap.add_argument("--anchor", default="contracts/product_anchor_project-4.yaml")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    import yaml
    from src.tools.product_anchor import audit_plan

    anchor_path = (args.anchor if os.path.isabs(args.anchor)
                   else os.path.join(PROJECT_ROOT, args.anchor))
    with open(anchor_path, "r", encoding="utf-8") as f:
        anchor = yaml.safe_load(f)
    with open(args.plan, "r", encoding="utf-8") as f:
        plan = json.load(f)

    result = audit_plan(plan, anchor, _ass_text(args.ass))
    result["plan_path"] = os.path.abspath(args.plan)
    result["anchor_path"] = os.path.abspath(anchor_path)
    result["product_anchor_enabled"] = bool(
        (plan.get("product_anchor") or {}).get("enabled"))
    result["selection_stats"] = (plan.get("product_anchor") or {}).get("selection", {})

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("=" * 62)
    print("S6 商品锚点成片级复算")
    print("=" * 62)
    for k, v in result.items():
        if k in ("checks", "selection_stats", "M06_他品词命中"):
            continue
        print(f"  {k}: {v}")
    print("  M06_他品词命中:", result.get("M06_他品词命中"))
    print("  checks:", json.dumps(result["checks"], ensure_ascii=False))
    print(f"  all_gates_passed = {result['all_gates_passed']}")
    print(f"  报告: {os.path.abspath(args.out)}")
    return 0 if result["all_gates_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
