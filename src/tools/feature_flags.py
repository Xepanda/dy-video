# -*- coding: utf-8 -*-
"""feature_flags.py — 统一能力开关的解析器（2026-11-01）

单一事实源 = `contracts/features.yaml`。本模块负责：
  1. `load_definitions()`  读取定义（GUI 与流水线共用同一份，禁止第二份硬编码）
  2. `resolve(spec)`       把「默认值 ← spec.options.features」合并成生效值，并应用组合规则
  3. `validate(payload)`   校验用户提交（未知键 / 非法值 / 未开放项），供 GUI 保存前拦截
  4. `apply(constraints)`  把生效值派生到既有配置键（funnel / transition / magnifier /
                           subtitle_action / duration_range），**开关本身不改 constraints.yaml**
  5. `features_of()`       供运行期模块读取（assembler / qc / build_plan）

设计原则：每个开关都必须有消费点。`state: disabled` 的项只允许保持默认值，
写其它值会被 validate() 拒绝 —— 这样不会重演"配置里写了但没人读"的死键问题。

CLI:
  python -m src.tools.feature_flags --spec job_spec.json          # 打印生效值
  python -m src.tools.feature_flags --spec job_spec.json --json   # 机器可读
  python -m src.tools.feature_flags --specs-only                  # 仅打印定义（GUI 用）
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from typing import Any, Dict, List, Optional

def _project_root() -> str:
    """项目根目录（打包后必须走 exe 目录，不能用 __file__ 推）。

    2026-11-01：PyInstaller onefile 下 `__file__` 位于临时解包目录
    （`<_MEIPASS>/desktop_spec_creator.py`），按 __file__ 推三层会指向
    _MEIPASS 的**祖先目录**，contracts/features.yaml 必然找不到 —— 界面上的
    开关面板会静默为空（exe 是 --windowed，连报错都看不到）。
    与 desktop_spec_creator.get_project_root() 保持同一口径。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


PROJECT_ROOT = _project_root()
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def default_definitions_path() -> str:
    """features.yaml 的查找顺序：项目根 → 打包内嵌副本 → 项目根（缺失时返回，便于报错定位）。"""
    p = os.path.join(_project_root(), "contracts", "features.yaml")
    if os.path.exists(p):
        return p
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        alt = os.path.join(meipass, "contracts", "features.yaml")
        if os.path.exists(alt):
            return alt
    return p


# 开关 → 派生的既有配置键（key_path: 值映射）
# 映射到"已存在的键"是本设计的关键：派生后各模块无需改动即可生效。
_DERIVE: Dict[str, Dict[str, Dict[str, Any]]] = {
    "duration_preset": {
        "standard": {"video.duration_range": [45, 65]},
        "compact": {"video.duration_range": [48, 55]},
    },
    "fabric_ratio": {
        "strict": {"video.funnel.fabric.max": 11},
        "loose": {"video.funnel.fabric.max": 20},
    },
    "transition": {
        "on": {"transition_effect.enabled": True},
        "off": {"transition_effect.enabled": False},
    },
    "magnifier": {
        "on": {"magnifier_effect.enabled": True},
        "off": {"magnifier_effect.enabled": False},
    },
    "violation_action": {
        "delete": {"violation_words.handling.subtitle_action": "delete"},
        "mask": {"violation_words.handling.subtitle_action": "mask"},
        # 纯记录：字幕动作置 off，assembler 不删不改，只保留判定清单
        "record_only": {"violation_words.handling.subtitle_action": "off"},
    },
}

_DEFS_CACHE: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------- 定义读取

def load_definitions(path: str = None, force_reload: bool = False) -> Dict[str, Any]:
    """读取 features.yaml（带缓存；GUI 与流水线共用）。"""
    global _DEFS_CACHE
    if _DEFS_CACHE is not None and not force_reload and path is None:
        return _DEFS_CACHE
    import yaml
    p = path or default_definitions_path()
    with open(p, "r", encoding="utf-8") as f:
        defs = yaml.safe_load(f) or {}
    defs.setdefault("groups", [])
    defs.setdefault("combos", [])
    if path is None:
        _DEFS_CACHE = defs
    return defs


def iter_items(defs: Dict[str, Any] = None):
    """遍历所有开关定义：(group, item)。GUI 渲染与校验共用。"""
    defs = defs or load_definitions()
    for g in defs.get("groups", []):
        for it in g.get("items", []):
            yield g, it


def item_index(defs: Dict[str, Any] = None) -> Dict[str, Dict[str, Any]]:
    return {it["key"]: it for _, it in iter_items(defs)}


def default_values(defs: Dict[str, Any] = None) -> Dict[str, str]:
    return {it["key"]: str(it.get("default")) for _, it in iter_items(defs)}


# ---------------------------------------------------------------- 解析

def resolve(spec: Dict[str, Any], definitions: Dict[str, Any] = None) -> Dict[str, Any]:
    """合并默认值与 spec 覆盖，并应用组合规则。

    返回 {
      values:   {key: value}          # 生效值
      explicit: {key: value}          # 用户在 spec 里显式写的
      forced:   [{key, from, to, note}]   # 被组合规则强制改写的
      warnings: [str]
      version:  int
      disabled_in_spec: [key]         # spec 里写了未开放开关（会被 validate 拒，但 resolve 容错）
    }
    """
    defs = definitions or load_definitions()
    idx = item_index(defs)
    values = default_values(defs)
    explicit: Dict[str, str] = {}
    warnings: List[str] = []
    disabled_in_spec: List[str] = []

    raw = ((spec or {}).get("options", {}) or {}).get("features") or {}
    if not isinstance(raw, dict):
        warnings.append("spec.options.features 不是对象，已忽略")
        raw = {}
    for k, v in raw.items():
        if k not in idx:
            warnings.append(f"未知开关 {k}（已忽略）")
            continue
        values[k] = str(v)
        explicit[k] = str(v)
        if idx[k].get("state") == "disabled":
            disabled_in_spec.append(k)

    # —— 组合规则：override ——
    forced: List[Dict[str, Any]] = []
    for rule in defs.get("combos", []):
        cond = rule.get("when") or {}
        if not cond or any(values.get(k) != str(v) for k, v in cond.items()):
            continue
        force = rule.get("force") or {}
        if str(rule.get("level", "override")) == "override":
            for fk, fv in force.items():
                if values.get(fk) != str(fv):
                    forced.append({"key": fk, "from": values.get(fk),
                                   "to": str(fv), "note": rule.get("note", "")})
                    values[fk] = str(fv)
        else:
            warnings.append(rule.get("note") or f"组合 {cond} 需要人工确认")

    return {
        "values": values,
        "explicit": explicit,
        "forced": forced,
        "warnings": warnings,
        "disabled_in_spec": disabled_in_spec,
        "version": int(defs.get("version", 1)),
    }


def resolve_from_spec_path(spec_path: str) -> Dict[str, Any]:
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)
    return resolve(spec)


# ---------------------------------------------------------------- 校验（GUI 保存前）

def validate(payload: Dict[str, Any], definitions: Dict[str, Any] = None) -> List[str]:
    """校验用户提交的 features 字段。返回错误列表（空 = 通过）。"""
    defs = definitions or load_definitions()
    idx = item_index(defs)
    errors: List[str] = []
    feats = (payload or {}).get("features")
    if feats is None:
        return errors            # 未提交 = 全默认，合法
    if not isinstance(feats, dict):
        return ["features 必须是对象"]

    for k, v in feats.items():
        it = idx.get(k)
        if it is None:
            errors.append(f"未知开关：{k}")
            continue
        allowed = [str(o["value"]) for o in it.get("options", [])]
        if str(v) not in allowed:
            errors.append(f"{it.get('label', k)} 取值非法：{v}（可选 {'/'.join(allowed)}）")
            continue
        if it.get("state") == "disabled" and str(v) != str(it.get("default")):
            errors.append(f"「{it.get('label', k)}」尚未开放"
                          f"（{it.get('disabled_reason', '功能未上线')}）")
    return errors


# ---------------------------------------------------------------- 派生到既有配置

def _set_path(d: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = d
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def apply(constraints: Dict[str, Any], spec: Dict[str, Any],
          definitions: Dict[str, Any] = None) -> Dict[str, Any]:
    """把开关生效值注入 constraints（深拷贝，不污染原对象）。

    注入两处：
      constraints["features"] = {"values": {...}, "render_profile": ..., "version": ...}
      constraints[既有键]     = 派生值（funnel/transition/magnifier/subtitle_action/时长）
    """
    res = resolve(spec, definitions)
    out = copy.deepcopy(constraints or {})
    # 1) 派生
    for key, mapping in _DERIVE.items():
        val = res["values"].get(key)
        for path, derived in (mapping.get(val) or {}).items():
            _set_path(out, path, derived)
    # 2) 供运行期直接读
    out["features"] = {
        "version": res["version"],
        "values": res["values"],
        "render_profile": res["values"].get("render_profile", "branded"),
        "forced": res["forced"],
        "warnings": res["warnings"],
    }
    return out


def features_of(constraints: Dict[str, Any]) -> Dict[str, Any]:
    """运行期读取（未经过 apply 时回落到全默认，保证老调用不出错）。"""
    f = (constraints or {}).get("features")
    if isinstance(f, dict) and f.get("values"):
        return f
    return {"version": 0, "values": default_values(), "render_profile": "branded",
            "forced": [], "warnings": ["constraints 未经 apply()（使用默认开关）"]}


def is_plain(constraints: Dict[str, Any]) -> bool:
    return str(features_of(constraints).get("render_profile", "branded")) == "plain"


# ---------------------------------------------------------------- CLI

def _print_human(res: Dict[str, Any]) -> None:
    defs = load_definitions()
    idx = item_index(defs)
    print(f"统一开关（features.yaml v{res['version']}）")
    print("-" * 62)
    for _, it in iter_items(defs):
        k = it["key"]
        v = res["values"].get(k)
        opt = next((o for o in it.get("options", []) if str(o["value"]) == str(v)), None)
        tag = "" if it.get("state") != "disabled" else "  [未开放]"
        mark = " *" if k in res["explicit"] else "  "
        print(f"{mark}{it.get('label', k):18s} = {opt.get('label', v) if opt else v}{tag}")
    if res["forced"]:
        print("\n组合规则强制收敛：")
        for f in res["forced"]:
            print(f"  - {idx.get(f['key'], {}).get('label', f['key'])}: "
                  f"{f['from']} → {f['to']}  ({f['note']})")
    if res["warnings"]:
        print("\n提示：")
        for w in res["warnings"]:
            print(f"  - {w}")


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description="统一能力开关解析器")
    ap.add_argument("--spec", help="job_spec.json 路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--defs-only", action="store_true", help="只输出定义（GUI 用）")
    ap.add_argument("--definitions", default=None, help="自定义 features.yaml")
    args = ap.parse_args(argv)

    if args.defs_only:
        print(json.dumps(load_definitions(args.definitions), ensure_ascii=False, indent=2))
        return 0

    spec = {}
    if args.spec:
        with open(args.spec, "r", encoding="utf-8") as f:
            spec = json.load(f)
    res = resolve(spec, load_definitions(args.definitions))
    errs = validate({"features": res["explicit"]}, load_definitions(args.definitions))
    if args.json:
        print(json.dumps({"resolved": res, "errors": errs}, ensure_ascii=False, indent=2))
    else:
        _print_human(res)
        if errs:
            print("\n校验错误：")
            for e in errs:
                print(f"  ! {e}")
    return 1 if errs else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
