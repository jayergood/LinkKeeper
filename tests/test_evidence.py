"""证据的创建、去重、过期清理与归属校验。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.db.client import Db
from src.evidence import (
    create_screenshot_evidence,
    create_text_evidence,
    evidence_belongs_to,
    get_evidence,
    is_expired,
    sweep_expired_screenshots,
)
from tests.conftest import make_contact, make_thread

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005000201a5f1b8f8000000004945"
    "4e44ae426082"
)


# ---------- 文字证据 ----------


async def test_create_text_evidence(db: Db) -> None:
    thread_id = await make_thread(db)
    contact_id = await make_contact(db)

    ref = await create_text_evidence(
        db, thread_id=thread_id, contact_id=contact_id, text="张三下周三有空"
    )

    assert ref.type == "user_note"
    assert ref.expires_at is None  # 文字不过期

    row = await get_evidence(db, ref.id)
    assert row is not None
    assert row["content"] == "张三下周三有空"
    assert row["contact_id"] == contact_id


# ---------- sha256 去重 ----------


async def test_screenshot_dedup(db: Db, tmp_path) -> None:
    """同一张图上传两次 → 只存一份，第二次复用旧 ID。"""
    thread_id = await make_thread(db)
    contact_id = await make_contact(db)

    first = await create_screenshot_evidence(
        db,
        thread_id=thread_id,
        contact_id=contact_id,
        data=PNG_BYTES,
        mime_type="image/png",
        evidence_dir=str(tmp_path),
    )
    assert first.deduplicated is False

    second = await create_screenshot_evidence(
        db,
        thread_id=thread_id,
        contact_id=contact_id,
        data=PNG_BYTES,
        mime_type="image/png",
        evidence_dir=str(tmp_path),
    )
    assert second.id == first.id  # 复用
    assert second.deduplicated is True

    count = await db.query_one("SELECT count(*) AS n FROM evidence")
    assert count is not None and count["n"] == 1


# ---------- 过期 ----------


async def test_screenshot_expiry(db: Db, tmp_path) -> None:
    """截图必须带 expires_at（24h），且到期后清理会删原图。"""
    thread_id = await make_thread(db)
    contact_id = await make_contact(db)

    ref = await create_screenshot_evidence(
        db,
        thread_id=thread_id,
        contact_id=contact_id,
        data=PNG_BYTES,
        mime_type="image/png",
        evidence_dir=str(tmp_path),
        ttl_hours=24,
    )

    row = await get_evidence(db, ref.id)
    assert row is not None
    assert row["expires_at"] is not None
    assert is_expired(row) is False

    # 把时间拨到 25 小时后
    future = datetime.now(timezone.utc) + timedelta(hours=25)
    removed = await sweep_expired_screenshots(db, now=future)
    assert removed == 1

    after = await get_evidence(db, ref.id)
    assert after is not None
    # 原图路径被清空，但行还在（追溯链不能断）
    assert after["storage_path"] is None
    assert after["sha256"] is not None
    assert after["source_summary"] is not None
    assert is_expired(after, now=future) is True


async def test_expired_screenshot_keeps_summary(db: Db, tmp_path) -> None:
    """过期截图清理后：文件已删、行保留、摘要可读。"""
    thread_id = await make_thread(db)
    ref = await create_screenshot_evidence(
        db,
        thread_id=thread_id,
        contact_id=None,
        data=PNG_BYTES,
        mime_type="image/png",
        evidence_dir=str(tmp_path),
        source_summary="聊天截图：对方提到下周喝咖啡",
    )

    path = tmp_path / ".."
    # 文件确实存在
    row = await get_evidence(db, ref.id)
    assert row is not None
    from pathlib import Path

    assert Path(row["storage_path"]).exists()

    future = datetime.now(timezone.utc) + timedelta(hours=25)
    await sweep_expired_screenshots(db, now=future)

    after = await get_evidence(db, ref.id)
    assert after is not None
    assert after["storage_path"] is None
    assert "喝咖啡" in (after["source_summary"] or "")


# ---------- 人物隔离 ----------


async def test_evidence_belongs_to_contact(db: Db) -> None:
    """证据挂在 A 上，用 B 的组去校验 → 判定非法。"""
    thread_id = await make_thread(db)
    a = await make_contact(db, "张三")
    b = await make_contact(db, "李四")

    ref_a = await create_text_evidence(
        db, thread_id=thread_id, contact_id=a, text="张三的信息"
    )

    # 用 A 的组校验 → 合法
    invalid = await evidence_belongs_to(
        db, evidence_ids=[ref_a.id], contact_group_ids=[a], thread_id=thread_id
    )
    assert invalid == []

    # 用 B 的组校验 → 非法（跨人物）
    invalid_b = await evidence_belongs_to(
        db, evidence_ids=[ref_a.id], contact_group_ids=[b], thread_id=thread_id
    )
    assert invalid_b == [ref_a.id]


async def test_evidence_belongs_to_thread(db: Db) -> None:
    """挂在会话上（contact_id 为空）的证据，必须属于当前 thread。"""
    thread_a = await make_thread(db, "会话A")
    thread_b = await make_thread(db, "会话B")

    ref = await create_text_evidence(
        db, thread_id=thread_a, contact_id=None, text="会话A里的信息"
    )

    ok = await evidence_belongs_to(
        db, evidence_ids=[ref.id], contact_group_ids=[], thread_id=thread_a
    )
    assert ok == []

    bad = await evidence_belongs_to(
        db, evidence_ids=[ref.id], contact_group_ids=[], thread_id=thread_b
    )
    assert bad == [ref.id]


async def test_nonexistent_evidence_is_invalid(db: Db) -> None:
    """引用不存在的 evidenceId 判为非法。"""
    invalid = await evidence_belongs_to(
        db,
        evidence_ids=["00000000-0000-0000-0000-000000000000"],
        contact_group_ids=[],
        thread_id="00000000-0000-0000-0000-000000000001",
    )
    assert len(invalid) == 1
