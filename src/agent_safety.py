"""运行预算守卫：外部裁判，不信任模型的自我约束。

在 LLM 调用前、工具执行前、工具返回后分别校验轮次、调用数、重复调用、连续错误与总时长；
超限抛 BudgetExceeded，由调用方把 run 落成 failed 并写入可展示的停止说明。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import BudgetExceeded

BudgetDimension = Literal[
    "turns",
    "tool_calls",
    "duplicate_calls",
    "consecutive_errors",
    "timeout",
]

# 停止说明模板：给用户看，必须能指导下一步动作
_STOP_HINTS: dict[BudgetDimension, str] = {
    "turns": "本轮推理轮次已达上限，请把任务拆小一些再试",
    "tool_calls": "本轮工具调用次数已达上限，请缩小查询范围后重试",
    "duplicate_calls": "检测到重复调用同一工具与参数，已停止以避免原地打转",
    "consecutive_errors": "连续多次工具调用失败，已停止；请检查数据或稍后重试",
    "timeout": "本轮处理超时，请把任务拆小一些再试",
}


def normalize_args(args: Any) -> str:
    """把工具参数规范化成稳定字符串，用于「相同调用」判重。

    必须排序 key：`{"a":1,"b":2}` 和 `{"b":2,"a":1}` 是同一次调用。
    """
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(args)


def call_signature(tool_name: str, args: Any) -> str:
    """一次调用的指纹：工具名 + 规范化参数。"""
    digest = hashlib.sha256(normalize_args(args).encode("utf-8")).hexdigest()[:16]
    return f"{tool_name}:{digest}"


@dataclass
class RunGuard:
    """单次 run 的预算守卫。

    默认上限：8 轮、12 次工具调用、同签名 2 次、连续错误 3 次、90 秒。
    """

    max_turns: int = 8
    max_tool_calls: int = 12
    max_duplicate_calls: int = 2
    max_consecutive_errors: int = 3
    max_seconds: int = 90
    label: str = "main"
    """用于日志区分主 Agent 与 Insight Agent（Insight 传 max_seconds=60）。"""

    turns: int = field(default=0, init=False)
    tool_calls: int = field(default=0, init=False)
    consecutive_errors: int = field(default=0, init=False)
    _call_counts: dict[str, int] = field(default_factory=dict, init=False)
    _started_at: float = field(default_factory=time.monotonic, init=False)

    # ---- 内部 ----

    def _elapsed_seconds(self) -> float:
        # 用 monotonic：系统时钟被 NTP 调整时不会误判超时
        return time.monotonic() - self._started_at

    def _raise(self, dimension: BudgetDimension, detail: str) -> None:
        raise BudgetExceeded(
            f"[{self.label}] {_STOP_HINTS[dimension]}（{detail}）",
            details={
                "dimension": dimension,
                "label": self.label,
                "hint": _STOP_HINTS[dimension],
                "turns": self.turns,
                "tool_calls": self.tool_calls,
                "consecutive_errors": self.consecutive_errors,
                "elapsed_seconds": round(self._elapsed_seconds(), 1),
            },
        )

    # ---- 三个检查点 ----

    def check_turn(self) -> None:
        """每轮 LLM 调用前调用：校验轮次上限与总时长。"""
        if self._elapsed_seconds() > self.max_seconds:
            self._raise("timeout", f"已用 {self._elapsed_seconds():.1f}s > {self.max_seconds}s")
        if self.turns >= self.max_turns:
            self._raise("turns", f"已用 {self.turns}/{self.max_turns} 轮")
        self.turns += 1

    def check_tool_call(self, tool_name: str, args: Any) -> None:
        """每次执行工具前调用：校验总数、重复调用、时长。

        相同工具与相同参数最多重复 2 次，第 3 次直接阻断。
        """
        if self._elapsed_seconds() > self.max_seconds:
            self._raise("timeout", f"已用 {self._elapsed_seconds():.1f}s > {self.max_seconds}s")

        if self.tool_calls >= self.max_tool_calls:
            self._raise("tool_calls", f"已用 {self.tool_calls}/{self.max_tool_calls} 次")

        signature = call_signature(tool_name, args)
        count = self._call_counts.get(signature, 0)
        if count >= self.max_duplicate_calls:
            self._raise(
                "duplicate_calls",
                f"{tool_name} 相同参数已调用 {count} 次（上限 {self.max_duplicate_calls}）",
            )

        self._call_counts[signature] = count + 1
        self.tool_calls += 1

    def record_tool_result(self, *, is_error: bool) -> None:
        """工具返回后调用：维护连续错误计数。

        语义是连续：一次成功就清零，偶发失败不会累积到上限。
        """
        if is_error:
            self.consecutive_errors += 1
            if self.consecutive_errors >= self.max_consecutive_errors:
                self._raise(
                    "consecutive_errors",
                    f"连续 {self.consecutive_errors} 次失败（上限 {self.max_consecutive_errors}）",
                )
        else:
            self.consecutive_errors = 0

    # ---- 观测 ----

    @property
    def elapsed_ms(self) -> int:
        return int(self._elapsed_seconds() * 1000)

    def snapshot(self) -> dict[str, Any]:
        """当前用量，写进 `agent_runs.budget` / `usage` 便于事后审计。"""
        return {
            "label": self.label,
            "turns": self.turns,
            "max_turns": self.max_turns,
            "tool_calls": self.tool_calls,
            "max_tool_calls": self.max_tool_calls,
            "consecutive_errors": self.consecutive_errors,
            "max_consecutive_errors": self.max_consecutive_errors,
            "elapsed_ms": self.elapsed_ms,
            "max_seconds": self.max_seconds,
            "duplicate_signatures": len(self._call_counts),
        }


def make_insight_guard() -> RunGuard:
    """Insight Agent 专用守卫：60 秒独立上限，不占用主循环的 90 秒额度。"""
    return RunGuard(max_turns=4, max_tool_calls=4, max_seconds=60, label="insight")


# ---- 图片限制：0–4 张，单张解码后 ≤8MB ----

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")


def assert_image_limits(images: list[Any]) -> None:
    """校验图片数量与大小；超规格不进入模型循环。"""
    import base64
    import binascii

    if len(images) > MAX_IMAGES:
        raise BudgetExceeded(
            f"最多 {MAX_IMAGES} 张图片，本次传入 {len(images)} 张",
            details={"dimension": "images", "count": len(images), "limit": MAX_IMAGES},
        )

    for index, image in enumerate(images):
        mime = getattr(image, "mime_type", None)
        if mime not in ALLOWED_IMAGE_TYPES:
            raise BudgetExceeded(
                f"第 {index + 1} 张图片格式不支持：{mime}（仅接受 jpeg/png/webp）",
                details={"dimension": "images", "index": index, "mime": mime},
            )
        raw = getattr(image, "data_base64", "")
        try:
            size = len(base64.b64decode(raw, validate=True))
        except (binascii.Error, ValueError) as exc:
            raise BudgetExceeded(
                f"第 {index + 1} 张图片不是合法 base64",
                details={"dimension": "images", "index": index},
            ) from exc
        if size > MAX_IMAGE_BYTES:
            raise BudgetExceeded(
                f"第 {index + 1} 张图片解码后 {size} 字节，超过 {MAX_IMAGE_BYTES} 上限",
                details={"dimension": "images", "index": index, "bytes": size},
            )
