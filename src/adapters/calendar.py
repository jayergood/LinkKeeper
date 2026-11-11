"""日历写入执行器。

与联系人执行器同构：接收调用方事务连接，带 `base_version` 的写入用 CAS。
`delete_event` 是软删除之外的硬删除 —— 事件本身没有保留价值，删除痕迹由回执记录。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..db.client import Connection
from ..domain import CreateEventPayload, DeleteEventPayload, UpdateEventPayload
from ..errors import ConflictError, ValidationError


def _parse_moment(value: str, field: str) -> datetime:
    """把 ISO 8601 字符串转成带时区的 datetime。

    asyncpg 的时间列只接受 datetime 对象，传字符串会在驱动层报错；顺带在这里
    把「格式不对」和「没带时区」转成业务错误，而不是让脏数据进库。
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} 不是合法的 ISO 8601 时间：{value}") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须带时区：{value}")
    return parsed


async def create_event(
    tx: Connection,
    payload: CreateEventPayload,
    evidence_ids: list[str],
) -> dict[str, Any]:
    starts_at = _parse_moment(payload.starts_at, "starts_at")
    ends_at = _parse_moment(payload.ends_at, "ends_at")

    row = await tx.execute_returning(
        """
        INSERT INTO calendar_events
          (title, starts_at, ends_at, timezone, location, notes, contact_ids, version)
        VALUES
          (:title, :starts, :ends, :tz, :location, :notes, CAST(:cids AS uuid[]), 1)
        RETURNING id, version
        """,
        {
            "title": payload.title,
            "starts": starts_at,
            "ends": ends_at,
            "tz": payload.timezone,
            "location": payload.location,
            "notes": payload.notes,
            "cids": payload.contact_ids,
        },
    )
    if row is None:
        raise ConflictError("创建日程失败")

    return {
        "event_id": str(row["id"]),
        "title": payload.title,
        "starts_at": starts_at.isoformat(),
        "ends_at": ends_at.isoformat(),
        "version": row["version"],
    }


async def update_event(
    tx: Connection,
    payload: UpdateEventPayload,
    evidence_ids: list[str],
) -> dict[str, Any]:
    base_version = payload.base_version
    if base_version is None:
        raise ConflictError("update_event 缺少 base_version，服务端应在提案时补齐")

    patch = payload.patch
    assignments: list[str] = []
    params: dict[str, Any] = {"eid": payload.event_id, "base": base_version}

    for column, param in (
        ("title", "title"),
        ("starts_at", "starts"),
        ("ends_at", "ends"),
        ("timezone", "tz"),
        ("location", "location"),
        ("notes", "notes"),
    ):
        if column not in patch:
            continue
        if column in ("starts_at", "ends_at"):
            params[param] = _parse_moment(patch[column], column)
        else:
            params[param] = patch[column]
        assignments.append(f"{column} = :{param}")

    if "contact_ids" in patch:
        assignments.append("contact_ids = CAST(:cids AS uuid[])")
        params["cids"] = patch["contact_ids"]

    assignments.append("version = version + 1")
    assignments.append("updated_at = now()")

    updated = await tx.execute_returning(
        f"""
        UPDATE calendar_events
        SET {", ".join(assignments)}
        WHERE id = :eid AND version = :base
        RETURNING id, version
        """,
        params,
    )
    if updated is None:
        raise ConflictError("日程已被修改（version 不匹配），请重新读取后再确认")

    return {
        "event_id": payload.event_id,
        "version": updated["version"],
        "changed_fields": sorted(patch),
    }


async def delete_event(
    tx: Connection,
    payload: DeleteEventPayload,
    evidence_ids: list[str],
) -> dict[str, Any]:
    base_version = payload.base_version
    if base_version is None:
        raise ConflictError("delete_event 缺少 base_version，服务端应在提案时补齐")

    row = await tx.execute_returning(
        """
        DELETE FROM calendar_events
        WHERE id = :eid AND version = :base
        RETURNING id, title, starts_at
        """,
        {"eid": payload.event_id, "base": base_version},
    )
    if row is None:
        raise ConflictError("日程不存在、已被删除或已被修改（version 不匹配）")

    return {
        "event_id": str(row["id"]),
        "deleted_title": row["title"],
        # 回执要落 JSONB，datetime 不可序列化，统一转 ISO 字符串
        "deleted_starts_at": row["starts_at"].isoformat(),
    }
