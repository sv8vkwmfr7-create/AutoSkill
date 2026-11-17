"""技能子进程 runner（固定代码，不参与生成）——自包含 + 运行时审计沙箱。

用法:  python -I -B runner.py <skill_dir>
stdin: {"params": {...}}（UTF-8 字节流）
stdout: 最后一行输出协议 JSON（ASCII 转义，避免 Windows 控制台编码问题）：
        {"ok": true, "data": ..., "log": "..."} 或 {"ok": false, "error": "...", "log": "..."}
退出码: 0 = 已按协议处理（含技能内异常）；3 = runner 自身错误。

安全分层（纵深防御，两层独立生效）：
- 静态层（ast_guard.py）：主进程内拒绝白名单外 import / builtin 调用 / dunder 访问 / 别名
- 运行时层（本文件 audit hook，PEP 578）：默认拒绝一切危险系统调用——
  exec/eval 动态执行、非白名单 import（含 __import__/importlib 动态路径）、
  文件写入、受限路径读取、os.*/socket.*/subprocess.*/ctypes.*/urllib.* 等事件。
  静态层漏掉的动态绕过（如内建别名）在此被兜住。
- 资源层（executor/local.py）：Windows Job Object 内存/进程数上限 + kill-on-close
- 生产层（executor/docker_exec.py）：--network none --read-only 容器隔离

设计要点：
- -I 隔离模式：忽略 PYTHON* 环境变量与用户 site-packages
- 自包含：不依赖 jsonschema（内置极简 JSON Schema 校验器），裸 python 镜像即可运行
- 技能内 print() 重定向到内存缓冲，随协议 JSON 的 log 字段返回
- 任何异常（含 BaseException）都转为协议 error，不向调用方泄漏裸栈
"""

import io
import json
import sys
import builtins
import importlib.util
from pathlib import Path

# 与 ast_guard.IMPORT_WHITELIST 保持一致（runner 自包含，独立副本）
_RUNTIME_IMPORT_WHITELIST = frozenset(
    {
        "base64", "collections", "csv", "datetime", "decimal", "difflib",
        "functools", "hashlib", "ipaddress", "itertools", "json", "math",
        "numbers", "random", "re", "secrets", "statistics", "string",
        "textwrap", "time", "typing", "unicodedata", "uuid",
    }
)

# 审计事件默认拒绝清单（前缀匹配；PEP 578 事件名）
_DENY_EVENT_PREFIXES = (
    "os.", "socket.", "subprocess.", "urllib.", "http.", "ftplib.",
    "smtplib.", "telnetlib.", "ctypes.", "winreg.", "msvcrt.", "pickle.",
    "shutil.", "tempfile.", "webbrowser.", "multiprocessing.", "crypt.",
)
_DENY_EVENTS = {"eval"}

# 危险标准库排除表：这些模块在 sys.stdlib_module_names 中但具备 IO/网络/执行能力，
# 传递性依赖判定时必须显式排除（socket/subprocess/os 等绝不给技能 import）
_DANGEROUS_STDLIB = frozenset(
    {
        "os", "sys", "subprocess", "socket", "shutil", "tempfile", "ctypes",
        "urllib", "http", "ftplib", "smtplib", "telnetlib", "poplib",
        "imaplib", "nntplib", "xmlrpc", "xml", "webbrowser", "tkinter",
        "sqlite3", "multiprocessing", "threading", "signal", "ssl",
        "asyncio", "concurrent", "pty", "tty", "curses", "msvcrt", "nt",
        "posix", "importlib", "pkgutil", "runpy", "code", "codeop",
        "compileall", "pdb", "site", "venv", "pip", "setuptools", "wheel",
        "pathlib", "io", "builtins", "pickle",
    }
)
_WRITE_MODE_CHARS = set("wax+")

PROCEDURE_EXIT_INTERNAL = 3


def _emit(payload: dict, exit_code: int) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, default=str) + "\n")
    sys.stdout.flush()
    sys.exit(exit_code)


def _fail(msg: str, exit_code: int = PROCEDURE_EXIT_INTERNAL) -> None:
    _emit({"ok": False, "error": msg}, exit_code)


# ---------------------------------------------------------------------------
# 极简 JSON Schema 校验器（自包含，替代 jsonschema；支持 MVP 所需子集）
# ---------------------------------------------------------------------------

def _type_ok(value, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True  # 未知类型声明不拦截（静态层与调用方预校验兜底）


def _minimal_validate(instance, schema, path="根"):
    """返回错误信息字符串或 None。支持 type/properties/required/enum/items/min/max。"""
    if not isinstance(schema, dict):
        return None
    expected = schema.get("type")
    if expected and not _type_ok(instance, expected):
        return f"{path} 必须是 {expected} 类型（当前 {type(instance).__name__}）"
    if "enum" in schema and instance not in schema["enum"]:
        return f"{path} 不在可选值 {schema['enum']!r} 中"
    if isinstance(instance, dict):
        props = schema.get("properties", {}) or {}
        for name in schema.get("required", []) or []:
            if name not in instance:
                return f"{path} 缺少必填参数: {name}"
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in props:
                    return f"{path} 不允许的额外参数: {key}"
        for key, sub in props.items():
            if key in instance:
                err = _minimal_validate(instance[key], sub, path=f"{path}.{key}")
                if err:
                    return err
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            err = _minimal_validate(item, schema["items"], path=f"{path}[{i}]")
            if err:
                return err
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            return f"{path} 不能小于 {schema['minimum']}"
        if "maximum" in schema and instance > schema["maximum"]:
            return f"{path} 不能大于 {schema['maximum']}"
    return None


# ---------------------------------------------------------------------------
# 运行时审计沙箱（PEP 578）
# ---------------------------------------------------------------------------

def _install_audit_hook(skill_dir: Path) -> None:
    """安装默认拒绝的审计钩子。技能代码无法移除已安装的钩子（无注销 API）。

    import 判定采用"解释器目录文件路径"而非仅模块名：whitelist 模块的传递性依赖
    （hashlib -> _hashlib、re -> enum、datetime -> time）都是标准库文件，必须放行；
    而 site-packages 中的第三方包（requests 等）无论模块名一律拒绝。
    """
    base = str(Path(getattr(sys, "base_prefix", sys.prefix)).resolve()).replace("\\\\", "/").lower().rstrip("/")
    skill_root = str(Path(skill_dir).resolve()).replace("\\\\", "/").lower().rstrip("/")

    def _allow_import(module: str, filename) -> bool:
        root = module.split(".")[0]
        if root in _RUNTIME_IMPORT_WHITELIST:
            return True
        if root in _DANGEROUS_STDLIB:
            return False
        # Python 3.14 实测：import 审计事件的 filename 为 None，
        # 传递性标准库依赖（struct/enum/time 等）用官方 stdlib 模块名集合判定（已排除危险模块）；
        # site-packages 三方包（requests 等）不在该集合中，天然拒绝
        if root in getattr(sys, "stdlib_module_names", frozenset()):
            return True
        if filename:
            f = str(filename).replace("\\\\", "/").lower()
            if "site-packages" in f or "dist-packages" in f:
                return False
            return f.startswith(base)
        return root.startswith("_")

    def hook(event, args):
        if event == "exec":
            # 仅拒绝 exec("源码字符串") 形态的动态执行；
            # 代码对象执行（含模块加载）放行——其内部危险操作由 import/open/os.* 事件兜底
            if args and isinstance(args[0], str):
                raise PermissionError("沙箱安全策略拒绝动态执行代码（安全隔离）: exec(<source>)")
            return
        if event in _DENY_EVENTS:
            raise PermissionError(f"沙箱安全策略拒绝动态执行代码（安全隔离）: {event}")
        if event == "import":
            module = str(args[0]) if args and args[0] else ""
            filename = args[1] if len(args) > 1 else None
            if module and not _allow_import(module, filename):
                raise PermissionError(f"沙箱安全策略拒绝导入模块（安全隔离）: {module}")
            return
        if event.startswith(_DENY_EVENT_PREFIXES):
            raise PermissionError(f"沙箱安全策略拒绝系统调用（安全隔离）: {event}")
        if event == "open":
            try:
                target, mode = args[0], (args[1] if len(args) > 1 else "") or ""
            except Exception:  # noqa: BLE001
                raise PermissionError("沙箱安全策略拒绝文件操作（安全隔离）: open") from None
            if isinstance(target, int):
                raise PermissionError("沙箱安全策略拒绝文件句柄操作（安全隔离）: open(fd)")
            if any(c in _WRITE_MODE_CHARS for c in str(mode) if c.isalpha()):
                raise PermissionError("沙箱安全策略拒绝文件写入（安全隔离）: 只读沙箱 open")
            try:
                resolved = str(Path(str(target)).resolve()).replace("\\\\", "/").lower()
            except Exception:  # noqa: BLE001
                raise PermissionError("沙箱安全策略拒绝受限路径（安全隔离）: open") from None
            if not (resolved.startswith(skill_root) or resolved.startswith(base)):
                raise PermissionError(f"沙箱安全策略拒绝读取受限路径（安全隔离）: open({event})")

    sys.addaudithook(hook)


# ---------------------------------------------------------------------------
# 受限内建命名空间：eval/exec/open/getattr 等在技能命名空间中直接不存在
# （CPython 的 builtin eval 不触发审计事件，必须从命名空间层面移除）
# ---------------------------------------------------------------------------

_BANNED_BUILTIN_NAMES = {
    "eval", "exec", "compile", "open", "__import__", "input", "globals",
    "locals", "vars", "breakpoint", "getattr", "setattr", "delattr",
    "hasattr", "dir", "help", "exit", "quit", "license", "credits", "copyright",
}


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = str(name).split(".")[0]
    if root in _RUNTIME_IMPORT_WHITELIST:
        return importlib.import_module(name)
    if root not in _DANGEROUS_STDLIB and root in getattr(sys, "stdlib_module_names", frozenset()):
        return importlib.import_module(name)
    raise PermissionError(f"沙箱安全策略拒绝导入模块（安全隔离）: {name}")


def _build_restricted_builtins() -> dict:
    restricted = {k: v for k, v in vars(builtins).items() if k not in _BANNED_BUILTIN_NAMES}
    restricted["__import__"] = _guarded_import
    return restricted


def main() -> None:
    sys.dont_write_bytecode = True  # 防止导入时写 __pycache__（只读沙箱内会被拒）

    if len(sys.argv) < 2:
        _fail("缺少 skill_dir 参数")
    skill_dir = Path(sys.argv[1]).resolve()

    try:
        meta = json.loads((skill_dir / "skill.json").read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        _fail(f"skill.json 读取失败: {e}")

    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        params = payload.get("params", {})
        if not isinstance(params, dict):
            _fail("入参必须是对象: {\"params\": {...}}", exit_code=0)
    except Exception as e:  # noqa: BLE001
        _fail(f"入参 JSON 解析失败: {e}", exit_code=0)

    # JSON Schema 校验（自包含极简校验器；调用方已预校验，此处是双保险）
    err = _minimal_validate(params, meta.get("params_schema") or {"type": "object"})
    if err:
        _emit({"ok": False, "error": f"入参不符合 JSON Schema: {err}"}, exit_code=0)

    version = int(meta.get("version", 1))
    impl_path = skill_dir / f"v{version}" / "impl.py"
    if not impl_path.exists():
        _fail(f"impl 不存在: {impl_path}", exit_code=0)

    # 安装审计沙箱：impl 的加载与执行全程处于默认拒绝策略下
    _install_audit_hook(skill_dir)

    spec = importlib.util.spec_from_file_location("skill_impl", impl_path)
    if spec is None or spec.loader is None:
        _fail(f"impl 加载失败: {impl_path}", exit_code=0)
    module = importlib.util.module_from_spec(spec)
    # 受限内建命名空间：eval/exec/open/getattr 等在技能代码中直接不存在（NameError 显式失败）
    module.__dict__["__builtins__"] = _build_restricted_builtins()
    try:
        spec.loader.exec_module(module)
    except BaseException as e:  # noqa: BLE001
        _fail(f"impl 导入失败: {type(e).__name__}: {e}", exit_code=0)

    fn = getattr(module, "run", None)
    if not callable(fn):
        _fail("impl.py 缺少可调用入口 run(params)", exit_code=0)

    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    try:
        data = fn(params)
        ok, err = True, None
    except BaseException as e:  # noqa: BLE001
        ok, err, data = False, f"{type(e).__name__}: {e}", None
    finally:
        sys.stdout = real_stdout

    payload_out = {"ok": ok, "data": data, "log": buf.getvalue()}
    if err:
        payload_out["error"] = err
    _emit(payload_out, exit_code=0)


if __name__ == "__main__":
    main()
