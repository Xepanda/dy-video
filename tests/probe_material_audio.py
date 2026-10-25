#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""素材音频探测：tiny 模型快速转录所有候选文件，定位各素材的真正讲解段"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from src.core.asr_engine import AsrEngine

RAW = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "raw_samples")
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "asr_probe.json")

def main():
    asr = AsrEngine(model_size="base")
    report = {}
    for mat in sorted(os.listdir(RAW)):
        mat_dir = os.path.join(RAW, mat)
        if not os.path.isdir(mat_dir):
            continue
        for f in sorted(os.listdir(mat_dir)):
            if not f.endswith(".mp4"):
                continue
            path = os.path.join(mat_dir, f)
            try:
                segs = asr.transcribe_video(path)
            except Exception as e:
                report[f] = {"error": str(e)}
                continue
            speech = sum(s.get("duration", s["end"] - s["start"]) for s in segs)
            report[f] = {
                "segments": len(segs),
                "speech_sec": round(speech, 1),
                "samples": [s["text"] for s in segs[:6]],
            }
            print(f"[PROBE] {mat[:14]} | {f[-12:]} | segs={len(segs)} speech={speech:.0f}s "
                  f"| {' / '.join(s['text'][:12] for s in segs[:3])}", flush=True)
    with open(OUT, "w", encoding="utf-8") as fp:
        json.dump(report, fp, ensure_ascii=False, indent=2)
    print(f"[PROBE] DONE -> {OUT}", flush=True)

if __name__ == "__main__":
    main()
