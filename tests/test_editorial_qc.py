# -*- coding: utf-8 -*-
"""
test_editorial_qc.py - PR-08 内容 QC、金标评测集与全流程门禁测试
"""

import os
import sys
import json
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.qc.editorial_qc import EditorialQcAuditor


def test_product_identity_gate_blocks_other_sku():
    """测试内容门禁：成片混入 OTHER_SKU 时必须坚决 BLOCK。"""
    auditor = EditorialQcAuditor(policy="strict")
    plan = {
        "clips": [
            {"id": "c1", "product_relation": "SAME_SKU", "text": "这件羽绒服很好看。"},
            {"id": "c2", "product_relation": "OTHER_SKU", "text": "换下一款毛衣。"},
        ]
    }
    profile = {"sku_id": "SKU-COAT", "selling_points": []}
    report = auditor.audit_editorial_quality(plan, profile)

    assert report["passed"] is False
    assert report["block_count"] >= 1
    id_check = next(c for c in report["checks"] if c["gate"] == "ProductIdentityGate")
    assert id_check["status"] == "BLOCK"
    assert "c2" in id_check["affected_clips"]


def test_product_identity_gate_passes_clean_plan():
    """测试合规本品片段全绿通过。"""
    auditor = EditorialQcAuditor(policy="strict")
    plan = {
        "clips": [
            {"id": "c1", "product_relation": "SAME_SKU", "text": "这件羽绒服保暖轻盈。"},
            {"id": "c2", "product_relation": "SAME_SKU_VARIANT", "text": "另外看下米白色版本。"},
        ]
    }
    profile = {"sku_id": "SKU-COAT", "selling_points": ["保暖", "轻盈"]}
    report = auditor.audit_editorial_quality(plan, profile)

    assert report["passed"] is True
    assert report["block_count"] == 0
    id_check = next(c for c in report["checks"] if c["gate"] == "ProductIdentityGate")
    assert id_check["status"] == "AUTOMATED_PASS"


def test_selling_point_missing_warns():
    """测试必须卖点缺失时发出报警。"""
    auditor = EditorialQcAuditor()
    plan = {
        "clips": [
            {"id": "c1", "product_relation": "SAME_SKU", "text": "这件衣服真的很好看。"}
        ]
    }
    profile = {"sku_id": "SKU-1", "selling_points": ["桑蚕丝", "抗皱"]}
    report = auditor.audit_editorial_quality(plan, profile)

    sp_check = next(c for c in report["checks"] if c["gate"] == "SellingPointEvidenceGate")
    assert sp_check["status"] in ("WARN", "BLOCK")
    assert "桑蚕丝" in sp_check["missing_points"]


def test_golden_set_manifest_structure():
    """测试金标评测集 Manifest 加载与场景覆盖。"""
    manifest_path = os.path.join(PROJECT_ROOT, "fixtures", "golden_set_manifest.json")
    assert os.path.isfile(manifest_path)
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    benchmarks = data.get("benchmarks", [])
    assert len(benchmarks) >= 30, f"金标集应至少包含 30 个基准用例，实际为 {len(benchmarks)}"
    categories = {b["category"] for b in benchmarks}
    assert len(categories) >= 10, "金标集类目覆盖度应丰富多样"


def main():
    test_product_identity_gate_blocks_other_sku()
    test_product_identity_gate_passes_clean_plan()
    test_selling_point_missing_warns()
    test_golden_set_manifest_structure()
    print("[TEST-EDITORIAL-QC] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
