"""本地子进程执行器：协议通信、schema 拦截、异常转译、超时杀树、print 污染防护。"""

import json
import sys

import pytest

from autoskill.executor import LocalSubprocessExecutor

SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}


def write_skill(tmp_path, code, schema=SCHEMA):
    d = tmp_path / "demo_skill"
    (d / "v1").mkdir(parents=True)
    (d / "skill.json").write_text(
        json.dumps({"name": "demo_skill", "version": 1, "params_schema": schema}, ensure_ascii=False),
        encoding="utf-8",
    )
    (d / "v1" / "impl.py").write_text(code, encoding="utf-8")
    return d


@pytest.fixture()
def executor():
    return LocalSubprocessExecutor()


class TestSuccess:
    def test_echo(self, executor, tmp_path):
        d = write_skill(tmp_path, "def run(params):\n    return {'echo': params['text']}\n")
        r = executor.execute(d, {"text": "你好 world"})
        assert r.ok
        assert r.data == {"echo": "你好 world"}
        assert r.exit_code == 0

    def test_print_pollution_isolated(self, executor, tmp_path):
        code = "def run(params):\n    print('垃圾输出 line1')\n    print({'fake': 'protocol'})\n    return {'v': 1}\n"
        d = write_skill(tmp_path, code)
        r = executor.execute(d, {"text": "x"})
        assert r.ok
        assert r.data == {"v": 1}  # 协议取最后一行，不受 print 干扰
        assert "垃圾输出" in r.protocol.get("log", "")

    def test_import_whitelisted_module(self, executor, tmp_path):
        code = "import hashlib\n\ndef run(params):\n    return hashlib.md5(params['text'].encode()).hexdigest()\n"
        d = write_skill(tmp_path, code)
        r = executor.execute(d, {"text": "hello"})
        assert r.ok and r.data == "5d41402abc4b2a76b9719d911017c592"


class TestFailures:
    def test_schema_reject(self, executor, tmp_path):
        d = write_skill(tmp_path, "def run(params):\n    return {}\n")
        r = executor.execute(d, {"text": 123})
        assert not r.ok
        assert "JSON Schema" in r.error

    def test_skill_exception_becomes_protocol_error(self, executor, tmp_path):
        d = write_skill(tmp_path, "def run(params):\n    raise ValueError('boom 中文')\n")
        r = executor.execute(d, {"text": "x"})
        assert not r.ok
        assert "ValueError" in r.error and "boom" in r.error

    def test_import_time_crash(self, executor, tmp_path):
        d = write_skill(tmp_path, "raise SystemExit(2)\n")
        r = executor.execute(d, {"text": "x"})
        assert not r.ok
        assert "导入失败" in r.error

    def test_missing_impl(self, executor, tmp_path):
        d = tmp_path / "empty_skill"
        d.mkdir()
        (d / "skill.json").write_text(json.dumps({"name": "empty_skill", "version": 1, "params_schema": SCHEMA}), encoding="utf-8")
        r = executor.execute(d, {"text": "x"})
        assert not r.ok
        assert "impl 不存在" in r.error

    def test_timeout_kills_process_tree(self, executor, tmp_path):
        code = "import time\n\ndef run(params):\n    time.sleep(30)\n    return {}\n"
        d = write_skill(tmp_path, code)
        r = executor.execute(d, {"text": "x"}, timeout_s=1.5)
        assert r.timed_out
        assert 1.0 <= r.duration_s < 8.0  # 超时即杀，不会等满 30s
        assert not r.ok
