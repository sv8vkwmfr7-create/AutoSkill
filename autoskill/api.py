"""FastAPI 接入层：技能生成、调用、管理与 Agent 对话。"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
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
            "skills": svc.list_skills().__len__(),
            "executor": svc.executor.healthcheck(),
            "embedding": svc.embedder.name,
            "store": svc.store.backend_name(),
            "llm_model": svc.settings.llm_model,
        }

    return app
