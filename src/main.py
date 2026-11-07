"""FastAPI 应用入口：lifespan 里探活数据库，注册健康检查与统一错误映射。

数据库不可用时只警告，不阻止启动。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import settings
from .db.client import close_db, get_db
from .errors import AppError

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db = get_db()
    ok = await db.healthcheck()
    if ok:
        log.info("数据库连接正常")
    else:
        log.warning("数据库暂时不可用；服务仍会启动，接口会返回 db=error")
    yield
    await close_db()


app = FastAPI(
    title="Relationship Agent",
    version="0.1.0",
    description="人际关系助手 Agent（自研 tool loop · 提案-确认-回执三态 · 分层记忆）",
    lifespan=lifespan,
)


@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    """把 `AppError` 映射成统一 JSON 错误响应，状态码取自异常类属性。"""
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


@app.get("/api/health", tags=["system"])
async def health() -> dict[str, object]:
    """健康检查：数据库探活 + 版本号。"""
    db_ok = await get_db().healthcheck()
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "version": "p0",
    }


def run() -> None:
    """`python -m src.main` 启动开发服务器。"""
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
