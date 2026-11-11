"""Action Card 的提案、确认与拒绝。"""

from __future__ import annotations

import pytest

from src.action_service import confirm_action, reject_action
from src.db.client import Db
from src.domain import ToolContext
from src.errors import ConflictError
from src.tools.index import build_tools, execute_tool
from tests.conftest import make_contact, make_evidence, make_thread

UPDATE_COMPANY = {
    "type": "update_contact",
    "contact_id": None,  # 由 helper 填
    "patch": {"company": "腾讯"},
}


# ---- 辅助 ----


async def propose(
    db: Db,
    *,
    thread_id: str,
    contact_id: str | None,
    evidence_id: str,
    payload: dict,
    rationale: str = "用户说张三跳槽了",
):
    """走真实的工具调用链路发起一次提案。"""
    ctx = ToolContext(
        user_id="local-user",
        thread_id=thread_id,
        run_id="run-for-test",
        contact_id=contact_id,
        db=db,
    )
    tools = build_tools(ctx)
    return await execute_tool(
        ctx,
        tools,
        "propose_action",
        {"payload": payload, "rationale": rationale, "evidence_ids": [evidence_id]},
    )


async def latest_action(db: Db) -> dict:
    row = await db.query_one(
        "SELECT * FROM actions ORDER BY created_at DESC LIMIT 1", {}
    )
    assert row is not None, "没有创建任何 Action"
    return row


async def contact_version(db: Db, contact_id: str) -> int:
    row = await db.query_one(
        "SELECT version FROM contacts WHERE id = :cid", {"cid": contact_id}
    )
    assert row is not None
    return int(row["version"])


async def fact_count(db: Db, contact_id: str, status: str = "confirmed") -> int:
    row = await db.query_one(
        "SELECT count(*) AS n FROM contact_facts WHERE contact_id = :cid AND status = :s",
        {"cid": contact_id, "s": status},
    )
    assert row is not None
    return int(row["n"])


# ============================================================
# 提案层（已实现，应当全绿）
# ============================================================


async def test_proposal_does_not_write_business_tables(db: Db) -> None:
    """提案只在 actions 落一行，联系人数据一个字都不能变。"""
    thread_id = await make_thread(db)
    contact_id = await make_contact(db)
    evidence_id = await make_evidence(db, thread_id=thread_id, contact_id=contact_id)

    payload = dict(UPDATE_COMPANY, contact_id=contact_id)
    result = await propose(
        db,
        thread_id=thread_id,
        contact_id=contact_id,
        evidence_id=evidence_id,
        payload=payload,
    )

    assert result.terminate is True
    assert not result.is_error

    assert await contact_version(db, contact_id) == 1
    assert await fact_count(db, contact_id) == 0

    action = await latest_action(db)
    assert action["status"] == "proposed"
    assert action["type"] == "update_contact"


async def test_proposal_stamps_server_version(db: Db) -> None:
    """模型没给 base_version 时，服务端把当前版本盖上去并标记来源。"""
    thread_id = await make_thread(db)
    contact_id = await make_contact(db)
    evidence_id = await make_evidence(db, thread_id=thread_id, contact_id=contact_id)

    await propose(
        db,
        thread_id=thread_id,
        contact_id=contact_id,
        evidence_id=evidence_id,
        payload=dict(UPDATE_COMPANY, contact_id=contact_id),
    )

    action = await latest_action(db)
    assert action["base_version"] == 1
    assert action["base_version_source"] == "server"


async def test_proposal_uses_model_supplied_version(db: Db) -> None:
    """模型自己给了且正确时，标记来源为 model。"""
    thread_id = await make_thread(db)
    contact_id = await make_contact(db, version=3)
    evidence_id = await make_evidence(db, thread_id=thread_id, contact_id=contact_id)

    await propose(
        db,
        thread_id=thread_id,
        contact_id=contact_id,
        evidence_id=evidence_id,
        payload=dict(UPDATE_COMPANY, contact_id=contact_id, base_version=3),
    )

    action = await latest_action(db)
    assert action["base_version"] == 3
    assert action["base_version_source"] == "model"


async def test_proposal_rejects_stale_model_version(db: Db) -> None:
    """模型给错版本号时直接拒绝，不创建 Action。"""
    thread_id = await make_thread(db)
    contact_id = await make_contact(db, version=5)
    evidence_id = await make_evidence(db, thread_id=thread_id, contact_id=contact_id)

    result = await propose(
        db,
        thread_id=thread_id,
        contact_id=contact_id,
        evidence_id=evidence_id,
        payload=dict(UPDATE_COMPANY, contact_id=contact_id, base_version=2),
    )

    assert result.is_error
    assert "stale_base_version" in result.text

    row = await db.query_one("SELECT count(*) AS n FROM actions", {})
    assert row is not None and row["n"] == 0


async def test_proposal_requires_evidence(db: Db) -> None:
    """没有 evidence_id 的改动不允许提案 —— 每条结论都要有出处。"""
    thread_id = await make_thread(db)
    contact_id = await make_contact(db)

    ctx = ToolContext(
        user_id="local-user",
        thread_id=thread_id,
        run_id="run-for-test",
        contact_id=contact_id,
        db=db,
    )
    tools = build_tools(ctx)
    result = await execute_tool(
        ctx,
        tools,
        "propose_action",
        {
            "payload": dict(UPDATE_COMPANY, contact_id=contact_id),
            "rationale": "随便改改",
            "evidence_ids": [],
        },
    )

    assert result.is_error
    row = await db.query_one("SELECT count(*) AS n FROM actions", {})
    assert row is not None and row["n"] == 0


# ============================================================
# 确认与拒绝（需要实现 action_service）
# ============================================================


async def _setup_proposal(db: Db) -> tuple[str, str]:
    """造一个待确认的 update_contact 卡片，返回 (contact_id, action_id)。"""
    thread_id = await make_thread(db)
    contact_id = await make_contact(db)
    evidence_id = await make_evidence(db, thread_id=thread_id, contact_id=contact_id)
    await propose(
        db,
        thread_id=thread_id,
        contact_id=contact_id,
        evidence_id=evidence_id,
        payload=dict(UPDATE_COMPANY, contact_id=contact_id),
    )
    action = await latest_action(db)
    return contact_id, str(action["id"])


async def test_confirm_executes_and_records_receipt(db: Db) -> None:
    """确认后：数据被写入、回执落库、Action 状态变 executed。"""
    contact_id, action_id = await _setup_proposal(db)

    result = await confirm_action(db, action_id=action_id)

    assert result.status == "executed"
    assert result.receipt_status == "success"
    assert result.receipt_id

    assert await contact_version(db, contact_id) == 2
    facts = await db.query(
        "SELECT field, value FROM contact_facts WHERE contact_id = :cid AND status = 'confirmed'",
        {"cid": contact_id},
    )
    assert [(f["field"], f["value"]) for f in facts] == [("company", "腾讯")]

    action = await latest_action(db)
    assert action["status"] == "executed"

    receipt = await db.query_one(
        "SELECT status, actual_result FROM action_receipts WHERE action_id = :aid",
        {"aid": action_id},
    )
    assert receipt is not None and receipt["status"] == "success"


async def test_confirm_twice_returns_same_receipt(db: Db) -> None:
    """连点两次确认：数据只改一次，两次返回同一条回执。"""
    contact_id, action_id = await _setup_proposal(db)

    first = await confirm_action(db, action_id=action_id)
    second = await confirm_action(db, action_id=action_id)

    assert first.receipt_id == second.receipt_id
    assert second.idempotent is True

    # 版本只 +1，回执只有一条
    assert await contact_version(db, contact_id) == 2
    row = await db.query_one(
        "SELECT count(*) AS n FROM action_receipts WHERE action_id = :aid", {"aid": action_id}
    )
    assert row is not None and row["n"] == 1


async def test_confirm_detects_stale_base_version(db: Db) -> None:
    """提案之后目标被改过 → CAS 冲突：数据不变、Action 置 conflict、有失败回执。"""
    contact_id, action_id = await _setup_proposal(db)

    # 模拟「卡片基于 version=1，但库里已经是 version=2」
    await db.execute(
        "UPDATE contacts SET version = 2 WHERE id = :cid", {"cid": contact_id}
    )

    with pytest.raises(ConflictError):
        await confirm_action(db, action_id=action_id)

    # 数据没有被覆盖
    assert await contact_version(db, contact_id) == 2
    assert await fact_count(db, contact_id) == 0

    action = await latest_action(db)
    assert action["status"] == "conflict"

    receipt = await db.query_one(
        "SELECT status, error FROM action_receipts WHERE action_id = :aid", {"aid": action_id}
    )
    assert receipt is not None
    assert receipt["status"] == "failed"
    assert receipt["error"]


async def test_reject_does_not_write(db: Db) -> None:
    """拒绝后：状态变 rejected，不写业务数据、不写回执。"""
    contact_id, action_id = await _setup_proposal(db)

    result = await reject_action(db, action_id=action_id)

    assert result.status == "rejected"
    assert await contact_version(db, contact_id) == 1
    assert await fact_count(db, contact_id) == 0

    action = await latest_action(db)
    assert action["status"] == "rejected"

    row = await db.query_one(
        "SELECT count(*) AS n FROM action_receipts WHERE action_id = :aid", {"aid": action_id}
    )
    assert row is not None and row["n"] == 0


async def test_confirm_after_reject_conflicts(db: Db) -> None:
    """已经拒绝的卡片不能再确认，且不能写入。"""
    contact_id, action_id = await _setup_proposal(db)

    await reject_action(db, action_id=action_id)

    with pytest.raises(ConflictError):
        await confirm_action(db, action_id=action_id)

    assert await contact_version(db, contact_id) == 1
    assert await fact_count(db, contact_id) == 0


async def test_confirm_create_contact(db: Db) -> None:
    """create_contact 走完整链路，不需要 base_version。"""
    thread_id = await make_thread(db)
    evidence_id = await make_evidence(db, thread_id=thread_id)

    await propose(
        db,
        thread_id=thread_id,
        contact_id=None,
        evidence_id=evidence_id,
        payload={
            "type": "create_contact",
            "display_name": "李四",
            "company": "美团",
        },
        rationale="用户新认识了李四",
    )
    action = await latest_action(db)
    assert action["base_version"] is None
    assert action["base_version_source"] is None

    result = await confirm_action(db, action_id=str(action["id"]))
    assert result.status == "executed"

    row = await db.query_one(
        "SELECT id FROM contacts WHERE display_name = '李四'", {}
    )
    assert row is not None


async def test_confirm_create_event(db: Db) -> None:
    """create_event 走完整链路，日程落库。"""
    thread_id = await make_thread(db)
    contact_id = await make_contact(db)
    evidence_id = await make_evidence(db, thread_id=thread_id, contact_id=contact_id)

    await propose(
        db,
        thread_id=thread_id,
        contact_id=contact_id,
        evidence_id=evidence_id,
        payload={
            "type": "create_event",
            "title": "和张三喝咖啡",
            "starts_at": "2026-11-20T15:00:00+08:00",
            "ends_at": "2026-11-20T16:00:00+08:00",
            "location": "国贸店",
            "contact_ids": [contact_id],
        },
        rationale="用户想约张三周三下午",
    )
    action = await latest_action(db)

    result = await confirm_action(db, action_id=str(action["id"]))
    assert result.status == "executed"

    row = await db.query_one(
        "SELECT title, version FROM calendar_events WHERE title = '和张三喝咖啡'", {}
    )
    assert row is not None
    assert row["version"] == 1


async def test_confirm_merge_contacts(db: Db) -> None:
    """merge_contacts 走完整链路：源联系人被标记 merged，数据不搬家。"""
    thread_id = await make_thread(db)
    target_id = await make_contact(db, "张三")
    source_id = await make_contact(db, "张三（工作号）")
    evidence_id = await make_evidence(db, thread_id=thread_id, contact_id=target_id)

    await propose(
        db,
        thread_id=thread_id,
        contact_id=target_id,
        evidence_id=evidence_id,
        payload={
            "type": "merge_contacts",
            "source_contact_ids": [source_id],
            "target_contact_id": target_id,
            "field_choices": {"company": "腾讯"},
        },
        rationale="两个张三其实是同一个人",
    )
    action = await latest_action(db)

    result = await confirm_action(db, action_id=str(action["id"]))
    assert result.status == "executed"

    row = await db.query_one(
        "SELECT status, merged_into_id FROM contacts WHERE id = :cid", {"cid": source_id}
    )
    assert row is not None
    assert row["status"] == "merged"
    assert row["merged_into_id"] == target_id
