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
    def __init__(self, model_size: str = "small", device: str = "auto",
                 use_initial_prompt: bool = False):
        """model_size: faster-whisper 模型规格（tiny/base/small/medium/large）。
        device: auto | cuda | cpu。
        use_initial_prompt: 是否向前端模型注入语境 prompt。
            **默认 False**（2026-10-24）：实测注入会挤掉开头约 30s 真实语音
            （prompt 回声被当成音频内容识别），详见 transcribe_video 内注释。
            领域词纠错由本地校对器 AsrContextVerifier 负责，无需依赖 prompt 偏置。
        """
        self.model_size = model_size
        self.device = device
        self.use_initial_prompt = use_initial_prompt
        self._model = None

    def _get_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            dev = self.device
            if dev == "auto":
                # torch 仅用于探测 CUDA 是否可用——faster-whisper 运行时走
                # CTranslate2，本身**不依赖 torch**（torch 只是它的可选
                # conversion extra）。因此 torch 缺失不能视为致命错误，
                # 直接降级 CPU int8 即可（否则干净环境会直接 ImportError）。
                dev = "cuda" if self._cuda_available() else "cpu"

            compute_type = "float16" if dev == "cuda" else "int8"
            print(f"[ASR] 加载 Faster-Whisper ({self.model_size}) on {dev} ({compute_type})...")
            try:
                self._model = WhisperModel(self.model_size, device=dev, compute_type=compute_type)
            except Exception as e:
                print(f"[ASR] CUDA 加载异常，平滑降级至 CPU int8: {e}")
                self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
        return self._model

    @staticmethod
    def _cuda_available() -> bool:
        """探测 CUDA 可用性。

        torch 未安装时返回 False（不是错误）——faster-whisper 用 CTranslate2
        推理，CPU int8 完全可用，不应因缺少 torch 而阻断流水线。
        """
        try:
            import torch
        except Exception:
            return False
        try:
            return bool(torch.cuda.is_available())
        except Exception:
            return False

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

        # ---------------------------------------------------------------
        # initial_prompt 回声污染防护（2026-10-24 实测修复）
        # ---------------------------------------------------------------
        # 背景：给 faster-whisper 注入 initial_prompt 后，它会把 prompt 文本
        # 当成音频内容识别出来，挤占**开头约 30 秒**的真实语音。
        # 实测（6 号素材 318s，small 模型）：
        #   有 prompt：段数 151 / 总字 1478 / 前 90s 仅 348 字 / 首段 [0-30s] = "连帽衫·绵羊毛"
        #   无 prompt：段数 162 / 总字 1625 / 前 90s 达 495 字 / 首段 [0-1.8s] = 真实内容
        #   → 有 prompt 反而少识别 147 字（-30%），且首段是纯 prompt 回声。
        # 8/11 素材的首段都能观察到回声（"主播带货视频"/"主播口语高频词"等）。
        #
        # 关键：内容是在**转录阶段**就被挤掉的，事后过滤回声段无法恢复
        # 那 30 秒的语音——所以必须从源头不注入。
        #
        # 领域词纠错另有其人：本地校对器 AsrContextVerifier 的确定性本体引擎
        # （STATIC_DOMAIN_HOMOPHONES + 滑动窗口消歧）已经负责同音错字修正，
        # 不依赖 Whisper 的 prompt 偏置。
        if initial_prompt and not self.use_initial_prompt:
            print("[ASR] 已跳过 initial_prompt 注入：实测会挤掉开头约 30s 真实语音"
                  "（prompt 回声污染），领域词纠错由本地校对器负责")
            initial_prompt = None
        elif initial_prompt:
            print(f"[ASR] 注入语境前置引导 Prompt: {initial_prompt[:60]}...")

        segments, info = model.transcribe(
            video_path,
            beam_size=5,
            language="zh",
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=250),
            # condition_on_previous_text=False：直播嘈杂音频场景下，默认的
            # 上下文条件传播会把提示词滚成复读循环（2026-10-21 实测：
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
