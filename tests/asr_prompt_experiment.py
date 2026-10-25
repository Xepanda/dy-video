#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ASR prompt 复读实验：small 模型 + 不同参数组合，定位 11 段复读幻觉的根因"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from faster_whisper import WhisperModel

V = "data/raw_samples/6 松弛帽衫·绵羊毛假两件连帽拉链卫衣11214/1011 松弛帽衫·绵羊毛假两件连帽拉链卫衣11214  0518.mp4"
PROMPT = ("这是一段抖音女装直播带货视频。品牌：MIXCITY，主推商品：松弛帽衫·绵羊毛假两件 连帽拉链卫衣。"
          "主播口语高频词：实物更漂亮、超显瘦、人瘦没了、码数、卡码、YYDS、螺纹T、编织裤、老钱风、休闲松弛、绵羊毛、假两件。")

model = WhisperModel("small", device="cpu", compute_type="int8")

for label, kw in [
    ("A: prompt + 默认(复现bug)", dict(initial_prompt=PROMPT)),
    ("B: prompt + condition_on_previous_text=False", dict(initial_prompt=PROMPT, condition_on_previous_text=False)),
    ("C: 无prompt + 默认", dict()),
]:
    segs, info = model.transcribe(V, beam_size=5, language="zh", vad_filter=True,
                                  vad_parameters=dict(min_silence_duration_ms=250), **kw)
    results = [s.text.strip() for s in segs if s.text.strip()]
    uniq = len(set(results))
    speech = 0.0
    segs2, _ = model.transcribe(V, beam_size=5, language="zh", vad_filter=True,
                                vad_parameters=dict(min_silence_duration_ms=250), **kw)
    for s in segs2:
        speech += s.end - s.start
    print(f"[{label}] segs={len(results)} 去重后={uniq} 语音总时长≈{speech:.0f}s")
    print(f"    前3句: {results[:3]}")
    print(f"    中段抽样: {results[len(results)//2:len(results)//2+2]}")
