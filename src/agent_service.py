"""一轮请求的编排：把 HTTP 请求翻译成一次 Agent run，并在结束后把结果落库。

这一层位于 tool loop 之外，只负责准备 ToolContext 与收好结果，不实现工具。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .agent import AgentResult, run_tool_loop
from .agent_safety import RunGuard, assert_image_limits
from .config import settings
from .conversation import append_message, append_messages, has_paired_tool_result, load_messages
from .domain import SendMessageInput, ToolContext
from .errors import ActiveRunError, BudgetExceeded, NotFoundError
from .evidence import create_screenshot_evidence, create_text_evidence
from .memory.snapshot import assemble_snapshot
from .questionnaire import (
    IncompleteAnswers,
    QuestionnaireAlreadyHandled,
    QuestionnaireNotFound,
    ResumeResult,
    build_continue_prompt,
    build_pairing_messages,
    find_unanswered,
    load_questionnaire,
    mark_answered,
    mark_cancelled,
    parse_questions,
)
from .tools.index import build_tools
from .util.ids import new_id
from .util.llm import LlmClient, MockLlmClient

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "main-agent.md"
PROMPT_VERSION = "p3"


def load_system_prompt_base() -> str:
    """读主 Agent 的 prompt 模板（进程内缓存）。"""
    global _base_prompt
    if _base_prompt is None:
        _base_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    return _base_prompt


_base_prompt: str | None = None


def build_system_prompt(snapshot_rendered: str, compaction_summary: str | None = None) -> str:
    """把快照拼进 system prompt：基础规则 → MemorySnapshot →（可选）压缩摘要。"""
    parts = [load_system_prompt_base(), "", "## MemorySnapshot", snapshot_rendered]
    if compaction_summary:
        parts.extend(["", "## 更早会话的压缩摘要", compaction_summary])
    return "\n".join(parts)


class StartedRun:
    """一次 run 的启动结果。"""

    __slots__ = ("run_id", "thread_id", "existing")

    def __init__(self, *, run_id: str, thread_id: str, existing: bool) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        self.existing = existing

    def __repr__(self) -> str:
        return f"<StartedRun {self.run_id}{' (existing)' if self.existing else ''}>"


# ---- 证据写入 ----


async def collect_evidence(
    db: Any,
    *,
    thread_id: str,
    contact_id: str | None,
    payload: SendMessageInput,
    user_id: str,
) -> list[str]:
    """把本轮输入写成证据，返回 evidence id 列表：文字 → user_note，每张图 → screenshot。"""
    assert_image_limits(payload.images)
    evidence_ids: list[str] = []

    if payload.text:
        ref = await create_text_evidence(
            db,
            thread_id=thread_id,
            contact_id=contact_id,
            text=payload.text,
            source_summary="用户输入",
            user_id=user_id,
        )
        evidence_ids.append(ref.id)

    for image in payload.images:
        import base64

        raw = base64.b64decode(image.data_base64)
        ref = await create_screenshot_evidence(
            db,
            thread_id=thread_id,
            contact_id=contact_id,
            data=raw,
            mime_type=image.mime_type,
            evidence_dir=settings.evidence_dir,
            ttl_hours=settings.evidence_ttl_hours,
            user_id=user_id,
        )
        evidence_ids.append(ref.id)

    return evidence_ids


# ---- run 生命周期 ----


async def ensure_thread_exists(db: Any, thread_id: str, user_id: str) -> None:
    """确认会话属于当前用户。"""
    row = await db.query_one(
        "SELECT id FROM threads WHERE id = :tid AND user_id = :uid",
        {"tid": thread_id, "uid": user_id},
    )
    if row is None:
        raise NotFoundError("会话不存在")


async def assert_no_active_run(db: Any, thread_id: str) -> None:
    """同一会话不能有两个在途 run，否则会并发推进消息历史导致 seq 冲突与重复执行工具。"""
    row = await db.query_one(
        """
        SELECT id FROM agent_runs
        WHERE thread_id = :tid
          AND status IN ('running', 'waiting_questionnaire', 'waiting_permission')
        LIMIT 1
        """,
        {"tid": thread_id},
    )
    if row is not None:
        raise ActiveRunError(
            "该会话还有未完成的请求（等待回答或正在处理），请先处理完再发新消息"
        )


async def find_run_by_request(db: Any, thread_id: str, request_id: str) -> str | None:
    """request_id 幂等：同一 thread 重复发送只产生一个 run。"""
    row = await db.query_one(
        "SELECT id FROM agent_runs WHERE thread_id = :tid AND request_id = :rid",
        {"tid": thread_id, "rid": request_id},
    )
    return str(row["id"]) if row else None


async def create_run(
    db: Any,
    *,
    thread_id: str,
    request_id: str,
    model: str,
    permission_mode: str,
    page_context: dict[str, Any],
    resumed_from_run_id: str | None = None,
) -> str:
    run_id = new_id()
    await db.execute(
        """
        INSERT INTO agent_runs
          (id, thread_id, request_id, status, model, prompt_version,
           permission_mode, page_context, resumed_from_run_id)
        VALUES
          (:id, :tid, :rid, 'running', :model, :pv, :pm, :pc, :resumed)
        """,
        {
            "id": run_id,
            "tid": thread_id,
            "rid": request_id,
            "model": model,
            "pv": PROMPT_VERSION,
            "pm": permission_mode,
            "pc": json.dumps(page_context, ensure_ascii=False),
            "resumed": resumed_from_run_id,
        },
    )
    return run_id


async def finish_run(
    db: Any,
    *,
    run_id: str,
    status: str,
    usage: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    await db.execute(
        """
        UPDATE agent_runs
        SET status = :status, usage = :usage, error = :error, ended_at = now()
        WHERE id = :id
        """,
        {
            "id": run_id,
            "status": status,
            "usage": json.dumps(usage or {}, ensure_ascii=False),
            "error": error,
        },
    )


async def emit_event(db: Any, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """写一条运行事件（SSE 回放用）。"""
    await db.execute(
        """
        INSERT INTO agent_events (run_id, seq, type, payload)
        VALUES (:rid, (SELECT COALESCE(MAX(seq), 0) + 1 FROM agent_events WHERE run_id = :rid),
                :type, :payload)
        """,
        {
            "rid": run_id,
            "type": event_type,
            "payload": json.dumps(payload, ensure_ascii=False),
        },
    )


# ---- 主流程 ----


async def start_message_run(
    db: Any,
    *,
    thread_id: str,
    user_id: str,
    payload: SendMessageInput,
    llm: LlmClient | MockLlmClient | None = None,
    guard: RunGuard | None = None,
    max_turns: int = 8,
) -> StartedRun:
    """处理一条用户消息，跑完一轮 Agent，返回 StartedRun。"""
    await ensure_thread_exists(db, thread_id, user_id)

    # 幂等：同一个 request_id 直接返回旧 run
    existing_run = await find_run_by_request(db, thread_id, payload.request_id)
    if existing_run is not None:
        log.info("request_id %s 命中幂等，返回已有 run %s", payload.request_id, existing_run)
        return StartedRun(run_id=existing_run, thread_id=thread_id, existing=True)

    # 互斥：不能有在途 run
    await assert_no_active_run(db, thread_id)

    contact_id = payload.page_context.contact_id

    # 证据先入库
    evidence_ids = await collect_evidence(
        db, thread_id=thread_id, contact_id=contact_id, payload=payload, user_id=user_id
    )

    # 创建 run
    run_id = await create_run(
        db,
        thread_id=thread_id,
        request_id=payload.request_id,
        model=getattr(llm, "model", settings.llm_model),
        permission_mode=payload.permission_mode,
        page_context=payload.page_context.model_dump(mode="json"),
    )

    # 装配快照 → system prompt
    snapshot_result = await assemble_snapshot(db, contact_id=contact_id, thread_id=thread_id)
    system_prompt = build_system_prompt(snapshot_result.rendered)

    # 构建工具（per-run 闭包，绑定作用域）
    ctx = ToolContext(
        user_id=user_id,
        thread_id=thread_id,
        run_id=run_id,
        contact_id=contact_id,
        evidence_ids=evidence_ids,
        permission_mode=payload.permission_mode,
        max_tool_calls=settings.max_tool_calls,
        db=db,
    )
    tools = build_tools(ctx)

    # 历史消息 + 本轮输入
    history = await load_messages(db, thread_id)
    user_message: dict[str, Any] = {"role": "user", "content": payload.text or ""}
    if payload.images:
        # 图片已经作为 evidence 落盘，这里只提示模型去看证据
        user_message["content"] = (
            (payload.text or "")
            + f"\n\n（本轮附带了 {len(payload.images)} 张截图，"
            f"evidence id: {', '.join(evidence_ids)}。请先用 record_screenshot_understanding 理解它们。）"
        )
    history.append(user_message)
    await append_message(db, thread_id, user_message)

    # 执行 tool loop
    effective_llm = llm or LlmClient()
    effective_guard = guard or RunGuard(
        max_turns=max_turns,
        max_tool_calls=settings.max_tool_calls,
        max_seconds=settings.max_run_seconds,
    )

    try:
        result: AgentResult = await run_tool_loop(
            llm=effective_llm,
            system_prompt=system_prompt,
            messages=history,
            tools=tools,
            ctx=ctx,
            guard=effective_guard,
            max_turns=max_turns,
        )
    except BudgetExceeded as exc:
        await finish_run(
            db, run_id=run_id, status="failed", error=str(exc), usage=effective_guard.snapshot()
        )
        await emit_event(db, run_id, "error", {"message": str(exc)})
        raise
    except Exception as exc:  # noqa: BLE001 —— 任何异常都要把 run 收尾
        log.exception("run %s 执行失败", run_id)
        await finish_run(db, run_id=run_id, status="failed", error=str(exc))
        await emit_event(db, run_id, "error", {"message": str(exc)})
        raise

    # transcript 落库：本轮产生的 assistant / tool 消息。
    # 不落库的话，恢复问卷时 find_last_tool_call() 找不到那条 assistant.tool_call，
    # 补配对就无对象可补。user 消息已单独存过，这里不含它。
    if result.messages:
        await append_messages(db, thread_id, result.messages)

    # 收尾：状态映射
    await finish_run(
        db, run_id=run_id, status=result.status, usage=result.usage or effective_guard.snapshot()
    )
    await emit_event(
        db,
        run_id,
        "run_completed",
        {
            "status": result.status,
            "text": result.text,
            "terminal": result.terminal.model_dump() if result.terminal else None,
        },
    )

    return StartedRun(run_id=run_id, thread_id=thread_id, existing=False)


async def answer_questionnaire(
    db: Any,
    *,
    questionnaire_id: str,
    answers: dict[str, Any],
    user_id: str = "local-user",
    llm: LlmClient | MockLlmClient | None = None,
    guard: RunGuard | None = None,
    max_turns: int = 8,
    allow_partial: bool = False,
) -> ResumeResult:
    """用户回答问卷后，新建 run 恢复同一规划链。"""
    # 读取问卷并校验
    row=await load_questionnaire(db,questionnaire_id,user_id)
    if row["status"]!="pending":
        raise QuestionnaireAlreadyHandled("问卷已处理")
    orig=await db.query_one(
        "SELECT page_context, permission_mode FROM agent_runs WHERE id = :rid",
        {"rid": row["run_id"]},
    )
    questions=parse_questions(row)
    missing=find_unanswered(questions,answers)
    if missing and not allow_partial:
        raise IncompleteAnswers(missing)
    # 标记已回答 —— CAS：并发双击提交时只有一个能过
    if not await mark_answered(db, questionnaire_id, answers):
        raise QuestionnaireAlreadyHandled("问卷已被其他请求处理")
    # 读历史，补配对
    history = await load_messages(db,row["thread_id"])
    # 已经配对过（has_paired_tool_result=True）→ 不能重复补
    if not row["tool_call_id"]:
        paired=[]
    else:    
        paired= build_pairing_messages(
        tool_call_id=row["tool_call_id"],
        questions=questions,
        answers=answers,
        already_paired=has_paired_tool_result(history,row["tool_call_id"]),
        )

    # 构造继续指令 + 新建 run
    continue_msg = {
           "role": "user",
           "content": build_continue_prompt(questions, answers),
       }

    thread_id = row["thread_id"]
    page_ctx = orig["page_context"] or {}
    contact_id = page_ctx.get("contact_id")

    # request_id 带尝试次数：恢复失败后要允许用户重提，否则会撞
    # UNIQUE(thread_id, request_id)。双击幂等已由 mark_answered 的 CAS 保证。
    prefix = f"resume-questionnaire-{questionnaire_id}"
    done = await db.query_one(
        "SELECT count(*) AS n FROM agent_runs WHERE thread_id = :tid AND request_id LIKE :p",
        {"tid": thread_id, "p": prefix + "%"},
    )
    attempt = int((done or {}).get("n") or 0) + 1

    new_run_id = await create_run(
           db,
           thread_id=thread_id,
           request_id=f"{prefix}-{attempt}",
           model=getattr(llm, "model", settings.llm_model),
           permission_mode=orig["permission_mode"],
           page_context=page_ctx,                                   # 沿用原 run 的
           resumed_from_run_id=row["run_id"],                       # 审计链
       )

    snapshot = await assemble_snapshot(db, contact_id=contact_id, thread_id=thread_id)
    system_prompt = build_system_prompt(snapshot.rendered)
    ctx = ToolContext(
        user_id=user_id,
        thread_id=thread_id,
        run_id=new_run_id,
        contact_id=contact_id,
        permission_mode=orig["permission_mode"],
        max_tool_calls=settings.max_tool_calls,
        db=db,
    )
    tools = build_tools(ctx)
    effective_llm = llm or LlmClient()
    effective_guard = guard or RunGuard(
        max_turns=max_turns,
        max_tool_calls=settings.max_tool_calls,
        max_seconds=settings.max_run_seconds,
    )

    try:
        result = await run_tool_loop(
            llm=effective_llm,
            system_prompt=system_prompt,
            messages=history + paired + [continue_msg],   # 完整历史
            tools=tools,
            ctx=ctx,
            guard=effective_guard,
            max_turns=max_turns,
        )
    except Exception as exc:  # noqa: BLE001 —— 任何异常都要收尾，否则 thread 被锁死
        log.exception("恢复问卷 %s 失败", questionnaire_id)
        await finish_run(db, run_id=new_run_id, status="failed", error=str(exc))
        await emit_event(db, new_run_id, "error", {"message": str(exc)})
        # 补偿：把问卷退回 pending，让用户能重新提交。
        # 注意：paired / continue_msg / transcript 都还没落库，
        # 所以重提不会留下重复的「用户已回答」消息。
        await db.execute(
            """
            UPDATE questionnaires
            SET status = 'pending', answers = NULL, answered_at = NULL
            WHERE id = :qid AND status = 'answered'
            """,
            {"qid": questionnaire_id},
        )
        # 原 run 保持 completed 不回滚：它的使命确实完成过（产出问卷、拿到答案），
        # 失败的是「恢复」这一次，不该改写历史。
        raise

    # 成功后一次性落库：补配对 + 继续指令 + 本轮 transcript。
    # 放在成功之后而不是之前，是为了失败时什么都不写（重提才干净）。
    to_persist = paired + [continue_msg] + list(result.messages)
    if to_persist:
        await append_messages(db, thread_id, to_persist)

    await finish_run(db, run_id=new_run_id, status=result.status, usage=result.usage)
    await emit_event(
        db,
        new_run_id,
        "run_completed",
        {
            "status": result.status,
            "text": result.text,
            "terminal": result.terminal.model_dump() if result.terminal else None,
        },
    )

    return ResumeResult(
               run_id=new_run_id,
               thread_id=row["thread_id"],
               questionnaire_id=questionnaire_id,
               status=result.status,
               text=result.text,
           )


async def cancel_questionnaire(
    db: Any, *, questionnaire_id: str, user_id: str = "local-user"
) -> None:
    """用户取消问卷：问卷 → cancelled，原 run → aborted。

    不补任何 tool 消息，也不执行后续写操作，答案不会升级为长期事实。
    """
    row = await load_questionnaire(db, questionnaire_id, user_id)
    if row["status"] != "pending":
        raise QuestionnaireAlreadyHandled(f"问卷已经是 {row['status']} 状态")
    await mark_cancelled(db, questionnaire_id)
    log.info("问卷 %s 已被用户取消", questionnaire_id)
