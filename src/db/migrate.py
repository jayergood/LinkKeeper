"""迁移执行器：按文件名顺序应用 migrations/*.sql，每个文件一个事务。

已应用的文件记录在 schema_migrations，重复执行不会重复应用；迁移文件内不要写 BEGIN/COMMIT。
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .client import Db
from ..config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def migration_files() -> list[Path]:
    """按文件名排序返回全部迁移（001_… 到 018_…）。"""
    if not MIGRATIONS_DIR.is_dir():
        raise FileNotFoundError(f"迁移目录不存在: {MIGRATIONS_DIR}")
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


async def applied_set(db: Db) -> set[str]:
    rows = await db.query("SELECT filename FROM schema_migrations")
    return {row["filename"] for row in rows}


def split_statements(sql: str) -> list[str]:
    """按分号拆成独立语句：asyncpg 走预处理语句协议，一条 SQL 里多语句会报错。

    本项目迁移只含 DDL，按 `;` 裸拆是安全的。
    """
    statements: list[str] = []
    for raw in sql.split(";"):
        # 去掉纯注释行，避免把注释当成语句发给 PG
        lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("--")]
        stmt = "\n".join(lines).strip()
        if stmt:
            statements.append(stmt)
    return statements


async def run_migrations(url: str | None = None, *, reset: bool = False) -> dict[str, int]:
    """应用迁移，返回统计。reset=True 会先清空 public schema（危险，仅本地）。"""
    db = Db(url or settings.database_url)
    applied_count = 0
    skipped_count = 0
    try:
        if reset:
            print("[WARN]  --reset：即将 DROP SCHEMA public CASCADE（仅限本地开发/评测库）")
            await db.execute("DROP SCHEMA public CASCADE")
            await db.execute("CREATE SCHEMA public")

        await db.execute(TRACKING_TABLE)
        already = await applied_set(db)

        for path in migration_files():
            if path.name in already:
                skipped_count += 1
                continue
            sql = path.read_text(encoding="utf-8-sig")  # utf-8-sig 自动剥离 BOM
            statements = split_statements(sql)
            if not statements:
                skipped_count += 1
                continue

            # 一个迁移文件 = 一个事务：失败整体回滚，不会留半套表结构
            async with db.transaction() as tx:
                for stmt in statements:
                    await tx.execute(stmt)
                await tx.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (:f)",
                    {"f": path.name},
                )
            applied_count += 1
            print(f"  [OK] {path.name}")
    finally:
        await db.close()

    print(f"\n迁移完成：新应用 {applied_count} 个，跳过 {skipped_count} 个")
    return {"applied": applied_count, "skipped": skipped_count}


def main() -> None:
    parser = argparse.ArgumentParser(description="执行数据库迁移")
    parser.add_argument("--url", default=None, help="目标数据库连接串（默认取 DATABASE_URL）")
    parser.add_argument("--reset", action="store_true", help="先清空 public schema（危险）")
    args = parser.parse_args()
    asyncio.run(run_migrations(args.url, reset=args.reset))


if __name__ == "__main__":
    main()
