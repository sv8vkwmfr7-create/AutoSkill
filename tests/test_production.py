"""工程化测试：启动校验、事件审计轨迹、并发生成锁、API 端点。"""

import json
import threading

import pytest
from fastapi.testclient import TestClient

from autoskill.config import Settings
from autoskill.service import AutoSkillService

from .conftest import ADD_SKILL, FakeLLM, FakeStore, make_service


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
        assert all(isinstance(json.loads(line), dict) for line in lines)

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


class TestShadowGate:
    """升级/修复的影子验证门禁：候选通过真实参数执行才热替换，失败自动回滚。"""

    def _quarantined_skill(self, svc, name="repair_me"):
        svc.registry.register(
            name=name,
            description="把字符串重复 repeat 次的演示技能",
            tags=["string"],
            params_schema={"type": "object", "properties": {"text": {"type": "string"}, "repeat": {"type": "integer"}},
                           "required": ["text", "repeat"]},
            impl_code="def run(params):\n    raise ValueError('v1 broken')\n",
            tests=[],
        )
        svc._index_skill(svc.registry.get(name))
        for _ in range(3):
            svc.registry.record_failure(name)
        return name

    V2_GOOD = {
        "name": "repair_me",
        "description": "把字符串重复 repeat 次的演示技能",
        "tags": ["string"],
        "params": {"type": "object", "properties": {"text": {"type": "string"}, "repeat": {"type": "integer"}},
                   "required": ["text", "repeat"]},
        "impl_code": "def run(params):\n    return params['text'] * params['repeat']\n",
        "tests": [
            {"input": {"text": "ab", "repeat": 3}, "expected": "ababab"},
            {"input": {"text": "x", "repeat": 1}, "expected": "x"},
            {"input": {"text": "", "repeat": 4}, "expected": ""},
        ],
    }

    def test_repair_shadow_promote(self, tmp_path):
        svc = make_service(tmp_path, llm=FakeLLM([]))
        name = self._quarantined_skill(svc)
        from autoskill.vectorstore import VecHit

        svc.router.store = FakeStore(hits=[VecHit(id=name, document="", metadata={"status": "quarantined"}, sim=0.3)])
        fake = FakeLLM([self.V2_GOOD, {"params": {"text": "ab", "repeat": 3}}])
        svc.set_llm(fake)
        r = svc.ensure("把字符串 ab 重复 3 次")
        assert r.ok, r.error
        assert r.output == "ababab"
        meta = svc.registry.get(name)
        assert meta.version == 2 and meta.status == "active"
        assert r.detail.get("shadow_gate") is True

    def test_repair_shadow_rollback(self, tmp_path):
        """候选 v2 通过自测、但对真实需求输入失败 -> 影子门禁拦截，不晋升，自动回滚保持 v1 隔离。"""
        svc = make_service(tmp_path, llm=FakeLLM([]))
        name = self._quarantined_skill(svc)
        from autoskill.vectorstore import VecHit

        svc.router.store = FakeStore(hits=[VecHit(id=name, document="", metadata={"status": "quarantined"}, sim=0.3)])
        bad = dict(self.V2_GOOD)
        # 通过自身 3 组自测（ab/3, x/1, ""/4），但对真实输入 text="xy" 抛错
        bad["impl_code"] = (
            "def run(params):\n"
            "    if params['text'] == 'xy':\n"
            "        raise ValueError('v2 bug on xy')\n"
            "    return params['text'] * params['repeat']\n"
        )
        fake = FakeLLM([bad, {"params": {"text": "xy", "repeat": 2}}])
        svc.set_llm(fake)
        r = svc.ensure("把字符串 xy 重复 2 次")
        assert not r.ok
        assert "回滚" in r.error
        meta = svc.registry.get(name)
        assert meta.version == 1 and meta.status == "quarantined"  # 自动回滚生效
        assert r.detail.get("shadow_rollback") is True

    def test_route_cache_hit_and_invalidate(self, tmp_path):
        """相同需求二次路由走缓存（不消耗 LLM 调用）；池变更后缓存失效。"""
        decision = {"decision": "use", "skill": "add_numbers", "params": {"a": 1, "b": 2}, "reason": "命中"}
        fake = FakeLLM([], default=decision)
        svc = make_service(tmp_path, llm=fake)
        svc.registry.register(
            name="add_numbers",
            description=ADD_SKILL["description"],
            tags=ADD_SKILL["tags"],
            params_schema=ADD_SKILL["params"],
            impl_code=ADD_SKILL["impl_code"],
            tests=[],
        )
        svc._index_skill(svc.registry.get("add_numbers"))
        r1 = svc.router.route("计算 1 加 2")
        assert r1.hit
        assert len(fake.calls) == 1
        r2 = svc.router.route("计算 1 加 2")
        assert r2.hit and r2.skill == "add_numbers"
        assert len(fake.calls) == 1  # 第二次走缓存，未消耗 LLM 调用
        svc._index_skill(svc.registry.get("add_numbers"))  # 池变更
        svc.router.route("计算 1 加 2")
        assert len(fake.calls) == 2  # 缓存已失效，重新裁决


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
