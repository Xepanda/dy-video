#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_dead_constraint_wiring.py - 死键接线验证（2026-10-25 新增）
====================================================================
背景：审计引擎（src/tools/audit_requirements.py）发现 constraints.yaml 里
有一批"声明了但代码从不读取"的键——改 yaml 不改行为。本测试锁定本轮
接线的两个键，确保后续不会被改回硬编码：

1. zoom_effect.zoom / zoom_effect.max_zoom
   - build_plan 不再硬编码面料块倍率 1.3，改为读 zoom_effect.zoom
   - max_zoom 同时作为渲染端（build_clip_filter）钳制上限
     与 QC 断言（qc_auditor）阈值，三方口径一致

2. mirror_effect.min_occlusion_ratio
   - video_assembler._apply_mirror_occlusion 计算遮挡覆盖率
   - 覆盖率低于阈值 → 调用 _undo_mirror 放弃该分镜的镜像

运行: python -u tests/test_dead_constraint_wiring.py
"""
import os
import sys
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONSTRAINTS = yaml.safe_load(
    open(os.path.join(ROOT, "contracts", "constraints.yaml"), encoding="utf-8"))

ZOOM_C = CONSTRAINTS["zoom_effect"]
MIRROR_C = CONSTRAINTS["mirror_effect"]


def case_zoom_keys_wired():
    """zoom_effect.zoom / max_zoom 三个消费点全部存在。"""
    assert "zoom" in ZOOM_C, "constraints.yaml 缺少 zoom_effect.zoom（面料块目标倍率）"
    assert "max_zoom" in ZOOM_C, "constraints.yaml 缺少 zoom_effect.max_zoom"
    assert 1.0 < float(ZOOM_C["zoom"]) <= float(ZOOM_C["max_zoom"]), \
        f"zoom 应落在 (1, max_zoom] 区间: {ZOOM_C['zoom']} / {ZOOM_C['max_zoom']}"
    assert 1.25 <= float(ZOOM_C["zoom"]) <= 1.35, \
        f"zoom 应符合 SKILL 规定的 1.25~1.35: {ZOOM_C['zoom']}"
    assert 1.25 <= float(ZOOM_C["max_zoom"]) <= 1.35, \
        f"max_zoom 应符合 SKILL 规定的 1.25~1.35: {ZOOM_C['max_zoom']}"

    # 渲染端钳制
    from src.core.video_assembler import build_clip_filter
    z_max = float(ZOOM_C["max_zoom"])
    vf = build_clip_filter({"effect": "none", "zoom": z_max + 0.5}, ZOOM_C)
    assert f"1+({z_max}-1)" in vf, \
        f"渲染端应把倍率钳制到 max_zoom={z_max}，实际滤镜: {vf}"
    assert "2.85" not in vf, "渲染端不应出现未钳制的原始倍率"

    # QC 端阈值读约束（不得硬编码 1.35）
    with open(os.path.join(ROOT, "src", "qc", "qc_auditor.py"), encoding="utf-8") as f:
        qc_src = f.read()
    assert "zoom_c.get(\"max_zoom\"" in qc_src, \
        "qc_auditor 应读 zoom_effect.max_zoom 作为断言阈值"
    assert "> 1.35" not in qc_src, "qc_auditor 不应硬编码 1.35 上限"

    # build_plan 不再硬编码 1.3
    with open(os.path.join(ROOT, "src", "tools", "build_plan.py"), encoding="utf-8") as f:
        bp_src = f.read()
    assert '"zoom": 1.3 if has_fabric_kw else 1.0' not in bp_src, \
        "build_plan 不应硬编码面料块倍率 1.3"
    assert 'zoom_c.get("zoom"' in bp_src, "build_plan 应读 zoom_effect.zoom"
    print(f"[PASS] 推镜倍率键已接线: zoom={ZOOM_C['zoom']} max_zoom={ZOOM_C['max_zoom']}")


def case_mirror_ratio_wired():
    """mirror_effect.min_occlusion_ratio 有真实消费者。"""
    assert "min_occlusion_ratio" in MIRROR_C, "缺少 mirror_effect.min_occlusion_ratio"
    thr = float(MIRROR_C["min_occlusion_ratio"])
    assert 0.0 < thr <= 1.0, f"min_occlusion_ratio 应在 (0,1]，实际 {thr}"

    with open(os.path.join(ROOT, "src", "core", "video_assembler.py"), encoding="utf-8") as f:
        va_src = f.read()
    assert 'mirror_c.get("min_occlusion_ratio"' in va_src, \
        "video_assembler 应读取 mirror_effect.min_occlusion_ratio"
    assert "def _undo_mirror" in va_src, "video_assembler 缺少 _undo_mirror（放弃镜像）"
    assert "_undo_mirror(clip, clip_path)" in va_src, \
        "覆盖率不足时应调用 _undo_mirror 放弃镜像"
    print(f"[PASS] 镜像遮挡覆盖率键已接线: min_occlusion_ratio={thr}")


def case_audit_no_longer_dead():
    """审计引擎确认这两个键不再是死键。"""
    from src.tools.audit_requirements import (
        find_dead_keys, load_constraints, SourceIndex, constraint_segments)
    constraints = load_constraints()
    index = SourceIndex(valid_segments=constraint_segments(constraints))
    dead = {d["key"] for d in find_dead_keys(index, constraints)}
    for k in ("zoom_effect.max_zoom", "zoom_effect.zoom",
              "mirror_effect.min_occlusion_ratio"):
        assert k not in dead, f"{k} 仍是死键（yaml 声明但代码无消费者）"
    print(f"[PASS] 审计引擎确认目标键已接线（当前死键 {len(dead)} 个）")


def main():
    failures = []
    for fn in (case_zoom_keys_wired, case_mirror_ratio_wired,
               case_audit_no_longer_dead):
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"[FAIL] {e}")
        except Exception as e:
            failures.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"[ERROR] {type(e).__name__}: {e}")
    print("=" * 60)
    if failures:
        print(f"[FAIL] 死键接线测试 {len(failures)} 项未通过")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("[PASS] 死键接线测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
