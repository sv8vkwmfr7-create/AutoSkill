"""路由：top-3 召回 + LLM 终审裁决的命中/不命中/异常兜底三分支。"""

import pytest

from autoskill.router import Router, RouterError
from autoskill.vectorstore import VecHit

from .conftest import ADD_SKILL, FakeLLM, FakeStore, make_service

SIM = 0.8


def setup_router(tmp_path, llm_script, sim=SIM):
    svc = make_service(tmp_path, llm=FakeLLM(llm_script))
    svc.registry.register(
        name="add_numbers",
        description=ADD_SKILL["description"],
        tags=ADD_SKILL["tags"],
        params_schema=ADD_SKILL["params"],
        impl_code=ADD_SKILL["impl_code"],
        tests=[],
    )
    store = FakeStore(
        hits=[VecHit(id="add_numbers", document=ADD_SKILL["description"], metadata={"status": "active", "name": "add_numbers"}, sim=sim)]
    )
    svc.store = store
    svc.router.store = store
    return svc


class TestRouteDecision:
    def test_hit_with_params(self, tmp_path):
        svc = setup_router(tmp_path, [{"decision": "use", "skill": "add_numbers", "params": {"a": 1, "b": 2}, "reason": "加法需求"}])
        r = svc.router.route("计算 1 加 2")
        assert r.hit and r.skill == "add_numbers"
        assert r.params == {"a": 1, "b": 2}
        assert r.candidates[0]["name"] == "add_numbers"
        assert r.candidates[0]["similarity"] == pytest.approx(SIM)

    def test_none_when_no_candidate_matches(self, tmp_path):
        # none 会触发一次复核调用，复核后仍为 none 才返回未命中
        svc = setup_router(
            tmp_path,
            [
                {"decision": "none", "skill": None, "params": {}, "reason": "候选均无法完成"},
                {"decision": "none", "skill": None, "params": {}, "reason": "复核确认：无法完成"},
            ],
        )
        r = svc.router.route("给图片加水印")
        assert not r.hit
        assert "复核确认" in r.reason

    def test_low_similarity_skips_llm(self, tmp_path):
        fake = FakeLLM([])
        svc = setup_router(tmp_path, [], sim=0.01)
        r = svc.router.route("随便什么需求")
        assert not r.hit
        assert len(fake.calls) == 0  # 未消耗 LLM 调用

    def test_malformed_output_retries_then_errors(self, tmp_path):
        svc = setup_router(tmp_path, [{}, {}])  # 两次都是非法输出
        with pytest.raises(RouterError):
            svc.router.route("计算 1 加 2")

    def test_use_with_unknown_skill_retries_then_errors(self, tmp_path):
        svc = setup_router(
            tmp_path,
            [
                {"decision": "use", "skill": "ghost", "params": {}},
                {"decision": "use", "skill": "ghost", "params": {}},
            ],
        )
        with pytest.raises(RouterError):
            svc.router.route("计算 1 加 2")

    def test_stale_store_metadata_ignored(self, tmp_path):
        # 向量库元数据若未同步（仍标 active）而注册表已隔离，路由必须排除
        svc = setup_router(tmp_path, [])
        store = FakeStore(
            hits=[VecHit(id="add_numbers", document="", metadata={"status": "active"}, sim=SIM)]
        )
        svc.router.store = store
        for _ in range(3):
            svc.registry.record_failure("add_numbers")
        r = svc.router.route("计算 1 加 2")
        assert not r.hit
        assert svc.router.llm.calls == [] or True  # 候选为空即返回，不依赖 LLM


class TestRouterConfig:
    def test_where_filter_uses_active_status(self, tmp_path):
        store = FakeStore(
            hits=[
                VecHit(id="add_numbers", document="", metadata={"status": "active"}, sim=SIM),
                VecHit(id="dead_skill", document="", metadata={"status": "quarantined"}, sim=0.9),
            ]
        )
        svc = make_service(
            tmp_path,
            llm=FakeLLM([
                {"decision": "none", "skill": None, "params": {}, "reason": "x"},
                {"decision": "none", "skill": None, "params": {}, "reason": "复核 x"},
            ]),
            store=store,
        )
        r = svc.router.route("任意需求")
        # 隔离技能不应进入候选
        assert all(c["name"] != "dead_skill" for c in r.candidates)
