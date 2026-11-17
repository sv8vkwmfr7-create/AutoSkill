"""Docker 沙箱执行器（生产级隔离，已实装）。

隔离参数（fail-closed 组合）：
    --network none            禁网
    --read-only               根文件系统只读（唯一可写点是 tmpfs /tmp）
    --memory/--cpus/--pids-limit   资源上限（内存炸弹、fork 炸弹的硬约束）
    --security-opt no-new-privileges   禁止提权
    skill 目录与 runner 以只读卷挂载；runner 自包含，裸 python 镜像即可运行

使用方式：本机安装 Docker Desktop（WSL2 后端）后设置 AUTOSKILL_EXECUTOR=docker，
同一评测脚本（python -m autoskill eval）可在 local/docker 双后端复跑出对比数据。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .base import ExecResult
from .local import CREATE_NO_WINDOW, RUNNER_PATH


class DockerExecutor:
    name = "docker"

    def __init__(
        self,
        image: str = "python:3.14-slim",
        memory: str = "256m",
        cpus: str = "0.5",
        pids_limit: int = 64,
        docker_exe: str = "docker",
    ):
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.docker_exe = docker_exe

    # ---- 诊断 -----------------------------------------------------------
    def healthcheck(self) -> str:
        if not shutil.which(self.docker_exe):
            return "DockerExecutor: 未检测到 docker CLI（请安装 Docker Desktop / WSL2 后端）"
        try:
            rc = subprocess.run(  # noqa: S603, S607
                [self.docker_exe, "version", "--format", "{{.Server.Version}}"],
                capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            return f"DockerExecutor(image={self.image}) daemon={rc.stdout.decode().strip() or 'unreachable'}"
        except Exception as e:  # noqa: BLE001
            return f"DockerExecutor(image={self.image}) daemon 不可达: {e}"

    # ---- 组卷 -----------------------------------------------------------
    def _stage(self, skill_dir: Path) -> tuple[Path, Path]:
        """把 skill 目录与 runner 复制到 ASCII 安全的临时目录（Windows 挂载路径不含中文）。"""
        staging = Path(tempfile.mkdtemp(prefix="autoskill_docker_"))
        staged_skill = staging / "skill"
        shutil.copytree(skill_dir, staged_skill)
        staged_runner = staging / "runner.py"
        shutil.copyfile(RUNNER_PATH, staged_runner)
        return staged_skill, staged_runner

    def _build_cmd(self, staged_skill: Path, staged_runner: Path, container_name: str) -> list:
        return [
            self.docker_exe, "run", "--rm",
            "--name", container_name,
            "--network", "none",
            "--read-only",
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", str(self.pids_limit),
            "--security-opt", "no-new-privileges",
            "--tmpfs", "/tmp:rw,size=16m,noexec",
            "-v", f"{staged_skill}:/skill:ro",
            "-v", f"{staged_runner}:/app/runner.py:ro",
            "--entrypoint", "python",
            self.image, "-I", "-B", "/app/runner.py", "/skill",
        ]

    # ---- 执行 -----------------------------------------------------------
    def execute(self, skill_dir: Path, params: dict, timeout_s: float | None = None) -> ExecResult:
        timeout = float(timeout_s or 10.0)
        if not shutil.which(self.docker_exe):
            return self._error_result("docker CLI 不可用：请安装 Docker Desktop 并设置 AUTOSKILL_EXECUTOR=docker 前先启动守护进程")

        container_name = f"autoskill_{uuid.uuid4().hex[:12]}"
        staged_skill, staged_runner = self._stage(Path(skill_dir))
        try:
            cmd = self._build_cmd(staged_skill, staged_runner, container_name)
            stdin_payload = json.dumps({"params": params}, ensure_ascii=True).encode("utf-8")
            start = time.monotonic()
            proc = subprocess.Popen(  # noqa: S603 - 固定 docker run 命令
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            timed_out = False
            try:
                stdout_b, stderr_b = proc.communicate(input=stdin_payload, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill(proc, container_name)
                stdout_b, stderr_b = b"", b""

            duration = time.monotonic() - start
            stdout = (stdout_b or b"").decode("utf-8", errors="replace")
            stderr = (stderr_b or b"").decode("utf-8", errors="replace")

            from autoskill.schemas import parse_protocol_output

            result = ExecResult(
                exit_code=None if timed_out else proc.returncode,
                stdout=stdout, stderr=stderr, duration_s=duration, timed_out=timed_out,
            )
            result.protocol = parse_protocol_output(stdout) if not timed_out else None
            result.timeout_s = timeout
            return result
        finally:
            shutil.rmtree(staged_skill.parent, ignore_errors=True)  # 清理整个 staging

    # ---- 内部 -----------------------------------------------------------
    def _kill(self, proc: subprocess.Popen, container_name: str) -> None:
        """超时：杀 docker CLI + 终止容器（--rm 自动清理）。"""
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],  # noqa: S603, S607
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        try:
            proc.kill()
        except OSError:
            pass
        subprocess.run([self.docker_exe, "kill", container_name],  # noqa: S603, S607
                       capture_output=True, timeout=15,
                       creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)

    def _error_result(self, message: str) -> ExecResult:
        result = ExecResult(exit_code=-1, stdout="", stderr=message, duration_s=0.0)
        result.protocol = {"ok": False, "error": message}
        return result
