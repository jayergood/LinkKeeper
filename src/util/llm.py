"""LLM 客户端封装（OpenAI 兼容端点）与 mock 实现。

Mock 按脚本逐步返回预设的工具调用序列，供评测和单元测试使用。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from ..config import settings

log = logging.getLogger(__name__)


# ---- 消息与响应结构 ----


@dataclass
class ToolCall:
    """模型请求的一次工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResponse:
    """一次 LLM 回复。有 tool_calls 说明要继续循环，只有 text 则可结束本轮。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: Literal["stop", "tool_calls", "error"] = "stop"


class LlmError(RuntimeError):
    """LLM 调用失败（网络、鉴权、配额）。"""


# ---- 真实客户端 ----


class LlmClient:
    """OpenAI 兼容端点客户端。只支持 function calling 与普通 chat 两种用法。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key or settings.llm_api_key
        self.base_url = base_url or settings.llm_base_url
        self.model = model or settings.llm_model
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise LlmError("未配置 LLM_API_KEY，无法调用模型")
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        """一次对话调用。带 tools 时可能返回 tool_calls。"""
        client = self._ensure_client()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if max_tokens:
            kwargs["max_tokens"] = max_tokens

        try:
            resp = await client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 —— 统一包成 LlmError
            raise LlmError(f"模型调用失败：{exc}") from exc

        choice = resp.choices[0]
        message = choice.message
        tool_calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=call.id, name=call.function.name, arguments=args))

        usage = {}
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }

        return ChatResponse(
            text=message.content or "",
            tool_calls=tool_calls,
            usage=usage,
            stop_reason="tool_calls" if tool_calls else "stop",
        )


# ---- Mock 客户端（评测用） ----


class MockLlmClient:
    """按脚本返回预设回复的假客户端；脚本耗尽后由 `exhausted` 决定返回文本还是抛错。"""

    def __init__(
        self,
        script: list[dict[str, Any]],
        *,
        exhausted: Literal["text", "error"] = "text",
        model: str = "mock-model",
    ) -> None:
        self.script = script
        self.exhausted = exhausted
        self.model = model
        self._index = 0
        self.calls: list[dict[str, Any]] = []  # 记录每次调用，供断言
        self._call_seq = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **_: Any,
    ) -> ChatResponse:
        self.calls.append({"messages": list(messages), "tools": tools})
        # 始终返回完整 usage，字段缺失由调用方用 .get(key, 0) 兜底
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        if self._index >= len(self.script):
            if self.exhausted == "error":
                raise LlmError("mock 脚本已耗尽")
            return ChatResponse(text="（mock 脚本已耗尽）", stop_reason="stop", usage=usage)

        step = self.script[self._index]
        self._index += 1

        tool_calls: list[ToolCall] = []
        for raw in step.get("tool_calls", []):
            self._call_seq += 1
            tool_calls.append(
                ToolCall(
                    id=f"call_{self._call_seq}",
                    name=raw["name"],
                    arguments=raw.get("arguments") or {},
                )
            )

        return ChatResponse(
            text=step.get("text", ""),
            tool_calls=tool_calls,
            usage=usage,
            stop_reason="tool_calls" if tool_calls else "stop",
        )


# ---- 纯文本补全（压缩摘要 / 记忆文档生成） ----


async def complete_text(
    system_prompt: str,
    user_content: str,
    *,
    client: LlmClient | None = None,
    max_tokens: int = 2_000,
) -> str:
    """纯文本补全，用于会话压缩和长期记忆文档生成，不需要 tools。"""
    llm = client or LlmClient()
    response = await llm.chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        max_tokens=max_tokens,
    )
    return response.text.strip()


def get_llm() -> LlmClient:
    """进程内默认客户端（延迟初始化，未配置 key 时在首次调用才报错）。"""
    global _default_llm
    if _default_llm is None:
        _default_llm = LlmClient()
    return _default_llm


_default_llm: LlmClient | None = None
