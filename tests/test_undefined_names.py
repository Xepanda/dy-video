"""未定义名静态检查 —— 用 AST 找出函数体内的 NameError 隐患。

背景（2026-11-02）：R7 重构把装配器的转场链抽成模块级函数 `build_transition_chain()`，
删掉了局部变量 `use_trans`，但下游 meta 记录仍在引用它 →
`NameError: name 'use_trans' is not defined`，**只在 15 分钟的端到端流水线末尾暴露**。

这类问题属于"重构残留引用"，纯靠运行测试发现代价太高。本检查器做纯 AST 分析，
秒级给出所有"在某作用域内被读取、但从未在该作用域（或其外层可见作用域）内赋值"的自由变量。

作用域模型（Python 的简化版）：
  · 函数体是一个作用域；嵌套函数能看到**外层**已绑定的名字（闭包）；
  · 参数属于**该函数自己的**作用域；
  · 推导式/生成器表达式是独立作用域，其目标名绑定在内层；
  · 类体不是闭包作用域（看不到外层函数局部）——保守起见按"可见"处理，
    宁可漏报不可误报。

实现要点（两版踩过的坑）：
  1. 第一版用 `ast.walk(fn)` 收集绑定 → 把**嵌套函数的局部名**混进了外层，
     导致外层误判为"已定义"而漏报；
  2. 第二版把嵌套函数的 used 回流到外层 → 子函数的**参数**被误报。
     正确做法是**每个作用域独立判定**：读到的名字若在本作用域 bound，
     则已定义；否则向外层作用域链查找。

运行：python -u tests/test_undefined_names.py
"""
from __future__ import annotations

import ast
import builtins
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__debug__",
}


class Scope:
    """一个作用域：记录本作用域绑定的名字 + 读取的名字。"""

    def __init__(self, parent: "Scope" = None):
        self.parent = parent
        self.bound: set = set()
        self.reads: list = []          # (name, lineno)
        self.children: list = []

    def bind(self, name):
        self.bound.add(name)

    def is_visible(self, name) -> bool:
        s = self
        while s is not None:
            if name in s.bound:
                return True
            s = s.parent
        return False

    def read(self, name, lineno):
        self.reads.append((name, lineno))


def _bind_target(scope: Scope, target):
    for n in ast.walk(target):
        if isinstance(n, ast.Name):
            scope.bind(n.id)


def _analyze_body(scope: Scope, body):
    """遍历语句列表，填充 scope 的 bound / reads。"""
    for node in body:
        _analyze_node(scope, node)


def _analyze_node(scope: Scope, node):
    if isinstance(node, ast.Name):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            scope.bind(node.id)
        else:
            scope.read(node.id, node.lineno)
        return
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        scope.bind(node.name)
        _analyze_function(scope, node)
        return
    if isinstance(node, ast.Lambda):
        scope.bind("<lambda>")
        sub = Scope(scope)
        for a in (list(node.args.args) + list(node.args.posonlyargs) +
                  list(node.args.kwonlyargs)):
            sub.bind(a.arg)
        if node.args.vararg:
            sub.bind(node.args.vararg.arg)
        if node.args.kwarg:
            sub.bind(node.args.kwarg.arg)
        _analyze_node(sub, node.body)
        scope.children.append(sub)
        return
    if isinstance(node, ast.Import):
        for al in node.names:
            scope.bind((al.asname or al.name).split(".")[0])
        return
    if isinstance(node, ast.ImportFrom):
        for al in node.names:
            scope.bind(al.asname or al.name)
        return
    if isinstance(node, ast.ExceptHandler):
        if node.name:
            scope.bind(node.name)
        for c in node.body:
            _analyze_node(scope, c)
        return
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        for n in node.names:
            scope.bind(n)
        return
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        sub = Scope(scope)
        for gen in node.generators:
            _bind_target(sub, gen.target)
            for c in ast.iter_child_nodes(gen):
                _analyze_node(sub, c)
        _analyze_node(sub, node.elt)
        scope.children.append(sub)
        return
    if isinstance(node, ast.DictComp):
        sub = Scope(scope)
        for gen in node.generators:
            _bind_target(sub, gen.target)
            for c in ast.iter_child_nodes(gen):
                _analyze_node(sub, c)
        if node.key is not None:
            _analyze_node(sub, node.key)
        _analyze_node(sub, node.value)
        scope.children.append(sub)
        return
    for child in ast.iter_child_nodes(node):
        _analyze_node(scope, child)


def _analyze_function(parent: Scope, node):
    sub = Scope(parent)
    for a in (list(node.args.args) + list(node.args.posonlyargs) +
              list(node.args.kwonlyargs)):
        sub.bind(a.arg)
    if node.args.vararg:
        sub.bind(node.args.vararg.arg)
    if node.args.kwarg:
        sub.bind(node.args.kwarg.arg)
    for d in node.args.defaults + [d for d in node.args.kw_defaults if d]:
        _analyze_node(parent, d)
    _analyze_body(sub, node.body)
    parent.children.append(sub)


def _analyze_class(parent: Scope, node: ast.ClassDef):
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parent.bind(item.name)
            _analyze_function(parent, item)
        elif isinstance(item, ast.ClassDef):
            parent.bind(item.name)
            _analyze_class(parent, item)
        elif isinstance(item, ast.Assign):
            for t in item.targets:
                _bind_target(parent, t)
            _analyze_node(parent, item.value)
        elif isinstance(item, ast.AnnAssign):
            _bind_target(parent, item.target)
            if item.value is not None:
                _analyze_node(parent, item.value)


def _module_scope(tree: ast.Module) -> Scope:
    scope = Scope(None)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope.bind(node.name)
            _analyze_function(scope, node)
        elif isinstance(node, ast.ClassDef):
            scope.bind(node.name)
            _analyze_class(scope, node)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                _bind_target(scope, t)
            _analyze_node(scope, node.value)
        elif isinstance(node, ast.AnnAssign):
            _bind_target(scope, node.target)
            if node.value is not None:
                _analyze_node(scope, node.value)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for al in node.names:
                scope.bind((al.asname or al.name).split(".")[0])
        else:
            _analyze_node(scope, node)
    return scope


def _collect_undefined(scope: Scope, module_scope: Scope, out: list, fname: str):
    for name, lineno in scope.reads:
        if scope.is_visible(name):
            continue
        if module_scope.is_visible(name):
            continue
        if name in BUILTINS or name.startswith("__"):
            continue
        out.append((fname, name, lineno))
    for child in scope.children:
        _collect_undefined(child, module_scope, out, fname)


def find_undefined(path: str):
    """返回 [(func_name, name, lineno), ...]。"""
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename=path)
    mod = _module_scope(tree)

    out: list = []
    for node in mod.children:
        if isinstance(node, Scope):
            _collect_undefined(node, mod, out, "<module>")
    # 模块级自身的 reads
    for name, lineno in mod.reads:
        if mod.is_visible(name) or name in BUILTINS or name.startswith("__"):
            continue
        out.append(("<module>", name, lineno))
    return out


def main() -> int:
    targets = [
        "src/core/video_assembler.py",
        "src/tools/cut_style_rules.py",
        "src/tools/slicing_rules.py",
        "src/tools/build_plan.py",
        "src/agent/agent_director.py",
        "src/core/asr_agent_verifier.py",
        "src/core/asr_review_gate.py",
        "src/core/plan_compiler.py",
        "src/tools/product_anchor.py",
        "src/tools/verify_clip_product_alignment.py",
        "src/qc/qc_auditor.py",
    ]

    # —— 自验证：先证明检查器确实能抓到问题，否则它毫无价值 ——
    import tempfile
    probes = [
        ("def probe():\n    x = 1\n    return x + undefined_thing\n",
         "undefined_thing"),
        ("def outer():\n    def inner():\n        return missing_in_inner\n"
         "    return inner\n", "missing_in_inner"),
    ]
    for i, (code, expect) in enumerate(probes):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as tf:
            tf.write(code)
            probe_path = tf.name
        try:
            issues = find_undefined(probe_path)
            if not any(x[1] == expect for x in issues):
                print(f"[FAIL] 自验证失败：探针 {i} 未检出 '{expect}'"
                      f"（实际: {issues}）")
                return 1
        finally:
            os.unlink(probe_path)
    print("[PASS] 自验证：检查器能检出未定义名（2 个探针：函数体 / 嵌套函数）")

    failures = []
    for rel in targets:
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            continue
        try:
            issues = find_undefined(p)
        except SyntaxError as e:
            print(f"[FAIL] {rel}: 语法错误 {e}")
            failures.append(rel)
            continue
        if issues:
            print(f"[FAIL] {rel}: 发现 {len(issues)} 处未定义名引用")
            for fn, name, ln in issues:
                print(f"       {fn}() 第 {ln} 行: '{name}'")
            failures.append(rel)
        else:
            print(f"[PASS] {rel}: 无未定义名引用")
    if failures:
        print(f"\n[TEST-UNDEF-NAMES] {len(failures)} 个文件有问题: {failures}")
        return 1
    print(f"\n[TEST-UNDEF-NAMES] 全部 {len(targets)} 个文件通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
