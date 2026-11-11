"""联系人写入执行器。

函数接收调用方的事务连接，不自己开事务 —— 「改数据 + 写回执」必须原子，
否则会出现数据改了却没有回执、或回执写了数据没改的记录。

带 `base_version` 的写入用 CAS：`WHERE version = :base`，影响 0 行即抛 ConflictError。
"""

from __future__ import annotations

from typing import Any

from ..db.client import Connection
from ..domain import CreateContactPayload, MergeContactsPayload, UpdateContactPayload
from ..errors import ConflictError, ValidationError

# `contacts` 表只存 display_name，其余字段落在 contact_facts
FACT_FIELDS = ("company", "title", "phone", "email", "notes")


async def _supersede_facts(
    tx: Connection, contact_ids: list[str], field: str
) -> None:
    """把指定联系人在某字段上的 confirmed 事实置为 superseded（保留旧行用于追溯）。"""
    await tx.execute(
        """
        UPDATE contact_facts
        SET status = 'superseded', updated_at = now()
        WHERE contact_id = ANY(CAST(:cids AS uuid[]))
          AND field = :field
          AND status = 'confirmed'
        """,
        {"cids": contact_ids, "field": field},
    )


async def _replace_fact(
    tx: Connection,
    contact_id: str,
    field: str,
    value: str,
    evidence_ids: list[str],
) -> None:
    await _supersede_facts(tx, [contact_id], field)
    await tx.execute(
        """
        INSERT INTO contact_facts (contact_id, field, value, status, confidence, evidence_ids)
        VALUES (:cid, :field, :value, 'confirmed', 'medium', CAST(:evids AS uuid[]))
        """,
        {"cid": contact_id, "field": field, "value": value, "evids": evidence_ids},
    )


async def create_contact(
    tx: Connection,
    payload: CreateContactPayload,
    evidence_ids: list[str],
) -> dict[str, Any]:
    row = await tx.execute_returning(
        """
        INSERT INTO contacts (display_name, version, status)
        VALUES (:name, 1, 'active')
        RETURNING id, version
        """,
        {"name": payload.display_name},
    )
    if row is None:
        raise ConflictError("创建联系人失败")

    contact_id = str(row["id"])
    written_fields: list[str] = []
    for field in FACT_FIELDS:
        value = getattr(payload, field, None)
        if not value:
            continue
        await _replace_fact(tx, contact_id, field, value, evidence_ids)
        written_fields.append(field)

    return {
        "contact_id": contact_id,
        "display_name": payload.display_name,
        "version": row["version"],
        "fact_fields": written_fields,
    }


async def update_contact(
    tx: Connection,
    payload: UpdateContactPayload,
    evidence_ids: list[str],
) -> dict[str, Any]:
    base_version = payload.base_version
    if base_version is None:
        raise ConflictError("update_contact 缺少 base_version，服务端应在提案时补齐")

    patch = payload.patch
    updated = await tx.execute_returning(
        """
        UPDATE contacts
        SET display_name = COALESCE(:name, display_name),
            version = version + 1,
            updated_at = now()
        WHERE id = :cid AND version = :base AND status = 'active'
        RETURNING id, version
        """,
        {
            "cid": payload.contact_id,
            "name": patch.get("display_name"),
            "base": base_version,
        },
    )
    if updated is None:
        raise ConflictError("联系人已被修改（version 不匹配），请重新读取后再确认")

    changed_fields: list[str] = []
    if patch.get("display_name"):
        changed_fields.append("display_name")
    for field in FACT_FIELDS:
        value = patch.get(field)
        if value is None:
            continue
        await _replace_fact(tx, payload.contact_id, field, value, evidence_ids)
        changed_fields.append(field)

    return {
        "contact_id": payload.contact_id,
        "version": updated["version"],
        "changed_fields": changed_fields,
    }


async def merge_contacts(
    tx: Connection,
    payload: MergeContactsPayload,
    evidence_ids: list[str],
) -> dict[str, Any]:
    base_version = payload.base_version
    if base_version is None:
        raise ConflictError("merge_contacts 缺少 base_version，服务端应在提案时补齐")

    # 去重并保持顺序：同一次合并里重复给出同一个源联系人不应重复计数
    source_ids = list(dict.fromkeys(payload.source_contact_ids))
    if payload.target_contact_id in source_ids:
        raise ValidationError("不能把联系人合并到自己")

    target = await tx.execute_returning(
        """
        UPDATE contacts SET version = version + 1, updated_at = now()
        WHERE id = :tid AND version = :base AND status = 'active'
        RETURNING id, version
        """,
        {"tid": payload.target_contact_id, "base": base_version},
    )
    if target is None:
        raise ConflictError("目标联系人已被修改（version 不匹配），请重新读取后再确认")

    merged_count = 0
    for source_id in source_ids:
        merged_count += await tx.execute(
            """
            UPDATE contacts
            SET status = 'merged', merged_into_id = :tid, merged_at = now(), updated_at = now()
            WHERE id = :sid AND status = 'active'
            """,
            {"tid": payload.target_contact_id, "sid": source_id},
        )
    if merged_count != len(source_ids):
        raise ConflictError("部分源联系人已被合并或状态不一致，请重新读取")

    for field, value in payload.field_choices.items():
        if not value:
            continue
        await _replace_fact(tx, payload.target_contact_id, field, value, evidence_ids)
        await _supersede_facts(tx, source_ids, field)

    # 源联系人的长期记忆文档归档；目标文档由记忆草案流程重新生成
    await tx.execute(
        """
        UPDATE memory_documents SET status = 'archived'
        WHERE contact_id = ANY(CAST(:sids AS uuid[])) AND status = 'active'
        """,
        {"sids": source_ids},
    )

    return {
        "target_contact_id": payload.target_contact_id,
        "version": target["version"],
        "merged_count": merged_count,
        "merged_from": source_ids,
    }
