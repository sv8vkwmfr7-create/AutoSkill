"""Agent loop：function-calling，工具列表每次请求从注册表动态注入。

技能池一变，下一轮对话的可用工具立刻变化——"自进化 Agent"的外部可见效果。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .service import AutoSkillService

logger = logging.getLogger("autoskill.agent")

AGENT_SYSTEM = """你是 AutoSkill Agent。你的工具来自技能池（动态变化），技能池没有的能力可以由系统自动生成。
使用工具完成任务，观察结果后继续；若没有合适的工具，诚实说明。
回答用中文，简洁给出关键结论与数值。"""


@dataclass
class AgentReply:
    content: str
    steps: list[dict] = field(default_factory=list)  # [{skill, params, ok, output|error}]
    rounds: int = 0

    def as_dict(self) -> dict:
        return {"content": self.content, "steps": self.steps, "rounds": self.rounds}


class Agent:
    def __init__(self, service: AutoSkillService):
        self.service = service

    def chat(self, message: str, history: list[dict] | None = None) -> AgentReply:
        self.service.check_budget()
        llm = self.service.llm
        settings = self.service.settings
        tools = self.service.registry.tool_specs()
        messages: list[dict] = [{"role": "system", "content": AGENT_SYSTEM}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": message})
        steps: list[dict] = []

        for round_no in range(1, settings.agent_max_rounds + 1):
            kwargs: dict = {
                "model": settings.llm_model,
                "messages": messages,
                "temperature": 0.3,
            }
            if tools:
                kwargs["tools"] = tools
            resp = llm.client.chat.completions.create(**kwargs)
            usage = getattr(resp, "usage", None)
            if usage is not None:
                self.service.metrics.inc("agent_llm_calls_total")
                self.service._log_event("agent_llm", round=round_no,
                                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                                        completion_tokens=getattr(usage, "completion_tokens", 0) or 0)
            msg = resp.choices[0].message
            tool_calls = msg.tool_calls or []
            if not tool_calls:
                self.service._log_event("agent", ok=True, rounds=round_no, steps=len(steps))
                return AgentReply(content=(msg.content or "").strip(), steps=steps, rounds=round_no)
            messages.append(msg.model_dump(exclude_none=True))
            for call in tool_calls:
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                    if not isinstance(args, dict):
                        args = {"_raw": args}
                except json.JSONDecodeError:
                    args = {}
                result = self.service.call(name, args)
                step = {"skill": name, "params": args, "ok": result.ok}
                step["output" if result.ok else "error"] = result.output if result.ok else result.error
                steps.append(step)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result.as_dict(), ensure_ascii=False)[:4000],
                    }
                )
        return AgentReply(
            content="已达到最大工具调用轮次仍未收敛，以下是已执行步骤。",
            steps=steps,
            rounds=settings.agent_max_rounds,
        )
