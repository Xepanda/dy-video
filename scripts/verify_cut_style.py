"""剪辑风格校验器 —— 对任意成片做 R7~R10 同口径测量与判定。

用法：
    python -u scripts/verify_cut_style.py --video <成片.mp4> [--cuts 4.72,6.50,...]
                                           [--segments <segments.json>]

输出：逐条规则的 PASS/FAIL + 实测数据，可直接用于回归比对。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import yaml  # noqa: E402

from src.tools.cut_style_rules import (  # noqa: E402
    check_audio_style, check_color_consistency, check_pacing_curve,
    check_transition_style, measure_color_stats, measure_pacing,
    resolve_audio_style, resolve_transition_style)


def _load_constraints(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _probe_transitions(video: str, cuts, half: float = 0.30):
    """用 α(t) 演化 + 混合残差凹陷判定每个切点是硬切还是叠化。"""
    import cv2
    import numpy as np

    def frames_seq(t0, t1, W=270, H=480):
        cap = cv2.VideoCapture(video)
        cap.set(cv2.CAP_PROP_POS_MSEC, t0 * 1000)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        out = []
        t = t0
        while t < t1:
            ok, f = cap.read()
            if not ok:
                break
            out.append((t, cv2.resize(f, (W, H)).astype(np.float32)))
            t += 1.0 / fps
            if len(out) > 200:
                break
        cap.release()
        return out, fps

    n_dissolve = 0
    n_hard = 0
    details = []
    for c in cuts:
        seq, fps = frames_seq(c - half, c + half)
        if len(seq) < 6:
            continue
        A = seq[0][1]
        B = seq[-1][1]
        e = B - A
        den = float((e * e).sum())
        if den < 1e-6:
            continue
        errA = float(np.abs(A - B).mean())
        als = []
        dips = 0
        for _, F in seq:
            al = float(((F - A) * e).sum() / den)
            als.append(al)
            recon = al * A + (1 - al) * B
            if float(np.abs(recon - F).mean()) < 0.35 * errA:
                dips += 1
        max_step = max(abs(als[i + 1] - als[i]) for i in range(len(als) - 1))
        is_dissolve = dips >= 2 and max_step < 0.85
        if is_dissolve:
            n_dissolve += 1
        else:
            n_hard += 1
        details.append({"cut": round(float(c), 3), "kind":
                        "dissolve" if is_dissolve else "hard",
                        "max_delta_alpha": round(float(max_step), 3),
                        "blend_frames": int(dips)})
    return {"cut_count": len(details), "dissolve_count": n_dissolve,
            "hard_cut_count": n_hard, "details": details}


def _probe_audio(video: str):
    """测量响度 / LRA / 真峰值 / 语音占比 / 低中频比 / Boom 痕迹。"""
    import numpy as np
    import subprocess

    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", video,
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    err = r.stderr or ""
    lufs = lra = peak = None
    for line in err.splitlines():
        t = line.strip()
        if t.startswith("I:"):
            try:
                lufs = float(t.split()[1])
            except Exception:
                pass
        elif t.startswith("LRA:"):
            try:
                lra = float(t.split()[1])
            except Exception:
                pass
        elif t.startswith("Peak:"):
            try:
                peak = float(t.split()[1])
            except Exception:
                pass

    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", video, "-ac", "1", "-ar", "16000",
         "-f", "s16le", "-"], capture_output=True).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    hop = 160
    n = len(x) // hop
    env = np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(env + 1e-9)

    # 低频/中频比：语音主导为负，BGM 会显著抬高
    import numpy.fft as fft
    N, hopf = 1024, 512
    frames = []
    for i in range(0, len(x) - N, hopf):
        frames.append(np.abs(fft.rfft(x[i:i + N] * np.hanning(N))))
    if frames:
        S = np.array(frames)
        freqs = fft.rfftfreq(N, 1 / 16000)
        def band(lo, hi):
            m = (freqs >= lo) & (freqs < hi)
            return 20 * np.log10(np.sqrt((S[:, m] ** 2).mean(axis=1)) + 1e-12)
        low = float(band(20, 200).mean())
        mid = float(band(200, 2000).mean())
        low_mid = low - mid
    else:
        low_mid = None

    # Boom 痕迹：45~90Hz 的**窄带**低频瞬态。
    # ⚠️ 不能用"前段电平比中位高 X dB"判定——主播正常大声说话同样满足该条件，
    #   参考成片开头就是主播在讲卖点，会被误判成 Boom（实测已发生）。
    #   正确判据：低频窄带（45~90Hz）能量显著高于其相邻频带（20~45 / 90~150Hz），
    #   即"窄带性"；人声的基频虽低但宽带分布，窄带性远低于合成低频冲击。
    boom = False
    boom_ratio = None
    if len(db) > 200 and len(x) > 8192:
        import numpy.fft as fft
        N, hopf = 2048, 1024
        seg0 = x[:N * 6]
        S0 = np.abs(fft.rfft(seg0 * np.hanning(len(seg0))))
        f0 = fft.rfftfreq(len(seg0), 1 / 16000)
        def _b(lo, hi):
            m = (f0 >= lo) & (f0 < hi)
            return float(np.sqrt((S0[m] ** 2).mean()) + 1e-12)
        target = _b(45, 90)
        neigh = (_b(20, 45) + _b(90, 150)) / 2.0
        boom_ratio = target / max(neigh, 1e-12)
        boom = bool(boom_ratio > 3.0)
    return {"integrated_lufs": lufs, "lra": lra, "true_peak": peak,
            "low_mid_ratio_db": low_mid, "boom_present": boom,
            "boom_narrowband_ratio": boom_ratio,
            "envelope_db": db}


def main() -> int:
    ap = argparse.ArgumentParser(description="剪辑风格校验器（R7~R10）")
    ap.add_argument("--video", required=True, help="待检成片")
    ap.add_argument("--cuts", default="", help="切点时间码，逗号分隔")
    ap.add_argument("--segments", default="", help="ASR segments JSON")
    ap.add_argument("--constraints",
                    default=os.path.join(os.path.dirname(os.path.dirname(
                        os.path.abspath(__file__))), "contracts", "constraints.yaml"))
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cons = _load_constraints(args.constraints)
    cuts = [float(x) for x in args.cuts.split(",") if x.strip()]

    # 成片时长 → 段边界
    import cv2
    cap = cv2.VideoCapture(args.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps if fps else 0.0
    cap.release()

    segs = []
    if args.segments and os.path.exists(args.segments):
        with open(args.segments, "r", encoding="utf-8") as f:
            segs = json.load(f)
    # 兼容两种结构：顶层 list，或 {"segments": [...]} / {"duration":..,"segments":[..]}
    if isinstance(segs, dict):
        segs = segs.get("segments") or []
    if segs and isinstance(segs[0], dict):
        segs = [s for s in segs
                if isinstance(s, dict) and "start" in s and "end" in s]
    else:
        segs = []

    bounds = [0.0] + cuts + [total]
    print(f"[VERIFY] {os.path.basename(args.video)}  {total:.2f}s  切点 {len(cuts)} 个")
    print(f"[VERIFY] 转场风格规则: {resolve_transition_style(cons)}")
    print(f"[VERIFY] 音频风格规则: {resolve_audio_style(cons)}")
    print()

    checks = []

    # R7
    tr = _probe_transitions(args.video, cuts)
    print(f"[R7] 切点 {tr['cut_count']} 个：硬切 {tr['hard_cut_count']}，"
          f"叠化 {tr['dissolve_count']}")
    for d in tr["details"][:8]:
        print(f"     t={d['cut']:7.3f} {d['kind']:9s} Δα={d['max_delta_alpha']:.3f} "
              f"混合帧={d['blend_frames']}")
    r7 = check_transition_style(cons, tr)
    checks.append(r7)

    # R8
    au = _probe_audio(args.video)
    seg_dicts = segs
    speech_ratio = None
    if seg_dicts:
        speech = sum(float(s["end"]) - float(s["start"]) for s in seg_dicts)
        speech_ratio = (speech / total) if total > 0 else None
    r8 = check_audio_style(cons, {
        "integrated_lufs": au.get("integrated_lufs"),
        "lra": au.get("lra"),
        "true_peak": au.get("true_peak"),
        "speech_ratio": speech_ratio,
        "boom_present": au.get("boom_present"),
        "bgm_present": False,
        "low_mid_ratio_db": au.get("low_mid_ratio_db"),
    })
    print(f"\n[R8] I={au.get('integrated_lufs')} LUFS  LRA={au.get('lra')}  "
          f"TP={au.get('true_peak')}  低中频比={au.get('low_mid_ratio_db')} dB  "
          f"Boom窄带比={au.get('boom_narrowband_ratio')} "
          f"(>3 判为有Boom) → 痕迹={au.get('boom_present')}")
    checks.append(r8)

    # R9
    col = measure_color_stats(args.video, list(zip(bounds[:-1], bounds[1:])),
                              samples_per_clip=int(
                                  ((cons.get("slicing_rules") or {})
                                   .get("color_consistency") or {})
                                  .get("samples_per_clip", 3) or 3))
    r9 = check_color_consistency(cons, col)
    print(f"\n[R9] 段间调色变异: " +
          "，".join(f"{k} {v:.1f}%" for k, v in (col.get("cv") or {}).items()))
    checks.append(r9)

    # R10
    pac = measure_pacing(cuts, segs, total)
    r10 = check_pacing_curve(cons, pac)
    print(f"\n[R10] {r10.get('detail')}")
    if pac:
        print(f"      句长 早{pac['sentence_len_early']:.2f}s / "
              f"中{pac['sentence_len_mid']:.2f}s / 后{pac['sentence_len_late']:.2f}s")
    checks.append(r10)

    print("\n" + "=" * 62)
    ok_all = True
    for c in checks:
        flag = "PASS" if c["passed"] else "FAIL"
        if not c["passed"]:
            ok_all = False
        print(f"  [{flag}/{c['level']}] {c['id']}: {c.get('detail')}")
    print("=" * 62)
    print(f"总体: {'ALL PASS' if ok_all else 'HAS FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
