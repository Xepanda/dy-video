#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_path_resolver.py - 路径可移植性测试（2026-10-24）
==========================================================
背景：spec 与 GUI 此前把素材/商品图路径硬编码成 `D:/Projects/...` 这类
机器专属绝对路径，换机器/换 CI runner 即失效。本模块统一做路径归一化。

用例：
  1. 相对路径 → 解析为绝对路径（相对仓库根 / 相对 spec 目录）
  2. 本机绝对路径 → 原样返回
  3. **旧机器绝对路径** → 按尾部片段在仓库内自动重新定位（关键能力）
  4. relativize → 转回相对路径（写回 spec 用）
  5. 往返一致性：relativize → resolve → 指向同一文件
  6. 不存在的路径返回 None / 原样保留（不抛异常）

运行: python -u tests/test_path_resolver.py
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.core.path_resolver import resolve_path, relativize, PROJECT_ROOT


def case_relative_path():
    """相对仓库根的路径应被解析为绝对路径。"""
    rel = "data/raw_samples/sample_product_card.png"
    abs_p = resolve_path(rel)
    assert abs_p and os.path.isabs(abs_p), f"应解析为绝对路径: {abs_p}"
    assert os.path.exists(abs_p), f"解析结果应真实存在: {abs_p}"
    print(f"[PASS] 相对路径解析: {rel} -> {os.path.basename(abs_p)}")


def case_absolute_path():
    """本机绝对路径应原样返回。"""
    target = os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
    got = resolve_path(target)
    assert got and os.path.exists(got), f"绝对路径应原样可用: {got}"
    print("[PASS] 绝对路径原样保留")


def case_stale_absolute_relocated():
    """旧机器绝对路径应按尾部片段在仓库内自动重新定位。

    覆盖三种"旧机器路径"形态（跨平台都可能遇到）：
      - Windows 盘符：D:/old/...
      - POSIX 根路径：/old/...
      - Windows 反斜杠：C:\\old\\...

    ⚠️ 这里刻意用**盘符形式**做主断言——`os.path.isabs()` 在 Linux 上对
    `D:/...` 返回 False、在 Windows 上对 `/...` 返回 False，
    两边都会把"旧机器绝对路径"误当相对路径（2026-10-24 CI 实测踩到）。
    """
    from src.core.path_resolver import _looks_absolute
    stale_forms = [
        "D:/Projects/OLD_MACHINE_NAME/data/raw_samples/sample_product_card.png",
        "/old/machine/data/raw_samples/sample_product_card.png",
        "C:\\old\\data\\raw_samples\\sample_product_card.png",
    ]
    for stale in stale_forms:
        assert _looks_absolute(stale), \
            f"应被识别为绝对路径形态: {stale!r}（isabs 跨平台不可靠）"
        got = resolve_path(stale)
        assert got and os.path.exists(got), \
            f"旧机器绝对路径应被重新定位到仓库内: {stale!r} -> {got!r}"
        assert os.path.abspath(got).startswith(os.path.abspath(PROJECT_ROOT)), \
            f"应定位到仓库内: {got!r}"
    print(f"[PASS] 旧机器绝对路径自动重定位（{len(stale_forms)} 种形态，跨平台）")


def case_relativize():
    """绝对路径应能转回相对路径。"""
    target = os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
    rel = relativize(target, PROJECT_ROOT)
    assert not os.path.isabs(rel), f"应转为相对路径: {rel}"
    assert rel.startswith("contracts"), f"应相对仓库根: {rel}"
    print(f"[PASS] relativize: {rel}")


def case_roundtrip():
    """往返一致性：relativize → resolve 指向同一文件。"""
    for rel in ("contracts/constraints.yaml",
                "data/raw_samples/sample_product_card.png"):
        abs_p = resolve_path(rel)
        assert abs_p and os.path.exists(abs_p), f"解析失败: {rel}"
        back = relativize(abs_p, PROJECT_ROOT)
        assert not os.path.isabs(back), f"往返后应是相对路径: {back}"
        again = resolve_path(back)
        assert again == abs_p, f"往返应指向同一文件: {again} != {abs_p}"
    print("[PASS] 往返一致（relativize → resolve 指向同一文件）")


def case_missing_path():
    """不存在的路径不应抛异常。"""
    got = resolve_path("data/definitely_not_exists_xyz.mp4")
    assert got is None or not os.path.exists(got), "不应解析出有效路径"
    got2 = resolve_path("")
    assert got2 is None, "空路径应返回 None"
    got3 = resolve_path(None)
    assert got3 is None, "None 应返回 None"
    print("[PASS] 缺失/空路径安全返回（不抛异常）")


def case_spec_no_absolute_paths():
    """已入库的 spec 不应再含机器专属绝对路径。"""
    spec_files = []
    for d in (".", "specs"):
        pdir = os.path.join(PROJECT_ROOT, d)
        if not os.path.isdir(pdir):
            continue
        for fn in sorted(os.listdir(pdir)):
            if fn.startswith("job_spec") and fn.endswith(".json"):
                spec_files.append(os.path.join(pdir, fn))
    assert spec_files, "未找到 spec 文件"
    offenders = []
    for fp in spec_files:
        with open(fp, "r", encoding="utf-8") as f:
            raw = f.read()
        try:
            spec = json.loads(raw)
        except json.JSONDecodeError as e:
            raise AssertionError(f"{os.path.basename(fp)} 不是合法 JSON: {e}")
        paths = []
        if spec.get("raw_video_path"):
            paths.append(spec["raw_video_path"])
        paths += spec.get("raw_video_paths") or []
        paths += (spec.get("product", {}) or {}).get("item_images") or []
        for p in paths:
            # Windows 绝对路径判定：盘符开头
            if len(p) > 2 and p[1] == ":" and p[2] in "\\/":
                offenders.append(f"{os.path.basename(fp)}: {p[:60]}")
    assert not offenders, "spec 仍含机器专属绝对路径:\n  " + "\n  ".join(offenders)
    print(f"[PASS] {len(spec_files)} 个 spec 均无机器专属绝对路径")


def main():
    failures = []
    for fn in (case_relative_path, case_absolute_path,
               case_stale_absolute_relocated, case_relativize,
               case_roundtrip, case_missing_path,
               case_spec_no_absolute_paths):
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"[FAIL] {e}")
        except Exception as e:
            failures.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"[ERROR] {type(e).__name__}: {e}")
    print(f"\n[TEST-PATH] {'全部通过' if not failures else '失败: ' + str(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
