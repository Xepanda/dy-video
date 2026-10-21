#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
asr_engine.py - Faster-Whisper 毫秒级语音转录与时间戳提取引擎
============================================================
提供单文件/整视频的高精转录，支持缓存机制。
输出标准结构: [{"start": 0.0, "end": 2.5, "text": "..."}]
"""

import os
import sys
import json
from typing import List, Dict, Any

class AsrEngine:
    def __init__(self, model_size: str = "small", device: str = "auto"):
        self.model_size = model_size
        self.device = device
        self._model = None

    def _get_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            import torch
            
            dev = self.device
            if dev == "auto":
                dev = "cuda" if torch.cuda.is_available() else "cpu"
                
            compute_type = "float16" if dev == "cuda" else "int8"
            print(f"[ASR] 加载 Faster-Whisper ({self.model_size}) on {dev} ({compute_type})...")
            try:
                self._model = WhisperModel(self.model_size, device=dev, compute_type=compute_type)
            except Exception as e:
                print(f"[ASR] CUDA 加载异常，平滑降级至 CPU int8: {e}")
                self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
        return self._model

    def transcribe_video(
        self,
        video_path: str,
        cache_json_path: str = None,
        force_recompute: bool = False,
        initial_prompt: str = None,
        cache_key: str = None
    ) -> List[Dict[str, Any]]:
        """转录视频，若缓存存在直接秒读。

        cache_key（B13）：与缓存同目录的 <cache>.meta.json 中记录的 key 比对，
        不一致（视频/spec/constraints 已变更）则重算，防止串味。
        """
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"视频不存在: {video_path}")

        if cache_json_path and os.path.exists(cache_json_path) and not force_recompute:
            key_ok = True
            if cache_key:
                meta_path = os.path.join(os.path.dirname(os.path.abspath(cache_json_path)),
                                         os.path.basename(cache_json_path) + ".meta.json")
                if os.path.exists(meta_path):
                    try:
                        with open(meta_path, "r", encoding="utf-8") as mf:
                            key_ok = json.load(mf).get("cache_key") == cache_key
                    except (json.JSONDecodeError, OSError):
                        key_ok = False
                else:
                    key_ok = False  # 旧缓存无 key 记录，保守重算
            if key_ok:
                print(f"[ASR] 命中时间戳缓存: {cache_json_path} (秒级加载)")
                with open(cache_json_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            print("[ASR] 缓存 key 不匹配（输入已变更），重新转录")

        model = self._get_model()
        print(f"[ASR] 正在对素材进行全量语音打点: {os.path.basename(video_path)} ...")
        if initial_prompt:
            print(f"[ASR] 注入语境前置引导 Prompt: {initial_prompt[:60]}...")
        segments, info = model.transcribe(
            video_path,
            beam_size=5,
            language="zh",
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=250),
            # condition_on_previous_text=False：直播嘈杂音频 + initial_prompt 场景下，
            # 默认的上下文条件传播会把提示词滚成复读循环（2026-10-21 实测：
            # 6 号素材 318s 仅转出 15 句且内容全是提示词；关闭后 151 句/147 去重正常）。
            condition_on_previous_text=False,
            initial_prompt=initial_prompt
        )

        results = []
        for s in segments:
            text = s.text.strip()
            if not text:
                continue
            results.append({
                "start": round(s.start, 3),
                "end": round(s.end, 3),
                "duration": round(s.end - s.start, 3),
                "text": text
            })

        print(f"[ASR] 转录完成，有效句块总数: {len(results)}")
        if cache_json_path:
            os.makedirs(os.path.dirname(os.path.abspath(cache_json_path)), exist_ok=True)
            with open(cache_json_path, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            print(f"[ASR] 时间戳已落盘缓存: {cache_json_path}")
            if cache_key:
                meta_path = cache_json_path + ".meta.json"
                with open(meta_path, "w", encoding="utf-8") as mf:
                    json.dump({"cache_key": cache_key, "video": os.path.basename(video_path)},
                              mf, ensure_ascii=False, indent=2)

        return results

if __name__ == "__main__":
    asr = AsrEngine()
    test_v = "data/raw_samples/10 绵羊 半岛棉镂空钩花气质显瘦针织衫7270/0826  绵羊 半岛棉镂空钩花气质显瘦针织衫7270  0425.mp4"
    cache = "data/0826_knitwear_segments.json"
    res = asr.transcribe_video(test_v, cache_json_path=cache)
    print(f"Top 5 segments: {res[:5]}")
