"""AutoSkillService：全链路编排。

核心执行链路（对应验收 G1-1）：
    用户需求 -> 向量召回 top-3 -> LLM 终审裁决
      命中 -> 参数预校验 -> 沙箱执行 -> 熔断计数更新 -> 返回
      未命中 -> 技能生成（四层校验 + ≤3 轮自修正） -> 入库 + 热加载注册 -> 参数提取 -> 执行 -> 返回
    熔断自修复（G1-3）：技能连续失败 N 次 -> quarantined（路由排除）->
      下一次同类需求 -> 高相似隔离技能检测 -> 以同名生成 v2 -> 校验通过热替换回 active
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .embeddings import make_embedder
from .executor import Executor, make_executor
from .generator import Generator
from .llm import LLMClient
from .registry import SkillRegistry
from .router import RouteResult, Router, RouterError
from .schemas import ACTIVE, QUARANTINED, validate_params
from .vectorstore import BaseStore, make_vector_store

logger = logging.getLogger("autoskill.service")

ARGS_SYSTEM = """你是 AutoSkill 的参数提取器。给定技能的 JSON Schema 参数定义与用户需求，从需求中抽取参数值。
只输出 JSON 对象：{"params": {...}}，键与 schema 的 properties 一致，类型严格匹配。不要输出任何解释。"""


@dataclass
class EnsureResult:
    ok: bool
    path: str  # recall | generated | rejected
    skill: str | None = None
    output: Any = None
    error: str | None = None
    rounds: int = 0
    elapsed_s: dict = field(default_factory=dict)
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "path": self.path,
            "skill": self.skill,
            "output": self.output,
            "error": self.error,
            "rounds": self.rounds,
            "elapsed_s": {k: round(v, 3) for k, v in self.elapsed_s.items()},
            "detail": self.detail,
        }


@dataclass
class CallResult:
    ok: bool
    skill: str
    output: Any = None
    error: str | None = None
    duration_s: float = 0.0

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "skill": self.skill,
            "output": self.output,
            "error": self.error,
            "duration_s": round(self.duration_s, 3),
        }


class AutoSkillService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.load()
        self.settings.skills_dir.mkdir(parents=True, exist_ok=True)
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.llm = LLMClient(self.settings)
        self.executor: Executor = make_executor(self.settings)
        self.embedder = make_embedder(self.settings)
        self.store: BaseStore = make_vector_store(self.settings)
        self.registry = SkillRegistry(self.settings.skills_dir, breaker_threshold=self.settings.breaker_threshold)
        self.router = Router(self.registry, self.store, self.embedder, self.llm, self.settings)
        self.generator = Generator(self.llm, self.executor, self.settings)
        logger.info(
            "服务就绪: llm=%s store=%s embedding=%s executor=%s skills=%d",
            self.settings.llm_model,
            self.store.backend_name(),
            self.embedder.name,
            self.executor.name,
            len(self.registry.names()),
        )

    # ---- 重启重建 -----------------------------------------------------------
    def set_llm(self, llm) -> None:
        """注入自定义/伪 LLM 客户端（测试与实验用）。"""
        self.llm = llm
        self.router.llm = llm
        self.generator.llm = llm

    def rebuild(self) -> dict:
        """注册表从磁盘重建 + 向量库对账（upsert 全量、清理孤儿）。"""
        total = self.registry.scan()
        reg_ids = set(self.registry.names())
        indexed = 0
        for name, meta in self.registry.all().items():
            self._index_skill(meta)
            indexed += 1
        orphans = set(self.store.list_ids()) - reg_ids
        if orphans:
            self.store.delete(list(orphans))
        report = {
            "skills": total,
            "indexed": indexed,
            "removed_orphans": len(orphans),
            "store": self.store.backend_name(),
        }
        logger.info("重建完成: %s", report)
        return report

    def list_skills(self) -> list[dict]:
        return [
            {
                "name": m.name,
                "description": m.description,
                "tags": m.tags,
                "version": m.version,
                "status": m.status,
                "fail_streak": m.fail_streak,
                "params_schema": m.params_schema,
                "history": m.history,
            }
            for m in self.registry.list()
        ]

    # ---- 直接调用 -----------------------------------------------------------
    def call(self, name: str, params: dict) -> CallResult:
        meta = self.registry.get(name)
        if meta is None:
            return CallResult(ok=False, skill=name, error=f"技能不存在: {name}")
        if meta.status == QUARANTINED:
            return CallResult(
                ok=False,
                skill=name,
                error=f"技能已熔断隔离（连续失败 {meta.fail_streak} 次），等待自修复升级",
            )
        ok, err = validate_params(params, meta.params_schema)
        if not ok:
            return CallResult(ok=False, skill=name, error=err)
        exec_res = self._execute(meta, params)
        if exec_res.ok:
            self.registry.record_success(name)
            return CallResult(ok=True, skill=name, output=exec_res.data, duration_s=exec_res.duration_s)
        self._after_failure(meta, exec_res.error)
        return CallResult(ok=False, skill=name, error=exec_res.error, duration_s=exec_res.duration_s)

    # ---- 全链路 ensure -----------------------------------------------------------
    def ensure(self, requirement: str) -> EnsureResult:
        requirement = (requirement or "").strip()
        if not requirement:
            return EnsureResult(ok=False, path="rejected", error="需求为空")

        t0 = time.monotonic()
        timings: dict[str, float] = {}
        detail: dict[str, Any] = {}

        # 1) 召回 + 裁决
        route: RouteResult | None = None
        router_error = ""
        t = time.monotonic()
        try:
            route = self.router.route(requirement)
        except RouterError as e:
            router_error = str(e)
            logger.warning("路由失败，走生成路径: %s", e)
        timings["route_s"] = time.monotonic() - t
        detail["route_reason"] = route.reason if route else router_error
        detail["candidates"] = route.candidates if route else []

        # 2) 命中 -> 参数预校验 -> 执行
        if route and route.hit:
            meta = self.registry.get(route.skill)
            if meta is not None and meta.status == ACTIVE:
                params, arg_err = self._extract_args(requirement, meta, route.params)
                detail["arg_fix_used"] = arg_err is not None and params is not None
                if params is None:
                    return EnsureResult(
                        ok=False,
                        path="recall",
                        skill=meta.name,
                        error=f"召回命中 {meta.name}，但参数构造失败: {arg_err}",
                        rounds=0,
                        elapsed_s=self._elapsed(t0, timings),
                        detail=detail,
                    )
                exec_res = self._execute(meta, params)
                timings["exec_s"] = exec_res.duration_s
                if exec_res.ok:
                    self.registry.record_success(meta.name)
                    return EnsureResult(
                        ok=True,
                        path="recall",
                        skill=meta.name,
                        output=exec_res.data,
                        elapsed_s=self._elapsed(t0, timings),
                        detail=detail,
                    )
                # 命中技能执行失败：熔断计数 +1，转入生成路径（自修复触发点之一）
                self._after_failure(meta, exec_res.error)
                detail["recall_failure"] = exec_res.error[:300]
                logger.warning("召回技能执行失败 %s: %s -> 转生成路径", meta.name, exec_res.error[:150])

        # 3) 未命中 -> 检测高相似隔离技能（熔断自修复入口）
        # 规则：top-k 中存在 quarantined 技能且相似度过召回下限，即视为同一意图，
        # 触发同名 v2 升级（本地哈希 embedding 的绝对相似度偏低，故不用固定高阈值）
        target_name = None
        if route is not None:
            embedding = self.embedder.embed([requirement])[0]
            for h in self.store.query(embedding, k=self.settings.top_k, where=None):
                meta = self.registry.get(h.id)
                if meta is not None and meta.status == QUARANTINED and h.sim >= self.settings.min_candidate_sim:
                    target_name = meta.name
                    detail["repair_target"] = meta.name
                    detail["repair_sim"] = round(h.sim, 4)
                    logger.info("命中隔离技能 %s (sim=%.3f)，触发生成修复 v2", meta.name, h.sim)
                    break

        # 4) 生成（四层校验 + 自修正）
        t = time.monotonic()
        gen = self.generator.generate(requirement, target_name=target_name)
        timings["generate_s"] = time.monotonic() - t
        detail["generate_kind"] = gen.error_kind
        detail["generate_violations"] = gen.violations
        if not gen.ok:
            reason = "生成失败（能力边界或质量门禁未过）"
            if gen.error_kind == "guard":
                reason = "需求超出技能能力边界：违反 import 白名单/安全约束，已被静态检查拒绝"
            return EnsureResult(
                ok=False,
                path="rejected",
                error=f"{reason}: {gen.error[:500]}",
                rounds=max(gen.rounds - 1, 0),
                elapsed_s=self._elapsed(t0, timings),
                detail=detail,
            )

        meta = self.registry.register(
            name=gen.skill.name,
            description=gen.skill.description,
            tags=gen.skill.tags,
            params_schema=gen.skill.params,
            impl_code=gen.skill.impl_code,
            tests=gen.skill.tests,
            note=("auto-repair v2" if target_name else "auto-generated"),
        )
        self._index_skill(meta)
        detail["version"] = meta.version

        # 5) 参数提取 -> 执行
        params, arg_err = self._extract_args(requirement, meta, None)
        if params is None:
            return EnsureResult(
                ok=False,
                path="generated",
                skill=meta.name,
                error=f"技能已生成入库（v{meta.version}），但参数提取失败: {arg_err}",
                rounds=max(gen.rounds - 1, 0),
                elapsed_s=self._elapsed(t0, timings),
                detail=detail,
            )
        exec_res = self._execute(meta, params)
        timings["exec_s"] = exec_res.duration_s
        if not exec_res.ok:
            self._after_failure(meta, exec_res.error)
            return EnsureResult(
                ok=False,
                path="generated",
                skill=meta.name,
                error=f"技能已生成入库（v{meta.version}）但执行失败: {exec_res.error[:300]}",
                rounds=max(gen.rounds - 1, 0),
                elapsed_s=self._elapsed(t0, timings),
                detail=detail,
            )
        self.registry.record_success(meta.name)
        return EnsureResult(
            ok=True,
            path="generated",
            skill=meta.name,
            output=exec_res.data,
            rounds=max(gen.rounds - 1, 0),
            elapsed_s=self._elapsed(t0, timings),
            detail=detail,
        )

    # ---- 内部 -----------------------------------------------------------
    def _execute(self, meta, params: dict):
        return self.executor.execute(
            self.registry.skill_dir(meta.name), params, timeout_s=self.settings.exec_timeout_s
        )

    def _after_failure(self, meta, error: str) -> None:
        updated, quarantined_now = self.registry.record_failure(meta.name)
        if quarantined_now and updated is not None:
            self._index_skill(updated)  # 同步向量库元数据，路由立即排除
            logger.warning("技能 %s 已熔断并从路由排除", meta.name)

    def _extract_args(self, requirement: str, meta, prefill: dict | None) -> tuple[dict | None, str]:
        """从需求抽取参数；预校验不过时带错误回喂重试一次（不计熔断）。"""
        params = dict(prefill or {})
        ok, err = validate_params(params, meta.params_schema)
        if ok:
            return params, ""
        schema_text = json_dumps(meta.params_schema)
        user = f"技能: {meta.name}\n参数 Schema:\n{schema_text}\n\n用户需求:\n{requirement}"
        for attempt in (1, 2):
            if attempt == 2:
                user = (
                    f"技能: {meta.name}\n参数 Schema:\n{schema_text}\n\n"
                    f"上一次抽取的参数:\n{json_dumps(params)}\n校验错误: {err}\n"
                    f"请修正后重新输出。用户需求:\n{requirement}"
                )
            try:
                res = self.llm.chat_json(ARGS_SYSTEM, user, temperature=0.0, max_tokens=800)
            except Exception as e:  # noqa: BLE001
                return None, f"参数提取 LLM 调用失败: {e}"
            cand = res.get("params")
            if not isinstance(cand, dict):
                err = "参数提取输出缺少 params 对象"
                continue
            params = cand
            ok, err = validate_params(params, meta.params_schema)
            if ok:
                return params, ""
        return None, err

    def _index_skill(self, meta) -> None:
        text = f"{meta.name} {meta.description} 标签: {' '.join(meta.tags)}"
        emb = self.embedder.embed([text])[0]
        self.store.add(
            [meta.name],
            [emb],
            [meta.description],
            [{"name": meta.name, "status": meta.status, "version": meta.version}],
        )

    @staticmethod
    def _elapsed(t0: float, timings: dict) -> dict:
        out = dict(timings)
        out["total_s"] = time.monotonic() - t0
        return out


def json_dumps(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)
