"""Questionnaire 终态工具：向用户发起 1–4 道结构化追问并暂停本轮。

问题落库后 run 转 `waiting_questionnaire`，用户回答再恢复同一规划链。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..domain import QuestionnairePayload, ToolContext, ToolDef, ToolResult
from .helpers import error_result, terminal_result

log = logging.getLogger(__name__)


# ---- 校验（服务端二次校验，不信任模型给的 payload） ----


def validate_questionnaire(payload: dict[str, Any]) -> QuestionnairePayload:
    """校验问卷结构；抛 ValidationError 时上层转成协议错误回给模型。"""
    return QuestionnairePayload.model_validate(payload)


# ---- 工具实现 ----


async def create_questionnaire(
    ctx: ToolContext,
    *,
    questions: list[dict[str, Any]],
    tool_call_id: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """落库问卷，返回 (questionnaire_id, 规范化后的问题列表)。

    `tool_call_id` 存下来，恢复时用它补出配对的 toolResult。
    """
    validated = validate_questionnaire({"questions": questions})
    normalized = [q.model_dump(mode="json") for q in validated.questions]

    row = await ctx.db.execute_returning(
        """
        INSERT INTO questionnaires (run_id, thread_id, questions, status, tool_call_id)
        VALUES (:run_id, :thread_id, :questions, 'pending', :tool_call_id)
        RETURNING id
        """,
        {
            "run_id": ctx.run_id,
            "thread_id": ctx.thread_id,
            "questions": json.dumps(normalized, ensure_ascii=False),
            "tool_call_id": tool_call_id,
        },
    )
    assert row is not None
    return str(row["id"]), normalized


async def _questionnaire_tool(
    ctx: ToolContext, **kwargs: Any
) -> ToolResult:
    """工具入口。参数由模型给出。"""
    raw_questions = kwargs.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        return error_result("questionnaire 需要非空的 questions 数组")

    try:
        questionnaire_id, normalized = await create_questionnaire(
            ctx,
            questions=raw_questions,
            tool_call_id=kwargs.get("_tool_call_id"),
        )
    except Exception as exc:  # noqa: BLE001 —— 协议错误回给模型，不炸 loop
        return error_result(f"问卷格式不合法，请修正后重试：{exc}")

    # 终态：把标记写进 ctx，tool loop 会立即停止
    ctx.terminal = {"type": "questionnaire", "id": questionnaire_id}  # type: ignore[assignment]

    return terminal_result(
        f"已向用户发起 {len(normalized)} 道追问，本轮暂停，等待用户回答。",
        details={
            "terminal_type": "questionnaire",
            "terminal_id": questionnaire_id,
            "questionnaire_id": questionnaire_id,
            "questions": normalized,
        },
    )


QUESTIONNAIRE_TOOL = ToolDef(
    name="questionnaire",
    description=(
        "当缺少关键信息（是谁、什么时候、改什么）时，向用户发起 1–4 道结构化问题。"
        "每题最多 4 个候选项。这是**终态工具**：调用后本轮立即结束，等待用户回答。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 60,
                            "description": "问题标识，回答时用来对应",
                        },
                        "prompt": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 300,
                            "description": "问题正文",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["single_choice", "multi_choice", "text", "date"],
                        },
                        "options": {
                            "type": "array",
                            "maxItems": 4,
                            "items": {"type": "string"},
                            "description": "选择题的候选项（2–4 个）",
                        },
                        "required": {"type": "boolean", "default": True},
                    },
                    "required": ["id", "prompt", "kind"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["questions"],
        "additionalProperties": False,
    },
    category="questionnaire",
    is_terminal=True,
    fn=_questionnaire_tool,
)


def build_questionnaire_tool(ctx: ToolContext) -> ToolDef:
    """返回绑定到本轮上下文的问卷工具（工具本身是模块级单例）。"""
    _ = ctx
    return QUESTIONNAIRE_TOOL
