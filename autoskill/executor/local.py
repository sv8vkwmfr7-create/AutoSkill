"""本地子进程执行器（dev-only，非安全沙箱）。

隔离手段：python -I 隔离解释器 + 超时杀整棵进程树 + stdin/stdout 协议通信。
生产级隔离由 DockerExecutor 提供（P1）。演示与文档必须标注 dev-only。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from .base import ExecResult

RUNNER_PATH = Path(__file__).resolve().parent.parent / "runner.py"
CREATE_NO_WINDOW = 0x08000000  # Windows：不闪黑窗


class LocalSubprocessExecutor:
    name = "local"

    def __init__(self, python_exe: str | None = None):
        self.python_exe = python_exe or sys.executable

    def healthcheck(self) -> str:
        return f"LocalSubprocessExecutor(python={self.python_exe}) dev-only"

    def execute(self, skill_dir: Path, params: dict, timeout_s: float | None = None) -> ExecResult:
        timeout = float(timeout_s or 10.0)
        cmd = [self.python_exe, "-I", str(RUNNER_PATH), str(skill_dir)]
        stdin_payload = json.dumps({"params": params}, ensure_ascii=True).encode("utf-8")

        start = time.monotonic()
        creationflags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.Popen(  # noqa: S603 - 固定 runner 命令，参数经 JSON 协议传递
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(skill_dir),
            creationflags=creationflags,
        )
        timed_out = False
        try:
            stdout_b, stderr_b = proc.communicate(input=stdin_payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_tree(proc)
            stdout_b, stderr_b = b"", b""

        duration = time.monotonic() - start
        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")

        from autoskill.schemas import parse_protocol_output

        result = ExecResult(
            exit_code=None if timed_out else proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_s=duration,
            timed_out=timed_out,
        )
        result.protocol = parse_protocol_output(stdout) if not timed_out else None
        result.timeout_s = timeout  # 供 error 描述使用
        return result

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """Windows 下必须杀整棵进程树，只 kill 直接子进程会留孤儿。"""
        if sys.platform == "win32":
            subprocess.run(  # noqa: S603, S607 - 固定系统命令
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
            )
        try:
            proc.kill()
        except OSError:
            pass
