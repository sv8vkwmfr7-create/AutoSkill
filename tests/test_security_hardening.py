"""安全加固测试：静态层收紧 + 运行时审计沙箱 + 资源围栏 + Docker 执行器。

核心实证：内建函数别名（f = open 后经 f(...) 调用）可绕过静态调用点检查，
但 runner 的 PEP 578 审计沙箱在运行时将其拒绝——两层防御独立生效。
"""

import json
import sys

import pytest

from autoskill.ast_guard import check_code
from autoskill.executor import DockerExecutor, LocalSubprocessExecutor

SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}, "x": {"type": "integer"}}, "required": ["path"]}


def write_skill(tmp_path, code, schema=SCHEMA):
    d = tmp_path / "hostile_skill"
    (d / "v1").mkdir(parents=True)
    (d / "skill.json").write_text(
        json.dumps({"name": "hostile_skill", "version": 1, "params_schema": schema}, ensure_ascii=False),
        encoding="utf-8",
    )
    (d / "v1" / "impl.py").write_text(code, encoding="utf-8")
    return d


@pytest.fixture()
def executor():
    return LocalSubprocessExecutor()


class TestStaticLayer:
    def test_builtin_alias_banned(self):
        assert not check_code("f = open\n\ndef run(params):\n    return f(params['path'])\n").ok

    def test_getattr_family_banned(self):
        for call in ("getattr", "setattr", "delattr", "hasattr"):
            code = f"def run(params):\n    return {call}(params, 'k')\n"
            assert not check_code(code).ok, call
            assert any("内置函数" in v for v in check_code(code).violations)

    def test_dunder_string_constant_banned(self):
        code = "def run(params):\n    return getattr  # 占位\n"
        code = "def run(params):\n    return 0\n\n\ndef other(o):\n    t = '__class__'\n    return t\n"
        report = check_code(code)
        assert not report.ok
        assert any("双下划线字符串常量" in v for v in report.violations)

    def test_docstring_mentions_are_allowed(self):
        code = 'def run(params):\n    """避免 __class__ 这类写法。"""\n    return 1\n'
        assert check_code(code).ok

    def test_container_alias_still_passes_static_runtime_will_catch(self):
        # 列表别名是静态分析的已知盲区 -> 由运行时层兜住（见 TestRuntimeLayer）
        code = "items = [open]\n\n\ndef run(params):\n    return items[0](params['path']).read()\n"
        assert check_code(code).ok


class TestRuntimeLayer:
    def test_alias_open_denied_at_runtime(self, executor, tmp_path):
        """静态层放行的别名攻击被运行时拒绝（受限命名空间中 open 不存在）。"""
        code = "items = [open]\n\n\ndef run(params):\n    return items[0](params['path']).read()\n"
        d = write_skill(tmp_path, code)
        r = executor.execute(d, {"path": str(tmp_path / "secret.txt")})
        assert not r.ok
        assert "安全" in r.error or "not defined" in r.error

    def test_dynamic_import_denied(self, executor, tmp_path):
        # 绕过静态层的手写 impl：直接 import socket -> 运行时拒绝
        d = write_skill(tmp_path, "import socket\n\n\ndef run(params):\n    return 1\n", schema={"type": "object"})
        r = executor.execute(d, {})
        assert not r.ok
        assert "安全" in r.error and "socket" in r.error

    def test_file_write_denied(self, executor, tmp_path):
        # open 别名写文件：受限命名空间中 open 不存在，显式失败
        code = "opens = [open]\n\n\ndef run(params):\n    opens[0](params['path'], 'w').write('x')\n    return 0\n"
        d = write_skill(tmp_path, code)
        r = executor.execute(d, {"path": str(tmp_path / "evil.txt")})
        assert not r.ok
        assert "安全" in r.error or "not defined" in r.error

    def test_eval_exec_denied_at_runtime(self, executor, tmp_path):
        code = "def run(params):\n    return {'v': eval(params['path'])}\n"  # 静态层已拦 eval；双保险
        d = write_skill(tmp_path, code)
        r = executor.execute(d, {"path": "1+1"})
        assert not r.ok
        assert "安全" in r.error or "not defined" in r.error

    def test_read_outside_allowed_roots_denied(self, executor, tmp_path):
        # 别名 open 只能读技能目录/解释器目录内的文件，外部路径一律拒绝
        code = "opens = [open]\n\n\ndef run(params):\n    return opens[0](params['path']).read()\n"
        d = write_skill(tmp_path, code)
        r = executor.execute(d, {"path": str(tmp_path.parent / "outside.txt")})
        assert not r.ok
        assert "安全" in r.error or "not defined" in r.error

    def test_normal_skill_unaffected(self, executor, tmp_path):
        d = write_skill(tmp_path, "import hashlib\n\n\ndef run(params):\n    return hashlib.md5(params['path'].encode()).hexdigest()\n")
        r = executor.execute(d, {"path": "hello"})
        assert r.ok and r.data == "5d41402abc4b2a76b9719d911017c592"

    def test_memory_bomb_contained(self, executor, tmp_path):
        """内存炸弹：看门狗/Job Object 在限额（256MB）内终止，显式失败且不拖垮主进程。"""
        code = "def run(params):\n    buf = bytearray(8 * 1024 * 1024 * 1024)\n    return {'len': len(buf)}\n"
        d = write_skill(tmp_path, code, schema={"type": "object"})
        r = executor.execute(d, {}, timeout_s=10.0)
        assert not r.ok
        assert r.memory_limit_hit or "MemoryError" in (r.error or "")
        assert r.duration_s < 10.0


class TestDockerExecutor:
    def test_build_cmd_security_flags(self, tmp_path):
        ex = DockerExecutor(image="python:3.14-slim", memory="256m", cpus="0.5", pids_limit=64)
        cmd = ex._build_cmd(tmp_path / "skill", tmp_path / "runner.py", "autoskill_test")
        joined = " ".join(cmd)
        for flag in ("--network none", "--read-only", "--memory 256m", "--cpus 0.5",
                     "--pids-limit 64", "no-new-privileges", ":/skill:ro", ":/app/runner.py:ro"):
            assert flag in joined, flag

    def test_stage_copies_skill_and_runner(self, tmp_path):
        ex = DockerExecutor()
        d = write_skill(tmp_path, "def run(params):\n    return 0\n")
        staged_skill, staged_runner = ex._stage(d)
        assert (staged_skill / "skill.json").exists()
        assert staged_runner.name == "runner.py" and staged_runner.exists()

    def test_execute_without_docker_explicit_error(self, tmp_path):
        if sys.platform != "win32":
            pytest.skip("Windows 环境专用（本机未装 docker）")
        ex = DockerExecutor()
        if ex.healthcheck().find("daemon=") != -1:
            pytest.skip("本机已装 docker，跳过无 docker 分支")
        d = write_skill(tmp_path, "def run(params):\n    return 0\n", schema={"type": "object"})
        r = ex.execute(d, {})
        assert not r.ok
        assert "docker" in r.error
