"""证据层：文字、截图、网页证据的写入、过期清理与归属校验。

截图过期只删原图文件，行与 sha256 保留，否则历史洞察的引用链会断。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .domain import EvidenceType
from .util.ids import new_id, sha256_bytes

log = logging.getLogger(__name__)

# 不受过期影响的类型
PERSISTENT_TYPES: frozenset[str] = frozenset({"user_note", "web_page", "action_receipt"})


class EvidenceRef:
    """证据写入/读取的结果。`id` 是唯一要记住的东西。"""

    __slots__ = ("id", "type", "sha256", "expires_at", "deduplicated")

    def __init__(
        self,
        id: str,
        type: str,
        sha256: str | None,
        expires_at: datetime | None,
        deduplicated: bool = False,
    ) -> None:
        self.id = id
        self.type = type
        self.sha256 = sha256
        self.expires_at = expires_at
        self.deduplicated = deduplicated

    def __repr__(self) -> str:
        mark = " (dedup)" if self.deduplicated else ""
        return f"<EvidenceRef {self.id} {self.type}{mark}>"


# ---- 写入 ----


async def create_text_evidence(
    db: Any,
    *,
    thread_id: str,
    contact_id: str | None,
    text: str,
    source_summary: str = "用户输入",
    user_id: str = "local-user",
) -> EvidenceRef:
    """写入文字证据（`user_note`）。文字不过期，是后续推理的原始依据。"""
    evidence_id = new_id()
    row = await db.execute_returning(
        """
        INSERT INTO evidence (id, user_id, thread_id, contact_id, type, content, source_summary)
        VALUES (:id, :user_id, :thread_id, :contact_id, 'user_note', :content, :summary)
        RETURNING id, type, sha256, expires_at
        """,
        {
            "id": evidence_id,
            "user_id": user_id,
            "thread_id": thread_id,
            "contact_id": contact_id,
            "content": text,
            "summary": source_summary,
        },
    )
    assert row is not None
    return EvidenceRef(str(row["id"]), row["type"], row["sha256"], row["expires_at"])


async def create_screenshot_evidence(
    db: Any,
    *,
    thread_id: str,
    contact_id: str | None,
    data: bytes,
    mime_type: str,
    evidence_dir: str,
    ttl_hours: int = 24,
    source_summary: str = "用户上传截图",
    user_id: str = "local-user",
) -> EvidenceRef:
    """写入截图证据（`screenshot`）：落盘 + 24h 过期 + sha256 去重。"""
    digest = sha256_bytes(data)

    # 去重：同一张图重复上传直接复用旧 ID
    existing = await db.query_one(
        "SELECT id, type, sha256, expires_at FROM evidence WHERE sha256 = :hash LIMIT 1",
        {"hash": digest},
    )
    if existing is not None:
        log.info("截图命中 sha256 去重，复用证据 %s", existing["id"])
        return EvidenceRef(str(existing["id"]), existing["type"], existing["sha256"], existing["expires_at"], True)

    # 落盘（按日期分目录，避免单目录文件过多拖慢查找）
    evidence_id = new_id()
    ext = {"image/png": "png", "image/webp": "webp"}.get(mime_type, "jpg")
    date_dir = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target_dir = Path(evidence_dir) / date_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    storage_path = target_dir / f"{evidence_id}.{ext}"
    storage_path.write_bytes(data)

    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)

    # 入库
    row = await db.execute_returning(
        """
        INSERT INTO evidence
          (id, user_id, thread_id, contact_id, type, storage_path, mime_type,
           sha256, source_summary, expires_at, metadata)
        VALUES
          (:id, :user_id, :thread_id, :contact_id, 'screenshot', :path, :mime,
           :hash, :summary, :expires, :meta)
        RETURNING id, type, sha256, expires_at
        """,
        {
            "id": evidence_id,
            "user_id": user_id,
            "thread_id": thread_id,
            "contact_id": contact_id,
            "path": str(storage_path),
            "mime": mime_type,
            "hash": digest,
            "summary": source_summary,
            "expires": expires_at,
            "meta": f'{{"bytes": {len(data)}}}',
        },
    )
    assert row is not None
    return EvidenceRef(str(row["id"]), row["type"], row["sha256"], row["expires_at"])


async def create_web_evidence(
    db: Any,
    *,
    thread_id: str,
    contact_id: str | None,
    url: str,
    text: str,
    user_id: str = "local-user",
) -> EvidenceRef:
    """写入网页证据（`web_page`）。正文是不可信材料，只能作为引用来源，不能当作指令执行。"""
    evidence_id = new_id()
    row = await db.execute_returning(
        """
        INSERT INTO evidence (id, user_id, thread_id, contact_id, type, content,
                              source_summary, metadata)
        VALUES (:id, :user_id, :thread_id, :contact_id, 'web_page', :content,
                :summary, :meta)
        RETURNING id, type, sha256, expires_at
        """,
        {
            "id": evidence_id,
            "user_id": user_id,
            "thread_id": thread_id,
            "contact_id": contact_id,
            "content": text,
            "summary": f"网页：{url}",
            "meta": f'{{"url": "{url}"}}',
        },
    )
    assert row is not None
    return EvidenceRef(str(row["id"]), row["type"], row["sha256"], row["expires_at"])


# ---- 过期与清理 ----


async def sweep_expired_screenshots(db: Any, *, now: datetime | None = None) -> int:
    """清理过期截图的原图文件，保留行、sha256 与摘要，否则历史洞察失去追溯依据。

    返回清理的截图数量。
    """
    moment = now or datetime.now(timezone.utc)
    rows = await db.query(
        """
        SELECT id, storage_path FROM evidence
        WHERE type = 'screenshot'
          AND expires_at IS NOT NULL
          AND expires_at < :now
          AND storage_path IS NOT NULL
        """,
        {"now": moment},
    )

    removed = 0
    for row in rows:
        path = Path(row["storage_path"])
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001 —— 文件删不掉不影响 DB 清理
            log.warning("删除截图文件失败 %s: %s", path, exc)
        await db.execute(
            "UPDATE evidence SET storage_path = NULL WHERE id = :id",
            {"id": row["id"]},
        )
        removed += 1

    if removed:
        log.info("清理过期截图 %d 张（保留摘要与来源关系）", removed)
    return removed


async def get_evidence(db: Any, evidence_id: str) -> dict[str, Any] | None:
    """读取一条证据。过期截图的 `storage_path` 已清空，调用方需先检查 `is_expired()`。"""
    return await db.query_one(
        """
        SELECT id, type, contact_id, thread_id, content, storage_path, mime_type,
               sha256, source_summary, metadata, captured_at, expires_at
        FROM evidence WHERE id = :id
        """,
        {"id": evidence_id},
    )


def is_expired(row: dict[str, Any], *, now: datetime | None = None) -> bool:
    """判断证据是否已过期（仅截图会过期）。"""
    expires_at = row.get("expires_at")
    if expires_at is None:
        return False
    moment = now or datetime.now(timezone.utc)
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    return expires_at < moment


async def evidence_belongs_to(
    db: Any,
    *,
    evidence_ids: list[str],
    contact_group_ids: list[str],
    thread_id: str,
) -> list[str]:
    """校验一批 evidenceId 是否都归属当前范围，返回非法的那些。

    证据挂在联系人上时须在该联系人的合并组内；挂在会话上时须属于当前 thread。
    """
    if not evidence_ids:
        return []

    unique = list(dict.fromkeys(evidence_ids))
    rows = await db.query(
        "SELECT id, contact_id, thread_id FROM evidence WHERE id = ANY(CAST(:ids AS uuid[]))",
        {"ids": unique},
    )
    found = {row["id"]: row for row in rows}

    invalid: list[str] = []
    for evidence_id in unique:
        row = found.get(evidence_id)
        if row is None:
            invalid.append(evidence_id)  # 不存在
            continue
        if row["contact_id"] is not None:
            if row["contact_id"] not in contact_group_ids:
                invalid.append(evidence_id)  # 跨人物
        elif row["thread_id"] != thread_id:
            invalid.append(evidence_id)  # 跨会话
    return invalid
