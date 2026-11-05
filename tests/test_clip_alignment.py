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
    assert any("价格/数字残留" in v for v in res["violations"])


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


def test_clip_alignment_catches_funnel_ratio_imbalance():
    """面料占比超出 25% 必须拦截。"""
    spec = {
        "product": {"title_line1": "法式高级感", "title_line2": "羊毛混纺针织上衣"}
    }
    plan = {
        "clips": [
            {"id": "c01", "dur": 30.0, "category": "fabric", "desc": "面料非常细腻羊毛舒适"}, # 50%
            {"id": "c02", "dur": 30.0, "category": "styling", "desc": "双色好看搭配"},
        ]
    }
    res = verify_clip_alignment(plan, spec)
    assert res["passed"] is False
    assert any("面料占比" in v for v in res["violations"])
