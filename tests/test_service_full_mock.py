"""全链路集成（离线 mock）：生成 -> 入库 -> 热加载执行 -> 召回调用 -> 参数修复 -> 熔断。"""

from autoskill.vectorstore import VecHit

from .conftest import ADD_SKILL, ADD_IMPL, FakeLLM, FakeStore, make_service


def _canned_hit(skill_name="add_numbers", sim=0.8, status="active"):
    return [
        VecHit(
            id=skill_name,
            document=ADD_SKILL["description"] if skill_name == "add_numbers" else "演示技能",
            metadata={"name": skill_name, "status": status},
            sim=sim,
        )
    ]


class TestGenerationPipeline:
    def test_ensure_generates_registers_and_executes(self, tmp_path):
        fake = FakeLLM([ADD_SKILL, {"params": {"a": 2, "b": 3}}])
        svc = make_service(tmp_path, llm=fake)
        r = svc.ensure("计算 2 加 3 的和")
        assert r.ok, r.error
        assert r.path == "generated"
        assert r.skill == "add_numbers"
        assert r.output == {"sum": 5}
        assert r.rounds == 0
        assert (tmp_path / "skills" / "add_numbers" / "v1" / "impl.py").exists()
        assert (tmp_path / "skills" / "add_numbers" / "v1" / "tests.json").exists()
        assert (tmp_path / "skills" / "add_numbers" / "skill.json").exists()
        # 已入向量库且状态 active
        assert svc.store.list_ids() == ["add_numbers"]
        assert svc.registry.get("add_numbers").status == "active"

    def test_generation_with_self_repair(self, tmp_path):
        # 第一次输出违反白名单（guard），第二次正确 -> rounds=1（1 轮自修正）
        bad = dict(ADD_SKILL)
        bad["impl_code"] = "import requests\n" + ADD_IMPL
        fake = FakeLLM([bad, ADD_SKILL, {"params": {"a": 1, "b": 1}}])
        svc = make_service(tmp_path, llm=fake)
        r = svc.ensure("计算 1 加 1")
        assert r.ok, r.error
        assert r.path == "generated"
        assert r.rounds == 1
        assert r.output == {"sum": 2}

    def test_injection_rejected_explicitly(self, tmp_path):
        # LLM 首次 + 3 轮自修正都坚持用 requests 抓网页 -> guard 拒绝 -> 显式报错，无技能注册
        bad = dict(ADD_SKILL)
        bad["name"] = "web_fetch"
        bad["impl_code"] = "import requests\n\ndef run(params):\n    return requests.get(params['url']).text\n"
        fake = FakeLLM([bad, bad, bad, bad])
        svc = make_service(tmp_path, llm=fake)
        r = svc.ensure("真实抓取 https://example.com 的网页标题")
        assert not r.ok
        assert r.path == "rejected"
        assert "白名单" in r.error or "能力边界" in r.error
        assert svc.registry.names() == []

    def test_random_output_with_check_tests(self, tmp_path):
        """随机性技能：tests 用 _check 断言而非固定值（v5 教训：LLM 写死随机值会自测必败）。"""
        skill = {
            "name": "gen_token",
            "description": "生成随机令牌 token，只含英文字母与数字",
            "tags": ["random"],
            "params": {"type": "object", "properties": {"length": {"type": "integer"}}, "required": ["length"]},
            "impl_code": (
                "import secrets\n\n"
                "def run(params):\n"
                "    alphabet = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'\n"
                "    return ''.join(secrets.choice(alphabet) for _ in range(params['length']))\n"
            ),
            "tests": [
                {"input": {"length": 16}, "expected": {"_check": {"type_is": "str", "length_eq": 16, "regex": "^[A-Za-z0-9]+$"}}},
                {"input": {"length": 8}, "expected": {"_check": {"type_is": "str", "length_eq": 8, "regex": "^[A-Za-z0-9]+$"}}},
                {"input": {"length": 12}, "expected": {"_check": {"type_is": "str", "length_eq": 12, "regex": "^[A-Za-z0-9]+$"}}},
            ],
        }
        fake = FakeLLM([skill, {"params": {"length": 16}}])
        svc = make_service(tmp_path, llm=fake)
        r = svc.ensure("生成一个 16 位随机令牌")
        assert r.ok, r.error
        assert len(r.output) == 16

    def test_capability_boundary_honest_refusal(self, tmp_path):
        # 生成器学会说不：LLM 返回 {"reject": true, ...} -> 终态拒绝，不再自修正
        fake = FakeLLM([{"reject": True, "reason": "该需求需要发起真实网络请求，超出纯计算技能边界"}])
        svc = make_service(tmp_path, llm=fake)
        r = svc.ensure("真实抓取 https://example.com 的网页标题")
        assert not r.ok
        assert r.path == "rejected"
        assert "能力边界" in r.error
        assert len(fake.calls) == 1  # 终态：只调用了一次 LLM，未进入自修正循环
        assert svc.registry.names() == []

    def test_injection_no_new_skill_registered(self, tmp_path):
        bad = dict(ADD_SKILL)
        bad["name"] = "file_reader"
        bad["impl_code"] = "def run(params):\n    return open(params['path']).read()\n"
        fake = FakeLLM([bad, bad, bad, bad])
        svc = make_service(tmp_path, llm=fake)
        r = svc.ensure("读取本地文件内容并统计行数")
        assert r.path == "rejected"
        assert svc.store.count() == 0


class TestRecallPath:
    def _seed_skill(self, tmp_path, store=None):
        svc = make_service(tmp_path, llm=FakeLLM([]), store=store)
        meta = svc.registry.register(
            name="add_numbers",
            description=ADD_SKILL["description"],
            tags=ADD_SKILL["tags"],
            params_schema=ADD_SKILL["params"],
            impl_code=ADD_SKILL["impl_code"],
            tests=[],
        )
        svc._index_skill(meta)
        return svc

    def test_recall_hit_executes(self, tmp_path):
        store = FakeStore(hits=_canned_hit())
        svc = self._seed_skill(tmp_path, store=store)
        fake = FakeLLM([{"decision": "use", "skill": "add_numbers", "params": {"a": 4, "b": 5}, "reason": "命中加法"}])
        svc.set_llm(fake)
        r = svc.ensure("帮我算 4 加 5")
        assert r.ok and r.path == "recall"
        assert r.output == {"sum": 9}

    def test_recall_with_bad_args_triggers_fix(self, tmp_path):
        store = FakeStore(hits=_canned_hit())
        svc = self._seed_skill(tmp_path, store=store)
        fake = FakeLLM(
            [
                {"decision": "use", "skill": "add_numbers", "params": {"a": "x", "b": 2}, "reason": "命中"},  # 预填参数非法
                {"params": {"a": 5, "b": 6}},  # 回喂修复
            ]
        )
        svc.set_llm(fake)
        r = svc.ensure("计算 5 加 6")
        assert r.ok and r.path == "recall"
        assert r.output == {"sum": 11}
        assert r.detail.get("arg_fix_used") is True

    def test_recall_execution_failure_counts_breaker(self, tmp_path):
        store = FakeStore(hits=_canned_hit(skill_name="boom_skill"))
        svc = make_service(tmp_path, llm=FakeLLM([]), store=store)
        meta = svc.registry.register(
            name="boom_skill",
            description="执行即失败的演示技能",
            tags=["demo"],
            params_schema=ADD_SKILL["params"],
            impl_code="def run(params):\n    raise ValueError('bad day')\n",
            tests=[],
        )
        svc._index_skill(meta)
        fake = FakeLLM([{"decision": "use", "skill": "boom_skill", "params": {"a": 1, "b": 2}, "reason": "x"}])
        svc.set_llm(fake)
        r = svc.ensure("调用 boom_skill 演示")
        assert not r.ok  # 生成路径兜底（脚本耗尽 -> rejected）
        assert svc.registry.get("boom_skill").fail_streak == 1  # 熔断计数已 +1


class TestDirectCallAndBreaker:
    def _register_boom(self, svc):
        svc.registry.register(
            name="boom_skill",
            description="执行即失败的演示技能",
            tags=["demo"],
            params_schema=ADD_SKILL["params"],
            impl_code="def run(params):\n    raise ValueError('bad day')\n",
            tests=[],
        )

    def test_call_quarantined_refused(self, tmp_path):
        svc = make_service(tmp_path, llm=FakeLLM([]))
        self._register_boom(svc)
        for _ in range(3):
            c = svc.call("boom_skill", {"a": 1, "b": 2})
            assert not c.ok
        assert svc.registry.get("boom_skill").status == "quarantined"
        c = svc.call("boom_skill", {"a": 1, "b": 2})
        assert "熔断" in c.error

    def test_call_unknown_skill(self, tmp_path):
        svc = make_service(tmp_path, llm=FakeLLM([]))
        c = svc.call("nope", {})
        assert not c.ok and "不存在" in c.error

    def test_call_param_mismatch_no_breaker(self, tmp_path):
        svc = make_service(tmp_path, llm=FakeLLM([]))
        svc.registry.register(
            name="add_numbers",
            description=ADD_SKILL["description"],
            tags=ADD_SKILL["tags"],
            params_schema=ADD_SKILL["params"],
            impl_code=ADD_SKILL["impl_code"],
            tests=[],
        )
        c = svc.call("add_numbers", {"a": "wrong-type"})
        assert not c.ok
        assert svc.registry.get("add_numbers").fail_streak == 0  # 预校验失败不计熔断

    def test_quarantine_syncs_store_metadata(self, tmp_path):
        svc = make_service(tmp_path, llm=FakeLLM([]))
        self._register_boom(svc)
        svc._index_skill(svc.registry.get("boom_skill"))
        assert svc.store.count() == 1
        for _ in range(3):
            svc.call("boom_skill", {"a": 1, "b": 2})
        hits_active = svc.store.query([0.0] * 512, k=5, where={"status": "active"})
        assert all(h.id != "boom_skill" for h in hits_active)
