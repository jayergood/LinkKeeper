"""`propose_action` 终态工具：把「想做的改动」写成一张待确认的卡片。

提案只往 `actions` 插一行，不碰任何业务表 —— 模型没有直接写库的工具，
所有写入都要等用户确认。`base_version` 记录提案那一刻目标对象的版本，
确认时用它做 CAS。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import TypeAdapter, ValidationError as PydanticValidationError

from ..domain import ActionPayload, ToolContext, ToolDef, ToolResult
from .helpers import assert_evidence_ids, error_result, terminal_result

log = logging.getLogger(__name__)

_PAYLOAD_ADAPTER = TypeAdapter(ActionPayload)

# 需要 base_version 的类型 → 目标对象在当前时刻的版本从哪里读
_CAS_TYPES = {"update_contact", "merge_contacts", "update_event", "delete_event"}


def parse_payload(raw: Any) -> ActionPayload:
    """按 `type` 判别联合校验 payload。不合法时抛 ValueError 由上层转成协议错误。"""
    if not isinstance(raw, dict):
        raise ValueError("payload 必须是对象")
    try:
        return _PAYLOAD_ADAPTER.validate_python(raw)
    except PydanticValidationError as exc:
        raise ValueError(str(exc)) from exc


async def _contact_version(ctx: ToolContext, contact_id: str) -> int:
    row = await ctx.db.query_one(
        "SELECT version FROM contacts WHERE id = :cid AND status = 'active'",
        {"cid": contact_id},
    )
    if row is None:
        raise ValueError(f"联系人 {contact_id} 不存在或已被合并")
    return int(row["version"])


async def _event_info(ctx: ToolContext, event_id: str) -> dict[str, Any]:
    row = await ctx.db.query_one(
        "SELECT version, contact_ids FROM calendar_events WHERE id = :eid",
        {"eid": event_id},
    )
    if row is None:
        raise ValueError(f"日程 {event_id} 不存在")
    return row


async def resolve_base_version(
    ctx: ToolContext, payload: ActionPayload
) -> tuple[int | None, str | None, str | None]:
    """解析提案的 CAS 基准。

    Returns:
        `(base_version, base_version_source, contact_id)`

    模型通常不知道版本号，所以服务端在提案这一刻把当前值盖上去（`source='server'`）。
    模型若主动给了版本号，必须与当前值一致 —— 不一致说明它读到的是旧数据，
    直接拒绝让它重新读取，而不是默默采用服务端的值。
    """
    kind = payload.type

    if kind == "create_contact":
        return None, None, None

    if kind == "create_event":
        return None, None, None

    if kind == "update_contact":
        current = await _contact_version(ctx, payload.contact_id)
        given = payload.base_version
        if given is not None and given != current:
            raise ValueError(
                f"stale_base_version: 联系人当前 version={current}，提案里是 {given}，"
                "请重新读取后再提案"
            )
        return given if given is not None else current, (
            "model" if given is not None else "server"
        ), payload.contact_id

    if kind == "merge_contacts":
        current = await _contact_version(ctx, payload.target_contact_id)
        given = payload.base_version
        if given is not None and given != current:
            raise ValueError(
                f"stale_base_version: 目标联系人当前 version={current}，提案里是 {given}，"
                "请重新读取后再提案"
            )
        return given if given is not None else current, (
            "model" if given is not None else "server"
        ), payload.target_contact_id

    # update_event / delete_event
    info = await _event_info(ctx, payload.event_id)
    current = int(info["version"])
    given = payload.base_version
    if given is not None and given != current:
        raise ValueError(
            f"stale_base_version: 日程当前 version={current}，提案里是 {given}，"
            "请重新读取后再提案"
        )
    event_contacts = info.get("contact_ids") or []
    return given if given is not None else current, (
        "model" if given is not None else "server"
    ), (event_contacts[0] if event_contacts else None)


async def create_action(
    ctx: ToolContext,
    *,
    payload: ActionPayload,
    rationale: str,
    evidence_ids: list[str],
) -> str:
    """落库一张 `proposed` 状态的卡片，返回 action_id。不修改任何业务数据。"""
    base_version, base_version_source, contact_id = await resolve_base_version(ctx, payload)

    # 把服务端盖好的 base_version 写回 payload 一起存。
    # 确认时执行器直接从 payload.base_version 取基准，不必再回查 actions 列。
    stored_payload = payload
    if base_version is not None and payload.type in _CAS_TYPES:
        stored_payload = payload.model_copy(update={"base_version": base_version})

    row = await ctx.db.execute_returning(
        """
        INSERT INTO actions
          (thread_id, contact_id, type, payload, status,
           base_version, base_version_source, evidence_ids, rationale)
        VALUES
          (:tid, :cid, :type, :payload, 'proposed',
           :base, :source, CAST(:evids AS uuid[]), :rationale)
        RETURNING id
        """,
        {
            "tid": ctx.thread_id,
            "cid": contact_id,
            "type": payload.type,
            "payload": json.dumps(stored_payload.model_dump(mode="json"), ensure_ascii=False),
            "base": base_version,
            "source": base_version_source,
            "evids": evidence_ids,
            "rationale": rationale,
        },
    )
    if row is None:
        raise ValueError("创建 Action 失败")
    return str(row["id"])


async def _propose_action_tool(ctx: ToolContext, **kwargs: Any) -> ToolResult:
    raw_payload = kwargs.get("payload")
    evidence_ids = kwargs.get("evidence_ids") or []
    rationale = (kwargs.get("rationale") or "").strip()

    if not rationale:
        return error_result("propose_action 需要 rationale：说明为什么要做这个改动")

    try:
        payload = parse_payload(raw_payload)
    except ValueError as exc:
        return error_result(f"payload 不合法，请修正后重试：{exc}")

    if not isinstance(evidence_ids, list) or not evidence_ids:
        return error_result("propose_action 需要至少一个 evidence_id 作为改动依据")

    try:
        await assert_evidence_ids(ctx, evidence_ids, ctx.contact_id)
    except ValueError as exc:
        return error_result(f"证据校验失败：{exc}")

    try:
        action_id = await create_action(
            ctx, payload=payload, rationale=rationale, evidence_ids=evidence_ids
        )
    except ValueError as exc:  # 含 stale_base_version 与目标不存在
        return error_result(str(exc))

    ctx.terminal = {"type": "propose_action", "id": action_id}  # type: ignore[assignment]

    return terminal_result(
        "已生成待确认的 Action 卡片，本轮暂停，等待用户确认。",
        details={
            "terminal_type": "propose_action",
            "terminal_id": action_id,
            "action_id": action_id,
            "action_type": payload.type,
            "payload": payload.model_dump(mode="json"),
            "rationale": rationale,
            "evidence_ids": evidence_ids,
        },
    )


PAYLOAD_FIELD_DOC = """按 type 给出不同字段：
- create_contact: display_name(必填), company, title, phone, email, notes
- update_contact: contact_id(必填), patch(必填，只含要改的字段，取值同 create_contact), base_version(可选)
- merge_contacts: source_contact_ids(必填，数组), target_contact_id(必填), field_choices(可选，字段名→采用的值), base_version(可选)
- create_event: title(必填), starts_at(必填，ISO 8601 带时区), ends_at(必填), timezone, location, notes, contact_ids
- update_event: event_id(必填), patch(必填), base_version(可选)
- delete_event: event_id(必填), base_version(可选)"""

PROPOSE_ACTION_TOOL = ToolDef(
    name="propose_action",
    description=(
        "提出一个会修改数据的改动，生成 Action 卡片等待用户确认。"
        "你没有直接修改联系人或日历的工具，所有写操作都必须走这里。"
        "这是终态工具：调用后本轮立即结束。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": sorted(
                    [
                        "create_contact",
                        "update_contact",
                        "merge_contacts",
                        "create_event",
                        "update_event",
                        "delete_event",
                    ]
                ),
            },
            "payload": {
                "type": "object",
                "description": PAYLOAD_FIELD_DOC,
            },
            "rationale": {
                "type": "string",
                "minLength": 1,
                "maxLength": 800,
                "description": "向用户解释为什么要做这个改动",
            },
            "evidence_ids": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
                "description": "本次改动的依据，必须是快照中出现过的证据 id",
            },
        },
        "required": ["type", "payload", "rationale", "evidence_ids"],
    },
    category="action",
    is_terminal=True,
    fn=_propose_action_tool,
)


def build_action_tool(ctx: ToolContext) -> ToolDef:
    """返回绑定到当前 run 的 propose_action 工具。"""
    return PROPOSE_ACTION_TOOL
