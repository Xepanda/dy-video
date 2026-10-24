#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_stage_cache_fingerprint.py - 阶段缓存的代码指纹机制测试
=============================================================
背景（2026-10-24 实测踩坑）：
  ASR 层的缓存键此前**只有视频路径**（src_key = str(src_path)），
  不含代码/配置指纹。后果：改了 asr_engine 的识别行为（禁用 initial_prompt）
  之后，缓存依然命中 → 跑出的是**带 prompt 回声的陈旧结果**，
  修复"看起来没生效"（回归指标与修复前完全一致，一度误判为已修复）。

  修复：ASR 层缓存键并入 code_fingerprint()（核心代码 + constraints 内容哈希）。

用例：
  1. code_fingerprint 存在且稳定（连续调用一致）
  2. 代码变更 → 指纹变化（改完还原后恢复）
  3. constraints 变更 → 指纹变化
  4. analyze_story_segments 确实把指纹并入 ASR 缓存键（源码级断言）
  5. 阶段缓存 load/save 往返正常

运行: python -u tests/test_stage_cache_fingerprint.py
"""
import os
import sys
import shutil
import tempfile
import inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.tools.stage_cache import (code_fingerprint, compute_pipeline_key,
                                   load_stage, save_stage, _CODE_FINGERPRINT_FILES)


def case_fingerprint_stable():
    """指纹应稳定（内容哈希，与 mtime 无关）。"""
    a = code_fingerprint()
    b = code_fingerprint()
    assert a == b, f"指纹不稳定: {a} != {b}"
    assert len(a) >= 8, f"指纹过短: {a}"
    print(f"[PASS] code_fingerprint 稳定: {a}")


def case_code_change_invalidates():
    """核心代码变更 → 指纹变化。"""
    target = None
    for rel in _CODE_FINGERPRINT_FILES:
        if rel.endswith("asr_engine.py"):
            target = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), rel)
            break
    assert target and os.path.exists(target), "找不到指纹文件"
    before = code_fingerprint()
    orig = open(target, "rb").read()
    try:
        with open(target, "ab") as f:
            f.write(b"\n# fingerprint probe\n")
        after = code_fingerprint()
    finally:
        with open(target, "wb") as f:
            f.write(orig)
    assert before != after, "代码变更后指纹未变（缓存不会失效）"
    restored = code_fingerprint()
    assert restored == before, f"还原后指纹应恢复: {restored} != {before}"
    print("[PASS] 代码变更 → 指纹变化 → 还原后恢复")


def case_constraints_change_invalidates():
    """constraints 变更 → 指纹变化（阈值改动必须使缓存失效）。"""
    cpath = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "contracts", "constraints.yaml")
    assert os.path.exists(cpath), "找不到 constraints.yaml"
    before = code_fingerprint()
    orig = open(cpath, "rb").read()
    try:
        with open(cpath, "ab") as f:
            f.write(b"\n# fingerprint probe\n")
        after = code_fingerprint()
    finally:
        with open(cpath, "wb") as f:
            f.write(orig)
    assert before != after, "constraints 变更后指纹未变"
    assert code_fingerprint() == before, "还原后指纹应恢复"
    print("[PASS] constraints 变更 → 指纹变化 → 还原后恢复")


def case_asr_cache_key_includes_fingerprint():
    """analyze_story_segments 必须把指纹并入 ASR 缓存键（源码级断言）。"""
    import src.tools.analyze_story_segments as mod
    src = inspect.getsource(mod)
    assert "code_fingerprint" in src, \
        "analyze_story_segments 未引用 code_fingerprint（ASR 缓存会命中陈旧结果）"
    assert "src_key" in src and "asr_fingerprint" in src, \
        "ASR 缓存键 src_key 未包含代码指纹"
    print("[PASS] analyze_story_segments 的 ASR 缓存键已含代码指纹")


def case_stage_roundtrip():
    """阶段缓存 load/save 往返正常。"""
    with tempfile.TemporaryDirectory() as td:
        key = "testkey123"
        payload = {"items": [1, 2, 3], "name": "测试"}
        save_stage(td, "story", key, payload)
        got = load_stage(td, "story", key)
        assert got is not None, "保存后应能读回"
        assert got.get("items") == [1, 2, 3], f"内容不符: {got}"
        # 不同 key 不应命中
        assert load_stage(td, "story", "otherkey") is None, "不同 key 不应命中"
    print("[PASS] 阶段缓存 save/load 往返正常")


def case_pipeline_key_differs_on_spec_change():
    """spec 变更 → pipeline key 变化。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = os.path.join(root, "job_spec.json")
    if not os.path.exists(spec):
        print("[SKIP] job_spec.json 不存在")
        return
    before = compute_pipeline_key(["a.mp4"], spec)
    orig = open(spec, "rb").read()
    try:
        with open(spec, "ab") as f:
            f.write(b"\n")
        after = compute_pipeline_key(["a.mp4"], spec)
    finally:
        with open(spec, "wb") as f:
            f.write(orig)
    assert before != after, "spec 变更后 pipeline key 未变"
    print("[PASS] spec 变更 → pipeline key 变化")


def main():
    failures = []
    for fn in (case_fingerprint_stable, case_code_change_invalidates,
               case_constraints_change_invalidates,
               case_asr_cache_key_includes_fingerprint,
               case_stage_roundtrip,
               case_pipeline_key_differs_on_spec_change):
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"[FAIL] {e}")
        except Exception as e:
            failures.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"[ERROR] {type(e).__name__}: {e}")
    print(f"\n[TEST-FINGERPRINT] {'全部通过' if not failures else '失败: ' + str(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
