"""统一错误类型，按 HTTP 层如何映射来分类。

`ConflictError` 同时承载 CAS 冲突与状态不允许，统一映射 409；
`ActiveRunError` 单独一类，因为前端提示与业务冲突不同。
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """所有业务异常的基类。携带 HTTP 状态码和可选的结构化详情。"""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class NotFoundError(AppError):
    """资源不存在（会话 / Action / 联系人 / 问卷）。"""

    status_code = 404
    code = "not_found"


class ValidationError(AppError):
    """入参或 payload 校验失败（服务端二次校验也会抛这个）。"""

    status_code = 422
    code = "validation_error"


class ConflictError(AppError):
    """状态冲突：CAS 版本不匹配、状态机不允许的流转、重复决策。"""

    status_code = 409
    code = "conflict"


class ActiveRunError(AppError):
    """同一会话已有 running/waiting_* 的 run，单轮互斥。"""

    status_code = 409
    code = "active_run"


class PermissionDenied(AppError):
    """工具调用被权限策略拒绝（不是用户拒绝，是策略本身不允许）。"""

    status_code = 403
    code = "permission_denied"


class BudgetExceeded(AppError):
    """运行预算超限（轮次 / 工具次数 / 时长）。"""

    status_code = 429
    code = "budget_exceeded"
