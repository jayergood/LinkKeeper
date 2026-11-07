"""MemorySnapshot 装配测试。

覆盖人物隔离、事实装配、同字段冲突、合并组聚合、截图过期与渲染。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.db.client import Db
from src.evidence import create_screenshot_evidence, create_text_evidence
from src.memory.snapshot import assemble_snapshot, render_snapshot, resolve_contact_group
from tests.conftest import (
    make_contact,
    make_fact,
    make_insight,
    make_memory,
    make_thread,
)


# ---------- 人物隔离 ----------


async def test_no_contact_does_not_leak(db: Db) -> None:
    """没有 contact_id 时，快照不得包含任何联系人的专属资料。"""
    thread_id = await make_thread(db)
    a = await make_contact(db, "张三")
    await make_fact(db, a, "company", "某科技公司")
    await make_memory(db, a, "喜欢喝美式", status="confirmed")

    result = await assemble_snapshot(db, contact_id=None, thread_id=thread_id)

    assert result.snapshot.contact is None
    assert result.snapshot.confirmed_memories == []
    # 渲染文本里明确禁止注入
    assert "不要注入任何联系人专属资料" in result.rendered
    # 绝不能出现张三的任何信息
    assert "张三" not in result.rendered
    assert "某科技公司" not in result.rendered


# ---------- 事实装配 ----------


async def test_assemble_with_facts(db: Db) -> None:
    thread_id = await make_thread(db)
    a = await make_contact(db, "张三", version=3)
    await make_fact(db, a, "company", "某科技公司")
    await make_fact(db, a, "title", "后端工程师")

    result = await assemble_snapshot(db, contact_id=a, thread_id=thread_id)

    contact = result.snapshot.contact
    assert contact is not None
    assert contact.display_name == "张三"
    assert contact.version == 3
    assert len(contact.facts) == 2
    fields = {f.field for f in contact.facts}
    assert fields == {"company", "title"}


# ---------- 冲突检测 ----------


async def test_conflict_detected_not_resolved(db: Db) -> None:
    """同字段两个 confirmed 值进 conflicts，不能擅自选一个。"""
    thread_id = await make_thread(db)
    a = await make_contact(db, "张三")
    await make_fact(db, a, "phone", "13800000000")
    await make_fact(db, a, "phone", "13900000000")

    result = await assemble_snapshot(db, contact_id=a, thread_id=thread_id)

    assert len(result.snapshot.conflicts) == 1
    conflict = result.snapshot.conflicts[0]
    assert conflict.field == "phone"
    assert set(conflict.values) == {"13800000000", "13900000000"}
    # 渲染时明确提示不要擅自裁决
    assert "不要擅自选一个" in result.rendered


async def test_no_conflict_when_same_value(db: Db) -> None:
    """同字段两个相同值 → 不算冲突。"""
    thread_id = await make_thread(db)
    a = await make_contact(db, "张三")
    await make_fact(db, a, "phone", "13800000000")
    await make_fact(db, a, "phone", "13800000000")

    result = await assemble_snapshot(db, contact_id=a, thread_id=thread_id)
    assert result.snapshot.conflicts == []


# ---------- 合并组 ----------


async def test_merged_group_aggregated(db: Db) -> None:
    """A 合并进 B 后，读 B 快照应能拿到 A 的事实（组内聚合）。"""
    thread_id = await make_thread(db)
    target = await make_contact(db, "张三")
    source = await make_contact(db, "张三（旧）")

    # 把 source 合并进 target
    await db.execute(
        """
        UPDATE contacts SET status = 'merged', merged_into_id = :t, merged_at = now()
        WHERE id = :s
        """,
        {"t": target, "s": source},
    )

    # 事实分别挂在两个人身上
    await make_fact(db, target, "company", "新公司")
    await make_fact(db, source, "phone", "13800000000")

    # 组解析：从 source 进去也应该拿到 target
    group = await resolve_contact_group(db, source)
    assert set(group) == {target, source}

    # 用 source id 装配快照，应聚合到 target 并标注 merged_from
    result = await assemble_snapshot(db, contact_id=source, thread_id=thread_id)
    contact = result.snapshot.contact
    assert contact is not None
    assert contact.id == target  # 根是 target
    assert len(contact.merged_from) == 1

    fields = {f.field for f in contact.facts}
    assert fields == {"company", "phone"}  # 两人事实都聚合进来
    assert "已合并" in result.rendered


# ---------- 过期截图 ----------


async def test_expired_screenshot_marked(db: Db, tmp_path) -> None:
    """过期截图在快照里标 expired=true，且渲染提示原图不可读。"""
    thread_id = await make_thread(db)
    a = await make_contact(db, "张三")

    ref = await create_screenshot_evidence(
        db,
        thread_id=thread_id,
        contact_id=a,
        data=b"\x89PNG" + b"0" * 100,
        mime_type="image/png",
        evidence_dir=str(tmp_path),
        source_summary="聊天截图：提到下周喝咖啡",
    )

    future = datetime.now(timezone.utc) + timedelta(hours=25)
    result = await assemble_snapshot(db, contact_id=a, thread_id=thread_id, now=future)

    expired = [e for e in result.snapshot.evidence if e.id == ref.id]
    assert len(expired) == 1
    assert expired[0].expired is True
    assert "已过期" in result.rendered


# ---------- 候选 vs 已确认 ----------


async def test_confirmed_vs_candidate_separated(db: Db) -> None:
    thread_id = await make_thread(db)
    a = await make_contact(db, "张三")
    ev = await create_text_evidence(db, thread_id=thread_id, contact_id=a, text="证据")

    await make_memory(db, a, "喜欢喝美式", status="confirmed", evidence_ids=[ev.id])
    await make_memory(db, a, "可能喜欢读书", status="candidate", evidence_ids=[ev.id])
    await make_memory(db, a, "被否定的猜想", status="dismissed", evidence_ids=[ev.id])

    result = await assemble_snapshot(db, contact_id=a, thread_id=thread_id)

    confirmed = {m.content for m in result.snapshot.confirmed_memories}
    candidates = {c.content for c in result.snapshot.candidates}

    assert "喜欢喝美式" in confirmed
    assert "可能喜欢读书" in candidates
    # dismissed 两边都不出现
    assert "被否定的猜想" not in confirmed
    assert "被否定的猜想" not in candidates


# ---------- 渲染 ----------


async def test_render_contains_evidence_ids(db: Db) -> None:
    """渲染结果必须保留 evidence ID 原文，供模型精确引用。"""
    thread_id = await make_thread(db)
    a = await make_contact(db, "张三")
    ref = await create_text_evidence(
        db, thread_id=thread_id, contact_id=a, text="张三说下周有空"
    )
    await make_fact(db, a, "company", "某公司", evidence_ids=[ref.id])

    result = await assemble_snapshot(db, contact_id=a, thread_id=thread_id)

    # evidence ID 至少出现两次：一次在事实引用，一次在证据列表
    assert result.rendered.count(ref.id) >= 2
    assert "只能引用下面出现的 evidence id" in result.rendered


async def test_insight_marked_as_inference(db: Db) -> None:
    """洞察在渲染里必须标明「推断，不是事实」。"""
    thread_id = await make_thread(db)
    a = await make_contact(db, "张三")
    ev = await create_text_evidence(db, thread_id=thread_id, contact_id=a, text="证据")
    await make_insight(db, a, "对方开始主动推进互动", evidence_ids=[ev.id])

    result = await assemble_snapshot(db, contact_id=a, thread_id=thread_id)
    assert "推断，不是事实" in result.rendered
    assert "对方开始主动推进互动" in result.rendered


async def test_dismissed_insight_excluded(db: Db) -> None:
    """refuted/superseded 的洞察不进快照。"""
    thread_id = await make_thread(db)
    a = await make_contact(db, "张三")
    ev = await create_text_evidence(db, thread_id=thread_id, contact_id=a, text="证据")
    await make_insight(db, a, "被否定的洞察", status="refuted", evidence_ids=[ev.id])
    await make_insight(db, a, "有效洞察", status="active", evidence_ids=[ev.id])

    result = await assemble_snapshot(db, contact_id=a, thread_id=thread_id)
    contents = {i.content for i in result.snapshot.insights}
    assert "有效洞察" in contents
    assert "被否定的洞察" not in contents
