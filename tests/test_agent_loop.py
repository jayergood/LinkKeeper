"""tool loop 的行为测试。"""

from __future__ import annotations

import pytest

from src.agent import run_tool_loop
from src.agent_safety import RunGuard
from src.domain import ToolContext, ToolDef, ToolResult
from src.errors import BudgetExceeded
from src.tools.index import build_tools
from src.util.llm import MockLlmClient


# ---- 测试夹具 ----


def make_ctx(**overrides) -> ToolContext:
    base = dict(
        user_id="local-user",
        thread_id="thread-1",
        run_id="run-1",
        contact_id=None,
        evidence_ids=[],
        permission_mode="ask",
    )
    base.update(overrides)
    return ToolContext(**base)


def make_terminal_tool(kind: str = "questionnaire") -> ToolDef:
    """一个假的终态工具，用来验证「终态立即停止」。"""

    async def _fn(ctx: ToolContext, **_: object) -> ToolResult:
        return ToolResult(
            text=f"{kind} 已创建",
            details={"terminal_type": kind, "terminal_id": "fake-terminal-id"},
            terminate=True,
        )

    return ToolDef(
        name=kind,
        description=f"假的 {kind} 终态工具",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        category="questionnaire" if kind == "questionnaire" else "action",
        is_terminal=True,
        fn=_fn,
    )


def make_echo_tool(name: str = "echo") -> ToolDef:
    """普通非终态工具。"""

    async def _fn(ctx: ToolContext, **kwargs: object) -> ToolResult:
        return ToolResult(text=f"echo: {kwargs}", details=kwargs)

    return ToolDef(
        name=name,
        description="回显参数",
        parameters={
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "additionalProperties": False,
        },
        category="read",
        fn=_fn,
    )


def make_error_tool(name: str = "boom") -> ToolDef:
    """总是失败的工具。"""

    async def _fn(ctx: ToolContext, **_: object) -> ToolResult:
        return ToolResult(text="炸了", is_error=True)

    return ToolDef(
        name=name,
        description="总是失败",
        parameters={"type": "object", "properties": {}},
        category="read",
        fn=_fn,
    )


SYSTEM_PROMPT = "你是 Relationship Agent。"


# ---- 基础行为 ----


async def test_direct_answer() -> None:
    """模型不需要工具，直接回答 → completed，无终态。"""
    llm = MockLlmClient([{"text": "你好，有什么可以帮你？"}])
    ctx = make_ctx()
    result = await run_tool_loop(
        llm=llm,
        system_prompt=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "你好"}],
        tools=build_tools(ctx),
        ctx=ctx,
        guard=RunGuard(),
    )
    assert result.status == "completed"
    assert "你好" in result.text
    assert result.terminal is None
    assert result.turns == 1


async def test_single_tool_then_answer() -> None:
    """一次工具调用后回答。"""
    llm = MockLlmClient(
        [
            {"tool_calls": [{"name": "echo", "arguments": {"q": "hi"}}]},
            {"text": "查到了。"},
        ]
    )
    ctx = make_ctx()
    tools = [make_echo_tool()]
    result = await run_tool_loop(
        llm=llm,
        system_prompt=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "查一下"}],
        tools=tools,
        ctx=ctx,
        guard=RunGuard(),
    )
    assert result.status == "completed"
    assert result.text == "查到了。"
    assert result.turns == 2
    assert result.tool_calls == 1


async def test_messages_format() -> None:
    """assistant 消息的 tool_calls.arguments 必须是 JSON **字符串**。"""
    llm = MockLlmClient(
        [
            {"tool_calls": [{"name": "echo", "arguments": {"q": "hi"}}]},
            {"text": "ok"},
        ]
    )
    messages: list[dict] = [{"role": "user", "content": "查"}]
    ctx = make_ctx()
    await run_tool_loop(
        llm=llm,
        system_prompt=SYSTEM_PROMPT,
        messages=messages,
        tools=[make_echo_tool()],
        ctx=ctx,
        guard=RunGuard(),
    )

    assistant_msg = next(m for m in messages if m["role"] == "assistant")
    assert isinstance(assistant_msg["tool_calls"][0]["function"]["arguments"], str)

    tool_msg = next(m for m in messages if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == assistant_msg["tool_calls"][0]["id"]


async def test_history_preserved() -> None:
    """传入的会话历史必须保留，不能被 system prompt 覆盖。"""
    llm = MockLlmClient([{"text": "好的"}])
    messages: list[dict] = [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "收到"},
        {"role": "user", "content": "第二句"},
    ]
    ctx = make_ctx()
    await run_tool_loop(
        llm=llm,
        system_prompt=SYSTEM_PROMPT,
        messages=messages,
        tools=build_tools(ctx),
        ctx=ctx,
        guard=RunGuard(),
    )

    # 三条历史一条都不能少
    contents = [m.get("content") for m in messages]
    assert "第一句" in contents
    assert "收到" in contents
    assert "第二句" in contents
    # system prompt 必须在最前面
    assert messages[0]["role"] == "system"

    # 真正发给模型的消息里也要能看到历史
    sent = llm.calls[0]["messages"]
    sent_contents = [m.get("content") for m in sent]
    assert "第一句" in sent_contents and "第二句" in sent_contents


# ---- 终态语义 ----


async def test_terminal_stops_immediately() -> None:
    """调用终态工具后 loop 立刻停，不再请求模型。"""
    llm = MockLlmClient(
        [
            {"tool_calls": [{"name": "questionnaire", "arguments": {}}]},
            {"text": "这句话不应该被用到"},
        ]
    )
    ctx = make_ctx()
    result = await run_tool_loop(
        llm=llm,
        system_prompt=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "约个会"}],
        tools=[make_terminal_tool("questionnaire")],
        ctx=ctx,
        guard=RunGuard(),
    )
    assert result.status == "waiting_questionnaire"
    assert ctx.terminal is not None
    assert ctx.terminal.type == "questionnaire"
    # 只调用了一次模型（第一次要工具，之后没再问）
    assert len(llm.calls) == 1


async def test_terminal_skips_remaining_tools_same_turn() -> None:
    """同一轮里终态工具之后的工具不允许执行。"""
    llm = MockLlmClient(
        [
            {
                "tool_calls": [
                    {"name": "questionnaire", "arguments": {}},
                    {"name": "echo", "arguments": {"q": "should-not-run"}},
                ]
            }
        ]
    )
    ctx = make_ctx()
    result = await run_tool_loop(
        llm=llm,
        system_prompt=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "约个会"}],
        tools=[make_terminal_tool("questionnaire"), make_echo_tool()],
        ctx=ctx,
        guard=RunGuard(),
    )
    assert result.status == "waiting_questionnaire"
    assert result.tool_calls == 1  # 只执行了 questionnaire


async def test_action_terminal_sets_proposed() -> None:
    llm = MockLlmClient([{"tool_calls": [{"name": "propose_action", "arguments": {}}]}])
    ctx = make_ctx()
    result = await run_tool_loop(
        llm=llm,
        system_prompt=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "改一下"}],
        tools=[make_terminal_tool("propose_action")],
        ctx=ctx,
        guard=RunGuard(),
    )
    assert result.status == "proposed"


# ---- 顺序执行 ----


async def test_sequential_execution() -> None:
    """多个工具按顺序执行，顺序与模型给出的顺序一致。"""
    order: list[str] = []

    def make_ordered(name: str) -> ToolDef:
        async def _fn(ctx: ToolContext, **_: object) -> ToolResult:
            order.append(name)
            return ToolResult(text=name)

        return ToolDef(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            category="read",
            fn=_fn,
        )

    llm = MockLlmClient(
        [
            {
                "tool_calls": [
                    {"name": "t1", "arguments": {}},
                    {"name": "t2", "arguments": {}},
                    {"name": "t3", "arguments": {}},
                ]
            },
            {"text": "done"},
        ]
    )
    ctx = make_ctx()
    await run_tool_loop(
        llm=llm,
        system_prompt=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "x"}],
        tools=[make_ordered("t1"), make_ordered("t2"), make_ordered("t3")],
        ctx=ctx,
        guard=RunGuard(),
    )
    assert order == ["t1", "t2", "t3"]


# ---- 预算拦截 ----


async def test_duplicate_call_blocked() -> None:
    """相同工具 + 相同参数第 3 次被拦 → BudgetExceeded。"""
    llm = MockLlmClient(
        [
            {"tool_calls": [{"name": "echo", "arguments": {"q": "same"}}]},
            {"tool_calls": [{"name": "echo", "arguments": {"q": "same"}}]},
            {"tool_calls": [{"name": "echo", "arguments": {"q": "same"}}]},
            {"text": "不该到这里"},
        ]
    )
    ctx = make_ctx()
    with pytest.raises(BudgetExceeded) as exc:
        await run_tool_loop(
            llm=llm,
            system_prompt=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "x"}],
            tools=[make_echo_tool()],
            ctx=ctx,
            guard=RunGuard(max_duplicate_calls=2),
        )
    assert exc.value.details["dimension"] == "duplicate_calls"


async def test_consecutive_errors_abort() -> None:
    """连续 3 次工具错误中止。"""
    llm = MockLlmClient(
        [
            {"tool_calls": [{"name": "boom", "arguments": {"i": 1}}]},
            {"tool_calls": [{"name": "boom", "arguments": {"i": 2}}]},
            {"tool_calls": [{"name": "boom", "arguments": {"i": 3}}]},
        ]
    )
    ctx = make_ctx()
    with pytest.raises(BudgetExceeded) as exc:
        await run_tool_loop(
            llm=llm,
            system_prompt=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "x"}],
            tools=[make_error_tool()],
            ctx=ctx,
            guard=RunGuard(max_consecutive_errors=3),
        )
    assert exc.value.details["dimension"] == "consecutive_errors"


async def test_turn_limit_abort() -> None:
    """模型一直要工具，轮次耗尽后中止。"""
    script = [{"tool_calls": [{"name": "echo", "arguments": {"i": i}}]} for i in range(10)]
    llm = MockLlmClient(script)
    ctx = make_ctx()
    with pytest.raises(BudgetExceeded) as exc:
        await run_tool_loop(
            llm=llm,
            system_prompt=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "x"}],
            tools=[make_echo_tool()],
            ctx=ctx,
            guard=RunGuard(max_turns=3, max_tool_calls=20),
        )
    assert exc.value.details["dimension"] == "turns"
