"""执行器抽象：统一的技能执行接口，可插拔（local / docker）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class ExecResult:
    """一次技能执行的原始结果。"""

    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    memory_limit_hit: bool = False  # 内存看门狗/Job Object 触发终止
    protocol: dict[str, Any] | None = field(default=None)  # runner 协议 JSON

    @property
    def ok(self) -> bool:
        """协议级成功：exit 0 且 runner 报告 ok=true 且有 data 字段。"""
        return (
            self.exit_code == 0
            and isinstance(self.protocol, dict)
            and self.protocol.get("ok") is True
            and "data" in self.protocol
        )

    @property
    def data(self) -> Any:
        return self.protocol.get("data") if self.protocol else None

    @property
    def error(self) -> str:
        if self.ok:
            return ""
        if self.protocol and self.protocol.get("error"):
            return str(self.protocol["error"])
        if self.timed_out:
            return f"执行超时（>{getattr(self, 'timeout_s', '?')}s，进程树已终止）"
        if getattr(self, "memory_limit_hit", False):
            return f"内存超过限额（{getattr(self, 'memory_limit_mb', '?')}MB），进程树已终止（安全隔离）"
        if self.exit_code not in (0, None):
            return f"runner 异常退出 code={self.exit_code}: {self.stderr[-300:]}"
        return "未知执行错误"


class Executor(Protocol):
    """技能执行器协议。实现方：LocalSubprocessExecutor / DockerExecutor。"""

    def execute(
        self,
        skill_dir: Path,
        params: dict,
        timeout_s: float | None = None,
    ) -> ExecResult: ...

    def healthcheck(self) -> str:
        """返回执行器后端描述（用于演示与诊断）。"""
        ...
