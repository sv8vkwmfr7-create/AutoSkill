"""FastAPI 接入层：技能生成、调用、管理与 Agent 对话。

错误约定：业务结果以 {"ok": false, "error": ...} 随 200 返回（结果式 API）；
未处理异常由全局兜底 handler 转为结构化 500，不泄漏裸栈。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .agent import Agent
from .service import AutoSkillService

logger = logging.getLogger("autoskill.api")


class EnsureRequest(BaseModel):
    requirement: str = Field(min_length=1, description="自然语言技能需求")


class CallRequest(BaseModel):
    params: dict = Field(default_factory=dict)


class AgentRequest(BaseModel):
    message: str = Field(min_length=1)
    history: list[dict] = Field(default_factory=list)


def create_app(service: AutoSkillService | None = None) -> FastAPI:
    svc = service or AutoSkillService()
    rebuild = svc.rebuild()  # 启动即从磁盘+向量库重建注册表
    app = FastAPI(title="AutoSkill", version=__version__, description="面向 Agent 的自进化技能生成与热加载系统")

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request, exc):  # pragma: no cover - 兜底路径
        logger.exception("未处理异常 %s", request.url.path)
        return JSONResponse(status_code=500, content={"ok": False, "error": f"服务内部错误: {exc}"})

    @app.post("/ensure")
    def ensure(req: EnsureRequest) -> dict:
        result = svc.ensure(req.requirement)
        return result.as_dict()

    @app.post("/skills/{name}/call")
    def call(name: str, req: CallRequest) -> dict:
        result = svc.call(name, req.params)
        return result.as_dict()

    @app.get("/skills")
    def skills() -> list[dict]:
        return svc.list_skills()

    @app.get("/skills/{name}")
    def skill_detail(name: str) -> dict:
        for s in svc.list_skills():
            if s["name"] == name:
                return s
        raise HTTPException(status_code=404, detail=f"技能不存在: {name}")

    @app.get("/events")
    def events(limit: int = 50) -> list[dict]:
        """事件审计轨迹（JSONL 尾部），用于运维观测。"""
        return svc.events(limit=max(1, min(limit, 500)))

    @app.post("/agent/chat")
    def agent_chat(req: AgentRequest) -> dict:
        try:
            reply = Agent(svc).chat(req.message, history=req.history)
        except Exception as e:  # noqa: BLE001
            logger.exception("agent 失败")
            raise HTTPException(status_code=502, detail=f"agent 调用失败: {e}") from e
        return reply.as_dict()

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "rebuild": rebuild,
            "skills": len(svc.list_skills()),
            "executor": svc.executor.healthcheck(),
            "embedding": svc.embedder.name,
            "store": svc.store.backend_name(),
            "llm_model": svc.settings.llm_model,
        }

    return app
