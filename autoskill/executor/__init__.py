"""执行器工厂：按配置返回 local / docker 后端。"""

from __future__ import annotations

from ..config import Settings
from .base import ExecResult, Executor
from .docker_exec import DockerExecutor
from .local import LocalSubprocessExecutor

__all__ = ["ExecResult", "Executor", "LocalSubprocessExecutor", "DockerExecutor", "make_executor"]


def make_executor(settings: Settings) -> Executor:
    if settings.executor == "docker":
        return DockerExecutor()
    return LocalSubprocessExecutor()
