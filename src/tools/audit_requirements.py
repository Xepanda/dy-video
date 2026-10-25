#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
audit_requirements.py — 既定需求审计引擎（第 30 步产物）
=========================================================
背景：
  本仓库的"既定需求"散落在三处文档：
    1. docs/editing_task_plan_2026-10-21.md（需求编号 + 字幕校对流水线）
    2. skills/live-ecommerce-video-editor/SKILL.md（7 大节制作规范）
    3. contracts/constraints.yaml + contracts/constraints_map.md（量化约束）
  过去"是否满足"只能靠人读文档 + 人读代码下结论，导致反复出现
  **声明与实现脱节**：yaml 里写了键，代码根本不读（"死键"），
  却在校验表里被标成 ✅。constraints.yaml 头部写着"阈值只改本文件，
  不改代码"，一旦出现死键，这条原则就被无声破坏。

本模块提供**机器可复核**的判定能力，核心是"段路径"（path）模型：

  constraints.get("video", {}).get("funnel", {}) → 路径 ("video", "funnel")
  q = funnel_c.get(cat, {})                      → 路径 ("video", "funnel", "*")
  q.get("min")                                   → 消费路径 ("video","funnel","*","min")

  yaml 中的键 `video.funnel.hook.min` 与消费路径
  ("video","funnel","*","min") 匹配（"*" 匹配任意动态段）→ 判定为**已接线**。

  若 yaml 里的键在代码中找不到任何匹配的读取路径 → **死键**（改了没反应）。

此外还提供：
  - QC 断言存在性：从 src/qc/qc_auditor.py 提取真实断言名。
  - 代码符号存在性：断言指定的函数/类确实存在。
  - 需求基线核对：contracts/requirements.yaml 中每条需求的证据锚点是否成立。

输出：
  - 人类可读表格（默认）
  - JSON（--json）——供 tests/ 与 CI 消费

CLI:
  python -u src/tools/audit_requirements.py
  python -u src/tools/audit_requirements.py --json
  python -u src/tools/audit_requirements.py --only-dead
  python -u src/tools/audit_requirements.py --req contracts/requirements.yaml
"""

import os
import re
import sys
import ast
import json
import glob
import argparse

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONSTRAINTS_PATH = os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
REQUIREMENTS_PATH = os.path.join(PROJECT_ROOT, "contracts", "requirements.yaml")
QC_AUDITOR_PATH = os.path.join(PROJECT_ROOT, "src", "qc", "qc_auditor.py")
SRC_GLOB = os.path.join(PROJECT_ROOT, "src", "**", "*.py")

# 非"约束键"的 yaml 顶层项，不参与死键判定
# （blacklist / subtitle_banned_tokens 是列表或映射，仍需参与——它们同样会被代码消费）
NON_CONSTRAINT_PREFIXES = ("version", "updated")

MODULE_SCOPE = "<module>"
ROOT = "<<root>>"      # 表示"约束字典本身"
DYNAMIC = "*"          # 动态段（key 由变量决定）


# =========================================================
# AST 基础工具
# =========================================================
def node_key(node):
    """把赋值目标/接收者归一化成可追踪的"变量名"。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            return "self." + node.attr
        return node.attr
    return None


def const_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def is_root_ref(node):
    """判断是否为约束字典的入口引用（constraints / self.constraints / cfg 之类）。"""
    if isinstance(node, ast.Name):
        return node.id in ("constraints", "cons", "cfg", "constraints_dict")
    if isinstance(node, ast.Attribute):
        return node.attr in ("constraints", "cons", "cfg")
    return False


def callee_name(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def path_match(consumed_path, key_path):
    """消费路径与 yaml 键路径是否匹配（'*' 匹配任意单段）。"""
    if len(consumed_path) != len(key_path):
        return False
    for c, k in zip(consumed_path, key_path):
        if c == DYNAMIC or k == DYNAMIC:
            continue
        if c != k:
            return False
    return True


# =========================================================
# 源码索引
# =========================================================
class SourceIndex:
    """AST 级索引：段路径消费关系、QC 断言名、符号表。"""

    def __init__(self, root: str = PROJECT_ROOT, valid_segments=None):
        self.root = root
        self.valid_segments = set(valid_segments) if valid_segments else None
        self.files = sorted(glob.glob(SRC_GLOB, recursive=True))
        self.rel = {p: os.path.relpath(p, root).replace("\\", "/") for p in self.files}
        self.trees = {}
        for p in self.files:
            try:
                self.trees[p] = ast.parse(open(p, encoding="utf-8").read())
            except SyntaxError:
                continue

        self.scope_of_node = {}
        self.functions = {}
        self.class_init = {}
        self.bindings = {}      # (path, scope, var) -> set(path tuple)
        self.calls = []
        self.param_paths = {}   # (func_name, param) -> set(path tuple)
        self._global_cache = {}
        self._param_paths_by_name = {}

        self._index_scopes()
        self._index_functions()
        self._index_bindings()
        self._resolve_params()
        self._param_paths_by_name = {}
        for (fname, pname), paths in self.param_paths.items():
            self._param_paths_by_name.setdefault(pname, set()).update(paths)

        self.consumed = []      # list[(path_tuple, rel_file, line)]
        self._scan_reads()

        self.qc_assertions = self._collect_qc_assertions()
        self.symbols = self._collect_symbols()

    # ---------- 作用域 ----------
    def _index_scopes(self):
        for p, tree in self.trees.items():
            def walk(node, fname):
                for child in ast.iter_child_nodes(node):
                    self.scope_of_node[id(child)] = fname
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        walk(child, child.name)
                    else:
                        walk(child, fname)
            walk(tree, MODULE_SCOPE)

    def _scope(self, node):
        return self.scope_of_node.get(id(node), MODULE_SCOPE)

    # ---------- 函数 ----------
    def _index_functions(self):
        for p, tree in self.trees.items():
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    a = node.args
                    params = [x.arg for x in
                              list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)]
                    self.functions.setdefault(node.name, (params, p))
                elif isinstance(node, ast.ClassDef):
                    # 类 → 其 __init__ 形参（构造时形参实参与方法内一致）
                    init = None
                    for sub in node.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                                and sub.name == "__init__":
                            init = sub
                            break
                    if init is not None:
                        a = init.args
                        params = [x.arg for x in
                                  list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)]
                        self.functions.setdefault(node.name, (params, p))
                        self.class_init[node.name] = params

    # ---------- 绑定（路径）----------
    def _index_bindings(self):
        """分阶段收敛：

        阶段 1 —— 直接绑定（右侧是 constraints.get("seg") / ["seg"]）；
        阶段 2 —— 解析形参透传（需要阶段 1 的实参绑定）；
        阶段 3 —— 再次收敛间接绑定（X = Y，Y 的绑定在阶段 2 才确定）。
        """
        self._index_bindings_pass()
        self._resolve_params()
        for _ in range(3):
            before = len(self.bindings)
            self._index_bindings_pass()
            if len(self.bindings) == before:
                break
        self._global_cache.clear()

    def _index_bindings_pass(self):
        for fpath, tree in self.trees.items():
            for node in ast.walk(tree):
                scope = self._scope(node)
                if isinstance(node, ast.Assign):
                    seg_path = self.resolve_path(node.value, fpath, scope)
                    if seg_path:
                        for tgt in node.targets:
                            k = node_key(tgt)
                            if k:
                                self.bindings.setdefault((fpath, scope, k), set()).add(seg_path)
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    seg_path = self.resolve_path(node.value, fpath, scope)
                    if seg_path:
                        k = node_key(node.target)
                        if k:
                            self.bindings.setdefault((fpath, scope, k), set()).add(seg_path)
                elif isinstance(node, ast.Call):
                    cname = callee_name(node.func)
                    if cname:
                        self.calls.append((fpath, scope, cname, node.args, node.keywords))

    # ---------- 路径解析 ----------
    def resolve_path(self, node, path, scope):
        """把表达式解析成"约束段路径"元组；无法确定返回 None。

        例：
          constraints.get("video", {})            → ("video",)
          constraints.get("video").get("funnel")  → ("video", "funnel")
          q = funnel_c.get(cat, {})               → ("video","funnel", "*")
        """
        if node is None:
            return None
        # 变量引用
        if isinstance(node, ast.Name):
            # 约束字典本身（constraints / cons）视为根
            if node.id in ("constraints", "cons"):
                return (ROOT,)
            r = self._lookup_var_path(path, scope, node.id)
            if r is not None:
                return r
            # 形参：按参数名全局解析（要求该参数名在所有函数中归属唯一）
            cand = self._param_paths_by_name.get(node.id)
            if cand and len(cand) == 1:
                return sorted(cand, key=len)[0]
            return None
        if isinstance(node, ast.Attribute):
            if is_root_ref(node):
                return (ROOT,)
            key = node_key(node)
            if key:
                return self._lookup_var_path(path, scope, key)
            return None
        # load_constraints() / yaml.safe_load(...) → 约束字典根
        if isinstance(node, ast.Call):
            cname = callee_name(node.func)
            if cname in ("load_constraints", "safe_load", "_load_constraints"):
                return (ROOT,)
        # x.get("k")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get":
            base = self.resolve_path(node.func.value, path, scope)
            if base is None:
                return None
            key = const_str(node.args[0]) if node.args else None
            if key is None:
                if not node.args:
                    return None
                key = DYNAMIC          # 动态键
            if base == (ROOT,):
                base = ()
            seg = key
            if self.valid_segments is not None and seg != DYNAMIC \
                    and seg not in self.valid_segments and base == ():
                return None
            return base + (seg,)
        # x["k"]
        if isinstance(node, ast.Subscript):
            base = self.resolve_path(node.value, path, scope)
            if base is None:
                return None
            key = const_str(node.slice)
            if key is None:
                return None
            if base == (ROOT,):
                base = ()
            return base + (key,)
        # a or b / a and b
        if isinstance(node, ast.BoolOp):
            for v in node.values:
                r = self.resolve_path(v, path, scope)
                if r is not None:
                    return r
            return None
        # dict 合并：{**DEFAULTS["text"], **(constraints.get("text") or {})}
        if isinstance(node, ast.Dict):
            for v in node.values:
                r = self.resolve_path(v, path, scope)
                if r is not None:
                    return r
            return None
        # dict(framing_params)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "dict" and node.args:
            return self.resolve_path(node.args[0], path, scope)
        return None

    def _lookup_var_path(self, path, scope, name):
        r = self.bindings.get((path, scope, name))
        if not r:
            r = self.bindings.get((path, MODULE_SCOPE, name))
        if not r:
            r = self._global_unique_path(name)
        if not r:
            return None
        # 多路径时取第一个（保守：任一匹配即可，后续由消费记录判定）
        return sorted(r, key=len)[0]

    def _global_unique_path(self, name):
        """跨作用域兜底：同名变量在全仓只有唯一绑定路径时才采用。

        用于处理 `cover_c = constraints.get("cover", {})` 定义在一个函数、
        在另一个函数里使用（成员函数共享状态）的情况。
        若同名变量在全仓有多个不同绑定 → 视为歧义，返回 None（宁可漏判
        也不误判，避免把"已接线"错报成"死键"）。
        """
        if name in self._global_cache:
            return self._global_cache[name]
        found = set()
        for (p, sc, k), paths in self.bindings.items():
            if k == name:
                found |= paths
        result = found if len(found) == 1 else None
        self._global_cache[name] = result
        return result

    # ---------- 形参透传 ----------
    def _resolve_params(self):
        self._param_paths_by_name = {}
        changed = True
        rounds = 0
        while changed and rounds < 8:
            changed = False
            rounds += 1
            for fpath, scope, fname, args, kwargs in self.calls:
                info = self.functions.get(fname)
                if not info:
                    continue
                params, _ = info
                pairs = []
                for idx, a in enumerate(args):
                    if idx < len(params):
                        pairs.append((params[idx], a))
                for kw in kwargs:
                    if kw.arg in params:
                        pairs.append((kw.arg, kw.value))
                for pname, anode in pairs:
                    seg_path = self.resolve_path(anode, fpath, scope)
                    if not seg_path:
                        continue
                    key = (fname, pname)
                    cur = self.param_paths.setdefault(key, set())
                    if seg_path not in cur:
                        cur.add(seg_path)
                        changed = True
                    byname = self._param_paths_by_name.setdefault(pname, set())
                    if seg_path not in byname:
                        byname.add(seg_path)

    # ---------- 读取扫描 ----------
    def _scan_reads(self):
        for p, tree in self.trees.items():
            for node in ast.walk(tree):
                scope = self._scope(node)
                leaf = None
                recv = None
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr == "get" and node.args:
                    leaf = const_str(node.args[0])
                    recv = node.func.value
                    if leaf is None:
                        leaf = DYNAMIC
                elif isinstance(node, ast.Subscript):
                    leaf = const_str(node.slice)
                    recv = node.value
                if leaf is None or recv is None:
                    continue
                base = self.resolve_path(recv, p, scope)
                if base is None:
                    continue
                if base == (ROOT,):
                    base = ()
                self.consumed.append((base + (leaf,), self.rel[p], node.lineno))

    # ---------- QC 断言 ----------
    def _collect_qc_assertions(self):
        names = set()
        if not os.path.exists(QC_AUDITOR_PATH):
            return names
        s = open(QC_AUDITOR_PATH, encoding="utf-8").read()
        for m in re.finditer(r"""_assert\(\s*(?:True|False|[^,]+?)\s*,\s*["']([^"']+)["']""", s):
            names.add(m.group(1))
        return names

    # ---------- 代码符号 ----------
    def _collect_symbols(self):
        syms = set()
        for tree in self.trees.values():
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    syms.add(node.name)
        return syms


# =========================================================
# 约束键遍历
# =========================================================
def flatten_constraints(data, prefix=""):
    out = []
    if isinstance(data, dict):
        for k, v in data.items():
            path = f"{prefix}{k}"
            if isinstance(v, dict):
                out.extend(flatten_constraints(v, path + "."))
            else:
                out.append((path, v))
    else:
        out.append((prefix, data))
    return out


def load_constraints(path=CONSTRAINTS_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def constraint_keys(data):
    keys = []
    for path, _ in flatten_constraints(data):
        if path.startswith(NON_CONSTRAINT_PREFIXES):
            continue
        keys.append(path)
    return keys


def constraint_segments(data):
    """yaml 顶层段名（列表型段如 subtitle_banned_tokens 也是合法段）。"""
    return [k for k in data.keys() if not k.startswith(NON_CONSTRAINT_PREFIXES)]


# =========================================================
# 死键判定
# =========================================================
def key_is_wired(index: SourceIndex, key: str):
    """返回 (是否接线, 证据列表)。"""
    key_path = tuple(key.split("."))
    evidence = []
    for cpath, rel, line in index.consumed:
        if path_match(cpath, key_path):
            evidence.append((rel, line))
    return (bool(evidence), sorted(set(evidence)))


def find_dead_keys(index: SourceIndex, constraints):
    """返回 [{key, seg, leaf}]——yaml 声明了但代码中无任何消费者的键。"""
    dead = []
    for key in constraint_keys(constraints):
        wired, _ = key_is_wired(index, key)
        if not wired:
            seg, _, leaf = key.rpartition(".")
            dead.append({"key": key, "seg": seg, "leaf": leaf,
                         "reason": "yaml 声明该键，但代码中无任何消费者"})
    return dead


# =========================================================
# 需求基线核对
# =========================================================
def audit_requirements(index: SourceIndex, requirements, constraints):
    rows = []
    all_keys = set(constraint_keys(constraints))
    for req in requirements.get("requirements", []):
        checks = []
        for key in req.get("constraint_keys", []) or []:
            exists = key in all_keys
            wired = key_is_wired(index, key)[0] if exists else False
            allow_dead = bool(req.get("allow_dead", False))
            checks.append({
                "type": "constraint", "ref": key,
                "exists": exists, "wired": wired,
                "ok": exists and (wired or allow_dead),
            })
        for name in req.get("qc_assertions", []) or []:
            checks.append({"type": "qc_assertion", "ref": name,
                           "ok": name in index.qc_assertions})
        for sym in req.get("code_symbols", []) or []:
            checks.append({"type": "code_symbol", "ref": sym,
                           "ok": sym in index.symbols})
        rows.append({
            "id": req.get("id"),
            "title": req.get("title"),
            "source": req.get("source"),
            "declared_status": req.get("status"),
            "checks": checks,
            "ok": all(c["ok"] for c in checks) if checks else True,
        })
    return rows


def load_requirements(path=REQUIREMENTS_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# =========================================================
# 主流程
# =========================================================
def main():
    ap = argparse.ArgumentParser(description="既定需求审计引擎（第 30 步）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--only-dead", action="store_true", help="只列死键")
    ap.add_argument("--req", default=REQUIREMENTS_PATH, help="需求基线文件路径")
    args = ap.parse_args()

    if yaml is None:
        print("需要 pyyaml：pip install pyyaml")
        return 2

    constraints = load_constraints()
    index = SourceIndex(valid_segments=constraint_segments(constraints))
    dead = find_dead_keys(index, constraints)

    report = {
        "total_constraint_keys": len(constraint_keys(constraints)),
        "dead_count": len(dead),
        "dead_keys": dead,
        "qc_assertion_count": len(index.qc_assertions),
        "qc_assertions": sorted(index.qc_assertions),
    }

    if os.path.exists(args.req):
        reqs = load_requirements(args.req)
        rows = audit_requirements(index, reqs, constraints)
        report["requirements"] = rows
        report["requirements_total"] = len(rows)
        report["requirements_failed"] = [r["id"] for r in rows if not r["ok"]]

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("=" * 70)
    print(f"既定需求审计（第 30 步）  约束键 {report['total_constraint_keys']} 个 / "
          f"死键 {report['dead_count']} 个 / QC 断言 {report['qc_assertion_count']} 个")
    print("=" * 70)
    if dead:
        print("\n[死键] yaml 有声明、代码无消费者（改这些键不会改变任何行为）：")
        for d in dead:
            print(f"  x {d['key']}")
    if "requirements" in report:
        print(f"\n[需求基线] 共 {report['requirements_total']} 条，"
              f"证据不成立 {len(report['requirements_failed'])} 条")
        for r in report["requirements"]:
            flag = "PASS" if r["ok"] else "FAIL"
            print(f"  [{flag}] {str(r['id']):<26} {str(r['declared_status']):<6} {r['title']}")
            if not r["ok"]:
                for c in r["checks"]:
                    if not c["ok"]:
                        print(f"        证据不成立: {c['type']}={c['ref']}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
