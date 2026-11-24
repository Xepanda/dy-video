# -*- coding: utf-8 -*-
"""
category_profile.py - 数据驱动的电商带货品类档案加载器（V3.3 / V33-01）
=======================================================================
负责从 contracts/profiles/<profile_id>.yaml 加载品类词表、换款/搭配正则、
躯干 ROI 区域策略、漏斗角色词库与防造假材质词库。
默认激活女装档案 (womenswear)，同时为未来美妆/食品/3C 等多品类预留插件位。
"""

import os
import sys
from functools import lru_cache
from typing import Dict, Any, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

PROFILES_DIR = os.path.join(PROJECT_ROOT, "contracts", "profiles")

_FALLBACK_PROFILE: Dict[str, Any] = {
    "profile_id": "womenswear",
    "display_name": "女装直播带货",
    "version": "1.0",
    "changeover_patterns": [
        "换下一款", "看下一件", "下一套", "下一号", "换掉", "拿走", "去换衣服",
        "下一款衣服", "换件衣服", "过款", "换款式", "走开一下", "我们换", "下一件",
        "换下一个", "不能再讲这个了", "我们要开始上对头了", "下楼",
    ],
    "styling_context_patterns": [
        r"搭(配)?.*?(裤|裙|鞋|包|开衫|马甲|西裤|打底)",
        r"配.*?(裤|裙|鞋|包|开衫|马甲|西裤|打底)",
        r"脚上(穿|搭)",
        r"下半身.*?(搭|配)",
        r"里面(搭|穿)",
    ],
    "promotes_other_item_pattern": r"(主推|拍|买|这款|这条|这件裤|给大家|安排|换下).*(裤|裙)",
    "inventory_changeover_regexes": [
        r".*库存.*", r".*只剩.*", r".*单了.*", r".*快下了.*", r".*上链接.*", r".*一号链接.*",
        r".*抢购.*", r".*秒杀.*", r".*现货.*", r".*发货.*", r".*不能再讲.*", r".*上对头.*",
        r".*换下一款.*", r".*下楼.*", r".*过款.*", r".*拿走.*", r".*去换衣服.*", r".*换款式.*",
        r".*不用讲.*", r".*随便对比.*", r".*凑单去拍.*",
        r".*抠衣.*", r".*扣[1一小].*", r".*加[鲜现]货.*", r".*一号[令练界链].*",
        r".*手批.*", r".*抢[毛大]衣.*", r".*先去抢.*", r".*不等了.*", r".*要.*的大宝.*",
    ],
    "narrative_role_keywords": {
        "HOOK": ["你看", "姐妹们", "闭眼入", "第一件", "今天这件", "必须抢"],
        "FABRIC": ["面料", "手感", "羊毛", "羊绒", "真丝", "棉", "材质", "羽绒"],
        "FIT": ["版型", "显瘦", "不挑人", "遮肉", "挺括", "落肩"],
        "STYLING": ["搭配", "穿法", "配裤子", "配裙子", "下半身"],
        "COLOR": ["颜色", "色系", "黑色", "白色", "米色", "咖色"],
        "CLOSING": ["小黄车", "拍一发", "库存", "带回家", "下单", "手慢无"],
    },
    "verified_lexicon": {
        "fabric": [
            "羊绒", "白鸭绒", "鹅绒", "蚕丝", "真丝", "全棉", "纯棉", "亚麻",
            "德绒", "发热", "保暖", "莫代尔", "三防", "防风", "防泼水", "羊毛",
        ],
        "fit": ["显瘦", "修身", "垂坠", "正肩", "落肩", "宽松", "直筒", "包容", "遮肉"],
        "styling": ["百搭", "一衣多穿", "叠穿", "气质", "搭配"],
    },
    "factual_claim_tokens": [
        "100%", "纯羊绒", "纯羊毛", "山羊绒", "桑蚕丝", "真丝", "白鸭绒",
        "鹅绒", "90绒", "95绒", "精梳棉", "三防", "防水", "防风", "羊绒", "羊毛", "德绒",
    ],
    "subcategories": {
        "knitwear": {
            "match_terms": ["针织", "毛衣", "衫", "上衣"],
            "synonym_tokens": [
                "毛衣", "针织", "大毛衫", "线衫", "羊毛衫", "开衫", "打底", "上衣",
                "纱线", "面料", "手感", "版型", "领形", "领口", "正肩", "落肩",
            ],
            "roi_region": "upper_torso",
        },
        "pants": {
            "match_terms": ["裤", "阔腿", "西裤"],
            "synonym_tokens": ["裤子", "西裤", "裤腿", "阔腿裤", "垂坠", "腰头"],
            "roi_region": "lower_body",
        },
        "dress": {
            "match_terms": ["裙", "连衣裙"],
            "synonym_tokens": ["裙子", "连衣裙", "裙摆", "下摆", "腰线"],
            "roi_region": "full_body_dress",
        },
    },
    "director_brief": {
        "max_featured_candidates": 54,
        "max_gray_zone_candidates": 12,
        "role_quotas": {
            "HOOK": 10,
            "FABRIC": 14,
            "FIT": 12,
            "STYLING": 10,
            "COLOR": 4,
            "CLOSING": 4,
            "PROOF": 8,
        },
    },
}


@lru_cache(maxsize=16)
def load_category_profile_by_id(profile_id: str = "womenswear") -> Dict[str, Any]:
    """按 profile_id 加载品类 YAML；若文件缺失安全回退内置女装默认档案。"""
    clean_id = str(profile_id or "womenswear").strip().lower()
    if not clean_id or "/" in clean_id or "\\" in clean_id or ".." in clean_id:
        clean_id = "womenswear"
    path = os.path.join(PROFILES_DIR, f"{clean_id}.yaml")
    if os.path.isfile(path):
        try:
            import yaml
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if isinstance(data, dict) and data:
                merged = dict(_FALLBACK_PROFILE)
                merged.update(data)
                return merged
        except Exception:
            pass
    return dict(_FALLBACK_PROFILE)


def load_category_profile(spec_or_profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """从 job_spec 或 product_profile 解析 domain_profile 并返回完整品类档案。"""
    profile_id = "womenswear"
    if isinstance(spec_or_profile, dict):
        prod = spec_or_profile.get("product")
        if isinstance(prod, dict) and prod.get("domain_profile"):
            profile_id = str(prod["domain_profile"])
        elif spec_or_profile.get("domain_profile"):
            profile_id = str(spec_or_profile["domain_profile"])
    return load_category_profile_by_id(profile_id)


def resolve_subcategory(category_text: str, profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """根据品类/商品名文本匹配子品类名称、扩展词列表与 ROI 区域策略。"""
    prof = profile or load_category_profile_by_id("womenswear")
    cat_lower = str(category_text or "").lower()
    subcats = prof.get("subcategories") or {}
    for sub_name, cfg in subcats.items():
        if not isinstance(cfg, dict):
            continue
        match_terms = cfg.get("match_terms") or []
        if any(str(t).lower() in cat_lower for t in match_terms if t):
            return {
                "subcategory": sub_name,
                "synonym_tokens": list(cfg.get("synonym_tokens") or []),
                "roi_region": str(cfg.get("roi_region") or "upper_torso"),
            }
    return {
        "subcategory": "default",
        "synonym_tokens": [],
        "roi_region": "upper_torso",
    }
