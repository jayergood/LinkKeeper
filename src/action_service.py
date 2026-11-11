"""Action 的确认与拒绝：把一张待确认的卡片变成真实的写入。

`propose_action` 只往 `actions` 插一行；真正改数据必须经过这里的 `confirm_action`，
改完留下 `action_receipts` 记录系统实际写了什么。

状态机：proposed → confirmed → executed / failed / conflict，proposed → rejected。

幂等靠一条 CAS 语句完成抢占：`UPDATE ... WHERE status = 'proposed'`。
`UPDATE` 会锁住被改的行，影响行数直接就是「有没有抢到执行权」，因此不需要额外的
显式行锁。同一张卡片被确认两次时，第二个请求会等第一个提交，看到状态已不是
`proposed`，影响 0 行，转去返回已有回执。

成功路径的「抢占 + 改业务数据 + 写回执 + 改状态」必须在同一事务内；失败时事务整体
回滚，抢占也会撤销，因此失败痕迹要另开事务写。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .adapters import calendar, contacts
from .domain import ActionPayload
from .errors import AppError, ConflictError, NotFoundError
from .tools.action import parse_payload

log = logging.getLogger(__name__)

# payload.type → 真正写库的执行器。执行器接收事务连接，加入调用方的事务。
EXECUTORS = {
    "create_contact": contacts.create_contact,
    "update_contact": contacts.update_contact,
    "merge_contacts": contacts.merge_contacts,
    "create_event": calendar.create_event,
    "update_event": calendar.update_event,
    "delete_event": calendar.delete_event,
}


@dataclass
class ActionResult:
    """一次确认 / 拒绝的结果。"""

    action_id: str
    status: str
    receipt_id: str | None = None
    receipt_status: str | None = None
    actual_result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    idempotent: bool = False
    """True 表示这次调用是重复确认，直接返回了已有回执、没有再次执行。"""


async def load_action(db: Any, action_id: str, user_id: str) -> dict[str, Any]:
    """读取 Action 并校验它属于当前用户。"""
    row = await db.query_one(
        """
        SELECT a.id, a.thread_id, a.contact_id, a.type, a.payload, a.status,
               a.version, a.base_version, a.base_version_source,
               a.evidence_ids, a.rationale
        FROM actions a
        JOIN threads t ON t.id = a.thread_id
        WHERE a.id = :aid AND t.user_id = :uid
        """,
        {"aid": action_id, "uid": user_id},
    )
    if row is None:
        raise NotFoundError(f"Action {action_id} 不存在")
    return row


async def load_receipt(db: Any, action_id: str) -> dict[str, Any] | None:
    """读取 Action 的回执；没有则返回 None。"""
    return await db.query_one(
        """
        SELECT id, action_id, status, actual_result, error, receipt_at
        FROM action_receipts
        WHERE action_id = :aid
        """,
        {"aid": action_id},
    )


def _result_from_receipt(
    action_id: str, receipt: dict[str, Any], *, idempotent: bool
) -> ActionResult:
    success = receipt["status"] == "success"
    return ActionResult(
        action_id=action_id,
        status="executed" if success else "failed",
        receipt_id=str(receipt["id"]),
        receipt_status=receipt["status"],
        actual_result=receipt.get("actual_result") or {},
        error=receipt.get("error"),
        idempotent=idempotent,
    )


async def _already_handled(db: Any, action_id: str, user_id: str) -> ActionResult:
    """抢占失败时的统一出口：有回执就返回它，否则说明状态不允许确认。"""
    receipt = await load_receipt(db, action_id)
    if receipt is not None:
        return _result_from_receipt(action_id, receipt, idempotent=True)

    action = await load_action(db, action_id, user_id)
    if action["status"] == "rejected":
        raise ConflictError(f"Action {action_id} 已被拒绝，不能确认")
    raise ConflictError(f"Action {action_id} 当前状态是 {action['status']}，不能确认")


async def _record_failure(db: Any, action_id: str, status: str, error: str) -> None:
    """在新事务里记录失败痕迹。

    走到这里时上一个事务已经回滚，抢占也一并撤销（状态退回 proposed），
    所以能按 proposed 重新标记。两条写语句放在一个事务里，避免半截状态。
    """
    async with db.transaction() as tx:
        await tx.execute(
            """
            UPDATE actions SET status = :status
            WHERE id = :aid AND status IN ('proposed', 'confirmed')
            """,
            {"status": status, "aid": action_id},
        )
        await tx.execute(
            """
            INSERT INTO action_receipts (action_id, status, error)
            VALUES (:aid, 'failed', :err)
            ON CONFLICT (action_id) DO NOTHING
            """,
            {"aid": action_id, "err": error[:2000]},
        )


async def confirm_action(
    db: Any,
    *,
    action_id: str,
    user_id: str = "local-user",
) -> ActionResult:
    """确认一张卡片：执行写入并记录回执。"""
    action = await load_action(db, action_id, user_id)

    # 快速幂等：已经执行过就直接返回原回执，不必再进事务
    existing = await load_receipt(db, action_id)
    if existing is not None:
        return _result_from_receipt(action_id, existing, idempotent=True)

    payload: ActionPayload = parse_payload(action["payload"])
    evidence_ids = list(action["evidence_ids"] or [])
    executor = EXECUTORS[payload.type]

    claimed = 0
    actual_result: dict[str, Any] = {}
    receipt_row: dict[str, Any] | None = None

    try:
        # 抢占 + 改业务数据 + 写回执 + 改状态，全部在同一个事务里。
        # 执行器接收的是 tx 而不是 db，所以它写的改动不会单独提交。
        async with db.transaction() as tx:
            claimed = await tx.execute(
                """
                UPDATE actions SET status = 'confirmed', decided_at = now()
                WHERE id = :aid AND status = 'proposed'
                """,
                {"aid": action_id},
            )
            if claimed:
                actual_result = await executor(tx, payload, evidence_ids)

                receipt_row = await tx.execute_returning(
                    """
                    INSERT INTO action_receipts (action_id, status, actual_result)
                    VALUES (:aid, 'success', :result)
                    RETURNING id
                    """,
                    {
                        "aid": action_id,
                        "result": json.dumps(
                            actual_result, ensure_ascii=False, default=str
                        ),
                    },
                )
                await tx.execute(
                    "UPDATE actions SET status = 'executed', executed_at = now() WHERE id = :aid",
                    {"aid": action_id},
                )
    except ConflictError as exc:
        # 事务已回滚（抢占也撤销了），另开事务留下失败痕迹
        await _record_failure(db, action_id, "conflict", str(exc))
        raise
    except AppError as exc:
        await _record_failure(db, action_id, "failed", str(exc))
        raise
    except Exception as exc:  # noqa: BLE001 —— 任何异常都要留下失败痕迹
        log.exception("Action %s 执行失败", action_id)
        await _record_failure(db, action_id, "failed", repr(exc))
        raise

    if not claimed:
        return await _already_handled(db, action_id, user_id)

    assert receipt_row is not None
    return ActionResult(
        action_id=action_id,
        status="executed",
        receipt_id=str(receipt_row["id"]),
        receipt_status="success",
        actual_result=actual_result,
        idempotent=False,
    )


async def reject_action(
    db: Any,
    *,
    action_id: str,
    user_id: str = "local-user",
) -> ActionResult:
    """拒绝一张卡片：只改状态，不写任何业务数据、不写回执。"""
    await load_action(db, action_id, user_id)

    rejected = await db.execute(
        """
        UPDATE actions SET status = 'rejected', decided_at = now()
        WHERE id = :aid AND status = 'proposed'
        """,
        {"aid": action_id},
    )
    if not rejected:
        raise ConflictError(f"Action {action_id} 已处理，不能重复拒绝")

    return ActionResult(action_id=action_id, status="rejected")
