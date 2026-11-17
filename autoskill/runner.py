"""技能子进程 runner（固定代码，不参与生成）。

用法:  python -I runner.py <skill_dir>
stdin: {"params": {...}}（UTF-8 字节流）
stdout: 最后一行输出协议 JSON（ASCII 转义，避免 Windows 控制台编码问题）：
        {"ok": true, "data": ..., "log": "..."} 或 {"ok": false, "error": "...", "log": "..."}
退出码: 0 = 已按协议处理（含技能内异常）；3 = runner 自身错误。

设计要点：
- -I 隔离模式：忽略 PYTHON* 环境变量与用户 site-packages，仅 venv 内依赖可用
- 技能内 print() 重定向到内存缓冲，随协议 JSON 的 log 字段返回，不污染协议行
- 任何异常（含 BaseException）都转为协议 error，不向调用方泄漏裸栈
"""

import io
import json
import sys
import importlib.util
from pathlib import Path


def _emit(payload: dict, exit_code: int) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, default=str) + "\n")
    sys.stdout.flush()
    sys.exit(exit_code)


def _fail(msg: str, exit_code: int = 3) -> None:
    _emit({"ok": False, "error": msg}, exit_code)


def main() -> None:
    if len(sys.argv) < 2:
        _fail("缺少 skill_dir 参数")
    skill_dir = Path(sys.argv[1])

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

    # JSON Schema 校验（调用方已预校验，此处是双保险）
    try:
        import jsonschema

        jsonschema.validate(instance=params, schema=meta.get("params_schema") or {"type": "object"})
    except Exception as e:  # noqa: BLE001
        _emit({"ok": False, "error": f"入参不符合 JSON Schema: {e}"}, exit_code=0)

    version = int(meta.get("version", 1))
    impl_path = skill_dir / f"v{version}" / "impl.py"
    if not impl_path.exists():
        _fail(f"impl 不存在: {impl_path}", exit_code=0)

    spec = importlib.util.spec_from_file_location("skill_impl", impl_path)
    if spec is None or spec.loader is None:
        _fail(f"impl 加载失败: {impl_path}", exit_code=0)
    module = importlib.util.module_from_spec(spec)
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

    if isinstance(data, tuple) and len(data) == 2 and isinstance(data[1], dict):
        # 约定：run 可返回 (data, extra_meta)，目前仅保留扩展位，MVP 直接取 data
        data = data[0]

    payload_out = {"ok": ok, "data": data, "log": buf.getvalue()}
    if err:
        payload_out["error"] = err
    _emit(payload_out, exit_code=0)


if __name__ == "__main__":
    main()
