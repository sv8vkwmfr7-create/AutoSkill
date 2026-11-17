"""重启重建：注册表磁盘重建 + 向量库对账（upsert 全量、清理孤儿、状态同步）。"""

from autoskill.registry import SkillRegistry

from .conftest import ADD_SKILL, TRIVIAL_IMPL, make_service

SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}


class TestRebuild:
    def test_rebuild_upserts_and_removes_orphans(self, tmp_path):
        svc = make_service(tmp_path)
        svc.registry.register(
            name="add_numbers",
            description=ADD_SKILL["description"],
            tags=ADD_SKILL["tags"],
            params_schema=ADD_SKILL["params"],
            impl_code=ADD_SKILL["impl_code"],
            tests=[],
        )
        svc.registry.register(
            name="trivial",
            description="回显参数的最小演示技能",
            tags=["demo"],
            params_schema=SCHEMA,
            impl_code=TRIVIAL_IMPL,
            tests=[],
        )
        svc.store.add(["ghost_skill"], [[0.1] * 512], ["孤儿文档"], [{"name": "ghost_skill", "status": "active"}])

        report = svc.rebuild()
        assert report["skills"] == 2
        assert report["indexed"] == 2
        assert report["removed_orphans"] == 1
        assert sorted(svc.store.list_ids()) == ["add_numbers", "trivial"]

    def test_quarantined_status_synced_to_store(self, tmp_path):
        svc = make_service(tmp_path)
        svc.registry.register(
            name="trivial",
            description="回显参数的最小演示技能",
            tags=["demo"],
            params_schema=SCHEMA,
            impl_code=TRIVIAL_IMPL,
            tests=[],
        )
        svc._index_skill(svc.registry.get("trivial"))
        for _ in range(3):
            svc.registry.record_failure("trivial")
        svc._index_skill(svc.registry.get("trivial"))

        hits_active = svc.store.query([0.0] * 512, k=5, where={"status": "active"})
        assert all(h.id != "trivial" for h in hits_active)
        hits_any = svc.store.query([0.0] * 512, k=5, where=None)
        assert any(h.id == "trivial" and h.metadata["status"] == "quarantined" for h in hits_any)

    def test_rescan_preserves_versions(self, tmp_path):
        svc = make_service(tmp_path)
        svc.registry.register(
            name="trivial",
            description="回显参数的最小演示技能",
            tags=["demo"],
            params_schema=SCHEMA,
            impl_code=TRIVIAL_IMPL,
            tests=[],
        )
        svc.registry.register(
            name="trivial",
            description="回显参数的最小演示技能",
            tags=["demo"],
            params_schema=SCHEMA,
            impl_code=TRIVIAL_IMPL,
            tests=[],
            note="upgrade",
        )
        reg2 = SkillRegistry(tmp_path / "skills", breaker_threshold=3)
        assert reg2.get("trivial").version == 2
        assert (tmp_path / "skills" / "trivial" / "v1" / "impl.py").exists()
        assert (tmp_path / "skills" / "trivial" / "v2" / "impl.py").exists()

    def test_corrupted_dir_skipped(self, tmp_path):
        svc = make_service(tmp_path)
        bad = tmp_path / "skills" / "bad_skill"
        bad.mkdir(parents=True)
        (bad / "skill.json").write_text("{not json", encoding="utf-8")
        assert svc.registry.scan() == 0
