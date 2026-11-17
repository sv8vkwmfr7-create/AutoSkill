"""熔断状态机：连续失败 -> quarantined -> v2 注册热替换回 active；重启后状态保持。"""

from autoskill.registry import SkillRegistry
from autoskill.schemas import ACTIVE, QUARANTINED

from .conftest import TRIVIAL_IMPL

SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}


def make_registry(tmp_path):
    reg = SkillRegistry(tmp_path / "skills", breaker_threshold=3)
    reg.register(
        name="calc_demo",
        description="演示技能",
        tags=["demo"],
        params_schema=SCHEMA,
        impl_code=TRIVIAL_IMPL,
        tests=[],
    )
    return reg


class TestBreaker:
    def test_failures_below_threshold(self, tmp_path):
        reg = make_registry(tmp_path)
        reg.record_failure("calc_demo")
        reg.record_failure("calc_demo")
        assert reg.get("calc_demo").status == ACTIVE
        assert reg.get("calc_demo").fail_streak == 2

    def test_success_resets_streak(self, tmp_path):
        reg = make_registry(tmp_path)
        reg.record_failure("calc_demo")
        reg.record_failure("calc_demo")
        reg.record_success("calc_demo")
        assert reg.get("calc_demo").fail_streak == 0
        assert reg.get("calc_demo").status == ACTIVE

    def test_threshold_trips_quarantine(self, tmp_path):
        reg = make_registry(tmp_path)
        for _ in range(3):
            meta, quarantined_now = reg.record_failure("calc_demo")
        assert quarantined_now is True
        assert reg.get("calc_demo").status == QUARANTINED
        assert reg.get("calc_demo").fail_streak == 3

    def test_quarantined_excluded_from_active_list(self, tmp_path):
        reg = make_registry(tmp_path)
        for _ in range(3):
            reg.record_failure("calc_demo")
        assert [m.name for m in reg.list(status=ACTIVE)] == []

    def test_record_failure_keeps_quarantine(self, tmp_path):
        reg = make_registry(tmp_path)
        for _ in range(3):
            reg.record_failure("calc_demo")
        meta, quarantined_now = reg.record_failure("calc_demo")
        assert quarantined_now is False
        assert meta.status == QUARANTINED

    def test_v2_registration_reactivates(self, tmp_path):
        reg = make_registry(tmp_path)
        for _ in range(3):
            reg.record_failure("calc_demo")
        meta = reg.register(
            name="calc_demo",
            description="演示技能 v2",
            tags=["demo"],
            params_schema=SCHEMA,
            impl_code=TRIVIAL_IMPL,
            tests=[],
            note="auto-repair v2",
        )
        assert meta.version == 2
        assert meta.status == ACTIVE
        assert meta.fail_streak == 0
        assert len(meta.history) == 2


class TestPersistence:
    def test_status_survives_rescan(self, tmp_path):
        reg = make_registry(tmp_path)
        for _ in range(3):
            reg.record_failure("calc_demo")
        reg2 = SkillRegistry(tmp_path / "skills", breaker_threshold=3)
        assert reg2.get("calc_demo").status == QUARANTINED
        assert reg2.get("calc_demo").fail_streak == 3

    def test_tool_specs_only_active(self, tmp_path):
        reg = make_registry(tmp_path)
        specs = reg.tool_specs()
        assert len(specs) == 1 and specs[0]["function"]["name"] == "calc_demo"
        for _ in range(3):
            reg.record_failure("calc_demo")
        assert reg.tool_specs() == []
