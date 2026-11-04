#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
detect_speech_pauses.py - 语音停顿检测（切片规则 R1 的数据源）
============================================================
为什么需要：
  参考成片（SSY410163_蝴蝶结高腰A字半身裙_切片.mp4）实测：9 个切点中 **7 个（78%）**
  落在 −60dB 以下的近数字静音区，而全片随机位置低于该阈值仅 0.52% → 富集 29 倍。
  即：**人工切片切在"气口"上，而不是切在半个句子中间**。
  本工具把"找气口"变成可复用的确定性能力：给定视频 → 输出停顿区间列表，
  供 build_plan 把切点吸附到语音停顿。

判定方式（与人工听感一致，且比 ASR 边界更准）：
  1. ffmpeg 抽单声道 16kHz PCM；
  2. 计算 10ms 跳步的 RMS 包络（dBFS）；
  3. 低于 `silence_db` 的连续区间即候选停顿，长度 ≥ `min_silence_sec` 才保留；
  4. 输出停顿的 [start, end]，其中点即"换气最充分处"，是切点的最佳锚。

ASR 句末标点吸附（build_plan._snap_end_to_sentence）已有，但标点只能给出**语义**边界；
本工具给出**声学**边界，二者结合才是人工剪辑的真实依据。

CLI:
  python src/tools/detect_speech_pauses.py --video a.mp4 --out-json pauses.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def extract_pcm(video_path: str, sample_rate: int = 16000) -> bytes:
    """抽单声道 PCM s16le。失败抛 RuntimeError。"""
    cmd = ["ffmpeg", "-v", "error", "-i", video_path,
           "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=600)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise RuntimeError(f"ffmpeg 执行失败: {e}")
    if out.returncode != 0:
        raise RuntimeError(f"ffmpeg 返回非零: {out.stderr.decode('utf-8', 'ignore')[:200]}")
    return out.stdout


def rms_envelope(pcm: bytes, sample_rate: int = 16000,
                 hop_ms: int = 10) -> List[Tuple[float, float]]:
    """RMS 包络 → [(时间秒, dBFS), ...]。"""
    import numpy as np
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if x.size == 0:
        return []
    hop = max(1, int(sample_rate * hop_ms / 1000))
    n = x.size // hop
    if n <= 0:
        return []
    trimmed = x[: n * hop].reshape(n, hop)
    rms = np.sqrt(np.mean(trimmed ** 2, axis=1) + 1e-12)
    db = 20.0 * np.log10(rms + 1e-9)
    step = hop / float(sample_rate)
    return [(i * step, float(db[i])) for i in range(n)]


def detect_pauses(video_path: str, silence_db: float = -60.0,
                  min_silence_sec: float = 0.05,
                  sample_rate: int = 16000,
                  hop_ms: int = 10,
                  dip_threshold_db: float = 8.0) -> Dict[str, Any]:
    """检测语音气口。返回 {dips, pauses, envelope_stats, params}。

    2026-11-01 实测修正：直播素材有环境底噪（实测 −53.4dB），绝对静音阈值无效；
    改用**自适应阈值 + 相对凹陷**判据（相对局部语流电平的下陷深度）。
    """
    pcm = extract_pcm(video_path, sample_rate)
    env = rms_envelope(pcm, sample_rate, hop_ms)
    if not env:
        return {"dips": [], "pauses": [], "envelope_stats": {}, "params": {}}

    from src.tools.slicing_rules import (find_speech_pauses, find_local_silence_dips,
                                         find_relative_dips, adaptive_silence_db)
    windows = [(t, db) for t, db in env]
    hop_sec = hop_ms / 1000.0
    thr, stats = adaptive_silence_db(windows, absolute_db=silence_db)
    pauses = find_speech_pauses(thr, min_silence_sec, windows)
    abs_dips = find_local_silence_dips(thr, windows, hop_sec)
    rel_dips = find_relative_dips(windows, min_dip_db=dip_threshold_db,
                                   hop_sec=hop_sec)

    dbs = [db for _, db in env]
    peak = max(dbs)
    quiet_ratio = sum(1 for d in dbs if d <= thr) / len(dbs)

    return {
        # 相对凹陷（直播素材可用）：[(时刻, 凹陷深度dB)]
        "dips": [[round(t, 3), round(db, 2)] for t, db in rel_dips],
        # 绝对静音凹陷（数字静音素材可用）：[(时刻, dB)]
        "abs_dips": [[round(t, 3), round(db, 2)] for t, db in abs_dips],
        "pauses": [[round(a, 3), round(b, 3)] for a, b in pauses],
        "envelope_stats": {
            "peak_db": round(peak, 2),
            "median_db": round(sorted(dbs)[len(dbs) // 2], 2),
            "quiet_ratio": round(quiet_ratio, 4),
            "n_frames": len(env),
            **stats,
        },
        "params": {"silence_db": silence_db, "min_silence_sec": min_silence_sec,
                   "sample_rate": sample_rate, "hop_ms": hop_ms,
                   "dip_threshold_db": dip_threshold_db,
                   "effective_silence_db": round(thr, 2)},
    }


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description="语音停顿检测（切片规则 R1 数据源）")
    ap.add_argument("--video", required=True, help="视频路径")
    ap.add_argument("--out-json", default=None, help="输出 JSON 路径")
    ap.add_argument("--silence-db", type=float, default=-60.0, help="绝对静音阈值 dBFS")
    ap.add_argument("--min-silence-sec", type=float, default=0.03, help="最短停顿秒")
    ap.add_argument("--dip-threshold-db", type=float, default=8.0,
                    help="相对凹陷最小深度 dB（直播素材的气口判据）")
    ap.add_argument("--constraints", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "contracts", "constraints.yaml"))
    args = ap.parse_args(argv)

    if not os.path.exists(args.video):
        print(f"[FATAL] 视频不存在: {args.video}")
        return 2

    silence_db = args.silence_db
    min_sec = args.min_silence_sec
    dip_thr = args.dip_threshold_db
    # 阈值默认取自 constraints（唯一事实源）；命令行可覆盖
    try:
        import yaml
        with open(args.constraints, "r", encoding="utf-8") as f:
            constraints = yaml.safe_load(f) or {}
        cp = (((constraints.get("slicing_rules") or {}).get("cut_on_speech_pause")) or {})
        if args.silence_db == -60.0 and "absolute_db" in cp:
            silence_db = float(cp["absolute_db"])
        if args.min_silence_sec == 0.05 and "min_silence_sec" in cp:
            min_sec = float(cp["min_silence_sec"])
        if args.dip_threshold_db == 8.0 and "dip_threshold_db" in cp:
            dip_thr = float(cp["dip_threshold_db"])
    except Exception as e:
        print(f"[WARN] 读取 constraints 失败，用默认阈值: {e}")

    result = detect_pauses(args.video, silence_db, min_sec, dip_threshold_db=dip_thr)
    result["video"] = os.path.basename(args.video)

    if args.out_json:
        os.makedirs(os.path.dirname(os.path.abspath(args.out_json)), exist_ok=True)
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[PAUSES] 已落盘: {args.out_json}")
    st = result.get('envelope_stats') or {}
    print(f"[PAUSES] {result['video']} | 相对气口 {len(result.get('dips') or [])} 处 "
          f"| 绝对静音 {len(result.get('abs_dips') or [])} 处 "
          f"| 底噪 {st.get('noise_floor_db', '?')} dB "
          f"| 自适应阈值 {st.get('adaptive_silence_db', '?')} dB "
          f"| 相对阈值 {dip_thr} dB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
