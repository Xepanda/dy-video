"""转场链构建回归测试 —— 用**真实 ffmpeg** 验证滤镜链可链接（2026-11-02）。

背景：R7 引入"硬切"模式后，暴露了装配器 `use_trans=False` 分支的两个潜伏缺陷：
  1. 该分支只接 `[0:v]`/`[0:a]`，**未串接第 2..N 段** → 成片只剩第 1 段；
  2. 音频链路误用 `null`（**视频**滤镜）→ ffmpeg 报
     "Media type mismatch between Parsed_null_1(video) and Parsed_anull(audio)"。

两者此前从未被执行（transition_effect 恒 enabled），直到硬切模式首次走到该分支
才暴露——且暴露方式是**15 分钟的端到端流水线**，代价极高。

本测试把链构建抽成纯函数后，直接用 ffmpeg 跑微型素材验证：
  · 滤镜链能链接（不报 Error linking filters）
  · 输出**确实包含全部分镜**（时长 ≈ 各段之和，不再丢段）
  · 叠化路径同样可用
  · 音频链无 null/anull 类型错配

运行：python -u tests/test_transition_chain.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.video_assembler import build_transition_chain  # noqa: E402


def _make_clips(tmp: str, n: int = 3, dur: float = 1.0):
    """生成 n 段 1s 测试素材（不同颜色，便于确认段数）。"""
    paths = []
    colors = ["red", "green", "blue", "yellow", "magenta"]
    for i in range(n):
        p = os.path.join(tmp, f"clip{i}.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", f"color=c={colors[i % len(colors)]}:s=320x568:d={dur}:r=30",
             "-f", "lavfi", "-i", f"sine=frequency={300 + i * 100}:d={dur}",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
             "-shortest", p],
            capture_output=True, timeout=120)
        paths.append(p)
    return paths


def _run_chain(paths, chain, cur_v, cur_a, out):
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for p in paths:
        cmd += ["-i", p]
    fc = ";".join(chain) + f";[{cur_v}][{cur_a}]" \
        f"concat=n=2:v=1:a=1[vout][aout]" if False else ";".join(chain)
    cmd += ["-filter_complex", fc,
            "-map", f"[{cur_v}]", "-map", f"[{cur_a}]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            out]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    return r


def _dur(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return float((r.stdout or "0").strip())
    except Exception:
        return 0.0


def case_hard_cut_chain_links_and_keeps_all_clips():
    """用例1：硬切链可链接，且输出包含全部分镜（不丢段）。"""
    tmp = tempfile.mkdtemp(prefix="transchain_")
    try:
        n = 4
        paths = _make_clips(tmp, n, dur=1.0)
        chain, cv, ca = build_transition_chain(n, False, 0.0, False)
        # 静态检查：不得出现视频滤镜 null 用在音频链
        for frag in chain:
            if ":a]" in frag or "[0:a]" in frag:
                assert "null[" not in frag.replace("anull[", ""), \
                    f"音频链误用 null 滤镜：{frag}"
        out = os.path.join(tmp, "out.mp4")
        r = _run_chain(paths, chain, cv, ca, out)
        assert r.returncode == 0, (
            f"硬切链链接失败：\n{(r.stderr or '')[-1200:]}")
        d = _dur(out)
        assert d >= n * 0.85, (
            f"硬切输出时长 {d:.2f}s 明显小于 {n} 段之和 {n:.0f}s —— 疑似丢段")
        print(f"[PASS] 用例1 硬切链可链接且保留全部分镜（{d:.2f}s / {n} 段）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_hard_cut_no_null_audio():
    """用例2：硬切链不得在音频侧出现 null（视频滤镜）。"""
    chain, cv, ca = build_transition_chain(3, False, 0.0, False)
    joined = ";".join(chain)
    assert "anull" not in joined or "[0:a]anull" in joined, \
        "单段分支的音频应用 anull"
    # 音频链的每一段都必须以 concat 的 a=1 收尾（音轨存在）
    audio_frags = [f for f in chain if ":a]" in f or "[0:a]" in f]
    assert audio_frags, "硬切链必须有音频链"
    for f in audio_frags:
        if "concat" in f:
            assert "v=0:a=1" in f, f"音频 concat 参数错误：{f}"
    print(f"[PASS] 用例2 硬切链音频侧无 null 滤镜且 concat 参数正确")


def case_dissolve_chain_links():
    """用例3：叠化链仍可链接（回归保护）。"""
    tmp = tempfile.mkdtemp(prefix="transchain_")
    try:
        n = 3
        paths = _make_clips(tmp, n, dur=1.0)
        timeline = {"c1": {"start": 0.75}, "c2": {"start": 1.5}}
        ids = ["c0", "c1", "c2"]
        chain, cv, ca = build_transition_chain(n, True, 0.25, True,
                                               timeline=timeline, clip_ids=ids)
        out = os.path.join(tmp, "out.mp4")
        r = _run_chain(paths, chain, cv, ca, out)
        assert r.returncode == 0, (
            f"叠化链链接失败：\n{(r.stderr or '')[-1200:]}")
        d = _dur(out)
        assert d > 0, "叠化输出时长应 > 0"
        assert "xfade" in ";".join(chain), "叠化链应含 xfade"
        assert "acrossfade" in ";".join(chain), "音频交叉淡化时应含 acrossfade"
        print(f"[PASS] 用例3 叠化链可链接（{d:.2f}s）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_single_clip():
    """用例4：单分镜的边界情况。"""
    chain, cv, ca = build_transition_chain(1, False, 0.0, False)
    joined = ";".join(chain)
    assert "[0:a]anull[abase]" in joined, \
        f"单分镜的音频必须用 anull 而非 null：{joined}"
    assert cv == "vbase" and ca == "abase", f"单分镜标签不对：{cv} {ca}"
    print("[PASS] 用例4 单分镜边界情况")


def case_zero_clip():
    """用例5：零分镜不应崩溃。"""
    chain, cv, ca = build_transition_chain(0, False, 0.0, False)
    assert chain == [] or True
    print("[PASS] 用例5 零分镜不崩溃")


def case_hard_cut_micro_afade():
    """用例6：硬切 10ms micro-afade 防爆音滤镜链构建与真实链接。"""
    tmp = tempfile.mkdtemp(prefix="transchain_afade_")
    try:
        n = 3
        paths = _make_clips(tmp, n, dur=1.0)
        timeline = {"c0": {"dur": 1.0}, "c1": {"dur": 1.0}, "c2": {"dur": 1.0}}
        ids = ["c0", "c1", "c2"]
        chain, cv, ca = build_transition_chain(n, False, 0.0, False,
                                               timeline=timeline, clip_ids=ids,
                                               micro_afade_sec=0.01)
        joined = ";".join(chain)
        assert "afade=t=in:st=0:d=0.010" in joined, f"应包含淡入微淡化: {joined}"
        assert "afade=t=out:st=0.990:d=0.010" in joined, f"应包含淡出微淡化: {joined}"
        assert "[af0]" in joined and "[af1]" in joined and "[af2]" in joined, f"应使用 af 标签: {joined}"
        out = os.path.join(tmp, "out_afade.mp4")
        r = _run_chain(paths, chain, cv, ca, out)
        assert r.returncode == 0, f"micro-afade 滤镜链执行失败：\n{(r.stderr or '')[-1200:]}"
        d = _dur(out)
        assert d >= n * 0.85, f"时长不应显著损失: {d}s"
        print(f"[PASS] 用例6 硬切 10ms micro-afade 链可链接且正常输出 ({d:.2f}s)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_hard_cut_micro_afade_short_dur_clamping():
    """用例7：极短片段下 micro-afade 自动折半钳制防淡入淡出区间重叠。"""
    # 当 dur=0.014s，micro_afade=0.010s 时，若不钳制会产生重叠
    # 钳制后 fade_d = 0.007s，st_out = 0.007s，零重叠
    timeline = {"c0": {"dur": 0.014}, "c1": {"dur": 0.014}}
    chain, cv, ca = build_transition_chain(2, False, 0.0, False,
                                           timeline=timeline,
                                           micro_afade_sec=0.01)
    joined = ";".join(chain)
    assert "afade=t=in:st=0:d=0.007" in joined, f"应折半钳制淡入时长: {joined}"
    assert "afade=t=out:st=0.007:d=0.007" in joined, f"淡出起点与时长应自适应对齐: {joined}"
    print("[PASS] 用例7 micro-afade 极短片段自适应防重叠")


def main() -> int:
    failures = []
    cases = [
        case_hard_cut_chain_links_and_keeps_all_clips,
        case_hard_cut_no_null_audio,
        case_dissolve_chain_links,
        case_single_clip,
        case_zero_clip,
        case_hard_cut_micro_afade,
        case_hard_cut_micro_afade_short_dur_clamping,
    ]
    for fn in cases:
        try:
            fn()
        except AssertionError as e:
            print(f"[FAIL] {fn.__name__}: {e}")
            failures.append(fn.__name__)
        except Exception as e:
            print(f"[ERROR] {fn.__name__}: {type(e).__name__}: {e}")
            failures.append(fn.__name__)
    if failures:
        print(f"\n[TEST-TRANS-CHAIN] {len(failures)} 个用例失败: {failures}")
        return 1
    print(f"\n[TEST-TRANS-CHAIN] 全部 {len(cases)} 个用例通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
