"""工具结果构造与通用校验辅助。

ToolResult 三个关键位：`text` 给模型看，`details` 结构化数据（写进轨迹供评测），
`terminate` 终态标记。`is_error=True` 的返回值参与连续错误预算计数。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..domain import ToolContext, ToolResult


def text_result(text: str, details: Any = None) -> ToolResult:
    """普通成功结果。"""
    return ToolResult(text=text, details=details if details is not None else {})


def error_result(text: str, details: Any = None) -> ToolResult:
    """可恢复错误：返回给模型让它自己纠正，计入连续错误预算。"""
    return ToolResult(
        text=text,
        details=details if details is not None else {},
        is_error=True,
    )


def terminal_result(text: str, details: Any = None) -> ToolResult:
    """终态结果：tool loop 看到后立即停止，本轮不再执行任何工具。"""
    return ToolResult(
        text=text,
        details=details if details is not None else {},
        terminate=True,
    )


def format_local_time(dt: datetime, timezone: str = "Asia/Shanghai") -> str:
    """按用户时区展示时间。服务端存 UTC，展示用用户时区。"""
    try:
        from zoneinfo import ZoneInfo

        return dt.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001 —— 时区不可用时退回原样输出
        return dt.isoformat()


def iso(dt: datetime) -> str:
    return dt.isoformat()


def compact_json(value: Any, limit: int = 500) -> str:
    """把结构化数据压成一行短文本，塞进给模型看的正文里。"""
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


async def assert_evidence_ids(
    ctx: ToolContext, ids: list[str], contact_id: str | None
) -> None:
    """校验 evidenceId 存在且归属当前联系人/会话。

    证据只能引用快照里出现过的 ID，不能信任模型给的 ID，服务端必须再校验一次。
    """
    if not ids:
        raise ValueError("至少需要一个 evidenceId")

    unique = list(dict.fromkeys(ids))
    rows = await ctx.db.query(
        "SELECT id, contact_id, thread_id FROM evidence WHERE id = ANY(CAST(:ids AS uuid[]))",
        {"ids": unique},
    )
    if len(rows) != len(unique):
        raise ValueError("引用了不存在的 evidenceId")

    group = await contact_group_ids(ctx, contact_id) if contact_id else []
    for row in rows:
        belongs_to_contact = row["contact_id"] is not None and row["contact_id"] in group
        belongs_to_thread = row["contact_id"] is None and row["thread_id"] == ctx.thread_id
        if not (belongs_to_contact or belongs_to_thread):
            raise ValueError("evidenceId 不属于当前联系人/会话")


async def contact_group_ids(ctx: ToolContext, contact_id: str) -> list[str]:
    """合并组内所有联系人 ID（target + 全部 merged 源）。

    合并是逻辑归并：数据不搬家，读取时按组聚合。
    """
    rows = await ctx.db.query(
        """
        WITH RECURSIVE root AS (
          SELECT id, merged_into_id FROM contacts WHERE id = :cid
          UNION ALL
          SELECT c.id, c.merged_into_id FROM contacts c JOIN root r ON r.merged_into_id = c.id
        ), grp AS (
          SELECT id FROM root WHERE merged_into_id IS NULL
          UNION ALL
          SELECT c.id FROM contacts c JOIN grp g ON c.merged_into_id = g.id
        )
        SELECT id FROM grp
        """,
        {"cid": contact_id},
    )
    return [row["id"] for row in rows]
