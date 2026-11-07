"""Relationship Agent 配置加载。

用 pydantic-settings 从环境变量或 `.env` 读取；预算与阈值集中在此，启动时校验失败即报错。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全部运行时配置。字段名与环境变量名一一对应（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- 数据库 ----------
    database_url: str = Field(
        default="postgresql+asyncpg://relationship:relationship@localhost:5432/relationship",
        description="开发库连接串（asyncpg 驱动）",
    )
    test_database_url: str = Field(
        default="postgresql+asyncpg://relationship:relationship@localhost:5432/relationship_test",
        description="评测专用库，会被重置",
    )

    # ---------- LLM ----------
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"

    # ---------- 运行预算 ----------
    max_turns: int = Field(default=8, ge=1, description="单次 run 模型轮次上限")
    max_tool_calls: int = Field(default=12, ge=1, description="单次 run 工具调用次数上限")
    max_run_seconds: int = Field(default=90, ge=1, description="单次 run 总时长上限")
    max_images: int = Field(default=4, ge=1, description="单条消息最多图片数")
    max_image_bytes: int = Field(default=8 * 1024 * 1024, ge=1, description="单张图片解码后字节上限")

    # ---------- 证据 ----------
    evidence_dir: str = "./data/evidence"
    evidence_ttl_hours: int = Field(default=24, ge=1, description="截图过期小时数")

    # ---------- 会话压缩 ----------
    compaction_threshold_tokens: int = Field(default=40_000, ge=1)
    compaction_keep_recent: int = Field(default=12, ge=1, description="压缩后保留的最近消息条数")

    # ---------- 记忆 ----------
    memory_document_max_chars: int = Field(
        default=8000, ge=1, description="长期记忆文档字符上限"
    )

    # ---------- 联网（可选）----------
    search_provider: str = "none"  # none | tavily | brave
    search_api_key: str = ""

    # ---------- Langfuse ----------
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3000"

    # ---------- 服务 ----------
    host: str = "127.0.0.1"
    port: int = 8787
    log_level: str = "INFO"

    # ---------- 本地单用户 ----------
    user_id: str = "local-user"
    user_timezone: str = "Asia/Shanghai"

    @property
    def langfuse_enabled(self) -> bool:
        """两个 key 都配了才启用追踪；否则静默降级，不阻塞主流程。"""
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试里可用 `get_settings.cache_clear()` 重置。"""
    return Settings()


settings = get_settings()
