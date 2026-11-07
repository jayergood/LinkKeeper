"""MemorySnapshot 装配与渲染。

按联系人装配结构化快照（时间、事实、证据、候选、冲突、目标、洞察、记忆、Action），
并渲染成 Markdown 注入 system prompt。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..domain import MemorySnapshot

log = logging.getLogger(__name__)

USER_TIMEZONE = "Asia/Shanghai"

# 各项注入上限（防止单类数据淹没上下文）
MAX_EVIDENCE = 15
MAX_CANDIDATES = 10
MAX_INSIGHTS = 10
MAX_MEMORIES = 10
MAX_GOALS = 5
MAX_ACTIONS = 5
MAX_FACTS_PER_FIELD = 5
EVIDENCE_SUMMARY_CHARS = 160
INSIGHT_CHARS = 200
DOCUMENT_CHARS = 2_000


class SnapshotResult:
    """装配结果：结构化对象 + 渲染好的 Markdown。"""

    __slots__ = ("snapshot", "rendered")

    def __init__(self, snapshot: MemorySnapshot, rendered: str) -> None:
        self.snapshot = snapshot
        self.rendered = rendered

    def __repr__(self) -> str:
        contact = self.snapshot.contact.display_name if self.snapshot.contact else "（无）"
        return f"<SnapshotResult contact={contact} chars={len(self.rendered)}>"


# ---- 合并组解析（递归 CTE） ----


async def resolve_root_contact(db: Any, contact_id: str) -> str | None:
    """顺着 `merged_into_id` 向上找到最终 target。多级合并时需递归。"""
    row = await db.query_one(
        """
        WITH RECURSIVE up AS (
          SELECT id, merged_into_id FROM contacts WHERE id = :cid
          UNION ALL
          SELECT c.id, c.merged_into_id FROM contacts c JOIN up u ON u.merged_into_id = c.id
        )
        SELECT id FROM up WHERE merged_into_id IS NULL LIMIT 1
        """,
        {"cid": contact_id},
    )
    return row["id"] if row else None


async def resolve_contact_group(db: Any, contact_id: str) -> list[str]:
    """拿到合并组的全部成员 ID（target + 所有层级的 merged 源）。"""
    root = await resolve_root_contact(db, contact_id)
    if root is None:
        return []
    rows = await db.query(
        """
        WITH RECURSIVE grp AS (
          SELECT id FROM contacts WHERE id = :root
          UNION ALL
          SELECT c.id FROM contacts c JOIN grp g ON c.merged_into_id = g.id
        )
        SELECT id FROM grp
        """,
        {"root": root},
    )
    return [row["id"] for row in rows]


# ---- 装配 ----


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _summarize_evidence(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    """证据摘要。过期截图不提供原图路径，只给摘要 + expired 标记。"""
    summary = (row.get("source_summary") or row.get("content") or "(无摘要)").strip()
    expires_at = row.get("expires_at")
    expired = False
    if expires_at is not None:
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        expired = expires_at < now

    return {
        "id": row["id"],
        "type": row["type"],
        "summary": _truncate(summary, EVIDENCE_SUMMARY_CHARS),
        "captured_at": (
            row["captured_at"].isoformat()
            if isinstance(row.get("captured_at"), datetime)
            else str(row.get("captured_at") or "")
        ),
        "expired": expired,
    }


async def assemble_snapshot(
    db: Any,
    *,
    contact_id: str | None,
    thread_id: str,
    compaction_summary: str | None = None,
    now: datetime | None = None,
) -> SnapshotResult:
    """按 personID 装配 MemorySnapshot，返回结构化对象与渲染文本。"""
    moment = now or datetime.now(timezone.utc)

    # ---------- 联系人 + 合并组 ----------
    contact: dict[str, Any] | None = None
    group_ids: list[str] = []
    merged_from: list[dict[str, str]] = []

    if contact_id:
        root_id = await resolve_root_contact(db, contact_id)
        if root_id is not None:
            group_ids = await resolve_contact_group(db, root_id)
            row = await db.query_one(
                """
                SELECT id, display_name, version FROM contacts WHERE id = :id
                """,
                {"id": root_id},
            )
            if row is not None:
                contact = row
            # 合并来源（用于在快照里标注「已合并：xxx」）
            if len(group_ids) > 1:
                others = await db.query(
                    """
                    SELECT id, display_name FROM contacts
                    WHERE id = ANY(CAST(:ids AS uuid[])) AND id <> :root
                    ORDER BY created_at
                    """,
                    {"ids": group_ids, "root": root_id},
                )
                merged_from = [
                    {"id": r["id"], "display_name": r["display_name"]} for r in others
                ]

    # ---------- 事实（confirmed + candidate 分开） ----------
    facts_rows: list[dict[str, Any]] = []
    if group_ids:
        facts_rows = await db.query(
            """
            SELECT field, value, status, evidence_ids
            FROM contact_facts
            WHERE contact_id = ANY(CAST(:ids AS uuid[])) AND status IN ('confirmed', 'candidate')
            ORDER BY field, created_at DESC
            """,
            {"ids": group_ids},
        )

    confirmed_facts = [f for f in facts_rows if f["status"] == "confirmed"]
    candidate_facts = [f for f in facts_rows if f["status"] == "candidate"]

    # 同字段多个 confirmed 值 → 冲突，不擅自选
    by_field: dict[str, list[dict[str, Any]]] = {}
    for fact in confirmed_facts:
        by_field.setdefault(fact["field"], []).append(fact)

    conflicts: list[dict[str, Any]] = []
    for field, rows in by_field.items():
        values = list(dict.fromkeys(r["value"] for r in rows))
        if len(values) > 1:
            conflicts.append(
                {
                    "field": field,
                    "values": values,
                    "evidence_ids": [eid for r in rows for eid in (r["evidence_ids"] or [])],
                }
            )

    # ---------- 证据 ----------
    evidence_rows: list[dict[str, Any]] = []
    if contact_id is not None or thread_id:
        evidence_rows = await db.query(
            """
            SELECT id, type, content, source_summary, captured_at, expires_at
            FROM evidence
            WHERE (contact_id = ANY(CAST(:ids AS uuid[])) OR thread_id = :tid)
            ORDER BY captured_at DESC
            LIMIT :limit
            """,
            {
                "ids": group_ids or ["00000000-0000-0000-0000-000000000000"],
                "tid": thread_id,
                "limit": MAX_EVIDENCE,
            },
        )

    # ---------- 候选记忆 ----------
    memory_rows: list[dict[str, Any]] = []
    if group_ids:
        memory_rows = await db.query(
            """
            SELECT id, kind, content, status, evidence_ids
            FROM memory_items
            WHERE contact_id = ANY(CAST(:ids AS uuid[])) AND status IN ('candidate', 'confirmed')
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            {"ids": group_ids, "limit": MAX_MEMORIES},
        )
    candidate_memories = [m for m in memory_rows if m["status"] == "candidate"]
    confirmed_memories = [m for m in memory_rows if m["status"] == "confirmed"]

    # ---------- 目标 ----------
    goal_rows: list[dict[str, Any]] = []
    if group_ids:
        goal_rows = await db.query(
            """
            SELECT id, description, evidence_ids FROM relationship_goals
            WHERE contact_id = ANY(CAST(:ids AS uuid[])) AND status = 'active'
            ORDER BY created_at DESC LIMIT :limit
            """,
            {"ids": group_ids, "limit": MAX_GOALS},
        )

    # ---------- 洞察（有效且未被否定的） ----------
    insight_rows: list[dict[str, Any]] = []
    if group_ids:
        insight_rows = await db.query(
            """
            SELECT id, content, confidence, evidence_ids, recheck_at
            FROM insights
            WHERE contact_id = ANY(CAST(:ids AS uuid[])) AND status = 'active'
            ORDER BY created_at DESC LIMIT :limit
            """,
            {"ids": group_ids, "limit": MAX_INSIGHTS},
        )

    # ---------- 长期记忆文档（唯一 active） ----------
    document: dict[str, Any] | None = None
    if group_ids:
        document = await db.query_one(
            """
            SELECT version, content_markdown FROM memory_documents
            WHERE contact_id = ANY(CAST(:ids AS uuid[])) AND status = 'active'
            ORDER BY version DESC LIMIT 1
            """,
            {"ids": group_ids},
        )

    # ---------- 本会话 Action ----------
    action_rows = await db.query(
        """
        SELECT id, type, status, rationale FROM actions
        WHERE thread_id = :tid ORDER BY created_at DESC LIMIT :limit
        """,
        {"tid": thread_id, "limit": MAX_ACTIONS},
    )

    # ---------- 组装 ----------
    snapshot = MemorySnapshot(
        now=moment.isoformat(),
        timezone=USER_TIMEZONE,
        contact=(
            {
                "id": contact["id"],
                "display_name": contact["display_name"],
                "version": contact["version"],
                "facts": [
                    {
                        "field": f["field"],
                        "value": f["value"],
                        "status": f["status"],
                        "evidence_ids": f["evidence_ids"] or [],
                    }
                    for f in confirmed_facts
                ],
                "merged_from": merged_from,
            }
            if contact
            else None
        ),
        evidence=[_summarize_evidence(r, moment) for r in evidence_rows],
        candidates=[
            {
                "id": f"{f['field']}:{f['value']}",
                "kind": f"fact:{f['field']}",
                "content": f["value"],
                "evidence_ids": f["evidence_ids"] or [],
            }
            for f in candidate_facts
        ]
        + [
            {
                "id": m["id"],
                "kind": m["kind"],
                "content": m["content"],
                "evidence_ids": m["evidence_ids"] or [],
            }
            for m in candidate_memories
        ],
        conflicts=conflicts,
        goals=[
            {
                "id": g["id"],
                "description": g["description"],
                "evidence_ids": g["evidence_ids"] or [],
            }
            for g in goal_rows
        ],
        insights=[
            {
                "id": i["id"],
                "content": i["content"],
                "confidence": i["confidence"],
                "evidence_ids": i["evidence_ids"] or [],
            }
            for i in insight_rows
        ],
        confirmed_memories=[
            {
                "id": m["id"],
                "kind": m["kind"],
                "content": m["content"],
                "evidence_ids": m["evidence_ids"] or [],
            }
            for m in confirmed_memories
        ],
        memory_document=(
            {
                "version": document["version"],
                "content_markdown": _truncate(document["content_markdown"], DOCUMENT_CHARS),
            }
            if document
            else None
        ),
        actions=[
            {
                "id": a["id"],
                "type": a["type"],
                "status": a["status"],
                "summary": a["rationale"] or a["type"],
            }
            for a in action_rows
        ],
        thread={"id": thread_id, "compaction_summary": compaction_summary},
    )

    return SnapshotResult(snapshot, render_snapshot(snapshot))


# ---- 渲染成 Markdown（注入 system prompt） ----


def render_snapshot(snapshot: MemorySnapshot) -> str:
    """把快照渲染成紧凑 Markdown。evidence ID 保留原文，便于模型逐字引用。"""
    lines: list[str] = []

    # 时间
    lines.append("# 当前时间")
    lines.append(f"{snapshot.now}（时区 {snapshot.timezone}）")
    lines.append("")

    # 联系人
    lines.append("# 当前联系人")
    if snapshot.contact is None:
        # 没指定人时明确禁止注入任何人专属资料
        lines.append(
            "本轮没有指定联系人。**不要注入任何联系人专属资料**；"
            "如果用户的请求需要指向某个人，先用 search_contacts 查或直接问用户是谁。"
        )
    else:
        lines.append(
            f"- {snapshot.contact.display_name}"
            f"（id={snapshot.contact.id}, version={snapshot.contact.version}）"
        )
        if snapshot.contact.merged_from:
            names = "、".join(m["display_name"] for m in snapshot.contact.merged_from)
            lines.append(f"- 已合并：{names}（数据按同一人聚合，无需分别处理）")
        lines.append("")
        lines.append("## 已确认结构化事实")
        if not snapshot.contact.facts:
            lines.append("- （暂无）")
        for fact in snapshot.contact.facts:
            refs = f" [{','.join(fact.evidence_ids)}]" if fact.evidence_ids else ""
            lines.append(f"- {fact.field}: {fact.value}{refs}")
    lines.append("")

    # 冲突（放在显眼位置，提醒模型不要擅自裁决）
    if snapshot.conflicts:
        lines.append("## 事实冲突（不要擅自选一个，需要向用户确认）")
        for conflict in snapshot.conflicts:
            values = " / ".join(conflict.values)
            refs = ",".join(conflict.evidence_ids)
            lines.append(f"- {conflict.field}: {values} [{refs}]")
        lines.append("")

    # 候选
    if snapshot.candidates:
        lines.append("## 候选事实 / 待确认记忆（尚未确认，不可当事实用）")
        for item in snapshot.candidates[:MAX_CANDIDATES]:
            refs = ",".join(item.evidence_ids) or "无来源"
            lines.append(f"- {item.kind}: {_truncate(item.content, INSIGHT_CHARS)} [{refs}]")
        lines.append("")

    # 目标
    if snapshot.goals:
        lines.append("## 活跃关系目标")
        for goal in snapshot.goals:
            refs = ",".join(goal.evidence_ids) or "无来源"
            lines.append(f"- {goal.description} [{refs}]")
        lines.append("")

    # 洞察（明确标注是推断）
    if snapshot.insights:
        lines.append("## 有效洞察（**推断，不是事实**）")
        for insight in snapshot.insights:
            refs = ",".join(insight.evidence_ids)
            lines.append(
                f"- ({insight.confidence}) {_truncate(insight.content, INSIGHT_CHARS)} [{refs}]"
            )
        lines.append("")

    # 已确认记忆 + 长期文档
    if snapshot.confirmed_memories:
        lines.append("## 已确认长期记忆")
        for memory in snapshot.confirmed_memories:
            refs = ",".join(memory.evidence_ids) or "无来源"
            lines.append(
                f"- {memory.kind}: {_truncate(memory.content, INSIGHT_CHARS)} [{refs}]"
            )
        lines.append("")

    if snapshot.memory_document:
        version = snapshot.memory_document.get("version")
        content = snapshot.memory_document.get("content_markdown", "")
        lines.append(f"## 长期记忆文档（v{version}）")
        lines.append(content)
        lines.append("")

    # 本会话 Action
    if snapshot.actions:
        lines.append("## 当前会话最近的 Action")
        for action in snapshot.actions:
            lines.append(
                f"- {action['id']} {action['type']} / {action['status']}："
                f"{_truncate(action['summary'], 120)}"
            )
        lines.append("")

    # 证据（最后放，且明确「只能引用这里出现的 id」）
    lines.append("## 相关证据（**只能引用下面出现的 evidence id**）")
    if not snapshot.evidence:
        lines.append("- （暂无）")
    for ev in snapshot.evidence:
        expired_note = "，**已过期，仅摘要可用，原图不可读**" if ev.expired else ""
        lines.append(f"- {ev.id} ({ev.type}, {ev.captured_at}{expired_note}): {ev.summary}")

    return "\n".join(lines)
