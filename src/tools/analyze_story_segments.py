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
# 需求2/4：ASR 识别结果必须经执行者审核后才可进入后续流程。
# 导入即把审核门禁挂进 AsrContextVerifier.verify_and_correct（常规路径），
# 此处再显式调用一次作为兜底，防止未来新增调用点绕过审核。
from src.core.asr_review_gate import enforce_review_gate, AsrReviewError

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
    """判定是否为价格/库存或离场杂质（纯内容信号，无硬编码时间窗）"""
    from src.tools.build_plan import is_blacklisted as _bl
    return _bl(text)


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


def _probe_duration(video_path: str) -> float:
    """探测媒体时长（秒）。失败返回 0.0（由审核门禁跳过密度校验）。"""
    try:
        import cv2
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        cap.release()
        if fps > 0 and frames > 0:
            return float(frames / fps)
    except Exception:
        pass
    try:
        import subprocess
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", video_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        return float((r.stdout or "0").strip() or 0.0)
    except Exception:
        return 0.0


def analyze_story_segments(
    video_path,
    spec_data: Dict[str, Any],
    cache_dir: str,
    output_json_path: str,
    cache_key: str = None,
) -> Dict[str, Any]:
    """提取并分类直播切片，生成 Agent 候选池。

    video_path 可以是单个路径，也可以是同一商品的多段直播素材路径列表
    （2026-10-21 需求：同目录多视频为同一商品的直播素材，跨素材挑选最优片段）。
    多源模式下每个文件独立 ASR + 独立语境校对，segments 携带 source_file，
    供 build_plan 跨源选段、video_assembler 按源切片。

    cache_key 提供时启用阶段缓存（B13）：story 清单命中即跳过（ASR 缓存同步校验 key）。
    """
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(output_json_path)), exist_ok=True)

    # 阶段缓存命中（B13 断点续跑）
    if cache_key:
        from src.tools.stage_cache import load_stage, save_stage
        cached = load_stage(cache_dir, "story", cache_key)
        if cached is not None:
            print(f"[STORY-ANALYZER] 命中阶段缓存（{cached.get('total_verified_segments')} 句）")
            with open(output_json_path, "w", encoding="utf-8") as f:
                json.dump(cached, f, ensure_ascii=False, indent=2)
            return cached

    sources = [video_path] if isinstance(video_path, str) else list(video_path)
    sources = [os.path.abspath(p) for p in sources]

    # 1. Faster-Whisper ASR 转录 + 2. 语境智能体质检（逐源独立，模型只加载一次）
    asr = AsrEngine()
    verifier = AsrContextVerifier(spec_data)
    prompt = verifier.build_initial_prompt()

    all_verified: List[Dict[str, Any]] = []
    per_source: List[Dict[str, Any]] = []
    for src_path in sources:
        tag = os.path.basename(src_path)
        print(f"\n[STORY-ANALYZER] === 源素材 ASR: {tag} ===")
        src_key = f"{src_path}"  # 逐源缓存隔离，避免同名串味
        raw_cache = os.path.join(cache_dir, "asr",
                                 os.path.splitext(tag)[0] + ".whisper.json")
        verified_cache = os.path.join(cache_dir, "asr",
                                      os.path.splitext(tag)[0] + ".verified.json")
        raw_segs = asr.transcribe_video(src_path, cache_json_path=raw_cache,
                                        initial_prompt=prompt, cache_key=src_key)
        segs, asr_report = verifier.verify_and_correct(raw_segs)
        # —— 兜底审核（需求2/4 第二重保险）——
        # 用媒体真实时长做识别密度校验（verify_and_correct 内部因未知时长会跳过 A5）。
        try:
            segs = enforce_review_gate(
                segs, media_duration=_probe_duration(src_path),
                report_path=os.path.join(
                    cache_dir, "asr",
                    os.path.splitext(tag)[0] + ".review.json"))
        except AsrReviewError as e:
            # 审核不通过：阻断该源的选段（不静默降级），失败信息随 story 清单落盘
            print(f"[STORY-ANALYZER] 源素材 {tag} 未通过 ASR 执行者审核，已阻断该源选段")
            per_source.append({
                "source_file": src_path, "source_tag": tag,
                "raw_segments": len(raw_segs), "verified_segments": 0,
                "corrected": 0, "review_passed": False, "review_error": str(e),
            })
            continue
        for s in segs:
            s["source_file"] = src_path
            s["source_tag"] = tag
        with open(verified_cache, "w", encoding="utf-8") as f:
            json.dump(segs, f, ensure_ascii=False, indent=2)
        all_verified.extend(segs)
        per_source.append({
            "source_file": src_path,
            "source_tag": tag,
            "raw_segments": len(raw_segs),
            "verified_segments": len(segs),
            "corrected": asr_report.get("corrected_segments_count", 0),
            "review_passed": True,
            "review_warnings": (asr_report.get("asr_review", {}) or {}).get("warn_count", 0),
        })
        print(f"[STORY-ANALYZER] {tag}: 校对后 {len(segs)} 句"
              f"（纠正 {asr_report.get('corrected_segments_count', 0)} 处）")

    verified_segs = all_verified

    # 多源模式下保留一份合并的 verified 缓存（兼容既有下游读取路径）
    verified_cache = os.path.join(cache_dir, "whisper_segments_verified.json")
    with open(verified_cache, "w", encoding="utf-8") as f:
        json.dump(verified_segs, f, ensure_ascii=False, indent=2)

    # 3. 语义分类打标
    pools = classify_segments(verified_segs)

    # 3.1 连续叙事候选块（由 build_plan 泛化合并逻辑派生，替代旧版硬编码大块）
    from src.tools.build_plan import build_blocks as _build_blocks
    from src.tools.build_plan import _load_constraints as _load_c
    blocks, _rejected = _build_blocks(verified_segs, _load_c())
    blocks.sort(key=lambda b: b["scores"].get("styling", 0) + b["scores"].get("fabric", 0)
                + b["scores"].get("fit", 0) + b["scores"].get("hook", 0), reverse=True)
    continuous_blocks_summary = [
        {
            "block_id": f"b{i+1:02d}",
            "recommended_st": b["start"],
            "recommended_end": b["end"],
            "duration": b["dur"],
            "categories": b["categories"],
            "narrative": b["text"][:40] + ("…" if len(b["text"]) > 40 else ""),
        }
        for i, b in enumerate(blocks[:8])
    ]

    story_manifest = {
        "project_id": spec_data.get("project_id", "demo"),
        "raw_video_path": sources[0] if len(sources) == 1 else "",
        "source_videos": sources,
        "multi_source": len(sources) > 1,
        "per_source": per_source,
        "total_verified_segments": len(verified_segs),
        "pools": pools,
        "continuous_blocks_summary": continuous_blocks_summary,
    }

    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(story_manifest, f, ensure_ascii=False, indent=2)

    if cache_key:
        from src.tools.stage_cache import save_stage
        save_stage(cache_dir, "story", cache_key, story_manifest)

    print(f"[STORY-ANALYZER] 叙事候选池生成完毕: {output_json_path}")
    print(f"                 源素材 {len(sources)} 个 | 合计校对后 {len(verified_segs)} 句")
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
