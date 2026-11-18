"""技能注册表：磁盘 skill.json 是唯一事实源，内存缓存 + 熔断状态机 + 重启重建。

设计说明（对应架构文档 trade-off）：热加载 = 子进程每次现起读最新文件，
注册表只存元数据与文件路径，因此无需 importlib 进程内导入，模块缓存/状态污染问题不存在。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from .schemas import ACTIVE, QUARANTINED, SkillMeta

logger = logging.getLogger("autoskill.registry")


class SkillRegistry:
    def __init__(self, skills_dir: Path, breaker_threshold: int = 3):
        self.skills_dir = Path(skills_dir)
        self.breaker_threshold = max(breaker_threshold, 1)
        self._skills: dict[str, SkillMeta] = {}
        self._lock = threading.RLock()
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.scan()

    # ---- 查询 -------------------------------------------------------------
    def get(self, name: str) -> SkillMeta | None:
        with self._lock:
            return self._skills.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return list(self._skills.keys())

    def all(self) -> dict[str, SkillMeta]:
        with self._lock:
            return dict(self._skills)

    def list(self, status: str | None = None) -> list[SkillMeta]:
        with self._lock:
            metas = list(self._skills.values())
        if status:
            metas = [m for m in metas if m.status == status]
        return sorted(metas, key=lambda m: m.name)

    def skill_dir(self, name: str) -> Path:
        return self.skills_dir / name

    def impl_path(self, name: str) -> Path:
        meta = self.get(name)
        version = meta.version if meta else 1
        return self.skill_dir(name) / f"v{version}" / "impl.py"

    # ---- 熔断状态机 ---------------------------------------------------------
    def record_success(self, name: str) -> SkillMeta | None:
        with self._lock:
            meta = self._skills.get(name)
            if meta is None:
                return None
            if meta.fail_streak or meta.status != ACTIVE:
                meta.fail_streak = 0
                if meta.status == QUARANTINED:  # 理论不可达：隔离技能不允许被调用
                    meta.status = ACTIVE
                self._persist(meta)
            return meta

    def record_failure(self, name: str) -> tuple[SkillMeta | None, bool]:
        """返回 (meta, 是否刚被熔断)。"""
        with self._lock:
            meta = self._skills.get(name)
            if meta is None:
                return None, False
            meta.fail_streak += 1
            meta.updated_at = time.time()
            quarantined_now = False
            if meta.fail_streak >= self.breaker_threshold and meta.status == ACTIVE:
                meta.status = QUARANTINED
                quarantined_now = True
                logger.warning("技能熔断: %s 连续失败 %d 次 -> quarantined", name, meta.fail_streak)
            self._persist(meta)
            return meta, quarantined_now

    # ---- 注册 / 版本 ---------------------------------------------------------
    def stage_files(self, name: str, impl_code: str, tests: list[dict]) -> int:
        """写入候选版本目录 v(N+1)，**不改变生效版本**（影子验证用）。

        返回候选版本号。候选目录在生效版本切换前对运行时不可见
        （runner 只读 skill.json 的 version）。
        """
        with self._lock:
            meta = self._skills.get(name)
            if meta is None:
                raise ValueError(f"技能不存在: {name}")
            version = meta.version + 1
            vdir = self.skill_dir(name) / f"v{version}"
            vdir.mkdir(parents=True, exist_ok=True)
            (vdir / "impl.py").write_text(impl_code, encoding="utf-8")
            (vdir / "tests.json").write_text(json.dumps(tests, ensure_ascii=False, indent=2), encoding="utf-8")
            return version

    def promote(self, name: str, version: int, note: str = "") -> SkillMeta:
        """把候选版本设为生效版本（热替换）：status 回 active、熔断计数清零。"""
        with self._lock:
            meta = self._skills.get(name)
            if meta is None:
                raise ValueError(f"技能不存在: {name}")
            now = time.time()
            meta.version = version
            meta.status = ACTIVE
            meta.fail_streak = 0
            meta.history.append({"version": version, "created_at": now, "note": note})
            meta.updated_at = now
            self._persist(meta)
            logger.info("技能生效版本切换: %s v%d (%s)", name, version, note or "promoted")
            return meta

    def register(
        self,
        name: str,
        description: str,
        tags: list[str],
        params_schema: dict,
        impl_code: str,
        tests: list[dict],
        note: str = "",
    ) -> SkillMeta:
        """写入新版本；同名技能自动 v(n+1) 并热替换（状态重置为 active）。"""
        with self._lock:
            existing = self._skills.get(name)
            now = time.time()
            if existing:
                version = existing.version + 1
                meta = SkillMeta(
                    name=name,
                    description=description,
                    tags=tags,
                    params_schema=params_schema,
                    version=version,
                    status=ACTIVE,
                    fail_streak=0,
                    history=existing.history + [{"version": version, "created_at": now, "note": note}],
                    created_at=existing.created_at,
                    updated_at=now,
                )
            else:
                version = 1
                meta = SkillMeta(
                    name=name,
                    description=description,
                    tags=tags,
                    params_schema=params_schema,
                    version=version,
                    status=ACTIVE,
                    fail_streak=0,
                    history=[{"version": 1, "created_at": now, "note": note}],
                    created_at=now,
                    updated_at=now,
                )
            vdir = self.skill_dir(name) / f"v{version}"
            vdir.mkdir(parents=True, exist_ok=True)
            (vdir / "impl.py").write_text(impl_code, encoding="utf-8")
            (vdir / "tests.json").write_text(json.dumps(tests, ensure_ascii=False, indent=2), encoding="utf-8")
            self._skills[name] = meta
            self._persist(meta)
            logger.info("技能注册: %s v%d (%s)", name, version, note or "new")
            return meta

    # ---- 持久化 / 重建 ---------------------------------------------------------
    def _persist(self, meta: SkillMeta) -> None:
        d = self.skill_dir(meta.name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "skill.json").write_text(meta.to_json(), encoding="utf-8")

    def scan(self) -> int:
        """从磁盘重建注册表（重启恢复；状态随 skill.json 保持）。"""
        with self._lock:
            self._skills.clear()
            if not self.skills_dir.exists():
                return 0
            for d in sorted(self.skills_dir.iterdir()):
                meta_file = d / "skill.json"
                if not d.is_dir() or not meta_file.exists():
                    continue
                try:
                    meta = SkillMeta.from_dict(json.loads(meta_file.read_text(encoding="utf-8")))
                    impl = self.skill_dir(meta.name) / f"v{meta.version}" / "impl.py"
                    if not impl.exists():
                        logger.warning("跳过损坏技能目录（impl 缺失）: %s", d.name)
                        continue
                    self._skills[meta.name] = meta
                except Exception as e:  # noqa: BLE001
                    logger.warning("跳过损坏技能目录: %s (%s)", d.name, e)
            return len(self._skills)

    def tool_specs(self) -> list[dict]:
        """Agent function-calling 的动态工具列表（仅 active）。"""
        specs = []
        for meta in self.list(status=ACTIVE):
            specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": meta.name,
                        "description": meta.description,
                        "parameters": meta.params_schema,
                    },
                }
            )
        return specs
