#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
sfx_generator.py - 黄金 3 秒影视级 Sub-Bass 重低音冲击波合成器
============================================================
基于阻尼正弦衰减物理公式，数学合成 45Hz~90Hz 低频重音 Boom。
免去寻找第三方版权音效烦恼，0 延迟、0 依赖纯本地生成。
"""

import os
import wave
import numpy as np

def generate_sub_bass_boom(
    output_path: str,
    duration: float = 1.2,
    sample_rate: int = 48000,
    f_start: float = 90.0,
    f_end: float = 45.0
) -> str:
    """生成立体声 24-bit/16-bit 阻尼 Sub-Bass 冲击音效"""
    if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
        return output_path
        
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    # 频率线性下潜
    freq = np.linspace(f_start, f_end, len(t))
    waveform = np.sin(2 * np.pi * freq * t)
    # 指数阻尼衰减
    envelope = np.exp(-4.5 * t)
    audio = (waveform * envelope * 32767 * 0.92).astype(np.int16)
    
    with wave.open(output_path, "w") as wf:
        wf.setnchannels(2)      # 双声道立体声
        wf.setsampwidth(2)      # 16-bit
        wf.setframerate(sample_rate)
        stereo = np.column_stack((audio, audio)).flatten()
        wf.writeframes(stereo.tobytes())
        
    print(f"[SFX] Sub-Bass Boom 冲击音已生成: {output_path}")
    return output_path

if __name__ == "__main__":
    out = "data/boom_test.wav"
    generate_sub_bass_boom(out)
