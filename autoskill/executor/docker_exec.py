"""Docker 沙箱执行器（接口与配置项就绪，实装属 P1）。

实装要点（P1，验收文档第 9 节）：
    docker run --rm --network none --read-only --memory=128m --cpus=0.5 \
        --pids-limit=64 --security-opt no-new-privileges \
        -v <skill_dir>:/skill:ro python:3.12-slim python -I /app/runner.py /skill
职责与本接口完全一致：把 LocalSubprocessExecutor 换成本类即可切换后端，
同一套评测脚本应在两种后端下复跑出对比数据（验收 P1 达标形式）。
"""

from __future__ import annotations

from pathlib import Path

from .base import ExecResult


class DockerExecutor:
    name = "docker"

    def __init__(self, image: str = "python:3.12-slim", memory: str = "128m", cpus: float = 0.5):
        self.image = image
        self.memory = memory
        self.cpus = cpus

    def healthcheck(self) -> str:
        return f"DockerExecutor(image={self.image}) 未实装（P1）"

    def execute(self, skill_dir: Path, params: dict, timeout_s: float | None = None) -> ExecResult:
        raise NotImplementedError(
            "DockerExecutor 未实装（P1 加分项）：接口与配置项已就绪，"
            "请改用 AUTOSKILL_EXECUTOR=local，或按模块 docstring 完成实装。"
        )
