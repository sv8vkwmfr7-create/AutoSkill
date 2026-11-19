"""FastAPI 接入层：技能生成、调用、管理与 Agent 对话。

错误约定：业务结果以 {"ok": false, "error": ...} 随 200 返回（结果式 API）；
未处理异常由全局兜底 handler 转为结构化 500，不泄漏裸栈。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import __version__
from .agent import Agent
from .service import AutoSkillService, BudgetExceeded
from .ui import UI_HTML

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

    # 异步任务：/ensure/async 提交，GET /jobs/{id} 轮询（内存态，重启即清；审计已在 events.jsonl）
    job_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="autoskill_job")
    jobs: dict[str, dict] = {}
    jobs_lock = threading.Lock()

    # 公网化三件套之二：每 IP 限流（滑动窗口，内存态）
    settings = svc.settings
    rate_limit = settings.rate_limit_per_hour
    rate_map: dict[str, list[float]] = {}
    rate_lock = threading.Lock()

    def _rate_guard(request: Request) -> None:
        if rate_limit <= 0:
            return
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        with rate_lock:
            window = [t for t in rate_map.get(ip, []) if now - t < 3600.0]
            if len(window) >= rate_limit:
                svc.metrics.inc("rate_limited_total")
                raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试（演示限流）")
            window.append(now)
            rate_map[ip] = window

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request, exc):  # pragma: no cover - 兜底路径
        logger.exception("未处理异常 %s", request.url.path)
        return JSONResponse(status_code=500, content={"ok": False, "error": f"服务内部错误: {exc}"})

    @app.post("/ensure")
    def ensure(request: Request, req: EnsureRequest) -> dict:
        _rate_guard(request)
        try:
            result = svc.ensure(req.requirement)
        except BudgetExceeded as e:
            raise HTTPException(status_code=429, detail=str(e)) from e
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
    def agent_chat(request: Request, req: AgentRequest) -> dict:
        _rate_guard(request)
        if settings.showcase:
            token = request.headers.get("x-showcase-token", "")
            if not settings.showcase_token or token != settings.showcase_token:
                raise HTTPException(
                    status_code=403,
                    detail="演示模式下 Agent 对话已关闭（操作员可配置 AUTOSKILL_SHOWCASE_TOKEN 放行）",
                )
        try:
            reply = Agent(svc).chat(req.message, history=req.history)
        except BudgetExceeded as e:
            raise HTTPException(status_code=429, detail=str(e)) from e
        except Exception as e:  # noqa: BLE001
            logger.exception("agent 失败")
            raise HTTPException(status_code=502, detail=f"agent 调用失败: {e}") from e
        return reply.as_dict()

    @app.get("/metrics")
    def metrics() -> PlainTextResponse:
        """Prometheus 文本格式指标（零依赖实现）。"""
        return PlainTextResponse(svc.metrics.render(), media_type="text/plain; version=0.0.4")

    @app.post("/ensure/async")
    def ensure_async(request: Request, req: EnsureRequest) -> dict:
        """长任务异步化：立即返回 job_id，避免 60s 级生成占用 HTTP 连接。"""
        _rate_guard(request)
        job_id = uuid.uuid4().hex[:12]
        with jobs_lock:
            jobs[job_id] = {"job_id": job_id, "status": "queued",
                            "requirement": req.requirement, "submitted_at": time.time()}
        seen = {"started": False}

        def _run() -> None:
            if not seen["started"]:
                seen["started"] = True
                with jobs_lock:
                    jobs[job_id]["status"] = "running"
                    jobs[job_id]["started_at"] = time.time()
            try:
                result = svc.ensure(req.requirement).as_dict()
                with jobs_lock:
                    jobs[job_id]["status"] = "done"
                    jobs[job_id]["result"] = result
                    jobs[job_id]["finished_at"] = time.time()
            except Exception as e:  # noqa: BLE001
                with jobs_lock:
                    jobs[job_id]["status"] = "error"
                    jobs[job_id]["result"] = {"ok": False, "error": str(e)[:300]}
                    jobs[job_id]["finished_at"] = time.time()

        job_executor.submit(_run)
        return {"job_id": job_id, "status": "queued"}

    @app.get("/jobs/{job_id}")
    def job_status(job_id: str) -> dict:
        with jobs_lock:
            job = dict(jobs.get(job_id) or {})
        if not job:
            raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
        return job

    @app.get("/ui", response_class=HTMLResponse)
    def ui() -> HTMLResponse:
        """Web 调试面板（单文件，零构建）；showcase 模式显示演示横幅。"""
        html = UI_HTML
        if settings.showcase:
            html = html.replace(
                "<!--SHOWCASE_BANNER-->",
                '<div class="banner">🎓 演示模式：沙箱五层纵深已启用，每 IP 限流，'
                "当日 LLM 额度用尽后接口返回 429</div>",
            )
        else:
            html = html.replace("<!--SHOWCASE_BANNER-->", "")
        return HTMLResponse(html)

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "showcase": settings.showcase,
            "rebuild": rebuild,
            "skills": len(svc.list_skills()),
            "executor": svc.executor.healthcheck(),
            "embedding": svc.embedder.name,
            "store": svc.store.backend_name(),
            "llm_model": svc.settings.llm_model,
        }

    return app
