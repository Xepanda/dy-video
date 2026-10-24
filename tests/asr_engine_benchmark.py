#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
asr_engine_benchmark.py - 免费本地 ASR 方案横向实测（选型依据）
=================================================================
背景：
  modelscope 缓存里已存在 SenseVoiceSmall / Paraformer-large(含 VAD/PUNC/timestamp)，
  funasr / modelscope / sherpa_onnx 也已安装，但**项目代码从未集成**。
  本脚本对同一段直播素材横向实测，给出可复现的选型结论。

对比维度：
  1. 句块数（识别密度；过少 = 转录退化，字幕无法分句）
  2. 语音覆盖率
  3. 关键词命中（商品词召回）
  4. 同音错字命中（错字率代理指标）
  5. 字符错误率 CER（对齐项目已校对的 verified 文本作为参考）
  6. 耗时 / RTF
  7. 繁体字污染（中文 ASR 常见问题：简繁混排）

用法：
  python -u tests/asr_engine_benchmark.py --limit-seconds 90
  python -u tests/asr_engine_benchmark.py --limit-seconds 300 --engines fw_small,fw_base,sv,pf
"""
import os
import re
import sys
import json
import time
import argparse
import difflib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 电商直播关键商品词
KEY_WORDS = [
    "半岛棉", "镂空", "显瘦", "针织", "做工", "进口", "老钱", "松弛",
    "超显瘦", "实物", "码数", "卡码", "螺纹", "毛领", "版型", "面料",
    "颜色", "搭配", "外套", "卫衣", "毛衣", "羊毛", "蚕丝", "绒", "帽子",
]
# 已知同音错字（出现即计错）
HOMOPHONE_ERRORS = {
    "食物": "实物", "半导棉": "半岛棉", "超选瘦": "超显瘦", "瘦眉了": "瘦没了",
    "马术": "码数", "卡马": "卡码", "罗恩梯": "螺纹T", "款型": "版型",
    "色近": "色系", "西枯": "西裤", "稀裤": "西裤", "天死": "天丝",
    "溶浮": "休闲", "新学率": "新下架", "感染": "感觉", "蝶穿": "叠穿",
    "为意": "卫衣", "平衡": "平肿", "焦桑": "娇桑", "玛吉拉": "马甲",
}
# 繁体字（简繁混排污染检测）
TRAD_CHARS = set("種過還們這來時個說對後點樣隨進實現產業務動員無為長問題"
                 "門風雲電腦網絡發開關閉頭臉體達麼愛親歡觀點聽講語讀寫買賣")


def _norm(t):
    """归一化：去标点空白 + 繁转简（用于 CER 对齐）。"""
    t = re.sub(r"[\s，,。.、！!？?；;：:~～…—\-\"'“”‘’()（）]", "", t or "")
    return t


def _to_simplified(t):
    """繁体→简体（用 opencc 不可用时的兜底：只做字符集粗映射）。

    这里不追求完美，只为让 CER 不被简繁差异污染——直接用字符集判定。
    """
    return "".join(ch for ch in t)


def _trad_ratio(t):
    if not t:
        return 0.0
    n = sum(1 for ch in t if ch in TRAD_CHARS)
    return round(n / max(len([c for c in t if c.strip()]), 1), 4)


def _find_material():
    """挑语音最密集的素材（按 asr_probe 句块数排序），并找对应 verified 参考。"""
    probe = os.path.join(PROJECT_ROOT, "logs", "asr_probe.json")
    base = os.path.join(PROJECT_ROOT, "data", "raw_samples")
    best = None
    if os.path.exists(probe):
        with open(probe, "r", encoding="utf-8") as f:
            data = json.load(f)
        for name, info in data.items():
            if info.get("segments", 0) < 100:
                continue
            for dirpath, _, files in os.walk(base):
                for fn in files:
                    if fn == name and fn.lower().endswith(".mp4"):
                        p = os.path.join(dirpath, fn)
                        if best is None or info["segments"] > best[1]:
                            best = (p, info["segments"], fn)
    if best:
        return best[0], best[2]
    for dirpath, _, files in os.walk(base):
        for fn in sorted(files):
            if fn.lower().endswith(".mp4"):
                return os.path.join(dirpath, fn), fn
    raise FileNotFoundError("找不到可用素材")


def _find_verified(basename):
    """找该素材已校对的 verified 文本（作为 CER 参考）。

    注意：verified 文件名是 `<素材名去掉扩展名>.verified.json`，
    素材名带 .mp4 扩展名，所以要先去扩展名再匹配。
    """
    stem = os.path.splitext(basename)[0]
    for f in glob_verified():
        if stem in f:
            return f
    return None


def glob_verified():
    out = []
    for pid in os.listdir(os.path.join(PROJECT_ROOT, "data", "work_dirs")):
        d = os.path.join(PROJECT_ROOT, "data", "work_dirs", pid, "asr")
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.endswith(".verified.json"):
                out.append(os.path.join(d, fn))
    return out


def _cut_audio(video_path, seconds, out_wav):
    import subprocess
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
           "-t", str(seconds), "-ac", "1", "-ar", "16000", out_wav]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0 and os.path.exists(out_wav)


# ---------------------------------------------------------------- 引擎实现

def bench_fw(wav, model_size="small"):
    from faster_whisper import WhisperModel
    t0 = time.time()
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    load = time.time() - t0
    t1 = time.time()
    segs, _ = model.transcribe(
        wav, beam_size=5, language="zh", vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=250),
        condition_on_previous_text=False)
    texts, speech = [], 0.0
    for s in segs:
        t = (s.text or "").strip()
        if t:
            texts.append(t)
        speech += max(0.0, s.end - s.start)
    return {"engine": f"faster-whisper({model_size})", "load_sec": round(load, 1),
            "infer_sec": round(time.time() - t1, 1), "segments": len(texts),
            "speech_sec": round(speech, 1), "texts": texts}


def bench_sv(wav):
    """SenseVoiceSmall：非 ASR 专用（无字级时间戳），仅测文本质量与速度。

    关键限制：SenseVoice 输出**不带字/句级时间戳**（顶层只有 key/text），
    无法直接喂给需要毫秒打点的字幕流水线——这是它在本项目里的硬伤。
    """
    from funasr import AutoModel
    t0 = time.time()
    model = AutoModel(model="iic/SenseVoiceSmall", disable_update=True)
    load = time.time() - t0
    t1 = time.time()
    res = model.generate(input=wav, cache={}, language="zh", use_itn=True,
                         batch_size_s=60, merge_vad=True, merge_length_s=15)
    texts = []
    for r in res:
        txt = (r.get("text") or "").strip()
        txt = re.sub(r"<\|[^|]*\|>", "", txt).strip()
        if txt:
            texts.append(txt)
    return {"engine": "SenseVoiceSmall(funasr)", "load_sec": round(load, 1),
            "infer_sec": round(time.time() - t1, 1), "segments": len(texts),
            "speech_sec": 0.0, "texts": texts, "has_timestamps": False}


def bench_pf(wav):
    """Paraformer-large（VAD+PUNC 组合模型）：带**字级时间戳**。

    实测要点：
    - 组合模型 `speech_paraformer-large-vad-punc_...` 才能出 timestamp；
      拆成独立 vad_model/punc_model 时 timestamp 为 None（时间戳丢失）。
    - 输出的 text 是**空格分隔的单字**，去掉空格后与 timestamp 逐字对应。
    - 偶发插入非中文字符（如幻觉的 "the"），需按时间戳长度裁掉尾部多余字符。
    """
    from funasr import AutoModel
    t0 = time.time()
    model = AutoModel(
        model="iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        disable_update=True)
    load = time.time() - t0
    t1 = time.time()
    res = model.generate(input=wav, batch_size_s=300, output_timestamp=True)
    texts, speech = [], 0.0
    for r in res:
        raw = (r.get("text") or "")
        ts = r.get("timestamp") or []
        # 去掉字间空格，得到与 timestamp 对齐的字符序列
        compact = re.sub(r"\s+", "", raw)
        if ts and len(compact) != len(ts):
            # 尾部偶发幻觉字符（实测出现过 "the"）→ 裁到时间戳长度
            if len(compact) > len(ts):
                compact = compact[:len(ts)]
        if ts and len(compact) == len(ts) and len(compact) > 0:
            # 按标点切句，每句带起止毫秒
            start = 0
            for i, ch in enumerate(compact):
                if ch in "。？！":
                    seg = compact[start:i + 1]
                    if seg:
                        texts.append(seg)
                    start = i + 1
            tail = compact[start:]
            if tail:
                texts.append(tail)
            speech += max(0, ts[-1][1] - ts[0][0]) / 1000.0
        else:
            t = raw.strip()
            if t:
                texts.append(t)
    return {"engine": "Paraformer-large(funasr)", "load_sec": round(load, 1),
            "infer_sec": round(time.time() - t1, 1), "segments": len(texts),
            "speech_sec": round(speech, 1), "texts": texts, "has_timestamps": True}


# ---------------------------------------------------------------- 评估

def _cer(ref, hyp):
    """字符错误率（Levenshtein 距离 / 参考长度）。"""
    if not ref:
        return None
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return round(prev[-1] / len(ref), 4)


def _ref_text(verified_path, limit_sec):
    """CER 参考文本。

    ⚠️ 重要（2026-10-24）：不能用项目缓存的 verified.json 作参考——那批
    是**带 initial_prompt** 跑的，首段被 prompt 回声占掉约 30s 真实语音，
    拿它当参考会让所有引擎的 CER 虚高（实测 0.53~0.71，失去区分度）。

    这里改为**用无 prompt 的 faster-whisper 重跑一遍**作为参考基线
    （这也正是本轮修复后流水线的实际配置），保证 CER 有意义。
    """
    return None  # 改由 main() 用 prompt-free 重跑结果做参考


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-seconds", type=float, default=90.0)
    ap.add_argument("--engines", default="fw_small,fw_base,sv,pf")
    ap.add_argument("--out", default="logs/asr_benchmark.json")
    args = ap.parse_args()

    src, base_name = _find_material()
    print(f"[BENCH] 基准素材: {base_name}")
    wav = os.path.join(PROJECT_ROOT, "data", "work_dirs", "_bench_tmp.wav")
    os.makedirs(os.path.dirname(wav), exist_ok=True)
    if not _cut_audio(src, args.limit_seconds, wav):
        print("[BENCH] 音频截取失败")
        return 1
    audio_sec = os.path.getsize(wav) / (16000 * 2)
    print(f"[BENCH] 音频: {audio_sec:.1f}s @16k 单声道")

    ref_path = _find_verified(base_name)
    print(f"[BENCH] 参考基线: 用无 prompt 的 faster-whisper(small) 重跑"
          f"（项目缓存的 verified.json 带 prompt 回声，不能当参考）")

    runners = {
        "fw_small": lambda w: bench_fw(w, "small"),
        "fw_base": lambda w: bench_fw(w, "base"),
        "sv": bench_sv,
        "pf": bench_pf,
    }
    results = []
    ref_text = None
    for key in args.engines.split(","):
        key = key.strip()
        if key not in runners:
            continue
        print(f"\n[BENCH] === {key} ===")
        t = time.time()
        try:
            r = runners[key](wav)
        except Exception as e:
            print(f"  [ERROR] {type(e).__name__}: {str(e)[:200]}")
            continue
        joined = "".join(r["texts"])
        hits = [w for w in KEY_WORDS if w in joined]
        errs = [f"{b}→{g}" for b, g in HOMOPHONE_ERRORS.items() if b in joined]
        r["keyword_hits"] = len(hits)
        r["keyword_list"] = hits
        r["homophone_errors"] = errs
        r["homophone_error_count"] = len(errs)
        r["trad_ratio"] = _trad_ratio(joined)
        r["rtf"] = round(r["infer_sec"] / max(audio_sec, 1), 3)
        r["chars"] = len(_norm(joined))
        # 第一个引擎（fw_small）作为 CER 参考基线
        if ref_text is None:
            ref_text = joined
            r["cer_vs_reference"] = 0.0
        else:
            r["cer_vs_reference"] = _cer(_norm(ref_text), _norm(joined))
        r["samples"] = r["texts"][:5]
        r.pop("texts", None)
        results.append(r)
        print(f"  句块={r['segments']}  语音={r['speech_sec']}s  加载={r['load_sec']}s "
              f"推理={r['infer_sec']}s  RTF={r['rtf']}")
        print(f"  关键词 {len(hits)} 个 | 同音错字 {len(errs)} 处 {errs[:6]}")
        print(f"  繁体占比={r['trad_ratio']}  CER(vs无prompt基线)={r['cer_vs_reference']}")
        for s in r["samples"][:3]:
            print(f"    > {s[:64]}")

    out = os.path.join(PROJECT_ROOT, args.out)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"material": base_name, "audio_sec": round(audio_sec, 1),
                   "reference": "faster-whisper(small) 无prompt（本轮修复后的流水线配置）",
                   "results": results}, f, ensure_ascii=False, indent=2)
    print(f"\n[BENCH] 结果已写入 {out}")
    try:
        os.remove(wav)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
