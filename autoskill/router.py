"""路由决策：向量 top-3 召回 + LLM 终审裁决（命中时一并给出调用参数）。

为什么不用纯相似度阈值：阈值在技能数量增长后误判率高，且"错误命中"是最危险的
失败模式（静默返回错误答案）。LLM 裁决一次调用的成本换来明确的语义判断与
参数抽取，是 MVP 规模下的最优 trade-off（详见架构文档）。
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from .config import Settings
from .llm import LLMClient, LLMFormatError
from .registry import SkillRegistry
from .schemas import ACTIVE
from .vectorstore import BaseStore

logger = logging.getLogger("autoskill.router")

ADJUDICATE_SYSTEM = """你是 AutoSkill 系统的技能路由裁判。输入：用户需求 + 候选技能列表（含各自的参数 Schema）。
判断任务：
1. 若某个候选技能能够完成用户需求，选出它，并按其 params_schema 构造参数；
2. 若所有候选都不能完成需求（能力不匹配、参数无法构造），判定为 none。
判定必须严格：能力不匹配就是 none，不要硬凑。
只输出 JSON 对象（不要解释文字）：
{"decision": "use" | "none", "skill": "选中的技能名或 null", "params": {按 schema 构造的参数对象，none 时为 {}}, "reason": "一句话理由"}"""


@dataclass
class RouteResult:
    hit: bool
    skill: str | None = None
    params: dict = field(default_factory=dict)
    reason: str = ""
    candidates: list[dict] = field(default_factory=list)  # [{name, description, sim, params_schema}]


class RouterError(RuntimeError):
    """裁决输出无法解析（重试后仍失败）。"""


CACHE_SIZE = 256


class Router:
    def __init__(self, registry: SkillRegistry, store: BaseStore, embedder, llm: LLMClient, settings: Settings):
        self.registry = registry
        self.store = store
        self.embedder = embedder
        self.llm = llm
        self.settings = settings
        # 路由结果 LRU 缓存：相同需求免 embed+裁决（复用路径毫秒级）；
        # 池变更（注册/熔断/重建）时由 service 调 invalidate() 失效
        self._cache: OrderedDict[str, RouteResult] = OrderedDict()
        self._cache_lock = threading.Lock()

    def invalidate(self) -> None:
        """技能池发生变化时清空路由缓存。"""
        with self._cache_lock:
            self._cache.clear()

    def route(self, requirement: str) -> RouteResult:
        key = (requirement or "").strip()
        if key:
            with self._cache_lock:
                cached = self._cache.get(key)
                if cached is not None:
                    self._cache.move_to_end(key)
                    return cached

        result = self._route_uncached(requirement)

        with self._cache_lock:
            self._cache[key] = result
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return result

    def _route_uncached(self, requirement: str) -> RouteResult:
        embedding = self.embedder.embed([requirement])[0]
        hits = self.store.query(embedding, k=self.settings.top_k, where={"status": ACTIVE})
        candidates = []
        for h in hits:
            meta = self.registry.get(h.id)
            if meta is None or meta.status != ACTIVE:
                continue
            if h.sim < self.settings.min_candidate_sim:
                continue
            candidates.append(
                {
                    "name": meta.name,
                    "description": meta.description,
                    "params_schema": meta.params_schema,
                    "similarity": round(h.sim, 4),
                }
            )
        if not candidates:
            return RouteResult(hit=False, reason="技能池无有效候选", candidates=[])

        user_payload = json.dumps(
            {"需求": requirement, "候选技能": candidates}, ensure_ascii=False
        )
        none_confirmed = False
        for attempt in (1, 2):  # 裁决输出非法时重试一次；none 时追加一次复核
            try:
                decision = self.llm.chat_json(ADJUDICATE_SYSTEM, user_payload, temperature=0.0)
                d = str(decision.get("decision", "")).lower()
                if d == "use":
                    skill = decision.get("skill")
                    params = decision.get("params")
                    if skill not in {c["name"] for c in candidates}:
                        raise ValueError(f"裁决返回的技能 {skill!r} 不在候选列表")
                    if not isinstance(params, dict):
                        raise ValueError("裁决的 params 必须是对象")
                    return RouteResult(
                        hit=True,
                        skill=skill,
                        params=params,
                        reason=str(decision.get("reason", "")),
                        candidates=candidates,
                    )
                if d == "none":
                    if attempt == 1:
                        # 假阴性复核（不对称设计：误 none 只多花一次调用，误 use 才危险）——
                        # 防止 LLM 因"参数构造复杂"而把可命中的候选误判为 none
                        user_payload = json.dumps(
                            {
                                "需求": requirement,
                                "候选技能": candidates,
                                "复核要求": "请逐个复核候选技能：只要某候选的功能描述与需求核心动作一致，就选择 use 并按其 schema 构造参数；"
                                "仅当能力确实无法完成需求时才返回 none。不要因为参数构造麻烦而误判 none。",
                            },
                            ensure_ascii=False,
                        )
                        none_confirmed = True
                        continue
                    return RouteResult(
                        hit=False,
                        reason=str(decision.get("reason", "候选均不匹配（已复核）")) if none_confirmed else str(decision.get("reason", "候选均不匹配")),
                        candidates=candidates,
                    )
                raise ValueError(f"未知 decision: {d!r}")
            except LLMFormatError as e:
                logger.warning("裁决输出解析失败 attempt=%d: %s", attempt, e)
                if attempt == 2:
                    raise RouterError(f"路由裁决输出无法解析: {e}") from e
            except ValueError as e:
                logger.warning("裁决结果非法 attempt=%d: %s", attempt, e)
                if attempt == 2:
                    raise RouterError(f"路由裁决结果非法: {e}") from e
        raise RouterError("路由裁决失败")  # pragma: no cover
