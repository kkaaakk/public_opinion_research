# 把 MySQL 原始记忆转换为可检索文档，保留来源 URI 和身份范围。
"""RAG document conversion for MySQL-backed memory records."""

from typing import Any, Optional, Sequence

from open_deep_research.memory.store import MySQLChatMemoryStore
from open_deep_research.memory.types import (
    INDEXABLE_MEMORY_TYPES,
    ChatMemoryRecord,
    normalize_record_types,
)
from open_deep_research.rag.types import RAGDocument


# 把原始记忆模型转换为带引用与类型元数据的索引文档。
def records_to_documents(
    records: Sequence[ChatMemoryRecord],
    index_record_types: Sequence[str] | None = INDEXABLE_MEMORY_TYPES,
) -> list[RAGDocument]:
    """Convert raw MySQL memory rows into indexable RAG documents."""
    allowed_record_types = set(
        normalize_record_types(index_record_types, default=INDEXABLE_MEMORY_TYPES)
    )
    documents = []
    for record in records:
        if record.record_type not in allowed_record_types:
            continue
        metadata = {
            **record.metadata,
            "source_type": "memory",
            "memory_backend": "mysql",
            "memory_id": record.memory_id,
            "memory_type": record.record_type,
            "conversation_id": record.conversation_id,
            "user_id": record.user_id,
            "index_status": record.index_status,
            "created_at": record.created_at.isoformat(),
        }
        if record.record_type == "deprecated":
            metadata["memory_usage"] = "do_not_adopt"
        documents.append(
            RAGDocument(
                content=record.content,
                source=f"memory://mysql/{record.conversation_id}/{record.memory_id}",
                title=record.title or record.record_type,
                metadata=metadata,
            )
        )
    return documents


# 按配置读取 MySQL 记忆，并转换为标准 RAG 文档。
def load_memory_documents_from_mysql(
    *,
    database_url: str,
    table_name: str,
    conversation_id: Optional[str] = None,
    limit: int = 1000,
    record_types: Sequence[str] | None = INDEXABLE_MEMORY_TYPES,
    user_id: Optional[str] = None,
) -> list[RAGDocument]:
    """Load MySQL memory rows and convert them to indexable RAG documents."""
    store = MySQLChatMemoryStore(database_url=database_url, table_name=table_name)
    return records_to_documents(
        store.load_records(
            conversation_id=conversation_id,
            user_id=user_id,
            limit=limit,
            record_types=record_types,
        ),
        index_record_types=record_types,
    )


# 读取轻量数据库指纹，供索引刷新判断使用。
def fingerprint_mysql_memory(
    *,
    database_url: Optional[str],
    table_name: str,
    conversation_id: Optional[str] = None,
    record_types: Sequence[str] | None = INDEXABLE_MEMORY_TYPES,
    user_id: Optional[str] = None,
) -> dict[str, Any]:
    """Read a lightweight MySQL memory fingerprint for RAG refresh decisions."""
    if not database_url:
        return {"enabled": False}
    try:
        store = MySQLChatMemoryStore(database_url=database_url, table_name=table_name)
        return {
            "enabled": True,
            **store.fingerprint(
                conversation_id=conversation_id,
                user_id=user_id,
                record_types=record_types,
            ),
        }
    except Exception as exc:  # pragma: no cover - depends on external MySQL
        return {
            "enabled": True,
            "table": table_name,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "record_types": normalize_record_types(
                record_types,
                default=INDEXABLE_MEMORY_TYPES,
            ),
            "unavailable": str(exc),
        }


__all__ = [
    "fingerprint_mysql_memory",
    "load_memory_documents_from_mysql",
    "records_to_documents",
]
