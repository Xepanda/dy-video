#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_requirements_audit.py — 既定需求审计基线测试（第 30 步）
=================================================================
背景：
  本仓库反复出现"声明与实现脱节"：constraints.yaml 声明了键，代码根本不读
  （死键），却在文档里被标成 ✅。第 30 步把既定需求抽成
  contracts/requirements.yaml（机器可读基线），本测试负责**防止基线自身
  腐化**——即代码改了但基线没改、或基线里的证据锚点失效。

用例：
  1. 基线文件存在且结构合法（版本/条目数/字段完整性/ID 唯一）
  2. 每条需求的证据锚点均可解析（constraints 键存在、QC 断言存在、代码符号存在）
  3. 死键集合与基线登记完全一致（不多不少）——死键增加即失败
  4. 已登记的死键在基线中都有对应条目（缺口登记守卫）
  5. 审计引擎自身的健壮性（路径匹配函数、空输入、AST 解析）

运行: python -u tests/test_requirements_audit.py
"""
import os
import sys
import ast
import glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "tools"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml  # noqa: E402
import audit_requirements as ar  # noqa: E402

REQUIREMENTS_PATH = os.path.join(ROOT, "contracts", "requirements.yaml")
CONSTRAINTS_PATH = os.path.join(ROOT, "contracts", "constraints.yaml")

# 基线中登记的死键条目 → 其 constraint_keys 展开后的死键集合
EXPECTED_DEAD_KEYS = {
    "cover.ghosting_energy_max",
    "cover.contour_completeness_min",
    "cover.landmark_visibility_min",
    "cover.knees_visible",
    "cover.ankles_visible",
    "cover.horizontal_margin_ratio",
    "cover.garment_match_min",
    "cover.ocr_area_ratio_max",
    "garment_match.clip_model",
    "text.brand_en_gradient",
    "text.product_name_gradient",
    "text.product_name_stroke",
    "text.brand_en_fill",
    "text.brand_en_stroke",
    "text.brand_cn_fill",
    "text.brand_cn_stroke",
    "split_effect.radius",
    "split_effect.border_color",
    "split_effect.border_width",
    "split_effect.vertical_center",
    "video.subtitle_safe_zone_px",
    "audio.sample_rate",
    "price.clip_exclude",
    "price.detector",
    "price.whitelist_units",
}

REQUIRED_FIELDS = ("id", "source", "title", "level", "status",
                   "constraint_keys", "qc_assertions", "code_symbols")
VALID_LEVELS = {"BLOCK", "WARN", "INFO"}
VALID_STATUS = {"implemented", "partial", "unimplemented"}


def _load():
    with open(REQUIREMENTS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def case_baseline_structure():
    """基线文件结构合法：版本、条目数、字段完整、ID 唯一。"""
    data = _load()
    assert isinstance(data, dict), "基线根节点必须是映射"
    assert data.get("version"), "缺少 version 字段"
    reqs = data.get("requirements")
    assert isinstance(reqs, list) and len(reqs) >= 30, \
        f"需求条目过少（{len(reqs) if isinstance(reqs, list) else 'N/A'}），基线应覆盖三份来源文档"

    ids = set()
    for r in reqs:
        for f in REQUIRED_FIELDS:
            assert f in r, f"需求 {r.get('id')} 缺少字段 {f}"
        rid = r["id"]
        assert rid not in ids, f"需求 ID 重复: {rid}"
        ids.add(rid)
        assert r["level"] in VALID_LEVELS, f"{rid}: 非法 level {r['level']}"
        assert r["status"] in VALID_STATUS, f"{rid}: 非法 status {r['status']}"
        for key in ("constraint_keys", "qc_assertions", "code_symbols"):
            assert isinstance(r[key], list), f"{rid}: {key} 必须是列表"
    print(f"[PASS] 基线结构合法：{len(reqs)} 条需求，ID 唯一")


def case_evidence_anchors_resolve():
    """每条需求的证据锚点必须可解析（键存在 / 断言存在 / 符号存在）。"""
    data = _load()
    constraints = ar.load_constraints(CONSTRAINTS_PATH)
    index = ar.SourceIndex(valid_segments=ar.constraint_segments(constraints))
    rows = ar.audit_requirements(index, data, constraints)
    failed = [r for r in rows if not r["ok"]]
    assert not failed, "以下需求证据不成立:\n" + "\n".join(
        f"  {r['id']}: " + ", ".join(
            f"{c['type']}={c['ref']}" for c in r["checks"] if not c["ok"])
        for r in failed)
    print(f"[PASS] 全部 {len(rows)} 条需求的证据锚点均可解析")


def case_dead_keys_match_baseline():
    """死键集合必须与基线登记完全一致（代码改了基线没改 → 失败）。"""
    constraints = ar.load_constraints(CONSTRAINTS_PATH)
    index = ar.SourceIndex(valid_segments=ar.constraint_segments(constraints))
    dead = {d["key"] for d in ar.find_dead_keys(index, constraints)}
    missing = dead - EXPECTED_DEAD_KEYS
    extra = EXPECTED_DEAD_KEYS - dead
    assert not missing, "出现基线未登记的新死键（说明实现退化了）:\n  " + \
        "\n  ".join(sorted(missing))
    assert not extra, "基线登记的死键已被修复，请更新 requirements.yaml:\n  " + \
        "\n  ".join(sorted(extra))
    print(f"[PASS] 死键集合与基线一致（{len(dead)} 个）")


def case_dead_keys_all_registered():
    """每个死键都必须在基线中有对应条目（allow_dead 登记守卫）。"""
    data = _load()
    registered = set()
    for r in data["requirements"]:
        if r.get("allow_dead"):
            registered |= set(r.get("constraint_keys") or [])
    dead = set(EXPECTED_DEAD_KEYS)
    unregistered = dead - registered
    assert not unregistered, "以下死键未在基线中登记（会变成隐形缺口）:\n  " + \
        "\n  ".join(sorted(unregistered))
    print(f"[PASS] {len(dead)} 个死键均已在基线中登记")


def case_engine_selfcheck():
    """审计引擎自身健壮性：路径匹配、AST 解析、空输入不崩。"""
    assert ar.path_match(("video", "funnel", "*", "min"),
                          ("video", "funnel", "hook", "min")), "通配段应匹配"
    assert not ar.path_match(("video", "funnel", "min"),
                              ("video", "funnel", "hook", "min")), "长度不同不应匹配"
    assert not ar.path_match(("cover",), ("text",)), "段名不同不应匹配"

    # 所有 src/*.py 必须能被 AST 解析（引擎依赖 AST，语法错误会导致漏判）
    files = glob.glob(os.path.join(ROOT, "src", "**", "*.py"), recursive=True)
    assert files, "找不到 src 下的 py 文件"
    for p in files:
        try:
            ast.parse(open(p, encoding="utf-8").read())
        except SyntaxError as e:
            raise AssertionError(f"{p} 语法错误，AST 审计会漏判: {e}")
    print(f"[PASS] 引擎自检通过（{len(files)} 个源文件均可解析）")


def main():
    failures = []
    for fn in (case_baseline_structure,
               case_evidence_anchors_resolve,
               case_dead_keys_match_baseline,
               case_dead_keys_all_registered,
               case_engine_selfcheck):
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"[FAIL] {e}")
        except Exception as e:
            failures.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"[ERROR] {type(e).__name__}: {e}")
    print(f"\n[TEST-REQ-AUDIT] {'全部通过' if not failures else '失败: ' + str(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
