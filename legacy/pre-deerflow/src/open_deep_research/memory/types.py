# 定义原始记忆记录及可索引类型，并兼容历史类型名称。
"""Shared memory data types.

Memory owns the raw facts that may be useful later. RAG only receives the
indexable subset as documents.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Sequence

from pydantic import BaseModel, Field, field_validator


# 枚举系统支持的标准记忆类别。
class MemoryType(str, Enum):
    """Canonical memory record types stored in MySQL."""

    CHAT_RAW = "chat_raw"
    SUMMARY = "summary"
    PREFERENCE = "preference"
    PROJECT_FACT = "project_fact"
    DECISION = "decision"
    CONSTRAINT = "constraint"
    DEPRECATED = "deprecated"


INDEXABLE_MEMORY_TYPES = (
    MemoryType.SUMMARY.value,
    MemoryType.PREFERENCE.value,
    MemoryType.PROJECT_FACT.value,
    MemoryType.DECISION.value,
    MemoryType.CONSTRAINT.value,
    MemoryType.DEPRECATED.value,
)
NON_INDEXABLE_MEMORY_TYPES = (MemoryType.CHAT_RAW.value,)
ALLOWED_MEMORY_TYPES = NON_INDEXABLE_MEMORY_TYPES + INDEXABLE_MEMORY_TYPES

LEGACY_MEMORY_TYPE_ALIASES = {
    "chat": MemoryType.CHAT_RAW.value,
    "memory": MemoryType.PROJECT_FACT.value,
}


# 表示 MySQL 中一条原始记忆；索引状态由索引流程维护。
class ChatMemoryRecord(BaseModel):
    """One raw memory row saved in MySQL.

    `index_status` is owned by the indexing workflow. New records are written as
    `pending`; the RAG indexer can later mark indexable records as `indexed`.
    """

    memory_id: str
    conversation_id: str
    record_type: str
    content: str
    title: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    user_id: Optional[str] = None
    index_status: str = "pending"

    # 在模型校验时将历史记录类型归一化为标准名称。
    @field_validator("record_type", mode="before")
    @classmethod
    def normalize_record_type_field(cls, value: Any) -> str:
        if isinstance(value, MemoryType):
            return value.value
        return normalize_memory_type(str(value))


# 统一单个记忆类型，并接受旧名称作为兼容输入。
def normalize_memory_type(value: str) -> str:
    """Normalize a single memory type, accepting old names for compatibility."""
    record_type = str(value or "").strip().lower()
    record_type = LEGACY_MEMORY_TYPE_ALIASES.get(record_type, record_type)
    if record_type not in ALLOWED_MEMORY_TYPES:
        allowed = ", ".join(ALLOWED_MEMORY_TYPES)
        raise ValueError(f"Memory record type must be one of: {allowed}.")
    return record_type


# 归一化并去重查询或索引使用的记忆类型列表。
def normalize_record_types(
    record_types: Sequence[str] | None,
    default: Sequence[str] | None = INDEXABLE_MEMORY_TYPES,
) -> list[str]:
    """Normalize and deduplicate memory types used for indexing or loading."""
    values = list(default or []) if record_types is None else list(record_types)
    normalized: list[str] = []
    for value in values:
        if value is None:
            continue
        record_type = normalize_memory_type(str(value))
        if record_type not in normalized:
            normalized.append(record_type)
    return normalized


# 判断该类记忆是否允许生成向量索引副本。
def is_indexable_memory_type(record_type: str) -> bool:
    """Return whether this memory type should have a vector-index copy."""
    return normalize_memory_type(record_type) in INDEXABLE_MEMORY_TYPES
