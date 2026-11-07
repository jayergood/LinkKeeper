"""数据库访问层：SQLAlchemy 2.0 Core + asyncpg，写原生 SQL，不用 ORM。

参数用命名风格 `:name`（`text()` 会做绑定转换），数组参数需显式 CAST。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from ..config import settings


class Connection:
    """一个事务内的连接包装：query / query_one 拿行，execute 拿影响行数。

    出口统一把 UUID 转 str，业务层只处理字符串 ID。
    """

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    @staticmethod
    def _stringify(value: Any) -> Any:
        """递归把 UUID 转 str（含 list / tuple / dict 内的嵌套值）。"""
        from uuid import UUID

        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, (list, tuple)):
            return [Connection._stringify(item) for item in value]
        if isinstance(value, dict):
            return {key: Connection._stringify(item) for key, item in value.items()}
        return value

    async def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result = await self._conn.execute(text(sql), params or {})
        return [
            {key: self._stringify(value) for key, value in row._mapping.items()}
            for row in result
        ]

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> int:
        """执行写语句，返回影响行数；CAS 场景必须检查返回值，0 表示别人先改了。"""
        result = await self._conn.execute(text(sql), params or {})
        return result.rowcount or 0

    async def execute_returning(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """带 RETURNING 的写语句（PG 特有），拿回写入后的行。"""
        result = await self._conn.execute(text(sql), params or {})
        row = result.fetchone()
        if row is None:
            return None
        return {key: self._stringify(value) for key, value in row._mapping.items()}


class Db:
    """连接池 + 事务入口。进程内单例。"""

    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            echo=False,
        )

    # ---- 只读查询（不提交，用 engine.connect）----
    #
    # query/query_one 只用于 SELECT：用它们执行 INSERT/UPDATE 会静默回滚，
    # 因为 engine.connect() 归还连接时回滚未提交的事务。写操作必须用
    # execute / execute_returning / transaction。

    async def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """只读查询，返回多行。"""
        async with self.engine.connect() as conn:
            return await Connection(conn).query(sql, params)

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """只读查询，返回单行。**不要用于写操作。**"""
        async with self.engine.connect() as conn:
            return await Connection(conn).query_one(sql, params)

    # ---- 写操作（自动提交，用 engine.begin）----

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> int:
        """写语句，返回影响行数（**自动提交**）。"""
        async with self.engine.begin() as conn:
            return await Connection(conn).execute(sql, params)

    async def execute_returning(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """写语句 + RETURNING，返回写入的行（**自动提交**）。

        用于 `INSERT ... RETURNING id`；改用 query_one() 执行时插入会被静默回滚。
        """
        async with self.engine.begin() as conn:
            return await Connection(conn).execute_returning(sql, params)

    # ---- 事务 ----

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Connection]:
        """事务上下文：正常退出提交，异常自动回滚。

        行锁 + CAS + 回执写入必须都在这个块里，任一步失败整体回滚。
        """
        async with self.engine.begin() as conn:
            yield Connection(conn)

    async def close(self) -> None:
        await self.engine.dispose()

    async def healthcheck(self) -> bool:
        try:
            await self.query_one("SELECT 1 AS ok")
            return True
        except Exception:  # noqa: BLE001 —— 探活失败就是 False，不向上抛
            return False


_db: Db | None = None


def get_db() -> Db:
    """进程内单例。测试可传自定义 url 直接构造 Db(...)。"""
    global _db
    if _db is None:
        _db = Db(settings.database_url)
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None
