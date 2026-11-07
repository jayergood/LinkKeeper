"""会话消息读写：messages 表按单调递增的 seq 追加与读取。

追加时先 FOR UPDATE 锁住 threads 行再分配 seq，否则并发写会撞 UNIQUE(thread_id, seq)。
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


async def load_messages(
    db: Any,
    thread_id: str,
    *,
    after_seq: int = 0,
) -> list[dict[str, Any]]:
    """按 seq 升序读取会话消息；after_seq 只取更大的 seq（压缩后用）。"""
    rows = await db.query(
        """
        SELECT seq, payload FROM messages
        WHERE thread_id = :tid AND seq > :after
        ORDER BY seq ASC
        """,
        {"tid": thread_id, "after": after_seq},
    )
    return [row["payload"] for row in rows]


async def load_messages_with_seq(
    db: Any,
    thread_id: str,
    *,
    after_seq: int = 0,
) -> list[tuple[int, dict[str, Any]]]:
    """同上，但保留 seq（压缩时需要知道压到第几条）。"""
    rows = await db.query(
        """
        SELECT seq, payload FROM messages
        WHERE thread_id = :tid AND seq > :after
        ORDER BY seq ASC
        """,
        {"tid": thread_id, "after": after_seq},
    )
    return [(row["seq"], row["payload"]) for row in rows]


async def append_message(db: Any, thread_id: str, message: dict[str, Any]) -> int:
    """追加一条消息，返回分配到的 seq。

    必须在事务里分配 seq：先锁 thread 行 → 查 max(seq) → 插入。
    """
    async with db.transaction() as tx:
        await tx.execute(
            "SELECT id FROM threads WHERE id = :tid FOR UPDATE", {"tid": thread_id}
        )
        next_seq = await tx.query_one(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM messages WHERE thread_id = :tid",
            {"tid": thread_id},
        )
        seq = next_seq["seq"] if next_seq else 1
        await tx.execute(
            """
            INSERT INTO messages (thread_id, seq, role, payload)
            VALUES (:tid, :seq, :role, :payload)
            """,
            {
                "tid": thread_id,
                "seq": seq,
                "role": message.get("role", "user"),
                "payload": json.dumps(message, ensure_ascii=False),
            },
        )
        await tx.execute(
            "UPDATE threads SET updated_at = now() WHERE id = :tid", {"tid": thread_id}
        )
    return seq


async def append_messages(db: Any, thread_id: str, messages: list[dict[str, Any]]) -> int:
    """批量追加，一次事务内分配连续 seq。

    恢复流程要一次性补上配对的 toolResult 和新的 user 提示，必须连续且有序。
    """
    if not messages:
        return 0

    async with db.transaction() as tx:
        await tx.execute(
            "SELECT id FROM threads WHERE id = :tid FOR UPDATE", {"tid": thread_id}
        )
        row = await tx.query_one(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM messages WHERE thread_id = :tid",
            {"tid": thread_id},
        )
        start = (row["seq"] if row else 0) + 1

        for offset, message in enumerate(messages):
            await tx.execute(
                """
                INSERT INTO messages (thread_id, seq, role, payload)
                VALUES (:tid, :seq, :role, :payload)
                """,
                {
                    "tid": thread_id,
                    "seq": start + offset,
                    "role": message.get("role", "user"),
                    "payload": json.dumps(message, ensure_ascii=False),
                },
            )
        await tx.execute(
            "UPDATE threads SET updated_at = now() WHERE id = :tid", {"tid": thread_id}
        )
    return start


async def find_last_tool_call(
    messages: list[dict[str, Any]], tool_name: str
) -> tuple[str, dict[str, Any]] | None:
    """从后往前找最后一条调用指定工具的 assistant 消息，返回 (tool_call_id, arguments)。

    恢复问卷时靠它找到当初那条 tool_call，才能补出配对的 toolResult。
    """
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name") != tool_name:
                continue
            raw_args = function.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            return call.get("id", ""), args
    return None


def has_paired_tool_result(messages: list[dict[str, Any]], tool_call_id: str) -> bool:
    """检查某个 tool_call 是否已经有配对的 toolResult。

    已配对过就必须跳过，否则一个 tool_call 对应两条结果，是协议错误。
    """
    return any(
        message.get("role") == "tool" and message.get("tool_call_id") == tool_call_id
        for message in messages
    )
