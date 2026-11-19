"""AutoSkillService：全链路编排。

核心执行链路（对应验收 G1-1）：
    用户需求 -> 向量召回 top-3 -> LLM 终审裁决
      命中 -> 参数预校验 -> 沙箱执行 -> 熔断计数更新 -> 返回
      未命中 -> 技能生成（四层校验 + ≤3 轮自修正） -> 入库 + 热加载注册 -> 参数提取 -> 执行 -> 返回
    熔断自修复（G1-3）：技能连续失败 N 次 -> quarantined（路由排除）->
      下一次同类需求 -> 高相似隔离技能检测 -> 以同名生成 v2 -> 校验通过热替换回 active

工程化保障：
- 并发安全：全局生成锁 + 获锁后二次路由（防止并发 ensure 对同一意图重复生成）
- 可观测性：data/events.jsonl 事件审计轨迹（ensure/call/rebuild 全量记录）
- 快速失败：启动时校验 LLM 配置，缺失即报可读错误
"""

from __future__ import annotations

import datetime
import json
import logging
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .embeddings import make_embedder
from .executor import Executor, make_executor
from .generator import Generator
from .llm import LLMClient
from .metrics import Metrics
from .registry import SkillRegistry
from .router import RouteResult, Router
from .schemas import ACTIVE, QUARANTINED, validate_params
from .vectorstore import BaseStore, make_vector_store

logger = logging.getLogger("autoskill.service")


class BudgetExceeded(RuntimeError):
    """当日 LLM token 预算耗尽（公网展示模式的费用护栏）。"""

    def __init__(self, used: int, budget: int):
        super().__init__(f"今日演示额度已用尽（{used}/{budget} tokens），明日自动恢复")
        self.used = used
        self.budget = budget

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
        # 快速失败：LLM 配置缺失时给出可读错误，而不是运行期神秘失败
        if not self.settings.llm_base_url or not self.settings.llm_api_key:
            raise RuntimeError(
                "LLM 未配置：请在项目根目录 .env 中设置 AUTOSKILL_LLM_BASE_URL 与 "
                "AUTOSKILL_LLM_API_KEY（模板见 .env.example），或通过环境变量注入"
            )
        self.settings.skills_dir.mkdir(parents=True, exist_ok=True)
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.llm = LLMClient(self.settings)
        self.executor: Executor = make_executor(self.settings)
        self.embedder = make_embedder(self.settings)
        self.store: BaseStore = make_vector_store(self.settings)
        self.registry = SkillRegistry(
            self.settings.skills_dir,
            breaker_threshold=self.settings.breaker_threshold,
            rollback_min_successes=self.settings.rollback_min_successes,
        )
        self.router = Router(self.registry, self.store, self.embedder, self.llm, self.settings)
        self.generator = Generator(self.llm, self.executor, self.settings)
        # 并发控制：全局生成锁（防并发 ensure 对同一意图重复生成；召回/调用不受影响）
        self._generation_lock = threading.Lock()
        # 事件审计轨迹（JSONL，线程安全；超过 5MB 轮转为 .1）
        self._events_path = self.settings.data_dir / "events.jsonl"
        self._events_lock = threading.Lock()
        self.metrics = Metrics()
        logger.info(
            "服务就绪: llm=%s store=%s embedding=%s executor=%s skills=%d",
            self.settings.llm_model,
            self.store.backend_name(),
            self.embedder.name,
            self.executor.name,
            len(self.registry.names()),
        )

    # ---- 事件审计轨迹 -----------------------------------------------------------
    def _usage_snapshot(self) -> dict | None:
        usage = getattr(self.llm, "usage", None)
        return dict(usage) if isinstance(usage, dict) else None

    def _usage_delta(self, before: dict | None) -> dict | None:
        now = self._usage_snapshot()
        if before is None or now is None:
            return None
        return {k: now[k] - before.get(k, 0) for k in now}

    def _today_token_usage(self) -> int:
        """从事件轨迹统计当日（本地时区 0 点起）LLM token 总消耗。"""
        midnight = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        total = 0
        for path in (self._events_path.with_suffix(".jsonl.1"), self._events_path):
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").strip().splitlines():
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (e.get("ts") or 0) < midnight:
                    continue
                usage = e.get("usage") or {}
                total += (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
        return total

    def check_budget(self) -> None:
        """每日 token 预算护栏（>0 时生效）；超限抛 BudgetExceeded（API 层转 429）。"""
        budget = self.settings.daily_token_budget
        if budget <= 0:
            return
        used = self._today_token_usage()
        if used >= budget:
            raise BudgetExceeded(used, budget)

    _EVENTS_MAX_BYTES = 5 * 1024 * 1024

    def _log_event(self, kind: str, **fields: Any) -> None:
        try:
            record = {"ts": round(time.time(), 3), "kind": kind, **fields}
            with self._events_lock:
                if self._events_path.exists() and self._events_path.stat().st_size > self._EVENTS_MAX_BYTES:
                    rotated = self._events_path.with_suffix(".jsonl.1")
                    if rotated.exists():
                        rotated.unlink()
                    self._events_path.rename(rotated)  # 轮转：当前 -> .1（只保留一代）
                with open(self._events_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception:  # noqa: BLE001 - 观测失败不阻塞业务
            logger.debug("事件日志写入失败", exc_info=True)

    def events(self, limit: int = 100) -> list[dict]:
        """读取最近 limit 条事件（最新在后）。"""
        if not self._events_path.exists():
            return []
        lines = self._events_path.read_text(encoding="utf-8").strip().splitlines()
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

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
        self._log_event("rebuild", **report)
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
            result = CallResult(ok=False, skill=name, error=f"技能不存在: {name}")
            self._log_event("call", **result.as_dict())
            return result
        if meta.status == QUARANTINED:
            result = CallResult(
                ok=False,
                skill=name,
                error=f"技能已熔断隔离（连续失败 {meta.fail_streak} 次），等待自修复升级",
            )
            self._log_event("call", **result.as_dict())
            return result
        ok, err = validate_params(params, meta.params_schema)
        if not ok:
            result = CallResult(ok=False, skill=name, error=err)
            self._log_event("call", **result.as_dict())
            return result
        exec_res = self._execute(meta, params)
        usage_before = self._usage_snapshot()
        if exec_res.ok:
            self.registry.record_success(name)
            result = CallResult(ok=True, skill=name, output=exec_res.data, duration_s=exec_res.duration_s)
        else:
            self._after_failure(meta, exec_res.error)
            result = CallResult(ok=False, skill=name, error=exec_res.error, duration_s=exec_res.duration_s)
        self.metrics.inc(f"call_total_ok_{int(result.ok)}")
        self.metrics.observe("call_duration_seconds", result.duration_s)
        self._log_event("call", **result.as_dict(), usage=self._usage_delta(usage_before))
        return result

    # ---- 全链路 ensure -----------------------------------------------------------
    def ensure(self, requirement: str) -> EnsureResult:
        requirement = (requirement or "").strip()
        if not requirement:
            result = EnsureResult(ok=False, path="rejected", error="需求为空")
            self._log_event("ensure", **result.as_dict())
            return result

        self.check_budget()
        t0 = time.monotonic()
        timings: dict[str, float] = {}
        detail: dict[str, Any] = {}
        usage_before = self._usage_snapshot()
        try:
            result = self._ensure_inner(requirement, t0, timings, detail)
        except Exception as e:  # noqa: BLE001 - 记录后向上抛（API 层结构化）
            self._log_event("ensure", ok=False, error=str(e)[:300],
                            elapsed_s=round(time.monotonic() - t0, 3),
                            usage=self._usage_delta(usage_before))
            raise
        elapsed_total = result.elapsed_s.get("total_s", 0.0)
        self.metrics.inc(f"ensure_total_path_{result.path}_ok_{int(result.ok)}")
        self.metrics.observe("ensure_duration_seconds", elapsed_total)
        self._log_event(
            "ensure", ok=result.ok, path=result.path, skill=result.skill,
            rounds=result.rounds, error=(result.error or "")[:200],
            elapsed_s=round(elapsed_total, 3),
            usage=self._usage_delta(usage_before),
        )
        return result

    def _ensure_inner(self, requirement: str, t0: float, timings: dict, detail: dict) -> EnsureResult:
        # 1) 召回 + 裁决
        tried_skills: set[str] = set()  # 本轮已尝试且执行失败的技能（复核时跳过）
        route = self._safe_route(requirement, timings, detail, key="route_s")
        result = self._attempt_recall(requirement, route, timings, detail, t0, tried_skills)
        if result is not None:
            return result

        # 2) 生成路径（全局生成锁 + 获锁后二次路由：并发 ensure 不会重复生成同一意图）
        with self._generation_lock:
            recheck = self._safe_route(requirement, timings, detail, key="recheck_s")
            if recheck is not None:
                result = self._attempt_recall(requirement, recheck, timings, detail, t0, tried_skills)
                if result is not None:
                    detail["recall_after_recheck"] = True
                    return result

            # 2.1) 未命中 -> 检测高相似隔离技能（熔断自修复入口）
            # 规则：top-k 中存在 quarantined 技能且相似度过召回下限，即视为同一意图，
            # 触发同名 v2 升级（本地哈希 embedding 的绝对相似度偏低，故不用固定高阈值）
            target_name = None
            active_route = recheck or route
            if active_route is not None:
                embedding = self.embedder.embed([requirement])[0]
                for h in self.store.query(embedding, k=self.settings.top_k, where=None):
                    meta = self.registry.get(h.id)
                    if meta is not None and meta.status == QUARANTINED and h.sim >= self.settings.min_candidate_sim:
                        target_name = meta.name
                        detail["repair_target"] = meta.name
                        detail["repair_sim"] = round(h.sim, 4)
                        logger.info("命中隔离技能 %s (sim=%.3f)，触发生成修复 v2", meta.name, h.sim)
                        break

            # 2.2) 生成（四层校验 + 自修正）
            t = time.monotonic()
            gen = self.generator.generate(requirement, target_name=target_name)
            timings["generate_s"] = time.monotonic() - t
            self.metrics.observe("generation_duration_seconds", timings["generate_s"])
            self.metrics.inc("generation_rounds_total", max(gen.rounds - 1, 0))
            if not gen.ok:
                self.metrics.inc(f"generation_fail_kind_{gen.error_kind or 'unknown'}")
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

            old_meta = self.registry.get(gen.skill.name)
            if old_meta is not None:
                # 升级/修复：影子验证门禁 —— 候选版本先用真实需求参数执行，通过才热替换；失败自动回滚
                return self._promote_with_shadow(requirement, gen, old_meta, target_name, timings, detail, t0)

            meta = self.registry.register(
                name=gen.skill.name,
                description=gen.skill.description,
                tags=gen.skill.tags,
                params_schema=gen.skill.params,
                impl_code=gen.skill.impl_code,
                tests=gen.skill.tests,
                note="auto-generated",
                egress=gen.skill.egress,
            )
            self._index_skill(meta)
            detail["version"] = meta.version

            # 2.3) 参数提取 -> 执行
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

    def _safe_route(self, requirement: str, timings: dict, detail: dict, key: str = "route_s") -> RouteResult | None:
        """路由（任何失败返回 None 并降级到生成路径——路由基础设施故障不击穿主链路）。"""
        t = time.monotonic()
        route: RouteResult | None = None
        router_error = ""
        try:
            route = self.router.route(requirement)
        except Exception as e:  # noqa: BLE001 - 路由失败必须降级而非中断
            router_error = f"{type(e).__name__}: {e}"
            logger.warning("路由失败，走生成路径: %s", e)
        timings[key] = time.monotonic() - t
        if key == "route_s":
            detail["route_reason"] = route.reason if route else router_error
            detail["candidates"] = route.candidates if route else []
        return route

    def _attempt_recall(self, requirement: str, route: RouteResult | None, timings: dict, detail: dict, t0: float, skip: set | None = None) -> EnsureResult | None:
        """召回路径：命中即执行。返回 None 表示未命中或执行失败（转生成路径）。
        skip：本轮已失败过的技能，复核路由时跳过（避免重复执行同一坏技能）。"""
        if route is None or not route.hit:
            return None
        if skip and route.skill in skip:
            return None
        meta = self.registry.get(route.skill)
        if meta is None or meta.status != ACTIVE:
            return None
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
        # 命中技能执行失败：熔断计数 +1，转生成路径（自修复触发点之一）
        self._after_failure(meta, exec_res.error)
        if skip is not None:
            skip.add(route.skill)
        detail["recall_failure"] = exec_res.error[:300]
        logger.warning("召回技能执行失败 %s: %s -> 转生成路径", meta.name, exec_res.error[:150])
        return None

    # ---- 内部 -----------------------------------------------------------
    def _promote_with_shadow(self, requirement: str, gen, old_meta, target_name: str | None,
                             timings: dict, detail: dict, t0: float) -> EnsureResult:
        """升级/修复的影子验证门禁：候选版本先用真实需求参数执行，通过才热替换；
        未通过则自动回滚（生效版本保持不变，状态保持 quarantined 等待下一轮修复）。"""
        import shutil
        from types import SimpleNamespace

        name = gen.skill.name
        version = self.registry.stage_files(name, gen.skill.impl_code, gen.skill.tests)
        detail["candidate_version"] = version

        cand_meta = SimpleNamespace(name=name, params_schema=gen.skill.params, description=gen.skill.description)
        params, arg_err = self._extract_args(requirement, cand_meta, None)
        if params is None:
            return EnsureResult(
                ok=False,
                path="generated",
                skill=name,
                error=f"候选 v{version} 已产出但参数提取失败（保持 v{old_meta.version} 生效）: {arg_err}",
                rounds=max(gen.rounds - 1, 0),
                elapsed_s=self._elapsed(t0, timings),
                detail=detail,
            )

        # 影子执行：在临时目录把 skill.json 的 version 指向候选版本（对运行时不可见）
        staging = Path(tempfile.mkdtemp(prefix="autoskill_shadow_"))
        try:
            dst = staging / name
            shutil.copytree(self.registry.skill_dir(name), dst)
            meta_path = dst / "skill.json"
            meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
            meta_data["version"] = version
            if gen.skill.egress:
                meta_data["egress"] = gen.skill.egress
            meta_path.write_text(json.dumps(meta_data, ensure_ascii=False), encoding="utf-8")
            exec_res = self.executor.execute(dst, params, timeout_s=self.settings.exec_timeout_s)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        timings["exec_s"] = exec_res.duration_s

        if not exec_res.ok:
            # 自动回滚：候选未通过影子验证，生效版本保持不变
            detail["shadow_rollback"] = True
            logger.warning("候选 v%d 影子验证失败，自动回滚保持 v%d 生效: %s",
                           version, old_meta.version, exec_res.error[:150])
            return EnsureResult(
                ok=False,
                path="generated",
                skill=name,
                error=f"候选 v{version} 影子执行失败，已自动回滚保持 v{old_meta.version} 生效: {exec_res.error[:300]}",
                rounds=max(gen.rounds - 1, 0),
                elapsed_s=self._elapsed(t0, timings),
                detail=detail,
            )

        meta = self.registry.promote(
            name, version,
            note=("auto-repair shadow v2" if target_name else "shadow-promoted upgrade"),
            egress=gen.skill.egress,
        )
        self._index_skill(meta)
        detail["version"] = meta.version
        detail["shadow_gate"] = True
        self.registry.record_success(name)
        return EnsureResult(
            ok=True,
            path="generated",
            skill=name,
            output=exec_res.data,
            rounds=max(gen.rounds - 1, 0),
            elapsed_s=self._elapsed(t0, timings),
            detail=detail,
        )

    def _execute(self, meta, params: dict):
        return self.executor.execute(
            self.registry.skill_dir(meta.name), params, timeout_s=self.settings.exec_timeout_s
        )

    def _after_failure(self, meta, error: str) -> None:
        updated, quarantined_now = self.registry.record_failure(meta.name)
        if quarantined_now and updated is not None:
            self.metrics.inc("breaker_trips_total")
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
        self.router.invalidate()  # 池变更使路由缓存失效

    @staticmethod
    def _elapsed(t0: float, timings: dict) -> dict:
        out = dict(timings)
        out["total_s"] = time.monotonic() - t0
        return out


def json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
