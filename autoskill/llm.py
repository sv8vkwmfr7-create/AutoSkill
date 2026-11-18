"""OpenAI 兼容 LLM 客户端：结构化 JSON 输出 + 稳健解析。

解析策略（provider 无关）：
1. 优先 response_format=json_object（DeepSeek 支持；不支持时自动降级）
2. 剥离 ```json 围栏 -> 截取首个 { 到最后一个 } -> json.loads
解析失败抛 LLMFormatError（携带原始输出），由上层生成器的自修正循环回喂。
"""

from __future__ import annotations

import json
import logging
import re

from .config import Settings

logger = logging.getLogger("autoskill.llm")


class LLMError(RuntimeError):
    """网络/服务端错误。"""


class LLMFormatError(LLMError):
    def __init__(self, message: str, raw: str):
        super().__init__(message)
        self.raw = raw


def extract_json(text: str) -> dict:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMFormatError("输出中未找到 JSON 对象", text)
    candidate = text[start : end + 1]
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError as e:
        raise LLMFormatError(f"JSON 解析失败: {e}", text) from e
    if not isinstance(obj, dict):
        raise LLMFormatError("JSON 顶层必须是对象", text)
    return obj


class LLMClient:
    """最小接口：chat_json / chat_text。测试可注入同签名 Fake。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None
        self._json_mode_ok = settings.llm_json_mode
        # 累计 token 用量（成本统计：events.jsonl 逐操作记录差值）
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    @property
    def client(self):
        if self._client is None:
            import openai

            self._client = openai.OpenAI(
                base_url=self.settings.llm_base_url or None,
                api_key=self.settings.llm_api_key or "EMPTY",
                timeout=self.settings.llm_timeout_s,
            )
        return self._client

    def _complete(self, messages: list[dict], temperature: float | None, max_tokens: int) -> str:
        last_err: Exception | None = None
        for attempt in range(3):  # 网络级重试（指数退避）
            try:
                kwargs: dict = {
                    "model": self.settings.llm_model,
                    "messages": messages,
                    "temperature": self.settings.llm_temperature if temperature is None else temperature,
                    "max_tokens": max_tokens,
                }
                if self._json_mode_ok:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = self.client.chat.completions.create(**kwargs)
                choice = resp.choices[0]
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    self.usage["calls"] += 1
                    self.usage["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
                    self.usage["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
                if getattr(choice, "finish_reason", None) == "length":
                    raise LLMFormatError(
                        f"输出被 max_tokens={kwargs.get('max_tokens')} 截断，请精简内容后重新输出",
                        choice.message.content or "",
                    )
                return choice.message.content or ""
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if self._json_mode_ok and "response_format" in msg:
                    logger.warning("provider 不支持 response_format，降级为纯提示约束")
                    self._json_mode_ok = False
                    last_err = e
                    continue
                last_err = e
                if attempt < 2:
                    import time

                    time.sleep(1.5 * (attempt + 1))
        raise LLMError(f"LLM 调用失败（已重试）: {last_err}") from last_err

    def snapshot_usage(self) -> dict:
        return dict(self.usage)

    def chat_json(self, system: str, user: str, temperature: float | None = None, max_tokens: int = 4000) -> dict:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        raw = self._complete(messages, temperature, max_tokens)
        return extract_json(raw)

    def chat_text(self, system: str, user: str, temperature: float | None = None, max_tokens: int = 4000) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return self._complete(messages, temperature, max_tokens)
