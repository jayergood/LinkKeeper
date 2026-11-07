"""主 Agent 的 tool loop —— 模型与工具的往返执行。

停止条件：模型返回纯文本（completed）、调用终态工具（由 terminal_status 映射）、
轮次耗尽（failed）。预算超限由 RunGuard 抛 BudgetExceeded，交调用方收尾。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .agent_safety import RunGuard
from .domain import RunStatus, ToolContext, ToolDef, ToolTerminalState
from .tools.index import execute_tool, to_openai_schema
from .util.llm import ChatResponse, LlmClient, MockLlmClient

log = logging.getLogger(__name__)


@dataclass
class AgentResult:
    """一轮 run 的最终产出。"""

    status: RunStatus
    text: str = ""
    terminal: ToolTerminalState | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    turns: int = 0
    tool_calls: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    """本轮新增的 assistant / tool 消息，不含 system 和历史。

    messages 是原地修改的，调用方无法区分哪些是新的；而 transcript 必须落库，
    否则恢复问卷时找不到那条 assistant.tool_call，补配对无对象可补。
    """


AssistantMessage = dict[str, Any]
ToolMessage = dict[str, Any]


def build_assistant_message(resp: ChatResponse) -> AssistantMessage:
    """把 LLM 回复转成 OpenAI 格式的 assistant 消息（arguments 需序列化为字符串）。"""
    return {
        "role": "assistant",
        "content": resp.text or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in resp.tool_calls
        ],
    }


def build_tool_message(call_id: str, text: str) -> ToolMessage:
    """工具结果消息。`tool_call_id` 必须与 assistant 消息里的 id 对应。"""
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def terminal_status(terminal: ToolTerminalState | None) -> RunStatus:
    """终态 → run 状态映射。"""
    if terminal is None:
        return "completed"
    if terminal.type == "questionnaire":
        return "waiting_questionnaire"
    if terminal.type == "propose_action":
        return "proposed"
    # publish_insight / skip_insight 是 Insight Agent 的终态，不属于主 run 状态机
    return "completed"


async def run_tool_loop(
    *,
    llm: LlmClient | MockLlmClient,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list[ToolDef],
    ctx: ToolContext,
    guard: RunGuard,
    max_turns: int = 8,
) -> AgentResult:
    """执行一轮完整的 tool loop。

    Args:
        messages: 已有会话消息（不含 system），本函数会原地追加
        ctx: 工具上下文，终态会写进 `ctx.terminal`
        guard: 预算守卫，超限时抛 BudgetExceeded

    Raises:
        BudgetExceeded: 任一预算维度超限，调用方负责把 run 落 failed
        LlmError: 模型调用失败（网络/鉴权/配额）
    """
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    new_messages: list[dict[str, Any]] = []   # 本轮新增，交给调用方落库
    messages.insert(0, {"role": "system", "content": system_prompt})
    schema = to_openai_schema(tools)
    for turn in range(max_turns):
        guard.check_turn()
        resp = await llm.chat(messages, tools=schema)
        for key in total_usage:
            total_usage[key] += resp.usage.get(key, 0)

        if resp.tool_calls:
            assistant_message = build_assistant_message(resp)
            messages.append(assistant_message)
            new_messages.append(assistant_message)
            for call in resp.tool_calls:
                guard.check_tool_call(call.name, call.arguments)
                ctx.current_tool_call_id = call.id
                result = await execute_tool(ctx, tools, call.name, call.arguments)
                guard.record_tool_result(is_error=result.is_error)
                tool_message = build_tool_message(call.id, result.text)
                messages.append(tool_message)
                new_messages.append(tool_message)
                if result.terminate or ctx.terminal is not None:
                    # 终态工具的回执是占位符而非真结果。落库会让
                    # has_paired_tool_result() 返回 True，恢复时补配对被跳过。
                    return AgentResult(
                        status=terminal_status(ctx.terminal),
                        text=result.text,
                        terminal=ctx.terminal,
                        usage=total_usage,
                        turns=turn + 1,
                        tool_calls=guard.tool_calls,
                        messages=new_messages[:-1],
                    )
                continue
        else:
            return AgentResult(
                status="completed",
                text=resp.text,
                terminal=None,
                usage=total_usage,
                turns=turn + 1,
                tool_calls=guard.tool_calls,
                messages=new_messages,
            )

    return AgentResult(
        status="failed",
        text="已达轮次上限，请缩小任务范围",
        terminal=None,
        usage=total_usage,
        turns=turn + 1,
        tool_calls=guard.tool_calls,
        messages=new_messages,
    )
