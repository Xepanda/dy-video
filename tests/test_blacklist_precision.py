#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_blacklist_precision.py - 黑名单与价格检测精度回归（2026-10-21 泛化验收修正）
==================================================================================
背景：4 条新素材验收暴露两类误杀，直接导致成片类目缺失（styling/fit 归零）：
  A. 价格检测器把裸码数当价格（"从125穿到150斤""从101开始穿""105你看穿出来"）
  B. 库存/促销/换款正则过宽，误杀穿搭与面料内容
     （牛仔裤/我就没拍了/自己买/款型/偏薄/价格/来看这个/主播）

用例全部取自真实素材（down_jacket_11241 / workwear_11186 / grid_coat_11231 的
ASR 实际输出），防止修复后回归。

运行: python -u tests/test_blacklist_precision.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.tools.detect_price import find_price_matches
from src.tools.build_plan import is_blacklisted

# 必须拦截的真实价格语料
MUST_BLOCK_PRICE = [
    "我们哪怕卖到你599699这裤子",
    "其实利润都没有299的高",
    "300多块钱 好穿哦 真的好穿",
    "这个商场也是卖到一千六百多块钱",
    "我给大家二百多块钱",
    "额绒的也要700多到800多900多块钱",
    "你自己看799米",
    "因为绒它也分369等",
    "这个价格只要299块钱",
    "到手价299",
    "适合的那个拼色的799的那件薄薄的那件俄容跟它是同一工厂做的",
    "俄容小弟弟你们799都没开",
]

# 必须放行的尺码/穿搭/面料内容（曾被误杀）
MUST_ALLOW_CONTENT = [
    "从125穿到150斤姐们",
    "所以其实是从105可以穿",
    "所以我们推是从101开始穿",
    "105你看穿出来",
    "如果105",
    "这个我觉得搭个蓝色牛仔裤吧",
    "一般的蓝牛仔",
    "我觉得很时尚 你就搭个小牛仔裤 搭个胃裤都好看",
    "我就没拍了",
    "这个款型特别显瘦",
    "这个偏薄但是很保暖",
    "不只是价格的问题",
    "大家都来看这个面料",
]

# 必须拦截的库存/促销/换款话术
MUST_BLOCK_BLACKLIST = [
    "只剩最后三条了我要赶紧",
    "库存不多了宝宝们",
    "上链接了宝宝们",
    "299秒杀",
    "你们今天福利来啦",
    "我们来穿下一款",
    "主播换衣服了",
    "这个款已经断码了只剩大码",
]


def main():
    failures = []

    print("[TEST-黑名单精度] A. 真实价格语料必须拦截")
    for t in MUST_BLOCK_PRICE:
        hits = find_price_matches(t)
        if not hits:
            failures.append(f"价格漏网: {t}")
            print(f"  [FAIL] 漏网: {t}")
        else:
            print(f"  [PASS] 拦截: {t}  -> {hits[0]['rule']}")

    print("\n[TEST-黑名单精度] B. 尺码/穿搭/面料内容必须放行")
    for t in MUST_ALLOW_CONTENT:
        hits = find_price_matches(t)
        if hits:
            failures.append(f"内容误判为价格: {t} -> {hits}")
            print(f"  [FAIL] 误判为价格: {t} -> {hits}")
            continue
        bad, reason = is_blacklisted(t)
        if bad:
            failures.append(f"内容被黑名单误杀: {t} ({reason})")
            print(f"  [FAIL] 黑名单误杀: {t} ({reason})")
        else:
            print(f"  [PASS] 放行: {t}")

    print("\n[TEST-黑名单精度] C. 库存/促销/换款必须拦截")
    for t in MUST_BLOCK_BLACKLIST:
        bad, reason = is_blacklisted(t)
        if not bad:
            failures.append(f"黑名单漏网: {t}")
            print(f"  [FAIL] 漏网: {t}")
        else:
            print(f"  [PASS] 拦截: {t} ({reason[:28]})")

    print(f"\n[TEST-黑名单精度] {'全部通过' if not failures else f'失败 {len(failures)} 项'}")
    if failures:
        for f in failures:
            print(f"  - {f}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
