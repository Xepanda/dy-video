#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_clip_alignment.py - 分镜片段品类与语义对齐门禁单元测试
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.tools.verify_clip_product_alignment import verify_clip_alignment


def test_clip_alignment_pass():
    """正常本品针织衫分镜：全项通过。"""
    spec = {
        "product": {"title_line1": "法式高级感", "title_line2": "羊毛混纺针织上衣"}
    }
    plan = {
        "clips": [
            {"id": "c01", "dur": 6.0, "category": "hook", "desc": "松弛非常显瘦全部冲这一件毛衣"},
            {"id": "c02", "dur": 9.0, "category": "fabric", "desc": "羊毛非常细腻像羊绒一样舒适顺滑"},
            {"id": "c03", "dur": 15.0, "category": "fit", "desc": "小斗篷遮肉显腰身S码100斤M码115斤"},
            {"id": "c04", "dur": 20.0, "category": "styling", "desc": "黑色燕麦色两个颜色搭大衣"},
            {"id": "c05", "dur": 10.0, "category": "decision_closing", "desc": "首批现货在仓卖完不补先拍现货"},
        ]
    }
    res = verify_clip_alignment(plan, spec)
    assert res["passed"] is True, f"应全项通过，实际违规: {res['violations']}"
    assert len(res["violations"]) == 0
    print("[PASS] test_clip_alignment_pass")


def test_clip_alignment_catches_negative_category():
    """混入下装/皮裤/外套等非本品负向词时必须拦截。"""
    spec = {
        "product": {"title_line1": "法式高级感", "title_line2": "羊毛混纺针织上衣"}
    }
    plan = {
        "clips": [
            {"id": "c01", "dur": 60.0, "category": "hook", "desc": "这一款搭配皮裤和石墨烯加绒裤特别好看"},
        ]
    }
    res = verify_clip_alignment(plan, spec)
    assert res["passed"] is False
    assert any("负向词" in v for v in res["violations"])
    print("[PASS] test_clip_alignment_catches_negative_category")


def test_clip_alignment_catches_price_stutter():
    """口误数字（如 3030多点）触发预警。"""
    spec = {
        "product": {"title_line1": "法式高级感", "title_line2": "羊毛混纺针织上衣"}
    }
    plan = {
        "clips": [
            {"id": "c01", "dur": 60.0, "category": "fabric", "desc": "这里面有3030多点羊毛成分"},
        ]
    }
    res = verify_clip_alignment(plan, spec)
    assert any("连读数字" in w or "价格" in w for w in res["warnings"])
    print("[PASS] test_clip_alignment_catches_price_stutter")


def test_clip_alignment_catches_violation_words():
    """极限违规词（如 最大）必须拦截。"""
    spec = {
        "product": {"title_line1": "法式高级感", "title_line2": "羊毛混纺针织上衣"}
    }
    plan = {
        "clips": [
            {"id": "c01", "dur": 60.0, "category": "fit", "desc": "这个尺码最大穿到130斤"},
        ]
    }
    res = verify_clip_alignment(plan, spec)
    assert res["passed"] is False
    assert any("极限词" in v for v in res["violations"])
    print("[PASS] test_clip_alignment_catches_violation_words")


def test_clip_alignment_catches_funnel_ratio_imbalance():
    """面料占比超出 25% 且配置为 BLOCK 时拦截。"""
    spec = {
        "product": {"title_line1": "法式高级感", "title_line2": "羊毛混纺针织上衣"}
    }
    plan = {
        "clips": [
            {"id": "c01", "dur": 30.0, "category": "fabric", "desc": "面料非常细腻羊毛舒适"}, # 50%
            {"id": "c02", "dur": 30.0, "category": "styling", "desc": "双色好看搭配"},
        ]
    }
    # 默认 WARN 级别下不拦截成 FAIL
    res_warn = verify_clip_alignment(plan, spec, constraints={"funnel_enforce_level": "WARN"})
    assert res_warn["passed"] is True
    assert any("面料占比" in w for w in res_warn["warnings"])

    # BLOCK 级别下强制拦截
    res_block = verify_clip_alignment(plan, spec, constraints={"funnel_enforce_level": "BLOCK"})
    assert res_block["passed"] is False
    assert any("面料占比" in v for v in res_block["violations"])
    print("[PASS] test_clip_alignment_catches_funnel_ratio_imbalance")


def test_clip_alignment_nested_constraints_hierarchy():
    """验证从 video.funnel / video.funnel_enforce_level 真实层级加载约束。"""
    spec = {
        "options": {"product_anchor": "contracts/product_anchor_project-4.yaml"},
        "product": {"title_line1": "法式高级感", "title_line2": "羊毛混纺针织上衣"}
    }
    # 模拟真实 contracts/constraints.yaml 结构
    nested_constraints = {
        "video": {
            "funnel_enforce_level": "BLOCK",
            "funnel": {
                "hook": {"min": 3, "max": 12},
                "fabric": {"min": 8, "max": 20},
                "craft_detail": {"min": 8, "max": 20},
                "fit": {"min": 8, "max": 15},
                "styling": {"min": 15, "max": 30},
                "decision_closing": {"min": 6, "max": 12},
            }
        }
    }
    # 制造 styling 严重超长违规 (styling=45s > 30s)
    plan_invalid = {
        "clips": [
            {"id": "c01", "dur": 5.0, "is_hook": True, "category": "hook", "desc": "松弛显瘦毛衣"},
            {"id": "c02", "dur": 10.0, "category": "fabric", "desc": "羊毛细腻"},
            {"id": "c03", "dur": 45.0, "category": "styling", "desc": "搭配大衣好看"},
        ]
    }
    res_invalid = verify_clip_alignment(plan_invalid, spec, constraints=nested_constraints)
    assert res_invalid["passed"] is False
    assert any("styling" in v and "上限" in v for v in res_invalid["violations"])
    print("[PASS] test_clip_alignment_nested_constraints_hierarchy")


def test_asr_non_knitwear_no_miscorrection():
    """验证非针织品类（如短裤/五金）在工业板型/浑坊/全民扣衣等语境下不发生全局误伤。"""
    from src.core.asr_agent_verifier import AsrContextVerifier

    spec_shorts = {
        "product": {"title_line1": "纯棉休闲短裤", "title_line2": "男士百搭夏季"}
    }
    verifier = AsrContextVerifier(spec_shorts)
    test_cases = [
        "工业板型设计很坚固",
        "大家先不要浑坊这件事情",
        "买气球吃鲜货",
        "大家全民扣衣去支持"
    ]
    for text in test_cases:
        res, _ = verifier.verify_and_correct([{"start": 0.0, "end": 3.0, "text": text}])
        assert res[0]["text"] == text, f"非针织文本发生误伤: {text} -> {res[0]['text']}"
    print("[PASS] test_asr_non_knitwear_no_miscorrection")


def main():
    test_clip_alignment_pass()
    test_clip_alignment_catches_negative_category()
    test_clip_alignment_catches_price_stutter()
    test_clip_alignment_catches_violation_words()
    test_clip_alignment_catches_funnel_ratio_imbalance()
    test_clip_alignment_nested_constraints_hierarchy()
    test_asr_non_knitwear_no_miscorrection()
    print("\n[TEST-CLIP-ALIGNMENT] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
