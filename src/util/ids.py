"""ID 与指纹工具。

ID 在应用层生成：证据落盘的文件名需要用 ID，幂等键也要在 INSERT 前就确定。
指纹是内容指纹（sha256），用于去重和追溯。
"""

from __future__ import annotations

import hashlib
import uuid


def new_id() -> str:
    """生成 UUID v4（字符串形式，与 PostgreSQL uuid 类型互转）。"""
    return str(uuid.uuid4())


def sha256_bytes(data: bytes) -> str:
    """字节内容的 sha256 十六进制摘要。"""
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """文本内容的 sha256（UTF-8 编码）。"""
    return sha256_bytes(text.encode("utf-8"))


def short_hash(text: str, *, length: int = 16) -> str:
    """短指纹，用于日志 / 幂等键前缀。"""
    return sha256_text(text)[:length]
