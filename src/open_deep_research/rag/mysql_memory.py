# 兼容旧 MySQL 记忆导入路径，重新导出存储与加载接口。
"""Compatibility exports for MySQL-backed memory.

New code should import from `open_deep_research.memory.*`. This module remains
so older RAG callers and tests do not need to change immediately.
"""

from open_deep_research.memory.extractor import build_chat_memory_records
from open_deep_research.memory.store import (
    MySQLChatMemoryStore,
    load_memory_record_by_id,
    load_memory_record_by_source,
    load_memory_record_by_source_id,
    normalize_mysql_url,
    parse_mysql_memory_source,
    stable_memory_id,
    validate_table_name,
)
from open_deep_research.memory.types import (
    INDEXABLE_MEMORY_TYPES as INDEXABLE_MEMORY_RECORD_TYPES,
)
from open_deep_research.memory.types import (
    ChatMemoryRecord,
    normalize_record_types,
)
from open_deep_research.rag.loaders.mysql_memory import (
    fingerprint_mysql_memory,
    load_memory_documents_from_mysql,
    records_to_documents,
)

ALLOWED_MEMORY_RECORD_TYPES = set(INDEXABLE_MEMORY_RECORD_TYPES) | {
    "chat_raw",
    "chat",
    "memory",
}

__all__ = [
    "ALLOWED_MEMORY_RECORD_TYPES",
    "ChatMemoryRecord",
    "INDEXABLE_MEMORY_RECORD_TYPES",
    "MySQLChatMemoryStore",
    "build_chat_memory_records",
    "fingerprint_mysql_memory",
    "load_memory_documents_from_mysql",
    "load_memory_record_by_id",
    "load_memory_record_by_source",
    "load_memory_record_by_source_id",
    "normalize_mysql_url",
    "normalize_record_types",
    "parse_mysql_memory_source",
    "records_to_documents",
    "stable_memory_id",
    "validate_table_name",
]
