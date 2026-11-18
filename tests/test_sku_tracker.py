# -*- coding: utf-8 -*-
"""
test_sku_tracker.py - PR-04 商品关系识别与视觉证据测试
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

from src.core.sku_tracker import SkuTracker
from src.core.vision_backend import LocalCVVisionBackend, VisionBackend


class MockVisionBackend(VisionBackend):
    def __init__(self, sku_relation="SAME_SKU", confidence=0.9, available=True):
        self._rel = sku_relation
        self._conf = confidence
        self._avail = available

    def is_available(self):
        return self._avail

    def analyze_candidate(self, candidate, frame_paths, product_profile):
        return {
            "candidate_id": candidate.get("candidate_id", ""),
            "vision_available": self._avail,
            "sku_relation": self._rel,
            "action_type": "FRONT_SHOW",
            "motion_phase": "CORE",
            "visual_evidence_refs": frame_paths[:1],
            "confidence": self._conf,
            "uncertainties": [] if self._avail else ["UNVERIFIABLE"],
            "why": "Mock decision",
        }


def _get_sample_profile():
    return {
        "sku_id": "SKU-COAT-01",
        "name": "短款立领羽绒服",
        "category": "羽绒服",
        "reference_image_ids": ["data/img1.jpg"],
        "allowed_variants": ["米白色", "黑色"],
        "selling_points": ["保暖", "轻盈", "显瘦"]
    }


def test_top_with_pants_styling(product_profile=None):
    """用例 1：上衣搭裤子 -> ACCESSORY_OR_STYLING。"""
    product_profile = product_profile or _get_sample_profile()
    tracker = SkuTracker()
    text = "这件羽绒服我们下半身搭一条西裤姐妹们看下整体效果。"
    rel, conf, reasons, gap = tracker.identify_relation(text, product_profile)
    assert rel == "ACCESSORY_OR_STYLING", f"期望 ACCESSORY_OR_STYLING，实际为 {rel}"
    assert any("搭配" in r for r in reasons)


def test_changeover_other_sweater(product_profile=None):
    """用例 2：换另一件毛衣 -> OTHER_SKU。"""
    product_profile = product_profile or _get_sample_profile()
    tracker = SkuTracker()
    text = "这件羽绒服先拿走，我们换下一款毛衣来看一下。"
    rel, conf, reasons, gap = tracker.identify_relation(text, product_profile)
    assert rel == "OTHER_SKU", f"期望 OTHER_SKU，实际为 {rel}"
    assert any("换款" in r for r in reasons)


def test_same_sku_different_color_variant(product_profile=None):
    """用例 3：同款不同色 -> SAME_SKU_VARIANT。"""
    product_profile = product_profile or _get_sample_profile()
    tracker = SkuTracker()
    text = "来看一下另外一个颜色米白色，也是特别显白好看的。"
    rel, conf, reasons, gap = tracker.identify_relation(text, product_profile)
    assert rel == "SAME_SKU_VARIANT", f"期望 SAME_SKU_VARIANT，实际为 {rel}"
    assert any("米白色" in r for r in reasons)


def test_conflict_audio_visual(product_profile=None):
    """用例 4：口播与画面冲突 -> UNCERTAIN。"""
    product_profile = product_profile or _get_sample_profile()
    mock_vision = MockVisionBackend(sku_relation="OTHER_SKU", confidence=0.85)
    tracker = SkuTracker(vision_backend=mock_vision)
    text = "现在身上穿的这件羽绒服版型极佳。"
    vis_dec = mock_vision.analyze_candidate({"candidate_id": "c1"}, ["f1.jpg"], product_profile)
    rel, conf, reasons, gap = tracker.identify_relation(text, product_profile, visual_decision=vis_dec)
    assert rel == "UNCERTAIN", f"音画冲突应为 UNCERTAIN，实际为 {rel}"


def test_temporary_absence(product_profile=None):
    """用例 5：临时离场 -> OTHER_SKU。"""
    product_profile = product_profile or _get_sample_profile()
    tracker = SkuTracker()
    text = "模特去换衣服了，大家稍等十秒钟。"
    rel, conf, reasons, gap = tracker.identify_relation(text, product_profile)
    assert rel == "OTHER_SKU"


def test_no_image_unverifiable():
    """用例 6：商品图不存在时视觉后端标 UNVERIFIABLE。"""
    backend = LocalCVVisionBackend()
    no_img_profile = {
        "sku_id": "SKU-NO-IMG",
        "name": "风衣",
        "category": "外套",
        "reference_image_ids": []
    }
    cand = {"candidate_id": "c_no_img", "duration_ms": 3000}
    dec = backend.analyze_candidate(cand, ["dummy_frame.jpg"], no_img_profile)
    assert dec["vision_available"] is False
    assert dec["sku_relation"] == "UNCERTAIN"
    assert any("UNVERIFIABLE" in u for u in dec["uncertainties"])


def main():
    prof = _get_sample_profile()
    test_top_with_pants_styling(prof)
    test_changeover_other_sweater(prof)
    test_same_sku_different_color_variant(prof)
    test_conflict_audio_visual(prof)
    test_temporary_absence(prof)
    test_no_image_unverifiable()
    print("[TEST-SKU-TRACKER] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
