"""领域模型与入参校验（Pydantic v2）：枚举、HTTP 入参、Action/Questionnaire/Insight 载荷、
工具上下文与 MemorySnapshot 结构。

内部统一 snake_case；枚举用 Literal，取值与 migrations/*.sql 的 CHECK 约束保持一致。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---- 枚举（与 migrations/*.sql 的 CHECK 约束严格一致） ----

PermissionMode = Literal["ask", "full"]
"""两态权限：ask=联网每次询问；full=联网放宽但写入仍需确认。"""

EvidenceType = Literal["user_note", "screenshot", "web_page", "action_receipt"]

FactStatus = Literal["candidate", "confirmed", "dismissed", "superseded"]
"""注意是 dismissed（用户否定）而不是 rejected（系统拒绝）。"""

MemoryStatus = Literal["candidate", "confirmed", "dismissed", "superseded"]

InsightStatus = Literal["active", "refuted", "superseded"]

ActionType = Literal[
    "create_contact",
    "update_contact",
    "merge_contacts",
    "create_event",
    "update_event",
    "delete_event",
]

ActionStatus = Literal["proposed", "confirmed", "rejected", "executed", "failed", "conflict"]

RunStatus = Literal[
    "running",
    "waiting_questionnaire",
    "waiting_permission",
    "completed",
    "proposed",
    "failed",
    "aborted",
]

QuestionKind = Literal["single_choice", "multi_choice", "text", "date"]

SkipReason = Literal[
    "no_new_signal",
    "insufficient_evidence",
    "identity_ambiguous",
    "not_person_related",
]
"""Insight 主动跳过的四种原因。"""

MemoryItemKind = Literal["fact", "preference", "relationship", "impression"]
"""细粒度记忆类型。"""

TerminalToolName = Literal["questionnaire", "propose_action", "publish_insight", "skip_insight"]
"""终态工具：一旦调用，本轮 tool loop 立即结束，不再执行其他工具。"""


# ---- 页面上下文 ----


class PageContext(BaseModel):
    """当前页面上下文。四个字段都允许为空 —— 首页发起对话时没有任何页面上下文。"""

    model_config = ConfigDict(extra="ignore")

    contact_id: str | None = Field(default=None, description="当前指向的联系人")
    insight_id: str | None = Field(default=None, description="当前查看的洞察")
    calendar_event_id: str | None = Field(default=None, description="当前查看的日程")
    revision: int | None = Field(default=None, ge=0, description="页面数据版本，用于检测过期")


# ---- 一轮请求的入参 ----


class ImageInput(BaseModel):
    """上传图片。只接受三种格式，解码后大小由 agent_safety 校验。"""

    mime_type: Literal["image/jpeg", "image/png", "image/webp"]
    data_base64: str = Field(min_length=1)


class SendMessageInput(BaseModel):
    """一轮请求的完整入参。回答 Questionnaire 时还会带上结构化答案。"""

    model_config = ConfigDict(extra="ignore")

    text: str | None = Field(default=None, max_length=8_000)
    images: list[ImageInput] = Field(default_factory=list)
    page_context: PageContext = Field(default_factory=PageContext)
    permission_mode: PermissionMode = "ask"
    request_id: str = Field(min_length=1, max_length=120, description="客户端生成的幂等键")

    @field_validator("text")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    def has_content(self) -> bool:
        """文字和图片至少一个非空，否则不是有效请求。"""
        return bool(self.text) or bool(self.images)


# ---- Action payload（6 种类型） ----


class _BasePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateContactPayload(_BasePayload):
    type: Literal["create_contact"]
    display_name: str = Field(min_length=1, max_length=120)
    company: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=60)
    email: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=4_000)


class UpdateContactPayload(_BasePayload):
    type: Literal["update_contact"]
    contact_id: str
    patch: dict[str, str] = Field(description="只允许 contact 的可写字段")
    base_version: int | None = Field(default=None, ge=1, description="CAS 依据；不填由服务端盖")

    @field_validator("patch")
    @classmethod
    def _patch_not_empty(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("patch 不能为空")
        allowed = {"display_name", "company", "title", "phone", "email", "notes"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"patch 含不可写字段: {sorted(unknown)}")
        return value


class MergeContactsPayload(_BasePayload):
    type: Literal["merge_contacts"]
    source_contact_ids: list[str] = Field(min_length=1)
    target_contact_id: str
    field_choices: dict[str, str] = Field(default_factory=dict, description="字段名 → 采用哪个值")
    base_version: int | None = Field(default=None, ge=1)


class CreateEventPayload(_BasePayload):
    type: Literal["create_event"]
    title: str = Field(min_length=1, max_length=200)
    starts_at: str = Field(description="ISO 8601 带时区")
    ends_at: str
    timezone: str = Field(default="Asia/Shanghai", min_length=1)
    location: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=4_000)
    contact_ids: list[str] = Field(default_factory=list)


class UpdateEventPayload(_BasePayload):
    type: Literal["update_event"]
    event_id: str
    patch: dict[str, Any] = Field(description="只允许 event 的可写字段")
    base_version: int | None = Field(default=None, ge=1)

    @field_validator("patch")
    @classmethod
    def _patch_not_empty(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("patch 不能为空")
        allowed = {"title", "starts_at", "ends_at", "timezone", "location", "notes", "contact_ids"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"patch 含不可写字段: {sorted(unknown)}")
        return value


class DeleteEventPayload(_BasePayload):
    type: Literal["delete_event"]
    event_id: str
    base_version: int | None = Field(default=None, ge=1)


ActionPayload = Annotated[
    Union[
        CreateContactPayload,
        UpdateContactPayload,
        MergeContactsPayload,
        CreateEventPayload,
        UpdateEventPayload,
        DeleteEventPayload,
    ],
    Field(discriminator="type"),
]
"""六种 Action payload。用 discriminated union 保证 `type` 与字段一致。"""


# ---- Questionnaire ----


class QuestionItem(BaseModel):
    """一道结构化问题。每题最多 4 个候选项。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=60)
    prompt: str = Field(min_length=1, max_length=300)
    kind: QuestionKind
    options: list[str] | None = Field(default=None, max_length=4)
    required: bool = True

    @field_validator("options")
    @classmethod
    def _check_options(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            if len(value) < 2:
                raise ValueError("选择题至少 2 个选项")
            if len(value) > 4:
                raise ValueError("每题最多 4 个选项")
            return value
        return value

    def model_post_init(self, __context: Any) -> None:
        needs_options = self.kind in ("single_choice", "multi_choice")
        if needs_options and not self.options:
            raise ValueError("选择题必须提供 options")
        if not needs_options and self.options:
            raise ValueError("非选择题不要带 options")


class QuestionnairePayload(BaseModel):
    """问卷整体：1–4 题。"""

    model_config = ConfigDict(extra="forbid")

    questions: list[QuestionItem] = Field(min_length=1, max_length=4)


# ---- Insight 输出（双终结工具） ----


class PublishInsightPayload(BaseModel):
    """publish_insight：发布一条洞察（可选附带长期记忆草案）。"""

    model_config = ConfigDict(extra="forbid")

    group: str | None = Field(default=None, max_length=100, description="洞察分组标签")
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=800)
    recommendation: str | None = Field(default=None, max_length=800)
    revaluate_at: str | None = Field(default=None, description="重评时点 ISO 8601；默认 30 天后")
    confidence: Literal["low", "medium", "high"]
    source_ids: list[str] = Field(min_length=1, description="来源集合；至少一条来自当前 Action")
    memory_document_draft: str | None = Field(
        default=None, max_length=8_000, description="可选：完整长期记忆文档草案"
    )


class SkipInsightPayload(BaseModel):
    """skip_insight：主动跳过，是正确终态而非失败。"""

    model_config = ConfigDict(extra="forbid")

    reason: SkipReason
    note: str | None = Field(default=None, max_length=300)


InsightTerminal = Annotated[
    Union[PublishInsightPayload, SkipInsightPayload],
    Field(discriminator=None),  # 二选一，靠字段存在性区分，不用 type 字段
]
"""Insight Agent 的两个终结工具载荷，二选一。"""


# ---- 工具上下文与终态 ----


class ToolTerminalState(BaseModel):
    """终态记录。tool loop 看到它非空就立即停止。"""

    model_config = ConfigDict(extra="ignore")

    type: TerminalToolName
    id: str


class ToolContext(BaseModel):
    """每次 run 的工具上下文。工具是 per-run 闭包，作用域钉死在本轮的
    contact / thread / evidence 范围内。"""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="ignore")

    user_id: str
    thread_id: str
    run_id: str
    contact_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    permission_mode: PermissionMode = "ask"
    granted_permission: str | None = Field(
        default=None, description="ask 模式下用户刚授权的那一个工具；只放行一次"
    )
    terminal: ToolTerminalState | None = None
    max_tool_calls: int = 12
    current_tool_call_id: str | None = Field(
        default=None,
        description="当前正在执行的 tool_call.id。tool loop 每次执行工具前必须写入 —— "
        "终态工具（如 questionnaire）要把这个 id 落库，恢复时才能补出配对的 toolResult",
    )

    # db 句柄由调用方注入（Pydantic 用 arbitrary_types 放行）
    db: Any = Field(default=None, exclude=True)
    vector_clock: dict[str, Any] = Field(default_factory=dict, exclude=True)


# ---- MemorySnapshot 结构 ----


class _UUIDSafeModel(BaseModel):
    """自动把 UUID 转成 str 的基类。asyncpg 对 `uuid` 列返回 `uuid.UUID`，
    协议层、JSON 输出与给模型看的文本一律用字符串。"""

    model_config = ConfigDict(extra="ignore")

    @classmethod
    def _coerce(cls, value: Any) -> Any:
        from uuid import UUID

        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, list):
            return [cls._coerce(item) for item in value]
        if isinstance(value, tuple):
            return [cls._coerce(item) for item in value]
        if isinstance(value, dict):
            return {key: cls._coerce(item) for key, item in value.items()}
        return value

    @model_validator(mode="before")
    @classmethod
    def _coerce_uuids(cls, data: Any) -> Any:
        return cls._coerce(data)


class SnapshotFact(_UUIDSafeModel):
    field: str
    value: str
    status: FactStatus
    evidence_ids: list[str] = Field(default_factory=list)


class SnapshotEvidence(_UUIDSafeModel):
    id: str
    type: EvidenceType
    summary: str
    captured_at: str
    expired: bool = False


class SnapshotCandidate(_UUIDSafeModel):
    id: str
    kind: str
    content: str
    evidence_ids: list[str] = Field(default_factory=list)


class SnapshotConflict(_UUIDSafeModel):
    field: str
    values: list[str]
    evidence_ids: list[str] = Field(default_factory=list)


class SnapshotGoal(_UUIDSafeModel):
    id: str
    description: str
    evidence_ids: list[str] = Field(default_factory=list)


class SnapshotInsight(_UUIDSafeModel):
    id: str
    content: str
    confidence: Literal["low", "medium", "high"]
    evidence_ids: list[str] = Field(default_factory=list)


class SnapshotMemory(_UUIDSafeModel):
    id: str
    kind: str
    content: str
    evidence_ids: list[str] = Field(default_factory=list)


class SnapshotContact(_UUIDSafeModel):
    id: str
    display_name: str
    version: int
    facts: list[SnapshotFact] = Field(default_factory=list)
    merged_from: list[dict[str, str]] = Field(default_factory=list)


class MemorySnapshot(_UUIDSafeModel):
    """按 personID 装配的上下文快照。"""

    now: str
    timezone: str
    contact: SnapshotContact | None = None
    evidence: list[SnapshotEvidence] = Field(default_factory=list)
    candidates: list[SnapshotCandidate] = Field(default_factory=list)
    conflicts: list[SnapshotConflict] = Field(default_factory=list)
    goals: list[SnapshotGoal] = Field(default_factory=list)
    insights: list[SnapshotInsight] = Field(default_factory=list)
    confirmed_memories: list[SnapshotMemory] = Field(default_factory=list)
    memory_document: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = Field(default_factory=list)
    thread: dict[str, Any] = Field(default_factory=dict)


# ---- 工具定义与结果 ----


class ToolResult(BaseModel):
    """工具执行结果。`terminate=True` 表示终态工具，tool loop 必须立即停止。"""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="ignore")

    text: str
    details: Any = None
    is_error: bool = False
    terminate: bool = False


class ToolDef(BaseModel):
    """工具定义（供 LLM 生成 function schema）。"""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="ignore")

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    category: Literal[
        "read", "time", "screenshot", "research", "candidate", "questionnaire", "action"
    ] = "read"
    """7 类工具分类。"""

    is_terminal: bool = False
    fn: Any = Field(exclude=True)
