"""pytest 共享夹具。

集成测试连独立测试库 `relationship_test`，每个测试开始前 TRUNCATE 全部业务表。
行锁、CAS、递归 CTE 只能在真库上验证，不做 mock。
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import text

from src.config import settings
from src.db.client import Db
from src.db.migrate import run_migrations

# 业务表（不含 schema_migrations —— 那个不能清，否则迁移记录丢失）
BUSINESS_TABLES = [
    "action_receipts",
    "actions",
    "agent_events",
    "tool_calls",
    "agent_runs",
    "questionnaires",
    "permission_requests",
    "insights",
    "memory_items",
    "memory_documents",
    "relationship_goals",
    "meet_intents",
    "calendar_events",
    "contact_facts",
    "evidence",
    "messages",
    "threads",
    "contacts",
]


@pytest.fixture(scope="session")
def _test_db_ready() -> None:
    """整个测试会话开始前，确保测试库已迁移。"""
    # 用同步 fixture + asyncio.run：session 级 async fixture 会和 function 级 event loop 冲突
    asyncio.run(run_migrations(settings.test_database_url))


@pytest_asyncio.fixture
async def db(_test_db_ready: None) -> Db:
    """已清空业务表的测试数据库句柄。"""
    database = Db(settings.test_database_url)
    # 清空业务数据（TRUNCATE 比 DELETE 快，且重置序列）
    await database.execute(
        f"TRUNCATE {', '.join(BUSINESS_TABLES)} RESTART IDENTITY CASCADE"
    )
    try:
        yield database
    finally:
        await database.close()


# ---------- 造数据的小工具 ----------


async def make_contact(
    db: Db,
    display_name: str = "张三",
    *,
    version: int = 1,
    status: str = "active",
    merged_into_id: str | None = None,
) -> str:
    """插入一个联系人，返回 id。"""
    row = await db.execute_returning(
        """
        INSERT INTO contacts (display_name, version, status, merged_into_id, merged_at)
        VALUES (:name, :version, :status, :merged_into, :merged_at)
        RETURNING id
        """,
        {
            "name": display_name,
            "version": version,
            "status": status,
            "merged_into": merged_into_id,
            "merged_at": "now()" if status == "merged" else None,
        },
    )
    assert row is not None
    return str(row["id"])


async def make_thread(db: Db, title: str = "测试会话") -> str:
    row = await db.execute_returning(
        "INSERT INTO threads (title) VALUES (:t) RETURNING id", {"t": title}
    )
    assert row is not None
    return str(row["id"])


async def make_fact(
    db: Db,
    contact_id: str,
    field: str,
    value: str,
    *,
    status: str = "confirmed",
    evidence_ids: list[str] | None = None,
) -> str:
    row = await db.execute_returning(
        """
        INSERT INTO contact_facts (contact_id, field, value, status, evidence_ids)
        VALUES (:cid, :field, :value, :status, CAST(:evids AS uuid[]))
        RETURNING id
        """,
        {
            "cid": contact_id,
            "field": field,
            "value": value,
            "status": status,
            "evids": evidence_ids or [],
        },
    )
    assert row is not None
    return str(row["id"])


async def make_memory(
    db: Db,
    contact_id: str,
    content: str,
    *,
    kind: str = "preference",
    status: str = "candidate",
    evidence_ids: list[str] | None = None,
) -> str:
    row = await db.execute_returning(
        """
        INSERT INTO memory_items (contact_id, kind, content, status, evidence_ids)
        VALUES (:cid, :kind, :content, :status, CAST(:evids AS uuid[]))
        RETURNING id
        """,
        {
            "cid": contact_id,
            "kind": kind,
            "content": content,
            "status": status,
            "evids": evidence_ids or [],
        },
    )
    assert row is not None
    return str(row["id"])


async def make_evidence(
    db: Db,
    *,
    thread_id: str | None = None,
    contact_id: str | None = None,
    evidence_type: str = "user_note",
    summary: str = "用户说张三公司改成腾讯",
    content: str | None = None,
) -> str:
    """插入一条证据，返回 id。提案的 evidence_ids 必须是真实存在的证据。"""
    row = await db.execute_returning(
        """
        INSERT INTO evidence (thread_id, contact_id, type, content, source_summary)
        VALUES (:tid, :cid, :type, :content, :summary)
        RETURNING id
        """,
        {
            "tid": thread_id,
            "cid": contact_id,
            "type": evidence_type,
            "content": content if content is not None else summary,
            "summary": summary,
        },
    )
    assert row is not None
    return str(row["id"])


async def make_insight(
    db: Db,
    contact_id: str,
    content: str,
    *,
    status: str = "active",
    confidence: str = "medium",
    evidence_ids: list[str] | None = None,
) -> str:
    row = await db.execute_returning(
        """
        INSERT INTO insights (contact_id, content, confidence, status, evidence_ids, source_ids)
        VALUES (:cid, :content, :conf, :status, CAST(:evids AS uuid[]), CAST(:src AS text[]))
        RETURNING id
        """,
        {
            "cid": contact_id,
            "content": content,
            "conf": confidence,
            "status": status,
            "evids": evidence_ids or [],
            "src": [f"ev_{e}" for e in (evidence_ids or [])],
        },
    )
    assert row is not None
    return str(row["id"])
