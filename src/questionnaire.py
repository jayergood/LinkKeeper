"""问卷的状态流转与恢复消息构造。

终态工具的 assistant tool_call 在历史里只留下半条记录，恢复时补成配对的 toolResult。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .domain import QuestionItem, ToolContext

log = logging.getLogger(__name__)


class QuestionnaireNotFound(Exception):
    """问卷不存在或不属于当前用户。"""


class QuestionnaireAlreadyHandled(Exception):
    """问卷已经被回答或取消过。"""


class IncompleteAnswers(Exception):
    """必答题未答全；调用方可以拒绝恢复，也可以把缺失项当作「用户跳过」。"""

    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"必答题未回答：{', '.join(missing)}")
        self.missing = missing


# ---- 读取与校验 ----


async def load_questionnaire(db: Any, questionnaire_id: str, user_id: str) -> dict[str, Any]:
    """读取问卷（校验归属当前用户）。"""
    row = await db.query_one(
        """
        SELECT q.id, q.run_id, q.thread_id, q.questions, q.answers, q.status, q.tool_call_id
        FROM questionnaires q
        JOIN threads t ON t.id = q.thread_id
        WHERE q.id = :qid AND t.user_id = :uid
        """,
        {"qid": questionnaire_id, "uid": user_id},
    )
    if row is None:
        raise QuestionnaireNotFound(f"问卷 {questionnaire_id} 不存在")
    return row


def parse_questions(row: dict[str, Any]) -> list[QuestionItem]:
    """把 JSONB 里的问题列表转成 Pydantic 对象。"""
    raw = row.get("questions") or []
    if isinstance(raw, str):
        raw = json.loads(raw)
    return [QuestionItem.model_validate(item) for item in raw]


def find_unanswered(questions: list[QuestionItem], answers: dict[str, Any]) -> list[str]:
    """找出未回答的必答题 ID。"""
    missing: list[str] = []
    for question in questions:
        if not question.required:
            continue
        value = answers.get(question.id)
        if value is None or value == "" or value == []:
            missing.append(question.id)
    return missing


# ---- 答案格式化（构造 toolResult 内容） ----


def format_answers(questions: list[QuestionItem], answers: dict[str, Any]) -> str:
    """把问答对格式化成「问题 → 答案」清单；模型对清单的理解比裸 JSON 更稳，也更省 token。"""
    lines: list[str] = []
    for question in questions:
        value = answers.get(question.id)
        if isinstance(value, list):
            answer_text = "、".join(str(item) for item in value) if value else "（未回答）"
        elif value is None or value == "":
            answer_text = "（用户跳过）"
        else:
            answer_text = str(value)
        lines.append(f"- {question.prompt} → {answer_text}")
    return "\n".join(lines)


def build_tool_result_content(questions: list[QuestionItem], answers: dict[str, Any]) -> str:
    """构造补进历史的那条 tool 消息的 content。"""
    body = format_answers(questions, answers)
    return f"问卷已由用户回答：\n{body}"


def build_pairing_messages(
    *,
    tool_call_id: str,
    questions: list[QuestionItem],
    answers: dict[str, Any],
    already_paired: bool,
) -> list[dict[str, Any]]:
    """构造「补配对」需要的消息。

    已配对时返回空列表；取消的问卷不调这个函数，不制造空工具调用。
    """
    if already_paired:
        return []
    return [
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": build_tool_result_content(questions, answers),
        }
    ]


def build_continue_prompt(questions: list[QuestionItem], answers: dict[str, Any]) -> str:
    """构造恢复后的推进指令（作为一条 user 消息）。

    toolResult 只是数据，还需要一条明确的「继续」指令，否则模型可能只复述答案。
    """
    body = format_answers(questions, answers)
    return (
        "用户已回答上一轮的问卷：\n"
        f"{body}\n\n"
        "请基于这些回答继续原来的任务。"
        "如果仍然缺关键信息，可以再次使用 questionnaire 追问；"
        "不要重复询问已经回答过的问题。"
    )


# ---- 状态标记 ----


async def mark_answered(
    db: Any, questionnaire_id: str, answers: dict[str, Any]
) -> bool:
    """把问卷标记为已回答并落库答案，返回本次调用是否真的完成了状态迁移。

    CAS：WHERE 条件是 status = 'pending'，并发双击时第二个拿到 0 行，
    调用方必须把 False 当成「已被别人处理」拒掉，否则会开出两个 run。
    """
    async with db.transaction() as tx:
        updated = await tx.execute(
            """
            UPDATE questionnaires
            SET status = 'answered', answers = :answers, answered_at = now()
            WHERE id = :qid AND status = 'pending'
            """,
            {"qid": questionnaire_id, "answers": json.dumps(answers, ensure_ascii=False)},
        )
        if not updated:
            return False
        await tx.execute(
            """
            UPDATE agent_runs SET status = 'completed', ended_at = now()
            WHERE id = (SELECT run_id FROM questionnaires WHERE id = :qid)
              AND status = 'waiting_questionnaire'
            """,
            {"qid": questionnaire_id},
        )
    return True


async def mark_cancelled(db: Any, questionnaire_id: str) -> None:
    """把问卷标记为 cancelled，原 run 收尾为 aborted；不执行任何后续写操作。"""
    async with db.transaction() as tx:
        await tx.execute(
            "UPDATE questionnaires SET status = 'cancelled' WHERE id = :qid AND status = 'pending'",
            {"qid": questionnaire_id},
        )
        await tx.execute(
            """
            UPDATE agent_runs SET status = 'aborted', ended_at = now()
            WHERE id = (SELECT run_id FROM questionnaires WHERE id = :qid)
              AND status = 'waiting_questionnaire'
            """,
            {"qid": questionnaire_id},
        )


# ---- 恢复结果 ----


class ResumeResult:
    """恢复后的结果。"""

    __slots__ = ("run_id", "status", "text", "thread_id", "questionnaire_id")

    def __init__(
        self,
        *,
        run_id: str,
        thread_id: str,
        questionnaire_id: str,
        status: str,
        text: str,
    ) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        self.questionnaire_id = questionnaire_id
        self.status = status
        self.text = text

    def __repr__(self) -> str:
        return f"<ResumeResult run={self.run_id} status={self.status}>"
