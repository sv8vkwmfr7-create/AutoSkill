"""执行器工厂：按配置返回 local / docker 后端。"""

from __future__ import annotations

from ..config import Settings
from .base import ExecResult, Executor
from .docker_exec import DockerExecutor
from .local import LocalSubprocessExecutor

__all__ = ["ExecResult", "Executor", "LocalSubprocessExecutor", "DockerExecutor", "make_executor"]


def make_executor(settings: Settings) -> Executor:
    if settings.executor == "docker":
        return DockerExecutor(
            image=settings.docker_image,
            memory=settings.docker_memory,
            cpus=settings.docker_cpus,
            pids_limit=settings.docker_pids_limit,
            egress_hosts=settings.egress_hosts,
            network=settings.docker_network,
        )
    return LocalSubprocessExecutor(
        memory_mb=settings.sandbox_memory_mb,
        max_processes=settings.sandbox_max_processes,
        egress_hosts=settings.egress_hosts,
    )
