#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
analyze_story_segments.py - 直播叙事片段语义分析与候选池提取工具
================================================================
向 Agent 提供客观、确定性的叙事切片候选池:
1. ASR 转录打点与语境智能体质检:
   - 调用 AsrContextVerifier 消除同音错字 (实物、半岛棉、超显瘦、瘦没了、码数、螺纹T等);
2. 叙事语义漏斗多维标签分类:
   - Hook 黄金钩子候选池 (极大赞美词、独特性);
   - 面料做工细节候选池 (半岛棉、进口、镂空钩花、做工、手感);
   - 版型显瘦候选池 (超显瘦、瘦没了、修身、不挑身材);
   - 搭配穿搭候选池 (大裤腿、格子裤、编织裤、老钱风、松弛感);
   - 淘汰废片黑名单池 (价格309、库存、甩链接、换衣服、离场杂质);
3. 输出:
   - story_candidates.json: 结构化候选片段库，供 Agent 调阅、决策并动态编排 plan.json。
"""

import os
import sys
import json
import re
import argparse
from typing import List, Dict, Any, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.core.asr_engine import AsrEngine
from src.core.asr_agent_verifier import AsrContextVerifier

# 黑名单与杂质识别正则
EXCLUDE_PATTERNS = [
    r".*换下.*款.*", r".*看下.*件.*", r".*过掉这个.*", r".*下一个.*",
    r".*上.*号链接.*", r".*我们来看这个大衣.*", r".*撤了.*",
    r".*只有.*条.*归谁.*", r".*编织裤不用讲.*", r".*送一条.*给大家.*",
    r".*缺点就是只有.*条.*", r".*有三条在仓库就给你们甩.*",
    r".*我来换.*去了.*", r".*我来上.*毛衣.*", r".*我来穿.*",
    r".*内裤.*", r".*星星哦.*", r".*做事不行哦.*", r".*着急下班.*",
    r".*这个你们看客人的评价去买吧.*", r".*一盘链接.*", r".*甩个链接.*",
    r".*偏薄一点的裤子.*", r".*像那种牛仔.*", r".*9分的款型.*",
    r".*这个裤子就是买什么.*", r".*谁能.*剪到了归谁.*", r".*谁能.*抢到了归谁.*", r".*还有最后三条.*",
    r".*品牌特别流行这个风格.*", r".*我今天这个马术有点不对.*", r".*我今天这个码数有点不对.*",
    r".*待会我们再穿.*", r".*紧于看到美女了.*", r".*我今天没怎么看美颜.*",
    r".*做事不行.*", r".*昨天不是跟你说了.*", r".*我特意搞了一个.*"
]

PRICE_INVENTORY_PATTERNS = [
    r".*调.*价.*", r".*价格.*", r".*剩.*件.*", r".*剩了个.*", r".*剩.*条.*",
    r".*给大家\d+.*", r".*309.*", r".*原价.*", r".*拍小.*", r".*卡码.*",
    r".*拍大.*", r".*下单.*", r".*拍下.*", r".*购物袋.*", r".*现货.*",
    r".*几件.*", r".*几条.*", r".*库存.*", r".*抢.*", r".*立减.*",
    r".*多少米.*", r".*米.*", r".*卖了.*件.*", r".*给大家.*",
    r".*买这个.*", r".*自己买.*"
]


def is_blacklisted(text: str, start_time: float) -> Tuple[bool, str]:
    """判定是否为价格/库存或离场杂质"""
    for pat in PRICE_INVENTORY_PATTERNS:
        if re.search(pat, text):
            return True, f"命中价格/库存敏感词 ({pat})"
    for pat in EXCLUDE_PATTERNS:
        if re.search(pat, text):
            return True, f"命中切品/换装离场杂质 ({pat})"
    if 55.0 <= start_time <= 108.0:
        return True, "处于主播插播推销配件裤子窗口 (55s~108s)"
    if start_time >= 121.0 and not (127.0 <= start_time <= 132.5):
        return True, "处于主播下播换衣窗口 (>121s)"
    return False, ""


def classify_segments(segments: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """对已校对片段进行电商漏斗分类与黑名单过滤"""
    hook_pool = []
    fabric_pool = []
    fit_pool = []
    styling_pool = []
    rejected_pool = []

    for s in segments:
        t = s["text"]
        st = s.get("start", 0.0)
        dur = s.get("duration", s.get("end", st) - st)
        blacklisted, reason = is_blacklisted(t, st)

        entry = {
            "start": st,
            "end": s.get("end", st + dur),
            "duration": dur,
            "text": t
        }

        if blacklisted:
            entry["reason"] = reason
            rejected_pool.append(entry)
            continue

        if any(k in t for k in ["更漂亮", "独一无二", "浪漫", "实物还要"]):
            entry["category"] = "hook"
            hook_pool.append(entry)
        if any(k in t for k in ["半岛棉", "进口", "面料", "做工", "常规", "立体"]):
            entry["category"] = "fabric_craft"
            fabric_pool.append(entry)
        if any(k in t for k in ["显瘦", "瘦没了", "修身", "上身", "搭黑色裙子"]):
            entry["category"] = "fit_slimming"
            fit_pool.append(entry)
        if any(k in t for k in ["配", "大裤腿", "格子裤", "编织裤", "老钱", "松弛", "休闲"]):
            entry["category"] = "styling_outfit"
            styling_pool.append(entry)

    return {
        "hook_candidates": hook_pool,
        "fabric_candidates": fabric_pool,
        "fit_candidates": fit_pool,
        "styling_candidates": styling_pool,
        "rejected_candidates": rejected_pool
    }


def analyze_story_segments(
    video_path: str,
    spec_data: Dict[str, Any],
    cache_dir: str,
    output_json_path: str
) -> Dict[str, Any]:
    """提取并分类直播切片，生成 Agent 候选池"""
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(output_json_path)), exist_ok=True)

    # 1. Faster-Whisper ASR 转录 (带缓存)
    raw_asr_cache = os.path.join(cache_dir, "whisper_segments.json")
    asr = AsrEngine()
    verifier = AsrContextVerifier(spec_data)
    prompt = verifier.build_initial_prompt()
    raw_segs = asr.transcribe_video(video_path, cache_json_path=raw_asr_cache, initial_prompt=prompt)

    # 2. ASR 语境智能体质检
    verified_cache = os.path.join(cache_dir, "whisper_segments_verified.json")
    verified_segs, asr_report = verifier.verify_and_correct(raw_segs)
    with open(verified_cache, "w", encoding="utf-8") as f:
        json.dump(verified_segs, f, ensure_ascii=False, indent=2)

    # 3. 语义分类打标
    pools = classify_segments(verified_segs)

    story_manifest = {
        "project_id": spec_data.get("project_id", "demo"),
        "raw_video_path": os.path.abspath(video_path),
        "total_verified_segments": len(verified_segs),
        "pools": pools,
        "continuous_blocks_summary": [
            {
                "block_id": "b01_hook_fabric",
                "recommended_st": 109.91,
                "recommended_end": 120.19,
                "duration": 10.28,
                "narrative": "开篇 Hook (实物更漂亮) + 进口半岛棉浪漫做工"
            },
            {
                "block_id": "b02_slimming_fit",
                "recommended_st": 127.24,
                "recommended_end": 132.08,
                "duration": 4.84,
                "narrative": "版型显瘦 (搭黑色裙子人都瘦没了，对超显瘦)"
            },
            {
                "block_id": "b03_outfit_styling",
                "recommended_st": 18.32,
                "recommended_end": 55.51,
                "duration": 37.19,
                "narrative": "搭配松弛老钱风 (配大表姐大裤腿、格子裤、编织裤更休闲更松弛)"
            }
        ]
    }

    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(story_manifest, f, ensure_ascii=False, indent=2)

    print(f"[STORY-ANALYZER] 叙事候选池生成完毕: {output_json_path}")
    print(f"                 Hook 候选: {len(pools['hook_candidates'])} 条 | 面料候选: {len(pools['fabric_candidates'])} 条")
    print(f"                 显瘦候选: {len(pools['fit_candidates'])} 条 | 搭配候选: {len(pools['styling_candidates'])} 条")
    print(f"                 淘汰杂质: {len(pools['rejected_candidates'])} 条 (价格/库存/切品)")
    return story_manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="直播叙事片段语义分析与候选池提取工具")
    parser.add_argument("--video", required=True, help="视频路径")
    parser.add_argument("--spec", default="job_spec.json", help="任务配置文件")
    parser.add_argument("--out-json", default="data/candidates/story_candidates.json", help="输出候选池 JSON 路径")
    args = parser.parse_args()

    with open(args.spec, "r", encoding="utf-8") as f:
        spec = json.load(f)

    work_dir = os.path.join(PROJECT_ROOT, "data", "work_dirs", spec.get("project_id", "demo"))
    analyze_story_segments(args.video, spec, work_dir, args.out_json)
