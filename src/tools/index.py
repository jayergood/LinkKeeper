"""工具注册中心：按本轮上下文构建工具清单，并负责按名字调度执行。

工具的 execute 是闭包，把 contact_id / thread_id / evidence_ids / permission_mode
钉死在本轮作用域内，不同会话的数据不会串。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..domain import ToolContext, ToolDef, ToolResult
from .helpers import text_result

# ---- 内置只读工具 ----


async def _get_current_time(ctx: ToolContext, **_: Any) -> ToolResult:
    """返回当前 UTC 时间与用户本地时间。

    签名带 `**kwargs`：execute_tool 会注入 `_tool_call_id`，所有工具都要能接受。
    """
    now_utc = datetime.now(timezone.utc)
    return text_result(
        f"当前时间：{now_utc.isoformat()}（UTC）。日程计算请用 find_free_slots。",
        details={"now_utc": now_utc.isoformat()},
    )


_CURRENT_TIME = ToolDef(
    name="get_current_time",
    description="读取服务器当前时间（UTC ISO 8601）。用于校准「今天/下周」这类相对时间。",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    category="read",
    is_terminal=False,
    fn=_get_current_time,
)


def build_tools(ctx: ToolContext) -> list[ToolDef]:
    """构建本轮可用工具清单。"""
    from .questionnaire import build_questionnaire_tool

    tools: list[ToolDef] = [_CURRENT_TIME, build_questionnaire_tool(ctx)]
    return tools


async def execute_tool(
    ctx: ToolContext, tools: list[ToolDef], name: str, args: dict[str, Any]
) -> ToolResult:
    """按名字查找并执行工具。

    1. 工具不存在 → 错误结果，让模型自己纠正
    2. 已进入终态 → 拒绝执行任何工具
    3. 执行抛异常 → 包成 error_result，不终止 loop
    """
    if ctx.terminal is not None:
        return ToolResult(
            text=f"本轮已进入终态（{ctx.terminal.type}），不能再调用任何工具。",
            is_error=True,
        )

    tool = next((t for t in tools if t.name == name), None)
    if tool is None:
        return ToolResult(
            text=f"不存在名为 {name} 的工具。可用工具：{', '.join(t.name for t in tools)}",
            is_error=True,
        )

    try:
        # 注入 tool_call_id：终态工具要落库，恢复时才能补出配对的 toolResult
        result = await tool.fn(ctx, **args, _tool_call_id=ctx.current_tool_call_id)
    except TypeError as exc:
        return ToolResult(text=f"工具 {name} 参数错误：{exc}", is_error=True)
    except Exception as exc:  # noqa: BLE001 —— 工具内部异常不应终止 loop
        return ToolResult(text=f"工具 {name} 执行失败：{exc}", is_error=True)

    if not isinstance(result, ToolResult):
        return ToolResult(text=str(result))

    # 终态工具把标记写进 ctx，后面的 loop 检查就看它
    if result.terminate and result.details:
        terminal_type = result.details.get("terminal_type")
        terminal_id = result.details.get("terminal_id")
        if terminal_type and terminal_id:
            from ..domain import ToolTerminalState

            ctx.terminal = ToolTerminalState(type=terminal_type, id=terminal_id)

    return result


def to_openai_schema(tools: list[ToolDef]) -> list[dict[str, Any]]:
    """转成 OpenAI function-calling 的 tools 数组。"""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters or {"type": "object", "properties": {}},
            },
        }
        for tool in tools
    ]
