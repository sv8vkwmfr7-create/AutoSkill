"""静态安全检查（AST）：import 白名单 + 内置函数黑名单 + 双下划线属性封禁。

这是 MVP 阶段唯一的安全机制（无 Docker 环境，dev-only），因此标准从严：
白名单外一律拒绝，无人工豁免通道。运行时隔离由 Executor 层负责（Docker 上线后双保险）。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

# import 白名单：纯计算模块，覆盖评测集全部技能场景
IMPORT_WHITELIST = frozenset(
    {
        "base64", "collections", "csv", "datetime", "decimal", "difflib",
        "functools", "hashlib", "ipaddress", "itertools", "json", "math",
        "random", "re", "secrets", "statistics", "string", "textwrap",
        "time", "typing", "unicodedata", "uuid",
    }
)

# 内置函数黑名单：文件/执行/求值类一律禁止
BUILTIN_BLACKLIST = frozenset(
    {"eval", "exec", "compile", "open", "__import__", "input", "globals", "locals", "vars", "breakpoint"}
)


@dataclass
class GuardReport:
    ok: bool
    violations: list[str] = field(default_factory=list)


def _check_source(source: str) -> tuple[ast.Module | None, list[str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return None, [f"语法错误: 第 {e.lineno} 行: {e.msg}"]
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in IMPORT_WHITELIST:
                    violations.append(f"禁止导入模块: {alias.name}（白名单外）")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                violations.append("禁止相对导入")
            root = (node.module or "").split(".")[0]
            if root and root not in IMPORT_WHITELIST:
                violations.append(f"禁止导入模块: {node.module}（白名单外）")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in BUILTIN_BLACKLIST:
                violations.append(f"禁止调用内置函数: {node.func.id}()")
        elif isinstance(node, ast.Attribute):
            attr = node.attr
            if attr.startswith("__") and attr.endswith("__"):
                violations.append(f"禁止访问双下划线属性: .{attr}")
        elif isinstance(node, ast.Name):
            if node.id.startswith("__"):
                violations.append(f"禁止引用双下划线名称: {node.id}")
    return tree, violations


def check_code(source: str) -> GuardReport:
    """完整静态检查：语法 + import 白名单 + builtin 黑名单 + dunder 封禁。"""
    _, violations = _check_source(source)
    return GuardReport(ok=not violations, violations=violations)


def check_contract(source: str) -> GuardReport:
    """技能契约检查：只负责顶层 run(params) 入口（import/安全违规由 check_code 负责）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return GuardReport(False, [f"语法错误: 第 {e.lineno} 行: {e.msg}"])
    violations: list[str] = []
    has_run = False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            has_run = True
            arg_names = [a.arg for a in node.args.args]
            if "params" not in arg_names:
                violations.append("run() 第一个参数必须名为 params")
    if not has_run:
        violations.append("impl.py 缺少顶层入口函数 run(params)")
    return GuardReport(ok=not violations, violations=violations)
