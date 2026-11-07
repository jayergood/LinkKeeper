"""问卷可恢复暂停流程的集成测试。"""

from __future__ import annotations

import json

import pytest

from src.agent_service import (
    StartedRun,
    answer_questionnaire,
    cancel_questionnaire,
    start_message_run,
)
from src.agent_safety import RunGuard
from src.conversation import load_messages
from src.db.client import Db
from src.domain import SendMessageInput
from src.errors import ActiveRunError
from src.questionnaire import (
    IncompleteAnswers,
    QuestionnaireAlreadyHandled,
    build_continue_prompt,
    build_pairing_messages,
    format_answers,
    parse_questions,
)
from src.util.llm import MockLlmClient
from tests.conftest import make_contact, make_thread


# ---- 辅助 ----

ONE_QUESTION = [
    {
        "id": "when",
        "prompt": "下周哪天方便？",
        "kind": "single_choice",
        "options": ["周一", "周三", "周五"],
        "required": True,
    }
]

TWO_QUESTIONS = [
    {
        "id": "when",
        "prompt": "下周哪天方便？",
        "kind": "date",
        "required": True,
    },
    {
        "id": "where",
        "prompt": "在哪里见面？",
        "kind": "text",
        "required": False,
    },
]


def make_payload(text: str = "帮我约张三下周喝咖啡", request_id: str = "req-1", **kwargs):
    return SendMessageInput(
        text=text,
        request_id=request_id,
        **kwargs,
    )


async def wait_questionnaire_run(db: Db, thread_id: str, contact_id: str | None = None):
    """跑一轮会触发问卷的 run，返回 (run_id, questionnaire_row)。"""
    llm = MockLlmClient(
        [{"tool_calls": [{"name": "questionnaire", "arguments": {"questions": ONE_QUESTION}}]}]
    )
    started = await start_message_run(
        db,
        thread_id=thread_id,
        user_id="local-user",
        payload=make_payload(
            page_context={"contact_id": contact_id} if contact_id else {},
        ),
        llm=llm,
        guard=RunGuard(),
    )
    # 找这个 run 产出的问卷
    row = await db.query_one(
        "SELECT * FROM questionnaires WHERE run_id = :rid ORDER BY created_at DESC LIMIT 1",
        {"rid": started.run_id},
    )
    return started.run_id, row


# ---- 工具层 ----


async def test_tool_creates_record(db: Db) -> None:
    """问卷落库：questions 是 JSONB、status=pending、带 tool_call_id。"""
    thread_id = await make_thread(db)
    run_id = await wait_questionnaire_run(db, thread_id)

    questionnaire_id, normalized = run_id[1]["id"], parse_questions(run_id[1])
    assert len(normalized) == 1
    assert normalized[0].id == "when"
    assert normalized[0].kind == "single_choice"
    assert len(normalized[0].options or []) == 3


async def test_run_enters_waiting_status(db: Db) -> None:
    """run 状态必须是 waiting_questionnaire。"""
    thread_id = await make_thread(db)
    run_id, _ = await wait_questionnaire_run(db, thread_id)

    run_row = await db.query_one(
        "SELECT status FROM agent_runs WHERE id = :rid", {"rid": run_id}
    )
    assert run_row is not None
    assert run_row["status"] == "waiting_questionnaire"


async def test_tool_rejects_too_many_questions(db: Db) -> None:
    """超过 4 题 → 协议错误，不进模型循环（不创建问卷记录）。"""
    thread_id = await make_thread(db)
    many = [dict(ONE_QUESTION[0], id=f"q{i}") for i in range(5)]
    llm = MockLlmClient(
        [{"tool_calls": [{"name": "questionnaire", "arguments": {"questions": many}}]}]
    )
    await start_message_run(
        db,
        thread_id=thread_id,
        user_id="local-user",
        payload=make_payload(request_id="req-many"),
        llm=llm,
        guard=RunGuard(),
    )
    count = await db.query_one("SELECT count(*) AS n FROM questionnaires")
    assert count is not None and count["n"] == 0


async def test_tool_rejects_too_many_options(db: Db) -> None:
    """每题超过 4 个选项 → 被拒。"""
    thread_id = await make_thread(db)
    bad = [
        {
            "id": "when",
            "prompt": "哪天？",
            "kind": "single_choice",
            "options": ["一", "二", "三", "四", "五"],
        }
    ]
    llm = MockLlmClient(
        [{"tool_calls": [{"name": "questionnaire", "arguments": {"questions": bad}}]}]
    )
    await start_message_run(
        db,
        thread_id=thread_id,
        user_id="local-user",
        payload=make_payload(request_id="req-opts"),
        llm=llm,
        guard=RunGuard(),
    )
    count = await db.query_one("SELECT count(*) AS n FROM questionnaires")
    assert count is not None and count["n"] == 0


async def test_tool_rejects_choice_without_options(db: Db) -> None:
    """选择题没给选项 → 被拒。"""
    thread_id = await make_thread(db)
    bad = [{"id": "when", "prompt": "哪天？", "kind": "single_choice"}]
    llm = MockLlmClient(
        [{"tool_calls": [{"name": "questionnaire", "arguments": {"questions": bad}}]}]
    )
    await start_message_run(
        db,
        thread_id=thread_id,
        user_id="local-user",
        payload=make_payload(request_id="req-noopts"),
        llm=llm,
        guard=RunGuard(),
    )
    count = await db.query_one("SELECT count(*) AS n FROM questionnaires")
    assert count is not None and count["n"] == 0


async def test_idempotent_request(db: Db) -> None:
    """同一 request_id 重复发送 → 返回同一个 run。"""
    thread_id = await make_thread(db)
    llm = MockLlmClient([{"text": "收到"}])
    payload = make_payload(request_id="same-req")

    first = await start_message_run(
        db, thread_id=thread_id, user_id="local-user", payload=payload, llm=llm
    )
    second = await start_message_run(
        db, thread_id=thread_id, user_id="local-user", payload=payload, llm=llm
    )

    assert first.run_id == second.run_id
    assert second.existing is True


async def test_active_run_blocks(db: Db) -> None:
    """有 waiting_questionnaire 的 run 时，新请求被拒（409）。"""
    thread_id = await make_thread(db)
    await wait_questionnaire_run(db, thread_id)

    with pytest.raises(ActiveRunError):
        await start_message_run(
            db,
            thread_id=thread_id,
            user_id="local-user",
            payload=make_payload(request_id="another", text="再问一句"),
            llm=MockLlmClient([{"text": "x"}]),
        )


# ---- 恢复流程 ----


async def test_answer_resumes(db: Db) -> None:
    """回答问卷后：新 run 产生，原 run 收尾，规划链继续。"""
    thread_id = await make_thread(db)
    contact_id = await make_contact(db)
    orig_run_id, qrow = await wait_questionnaire_run(db, thread_id, contact_id)
    questionnaire_id = qrow["id"]

    # 恢复后模型直接回答
    llm = MockLlmClient([{"text": "好的，定在周三。"}])
    result = await answer_questionnaire(
        db,
        questionnaire_id=questionnaire_id,
        answers={"when": "周三"},
        llm=llm,
        guard=RunGuard(),
    )

    assert result.questionnaire_id == questionnaire_id
    assert result.run_id != orig_run_id  # 必须是新 run
    assert result.status == "completed"
    assert "周三" in result.text

    # 新 run 记录 resumed_from_run_id
    new_run = await db.query_one(
        "SELECT resumed_from_run_id, status FROM agent_runs WHERE id = :rid",
        {"rid": result.run_id},
    )
    assert new_run is not None
    assert new_run["resumed_from_run_id"] == orig_run_id
    assert new_run["status"] == "completed"

    # 原 run 已收尾
    old_run = await db.query_one(
        "SELECT status FROM agent_runs WHERE id = :rid", {"rid": orig_run_id}
    )
    assert old_run is not None
    assert old_run["status"] == "completed"

    # 问卷标记已答 + 答案落库
    answered = await db.query_one(
        "SELECT status, answers FROM questionnaires WHERE id = :qid",
        {"qid": questionnaire_id},
    )
    assert answered is not None
    assert answered["status"] == "answered"


async def test_tool_result_paired(db: Db) -> None:
    """恢复后历史里必须有配对的 toolResult：tool_call_id 一致且内容含用户答案。"""
    thread_id = await make_thread(db)
    orig_run_id, qrow = await wait_questionnaire_run(db, thread_id)
    questionnaire_id = qrow["id"]
    tool_call_id = qrow["tool_call_id"]
    assert tool_call_id, "问卷必须记录 tool_call_id（迁移 019）"

    await answer_questionnaire(
        db,
        questionnaire_id=questionnaire_id,
        answers={"when": "周三"},
        llm=MockLlmClient([{"text": "好"}]),
        guard=RunGuard(),
    )

    history = await load_messages(db, thread_id)

    # ① 找到那条 assistant tool_call
    assistant_calls = [
        call
        for msg in history
        if msg.get("role") == "assistant"
        for call in (msg.get("tool_calls") or [])
        if (call.get("function") or {}).get("name") == "questionnaire"
    ]
    assert len(assistant_calls) == 1
    call_id = assistant_calls[0]["id"]
    assert call_id == tool_call_id

    # ② 找到配对的 toolResult
    tool_msgs = [
        msg for msg in history if msg.get("role") == "tool" and msg.get("tool_call_id") == call_id
    ]
    assert len(tool_msgs) == 1, "必须有且只有一条配对的 toolResult"
    assert "周三" in tool_msgs[0]["content"]


async def test_no_duplicate_pairing(db: Db) -> None:
    """第二次恢复同一问卷不能重复补 toolResult。"""
    thread_id = await make_thread(db)
    _, qrow = await wait_questionnaire_run(db, thread_id)
    questionnaire_id = qrow["id"]
    tool_call_id = qrow["tool_call_id"]

    await answer_questionnaire(
        db,
        questionnaire_id=questionnaire_id,
        answers={"when": "周三"},
        llm=MockLlmClient([{"text": "好"}]),
        guard=RunGuard(),
    )

    # 再回答一次 → 应该被拒绝（状态已不是 pending）
    with pytest.raises(QuestionnaireAlreadyHandled):
        await answer_questionnaire(
            db,
            questionnaire_id=questionnaire_id,
            answers={"when": "周四"},
            llm=MockLlmClient([{"text": "又答一次"}]),
            guard=RunGuard(),
        )

    history = await load_messages(db, thread_id)
    tool_msgs = [
        msg for msg in history if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id
    ]
    assert len(tool_msgs) == 1, "toolResult 不能重复"


async def test_pairing_skipped_without_tool_call_id(db: Db) -> None:
    """问卷没有 tool_call_id 时不造假 toolResult，但仍能恢复。"""
    thread_id = await make_thread(db)
    _, qrow = await wait_questionnaire_run(db, thread_id)
    questionnaire_id = qrow["id"]

    # 手工清掉 tool_call_id，模拟老数据
    await db.execute(
        "UPDATE questionnaires SET tool_call_id = NULL WHERE id = :qid",
        {"qid": questionnaire_id},
    )

    result = await answer_questionnaire(
        db,
        questionnaire_id=questionnaire_id,
        answers={"when": "周三"},
        llm=MockLlmClient([{"text": "好"}]),
        guard=RunGuard(),
    )
    assert result.status == "completed"

    history = await load_messages(db, thread_id)
    # 不能有 tool 消息（因为没有可配对的 tool_call_id）
    assert not [msg for msg in history if msg.get("role") == "tool"]
    # 但要有继续指令
    assert any(
        "用户已回答" in (msg.get("content") or "") for msg in history
    )


async def test_cancelled_no_fake_tool_call(db: Db) -> None:
    """取消问卷不补 toolResult、不执行后续写操作。"""
    thread_id = await make_thread(db)
    orig_run_id, qrow = await wait_questionnaire_run(db, thread_id)
    questionnaire_id = qrow["id"]

    await cancel_questionnaire(db, questionnaire_id=questionnaire_id)

    # 问卷状态
    row = await db.query_one(
        "SELECT status FROM questionnaires WHERE id = :qid", {"qid": questionnaire_id}
    )
    assert row is not None and row["status"] == "cancelled"

    # 原 run 收尾为 aborted
    run = await db.query_one(
        "SELECT status FROM agent_runs WHERE id = :rid", {"rid": orig_run_id}
    )
    assert run is not None and run["status"] == "aborted"

    # 历史里**不能**出现伪造的 toolResult
    history = await load_messages(db, thread_id)
    assert not [msg for msg in history if msg.get("role") == "tool"]

    # 已取消的问卷不能再回答
    with pytest.raises(QuestionnaireAlreadyHandled):
        await answer_questionnaire(
            db,
            questionnaire_id=questionnaire_id,
            answers={"when": "周三"},
            llm=MockLlmClient([{"text": "x"}]),
        )


async def test_incomplete_answers_rejected(db: Db) -> None:
    """必答题未答全 → 拒绝恢复（除非 allow_partial=True）。"""
    thread_id = await make_thread(db)
    llm = MockLlmClient(
        [{"tool_calls": [{"name": "questionnaire", "arguments": {"questions": TWO_QUESTIONS}}]}]
    )
    started = await start_message_run(
        db,
        thread_id=thread_id,
        user_id="local-user",
        payload=make_payload(request_id="req-two"),
        llm=llm,
        guard=RunGuard(),
    )
    qrow = await db.query_one(
        "SELECT * FROM questionnaires WHERE run_id = :rid", {"rid": started.run_id}
    )

    # when 是必答，缺失 → 报 IncompleteAnswers
    with pytest.raises(IncompleteAnswers) as exc:
        await answer_questionnaire(
            db,
            questionnaire_id=qrow["id"],
            answers={"where": "咖啡馆"},
            llm=MockLlmClient([{"text": "x"}]),
        )
    assert "when" in exc.value.missing


async def test_multi_round_questionnaire(db: Db) -> None:
    """多轮追问：恢复后模型又问一次 → 再次进入 waiting_questionnaire。"""
    thread_id = await make_thread(db)
    _, first_qrow = await wait_questionnaire_run(db, thread_id)

    # 恢复时模型又发起一次问卷
    llm = MockLlmClient(
        [{"tool_calls": [{"name": "questionnaire", "arguments": {"questions": ONE_QUESTION}}]}]
    )
    result = await answer_questionnaire(
        db,
        questionnaire_id=first_qrow["id"],
        answers={"when": "周三"},
        llm=llm,
        guard=RunGuard(),
    )

    assert result.status == "waiting_questionnaire"

    # 应该存在第二个 pending 问卷
    pending = await db.query_one(
        "SELECT count(*) AS n FROM questionnaires WHERE thread_id = :tid AND status = 'pending'",
        {"tid": thread_id},
    )
    assert pending is not None and pending["n"] == 1


async def test_resume_failure_reverts_to_pending(db: Db) -> None:
    """恢复中模型失败：run 收尾为 failed、问卷退回 pending、答案不被吃掉。"""
    from src.util.llm import LlmError

    thread_id = await make_thread(db)
    _, qrow = await wait_questionnaire_run(db, thread_id)
    questionnaire_id = qrow["id"]
    orig_run_id = qrow["run_id"]

    # 空脚本 + exhausted="error" → 第一次 chat 就抛 LlmError
    with pytest.raises(LlmError):
        await answer_questionnaire(
            db,
            questionnaire_id=questionnaire_id,
            answers={"when": "周三"},
            llm=MockLlmClient([], exhausted="error"),
            guard=RunGuard(),
        )

    # ① 新 run 已收尾为 failed，没有 running 残留
    stuck = await db.query_one(
        "SELECT count(*) AS n FROM agent_runs WHERE thread_id = :tid AND status = 'running'",
        {"tid": thread_id},
    )
    assert stuck is not None and stuck["n"] == 0

    failed = await db.query_one(
        "SELECT status, error FROM agent_runs WHERE resumed_from_run_id = :rid",
        {"rid": orig_run_id},
    )
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["error"]

    # ② 问卷退回 pending
    q = await db.query_one(
        "SELECT status, answers FROM questionnaires WHERE id = :qid", {"qid": questionnaire_id}
    )
    assert q is not None
    assert q["status"] == "pending"
    assert q["answers"] is None

    # ③ 失败没写入任何消息：历史里只有 user 原话 + assistant tool_call
    history = await load_messages(db, thread_id)
    assert not [msg for msg in history if msg.get("role") == "tool"]
    assert not [msg for msg in history if "用户已回答" in (msg.get("content") or "")]

    # ④ 可以重提（request_id 不能撞 UNIQUE）
    result = await answer_questionnaire(
        db,
        questionnaire_id=questionnaire_id,
        answers={"when": "周四"},
        llm=MockLlmClient([{"text": "好，周四。"}]),
        guard=RunGuard(),
    )
    assert result.status == "completed"
    assert "周四" in result.text

    q2 = await db.query_one(
        "SELECT status, answers FROM questionnaires WHERE id = :qid", {"qid": questionnaire_id}
    )
    assert q2 is not None and q2["status"] == "answered"


# ---- 纯函数 ----


def test_format_answers() -> None:
    from src.domain import QuestionItem

    questions = [
        QuestionItem(
            id="when", prompt="哪天？", kind="single_choice", options=["周一", "周三"]
        ),
        QuestionItem(id="where", prompt="在哪？", kind="text", required=False),
    ]
    text = format_answers(questions, {"when": "周三"})
    assert "哪天？ → 周三" in text
    assert "在哪？ → （用户跳过）" in text


def test_build_pairing_skips_when_already_paired() -> None:
    from src.domain import QuestionItem

    questions = [QuestionItem(id="when", prompt="哪天？", kind="text")]
    assert (
        build_pairing_messages(
            tool_call_id="call_1",
            questions=questions,
            answers={"when": "周三"},
            already_paired=True,
        )
        == []
    )

    msgs = build_pairing_messages(
        tool_call_id="call_1",
        questions=questions,
        answers={"when": "周三"},
        already_paired=False,
    )
    assert len(msgs) == 1
    assert msgs[0]["role"] == "tool"
    assert msgs[0]["tool_call_id"] == "call_1"
    assert "周三" in msgs[0]["content"]


def test_build_continue_prompt_mentions_no_repeat() -> None:
    from src.domain import QuestionItem

    questions = [QuestionItem(id="when", prompt="哪天？", kind="text")]
    prompt = build_continue_prompt(questions, {"when": "周三"})
    assert "不要重复询问已经回答过的问题" in prompt
