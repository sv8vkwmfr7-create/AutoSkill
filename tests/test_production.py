"""工程化测试：启动校验、事件审计轨迹、并发生成锁、API 端点。"""

import json
import threading

import pytest
from fastapi.testclient import TestClient

from autoskill.config import Settings
from autoskill.service import AutoSkillService

from .conftest import ADD_SKILL, FakeLLM, make_service


class TestStartupValidation:
    def test_missing_llm_config_fails_fast(self, tmp_path):
        s = Settings()
        s.skills_dir = tmp_path / "skills"
        s.data_dir = tmp_path / "data"
        s.llm_base_url = ""
        s.llm_api_key = ""
        with pytest.raises(RuntimeError) as ei:
            AutoSkillService(s)
        assert "AUTOSKILL_LLM_API_KEY" in str(ei.value)  # 可读错误而非运行期神秘失败

    def test_partial_config_fails_fast(self, tmp_path):
        s = Settings()
        s.skills_dir = tmp_path / "skills"
        s.data_dir = tmp_path / "data"
        s.llm_base_url = "http://localhost:9"
        s.llm_api_key = ""
        with pytest.raises(RuntimeError):
            AutoSkillService(s)


class TestEventLog:
    def test_ensure_and_call_logged(self, tmp_path):
        fake = FakeLLM([ADD_SKILL, {"params": {"a": 2, "b": 3}}])
        svc = make_service(tmp_path, llm=fake)
        svc.ensure("计算 2 加 3 的和")
        svc.call("add_numbers", {"a": 1, "b": 1})
        events = svc.events()
        kinds = [e["kind"] for e in events]
        assert "ensure" in kinds and "call" in kinds
        ensure_evt = next(e for e in events if e["kind"] == "ensure")
        assert ensure_evt["ok"] is True and ensure_evt["path"] == "generated"
        call_evt = next(e for e in events if e["kind"] == "call")
        assert call_evt["ok"] is True
        # JSONL 文件可解析
        lines = (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert all(isinstance(json.loads(l), dict) for l in lines)

    def test_failure_logged_with_error(self, tmp_path):
        svc = make_service(tmp_path, llm=FakeLLM([]))
        r = svc.call("nope", {})
        assert not r.ok
        events = svc.events()
        assert any(e["kind"] == "call" and e["ok"] is False for e in events)


class TestConcurrentGeneration:
    def test_concurrent_ensure_generates_once(self, tmp_path):
        """并发 ensure 同一意图：全局生成锁保证只生成一次（不重复注册）。"""
        decision = {"decision": "use", "skill": "repeat_text", "params": {"text": "ab", "repeat": 3}}
        fake = FakeLLM([], default=decision)
        fake.script = [
            {
                "name": "repeat_text",
                "description": "把字符串重复 repeat 次并返回 repeat_text 结果",
                "tags": ["string"],
                "params": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}, "repeat": {"type": "integer"}},
                    "required": ["text", "repeat"],
                },
                "impl_code": "def run(params):\n    return params['text'] * params['repeat']\n",
                "tests": [
                    {"input": {"text": "ab", "repeat": 3}, "expected": "ababab"},
                    {"input": {"text": "x", "repeat": 1}, "expected": "x"},
                    {"input": {"text": "", "repeat": 4}, "expected": ""},
                ],
            },
            {"params": {"text": "ab", "repeat": 3}},
        ]
        svc = make_service(tmp_path, llm=fake)

        results = []
        errors = []
        barrier = threading.Barrier(3)

        def worker():
            barrier.wait()
            try:
                results.append(svc.ensure("把字符串 ab 重复 3 次"))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert not errors, errors
        assert len(results) == 3
        assert all(r.ok for r in results), [r.error for r in results]
        assert all(r.output == "ababab" for r in results)
        # 生成锁保证：同名技能只注册一次（无重复生成）
        metas = [m for m in svc.registry.all().values() if m.name == "repeat_text"]
        assert len(metas) == 1 and metas[0].version == 1
        assert len(svc.registry.names()) == 1

    def test_concurrent_call_breaker_consistent(self, tmp_path):
        """并发调用失败技能：熔断计数准确（无竞态丢失）。"""
        svc = make_service(tmp_path, llm=FakeLLM([]))
        svc.registry.register(
            name="boom",
            description="执行即失败的演示技能",
            tags=["demo"],
            params_schema={"type": "object"},
            impl_code="def run(params):\n    raise ValueError('bad')\n",
            tests=[],
        )
        threads = [threading.Thread(target=lambda: svc.call("boom", {})) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert svc.registry.get("boom").fail_streak == 6
        assert svc.registry.get("boom").status == "quarantined"


class TestAPI:
    def _make_app(self, tmp_path, fake=None):
        svc = make_service(tmp_path, llm=fake or FakeLLM([]))
        from autoskill.api import create_app

        return TestClient(create_app(service=svc)), svc

    def test_health(self, tmp_path):
        client, _ = self._make_app(tmp_path)
        r = client.get("/health")
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "ok" and d["rebuild"]["skills"] == 0

    def test_ensure_generated_via_api(self, tmp_path):
        fake = FakeLLM([ADD_SKILL, {"params": {"a": 2, "b": 3}}])
        client, _ = self._make_app(tmp_path, fake=fake)
        r = client.post("/ensure", json={"requirement": "计算 2 加 3 的和"})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True and d["path"] == "generated" and d["output"] == {"sum": 5}

    def test_call_unknown_skill_is_structured(self, tmp_path):
        client, _ = self._make_app(tmp_path)
        r = client.post("/skills/nope/call", json={"params": {}})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is False and "不存在" in d["error"]

    def test_skill_detail_404(self, tmp_path):
        client, _ = self._make_app(tmp_path)
        assert client.get("/skills/nope").status_code == 404
        assert client.get("/skills").status_code == 200

    def test_events_endpoint(self, tmp_path):
        fake = FakeLLM([ADD_SKILL, {"params": {"a": 2, "b": 3}}])
        client, svc = self._make_app(tmp_path, fake=fake)
        client.post("/ensure", json={"requirement": "计算 2 加 3 的和"})
        r = client.get("/events?limit=10")
        assert r.status_code == 200
        assert any(e["kind"] in ("ensure", "rebuild") for e in r.json())

    def test_empty_requirement_structured(self, tmp_path):
        client, _ = self._make_app(tmp_path)
        # pydantic min_length 校验 -> 422；空白串绕过校验 -> 结构化拒绝
        r = client.post("/ensure", json={"requirement": "   "})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is False and d["path"] == "rejected"
