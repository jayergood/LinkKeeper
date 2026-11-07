"""预算守卫的单元测试，覆盖轮次、工具数、重复调用、连续错误、时长五个维度。"""

from __future__ import annotations

import base64

import pytest

from src.agent_safety import (
    MAX_IMAGES,
    RunGuard,
    assert_image_limits,
    call_signature,
    make_insight_guard,
    normalize_args,
)
from src.domain import ImageInput
from src.errors import BudgetExceeded


# ---------- 维度 1：模型 turn 上限 ----------


def test_turn_limit() -> None:
    guard = RunGuard(max_turns=3)
    for _ in range(3):
        guard.check_turn()
    assert guard.turns == 3
    with pytest.raises(BudgetExceeded) as exc:
        guard.check_turn()
    assert exc.value.details["dimension"] == "turns"


# ---------- 维度 2：工具调用总数 ----------


def test_tool_call_limit() -> None:
    guard = RunGuard(max_tool_calls=2, max_duplicate_calls=10)
    guard.check_tool_call("a", {"x": 1})
    guard.check_tool_call("b", {"x": 2})
    with pytest.raises(BudgetExceeded) as exc:
        guard.check_tool_call("c", {"x": 3})
    assert exc.value.details["dimension"] == "tool_calls"


# ---------- 维度 3：相同工具 + 相同参数 ----------


def test_normalize_args_is_order_insensitive() -> None:
    """参数 key 顺序不同，必须视为同一次调用。"""
    assert normalize_args({"a": 1, "b": 2}) == normalize_args({"b": 2, "a": 1})
    assert call_signature("t", {"a": 1, "b": 2}) == call_signature("t", {"b": 2, "a": 1})


def test_duplicate_call_limit() -> None:
    """相同工具 + 相同参数最多 2 次，第 3 次阻断。"""
    guard = RunGuard(max_tool_calls=20, max_duplicate_calls=2)
    guard.check_tool_call("search", {"q": "张三"})
    guard.check_tool_call("search", {"q": "张三"})
    with pytest.raises(BudgetExceeded) as exc:
        guard.check_tool_call("search", {"q": "张三"})
    assert exc.value.details["dimension"] == "duplicate_calls"


def test_different_args_not_counted_as_duplicate() -> None:
    guard = RunGuard(max_tool_calls=20, max_duplicate_calls=2)
    guard.check_tool_call("search", {"q": "张三"})
    guard.check_tool_call("search", {"q": "李四"})
    guard.check_tool_call("search", {"q": "王五"})  # 参数不同，不算重复
    assert guard.tool_calls == 3


# ---------- 维度 4：连续工具错误 ----------


def test_consecutive_errors() -> None:
    guard = RunGuard(max_consecutive_errors=3)
    guard.record_tool_result(is_error=True)
    guard.record_tool_result(is_error=True)
    with pytest.raises(BudgetExceeded) as exc:
        guard.record_tool_result(is_error=True)
    assert exc.value.details["dimension"] == "consecutive_errors"


def test_consecutive_errors_reset_on_success() -> None:
    """关键语义：是**连续**而不是累计 —— 一次成功就清零。"""
    guard = RunGuard(max_consecutive_errors=3)
    guard.record_tool_result(is_error=True)
    guard.record_tool_result(is_error=True)
    guard.record_tool_result(is_error=False)  # 清零
    guard.record_tool_result(is_error=True)
    guard.record_tool_result(is_error=True)
    # 没有到 3 连错，不应抛
    assert guard.consecutive_errors == 2


# ---------- 维度 5：总时长 ----------


def test_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = RunGuard(max_seconds=1)
    # 把单调时钟往前拨 2 秒
    base = guard._started_at
    monkeypatch.setattr(
        "src.agent_safety.time.monotonic", lambda: base + 2.0
    )
    with pytest.raises(BudgetExceeded) as exc:
        guard.check_turn()
    assert exc.value.details["dimension"] == "timeout"


def test_timeout_checked_in_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = RunGuard(max_seconds=1)
    base = guard._started_at
    monkeypatch.setattr(
        "src.agent_safety.time.monotonic", lambda: base + 5.0
    )
    with pytest.raises(BudgetExceeded):
        guard.check_tool_call("x", {})


# ---------- Insight Agent 独立 60 秒 ----------


def test_insight_guard_uses_60_seconds() -> None:
    guard = make_insight_guard()
    assert guard.max_seconds == 60
    assert guard.label == "insight"


# ---------- 图片限制 ----------


def test_image_count_limit() -> None:
    one_px_png = base64.b64encode(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000d4944415478da63f8cfc0f01f0005000201a5f1b8f8000000004945"
            "4e44ae426082"
        )
    ).decode()
    ok = [ImageInput(mime_type="image/png", data_base64=one_px_png)] * MAX_IMAGES
    assert_image_limits(ok)  # 4 张正好在上限

    too_many = [ImageInput(mime_type="image/png", data_base64=one_px_png)] * (
        MAX_IMAGES + 1
    )
    with pytest.raises(BudgetExceeded) as exc:
        assert_image_limits(too_many)
    assert exc.value.details["dimension"] == "images"


def test_image_type_rejected() -> None:
    with pytest.raises(Exception):
        ImageInput(mime_type="image/gif", data_base64="AAA")  # type: ignore[arg-type]


def test_image_oversize_rejected() -> None:
    big = base64.b64encode(b"\x00" * (8 * 1024 * 1024 + 1)).decode()
    with pytest.raises(BudgetExceeded) as exc:
        assert_image_limits([ImageInput(mime_type="image/png", data_base64=big)])
    assert exc.value.details["dimension"] == "images"


# ---------- 观测 ----------


def test_guard_snapshot() -> None:
    guard = RunGuard(max_turns=8, max_tool_calls=12)
    guard.check_turn()
    guard.check_tool_call("a", {"x": 1})
    snap = guard.snapshot()
    assert snap["turns"] == 1
    assert snap["tool_calls"] == 1
    assert snap["max_turns"] == 8
    assert "elapsed_ms" in snap
