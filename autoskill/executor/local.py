"""本地子进程执行器（dev-only，非安全沙箱）。

隔离手段（纵深防御，各层独立生效）：
- python -I -B 隔离解释器（忽略 PYTHON* 环境变量、不写字节码缓存）
- runner 内受限内建命名空间 + PEP 578 审计沙箱（见 runner.py）
- 内存看门狗：轮询子进程 commit 内存，超限杀整棵进程树（任何环境可用）
- Windows Job Object：内存/进程数上限 + kill-on-close（在有 Job 的桌面环境生效；
  若当前进程已属于其他 Job —— 如被 IDE/Agent 沙箱托管 —— 分配会失败并静默降级）
- 超时杀整棵进程树
生产级隔离由 DockerExecutor 提供（已实装，装 Docker 后切换）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from .base import ExecResult

RUNNER_PATH = Path(__file__).resolve().parent.parent / "runner.py"
CREATE_NO_WINDOW = 0x08000000  # Windows：不闪黑窗
_POLL_INTERVAL_S = 0.05


def _resolve_real_python(exe: str) -> str:
    """uv/venv 的 python.exe 可能是 trampoline 跳板（真实解释器是其子进程），
    解析 pyvenv.cfg 找到基础解释器直接启动——runner 已零第三方依赖，裸 python 即可。
    """
    try:
        p = Path(exe)
        for cfg_dir in (p.parent, p.parent.parent):  # pyvenv.cfg 在 venv 根（Scripts 的上一级）
            cfg = cfg_dir / "pyvenv.cfg"
            if not cfg.exists():
                continue
            for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.lower().startswith("home"):
                    home = Path(line.split("=", 1)[1].strip().strip('"'))
                    candidate = home / "python.exe"
                    if candidate.exists():
                        return str(candidate)
    except Exception:  # noqa: BLE001
        pass
    return exe


def _private_mb(pid: int) -> float | None:
    """读取单个进程 commit 内存（PrivateUsage，MB）。失败返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            counters = PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return None
            return counters.PrivateUsage / (1024 * 1024)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


def _descendant_pids(root_pid: int) -> set:
    """Toolhelp32 快照枚举全部进程，取 root_pid 的全部子孙 pid。"""
    if sys.platform != "win32":
        return set()
    try:
        import ctypes

        TH32CS_SNAPPROCESS = 0x2

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_ulong),
                ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", ctypes.c_ulong),
                ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
        kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap:
            return set()
        ppid_map: dict = {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                ppid_map[entry.th32ProcessID] = entry.th32ParentProcessID
                ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)
        descendants = set()
        frontier = [root_pid]
        while frontier:
            pid = frontier.pop()
            for child, parent in ppid_map.items():
                if parent == pid and child not in descendants:
                    descendants.add(child)
                    frontier.append(child)
        return descendants
    except Exception:  # noqa: BLE001
        return set()


def _tree_commit_mb(root_pid: int) -> float | None:
    """聚合 root_pid 及其全部子孙进程的 commit 内存（MB）。失败返回 None。"""
    total = _private_mb(root_pid)
    if total is None:
        return None
    for pid in _descendant_pids(root_pid):
        mb = _private_mb(pid)
        if mb:
            total += mb
    return total


class _WindowsJobObject:
    """Windows Job Object 资源围栏：内存/进程数上限 + 句柄关闭即全树终止。

    局限：进程只能属于一个 Job——若本进程已被外部 Job 托管（IDE/Agent 沙箱），
    Assign 会失败并静默降级（此时由内存看门狗兜底）。
    """

    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    LIMIT_PROCESS_MEMORY = 0x100
    LIMIT_ACTIVE_PROCESS = 0x8
    LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    def __init__(self, memory_mb: int, max_processes: int):
        self.handle = None
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            # 64 位下必须显式声明 HANDLE 返回类型，否则句柄被截断为无效值
            kernel32.CreateJobObjectW.restype = ctypes.c_void_p
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
            kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("LimitFlags", wintypes.DWORD),
                    # x64 实测内核要求 BASIC=64 字节（EXT=144），尾部 8 字节为头文件保留区
                    ("_reserved", ctypes.c_uint64),
                ]

            class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            limits = EXTENDED_LIMIT_INFORMATION()
            limits.BasicLimitInformation.LimitFlags = (
                self.LIMIT_PROCESS_MEMORY | self.LIMIT_ACTIVE_PROCESS | self.LIMIT_KILL_ON_JOB_CLOSE
            )
            limits.ProcessMemoryLimit = memory_mb * 1024 * 1024
            limits.BasicLimitInformation.ActiveProcessLimit = max_processes
            if not kernel32.SetInformationJobObject(
                handle, self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits), ctypes.sizeof(limits),
            ):
                kernel32.CloseHandle(handle)
                return
            self.handle = handle
            self._kernel32 = kernel32
        except Exception:  # noqa: BLE001 - 资源围栏失败不阻塞执行
            self.handle = None

    def assign(self, process) -> bool:
        """把子进程（含其子孙）加入围栏。"""
        if not self.handle:
            return False
        try:
            return bool(self._kernel32.AssignProcessToJobObject(self.handle, int(process._handle)))
        except Exception:  # noqa: BLE001
            return False

    def close(self, terminate: bool) -> None:
        if not self.handle:
            return
        try:
            if terminate:
                self._kernel32.TerminateJobObject(self.handle, 0)
            self._kernel32.CloseHandle(self.handle)
        except Exception:  # noqa: BLE001
            pass
        self.handle = None


class LocalSubprocessExecutor:
    name = "local"

    def __init__(self, python_exe: str | None = None, memory_mb: int = 256, max_processes: int = 32,
                 egress_hosts: list[str] | None = None):
        # 解析 trampoline：uv venv 的 python.exe 是跳板，直接启动真实解释器
        self.python_exe = _resolve_real_python(python_exe or sys.executable)
        self.memory_mb = memory_mb
        self.max_processes = max_processes
        self.egress_hosts = [h.lower() for h in (egress_hosts or [])]

    def healthcheck(self) -> str:
        return (
            f"LocalSubprocessExecutor(python={self.python_exe}) dev-only | "
            f"受限内建+审计沙箱(PEP578)+内存看门狗({self.memory_mb}MB)+JobObject"
            + (f"+egress({','.join(self.egress_hosts)})" if self.egress_hosts else "")
        )

    def execute(self, skill_dir: Path, params: dict, timeout_s: float | None = None) -> ExecResult:
        timeout = float(timeout_s or 10.0)
        cmd = [self.python_exe, "-I", "-B", str(RUNNER_PATH), str(skill_dir)]
        if self.egress_hosts:
            cmd += ["--egress", ",".join(self.egress_hosts)]
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
        job = _WindowsJobObject(self.memory_mb, self.max_processes)
        job_assigned = job.assign(proc)

        timed_out = False
        memory_limit_hit = False
        holder: dict = {}

        def _communicate():
            try:
                holder["io"] = proc.communicate(input=stdin_payload)
            except Exception as e:  # noqa: BLE001
                holder["error"] = e

        reader = threading.Thread(target=_communicate, daemon=True)
        reader.start()
        while reader.is_alive():
            reader.join(timeout=_POLL_INTERVAL_S)
            if not reader.is_alive():
                break
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                timed_out = True
                self._kill_tree(proc)
                break
            commit_mb = _tree_commit_mb(proc.pid)
            if commit_mb is not None and commit_mb > self.memory_mb:
                memory_limit_hit = True
                self._kill_tree(proc)
                break
        reader.join(timeout=5)
        stdout_b, stderr_b = holder.get("io", (b"", b""))
        job.close(terminate=timed_out or memory_limit_hit)

        duration = time.monotonic() - start
        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")

        from autoskill.schemas import parse_protocol_output

        result = ExecResult(
            exit_code=None if (timed_out or memory_limit_hit) else proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_s=duration,
            timed_out=timed_out,
            memory_limit_hit=memory_limit_hit,
        )
        result.protocol = parse_protocol_output(stdout) if not (timed_out or memory_limit_hit) else None
        result.timeout_s = timeout  # 供 error 描述使用
        result.memory_limit_mb = self.memory_mb
        result.job_assigned = job_assigned
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
