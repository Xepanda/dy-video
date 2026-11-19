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


def test_product_identity_gate_blocks_missing_or_unverifiable_relation():
    """测试内容门禁：当分镜缺少商品关系 (UNVERIFIABLE/None) 时必须硬阻断 BLOCK。"""
    auditor = EditorialQcAuditor(policy="strict")
    # c1 缺失 product_relation，c2 标记为 UNVERIFIABLE
    plan = {
        "clips": [
            {"id": "c1", "text": "这件衣服面料很好。"},
            {"id": "c2", "product_relation": "UNVERIFIABLE", "text": "看下细节。"},
        ]
    }
    profile = {"sku_id": "SKU-COAT", "selling_points": []}
    report = auditor.audit_editorial_quality(plan, profile)

    assert report["passed"] is False
    assert report["block_count"] >= 1
    id_check = next(c for c in report["checks"] if c["gate"] == "ProductIdentityGate")
    assert id_check["status"] == "BLOCK"
    assert "c1" in id_check["affected_clips"]
    assert "c2" in id_check["affected_clips"]
    assert "UNVERIFIABLE" in id_check["detail"]


def test_golden_set_manifest_structure():
    """测试金标评测集 Manifest 加载、场景覆盖与真实标注字段完整性。"""
    manifest_path = os.path.join(PROJECT_ROOT, "fixtures", "golden_set_manifest.json")
    assert os.path.isfile(manifest_path)
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    benchmarks = data.get("benchmarks", [])
    assert len(benchmarks) >= 30, f"金标集应至少包含 30 个基准用例，实际为 {len(benchmarks)}"
    categories = {b["category"] for b in benchmarks}
    assert len(categories) >= 10, "金标集类目覆盖度应丰富多样"

    for b in benchmarks:
        assert "material_ref" in b, f"{b.get('id')} 缺少素材引用 material_ref"
        assert "annotated_sku_intervals" in b, f"{b.get('id')} 缺少人工标注 SKU 区间"
        assert isinstance(b["annotated_sku_intervals"], list), f"{b.get('id')} annotated_sku_intervals 必须为列表"
        assert "optimal_cut_points" in b, f"{b.get('id')} 缺少最佳切点 optimal_cut_points"
        assert "expected_duration_range" in b, f"{b.get('id')} 缺少期望时长区间"
        assert "expected_metrics" in b, f"{b.get('id')} 缺少期望指标 expected_metrics"


def main():
    test_product_identity_gate_blocks_other_sku()
    test_product_identity_gate_passes_clean_plan()
    test_product_identity_gate_blocks_missing_or_unverifiable_relation()
    test_selling_point_missing_warns()
    test_golden_set_manifest_structure()
    print("[TEST-EDITORIAL-QC] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
