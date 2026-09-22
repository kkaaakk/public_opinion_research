"""MySQL-backed raw memory storage.

MySQL stores the complete chat transcript, summaries, and structured long-term
memories. The vector store keeps only indexable copies of summary/fact-like
records, and uses `memory_id` / citation source to fetch the full row later.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional, Sequence

from open_deep_research.memory.types import (
    INDEXABLE_MEMORY_TYPES,
    ChatMemoryRecord,
    normalize_record_types,
)

if TYPE_CHECKING:
    from open_deep_research.rag.types import RAGDocument

TABLE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class MySQLChatMemoryStore:
    """Store and load raw memory records from MySQL."""

    def __init__(self, database_url: str, table_name: str = "rag_chat_memories"):
        if not database_url:
            raise ValueError("rag_memory_mysql_url is required.")
        self.database_url = normalize_mysql_url(database_url)
        self.table_name = validate_table_name(table_name)
        self._engine = None

    @property
    def engine(self):
        """Lazily create the SQLAlchemy engine only when MySQL memory is enabled."""
        if self._engine is None:
            try:
                from sqlalchemy import create_engine
            except ImportError as exc:  # pragma: no cover - dependency guard
                raise ImportError("Install sqlalchemy to use MySQL chat memory.") from exc
            self._engine = create_engine(self.database_url, pool_pre_ping=True, future=True)
        return self._engine

    def ensure_schema(self) -> None:
        """Create or lightly migrate the raw memory table."""
        from sqlalchemy import text

        table = quote_identifier(self.table_name)
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            memory_id VARCHAR(96) NOT NULL,
            user_id VARCHAR(255) NULL,
            conversation_id VARCHAR(255) NOT NULL,
            record_type VARCHAR(32) NOT NULL,
            index_status VARCHAR(32) NOT NULL DEFAULT 'pending',
            title VARCHAR(512) NULL,
            content LONGTEXT NOT NULL,
            metadata_json JSON NULL,
            created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
            PRIMARY KEY (id),
            UNIQUE KEY uq_memory_id (memory_id),
            KEY idx_user_id (user_id),
            KEY idx_conversation_id (conversation_id),
            KEY idx_record_type (record_type),
            KEY idx_index_status (index_status),
            KEY idx_updated_at (updated_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
        with self.engine.begin() as connection:
            connection.execute(text(ddl))
            self._best_effort_migrate_schema(connection)

    def upsert_records(self, records: Sequence[ChatMemoryRecord]) -> int:
        """Write raw records to MySQL and leave them pending for the indexer."""
        if not records:
            return 0

        from sqlalchemy import text

        self.ensure_schema()
        table = quote_identifier(self.table_name)
        statement = text(
            f"""
            INSERT INTO {table}
                (memory_id, user_id, conversation_id, record_type, index_status,
                 title, content, metadata_json, created_at)
            VALUES
                (:memory_id, :user_id, :conversation_id, :record_type, :index_status,
                 :title, :content, :metadata_json, :created_at)
            ON DUPLICATE KEY UPDATE
                user_id = VALUES(user_id),
                conversation_id = VALUES(conversation_id),
                record_type = VALUES(record_type),
                index_status = VALUES(index_status),
                title = VALUES(title),
                content = VALUES(content),
                metadata_json = VALUES(metadata_json),
                updated_at = CURRENT_TIMESTAMP(6)
            """
        )
        payload = [
            {
                "memory_id": record.memory_id,
                "user_id": record.user_id,
                "conversation_id": record.conversation_id,
                "record_type": record.record_type,
                "index_status": record.index_status or "pending",
                "title": record.title,
                "content": record.content,
                "metadata_json": json.dumps(record.metadata, ensure_ascii=False, default=str),
                "created_at": record.created_at.replace(tzinfo=None),
            }
            for record in records
        ]
        with self.engine.begin() as connection:
            connection.execute(statement, payload)
        return len(records)

    def load_records(
        self,
        conversation_id: Optional[str] = None,
        limit: int = 1000,
        record_types: Sequence[str] | None = None,
        user_id: Optional[str] = None,
        index_status: Optional[str] = None,
    ) -> list[ChatMemoryRecord]:
        """Load raw memory rows, optionally scoped by user/conversation/type/status."""
        from sqlalchemy import text

        self.ensure_schema()
        table = quote_identifier(self.table_name)
        indexed_record_types = normalize_record_types(record_types, default=None)
        conditions = []
        if user_id:
            conditions.append("user_id = :user_id")
        if conversation_id:
            conditions.append("conversation_id = :conversation_id")
        if index_status:
            conditions.append("index_status = :index_status")
        if indexed_record_types:
            type_placeholders = ", ".join(
                f":record_type_{index}" for index, _ in enumerate(indexed_record_types)
            )
            conditions.append(f"record_type IN ({type_placeholders})")
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        statement = text(
            f"""
            SELECT memory_id, user_id, conversation_id, record_type, index_status,
                   title, content, metadata_json, created_at
            FROM {table}
            {where_clause}
            ORDER BY updated_at DESC, id DESC
            LIMIT :limit
            """
        )
        params: dict[str, Any] = {"limit": max(1, limit)}
        if user_id:
            params["user_id"] = user_id
        if conversation_id:
            params["conversation_id"] = conversation_id
        if index_status:
            params["index_status"] = index_status
        for index, record_type in enumerate(indexed_record_types):
            params[f"record_type_{index}"] = record_type

        with self.engine.begin() as connection:
            rows = connection.execute(statement, params).mappings().all()

        return [memory_record_from_row(row) for row in rows]

    def load_pending_indexable_records(
        self,
        conversation_id: Optional[str] = None,
        limit: int = 1000,
        user_id: Optional[str] = None,
        record_types: Sequence[str] | None = INDEXABLE_MEMORY_TYPES,
    ) -> list[ChatMemoryRecord]:
        """Load indexable records still waiting to be synced to the vector store."""
        return self.load_records(
            conversation_id=conversation_id,
            user_id=user_id,
            limit=limit,
            record_types=record_types,
            index_status="pending",
        )

    def mark_records_indexed(self, memory_ids: Sequence[str]) -> int:
        """Mark records as indexed after the RAG indexer syncs them."""
        if not memory_ids:
            return 0

        from sqlalchemy import text

        self.ensure_schema()
        table = quote_identifier(self.table_name)
        placeholders = ", ".join(f":memory_id_{index}" for index, _ in enumerate(memory_ids))
        statement = text(
            f"""
            UPDATE {table}
            SET index_status = 'indexed', updated_at = CURRENT_TIMESTAMP(6)
            WHERE memory_id IN ({placeholders})
            """
        )
        params = {f"memory_id_{index}": memory_id for index, memory_id in enumerate(memory_ids)}
        with self.engine.begin() as connection:
            result = connection.execute(statement, params)
        return int(result.rowcount or 0)

    def get_record_by_memory_id(
        self,
        memory_id: str,
        conversation_id: Optional[str] = None,
    ) -> ChatMemoryRecord | None:
        """Load one complete raw memory record by its stable memory id."""
        from sqlalchemy import text

        if not memory_id:
            return None

        self.ensure_schema()
        table = quote_identifier(self.table_name)
        conditions = ["memory_id = :memory_id"]
        params = {"memory_id": memory_id}
        if conversation_id:
            conditions.append("conversation_id = :conversation_id")
            params["conversation_id"] = conversation_id
        statement = text(
            f"""
            SELECT memory_id, user_id, conversation_id, record_type, index_status,
                   title, content, metadata_json, created_at
            FROM {table}
            WHERE {' AND '.join(conditions)}
            LIMIT 1
            """
        )
        with self.engine.begin() as connection:
            row = connection.execute(statement, params).mappings().first()
        if row is None:
            return None
        return memory_record_from_row(row)

    def get_record_by_source(self, source: str) -> ChatMemoryRecord | None:
        """Load one raw memory row from a `memory://mysql/...` citation source."""
        parsed_source = parse_mysql_memory_source(source)
        if parsed_source is None:
            return None
        conversation_id, memory_id = parsed_source
        return self.get_record_by_memory_id(
            memory_id=memory_id,
            conversation_id=conversation_id,
        )

    def fingerprint(
        self,
        conversation_id: Optional[str] = None,
        record_types: Sequence[str] | None = None,
        user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Return a lightweight table fingerprint for diagnostics."""
        from sqlalchemy import text

        self.ensure_schema()
        table = quote_identifier(self.table_name)
        indexed_record_types = normalize_record_types(record_types, default=None)
        conditions = []
        if user_id:
            conditions.append("user_id = :user_id")
        if conversation_id:
            conditions.append("conversation_id = :conversation_id")
        if indexed_record_types:
            type_placeholders = ", ".join(
                f":record_type_{index}" for index, _ in enumerate(indexed_record_types)
            )
            conditions.append(f"record_type IN ({type_placeholders})")
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        statement = text(
            f"""
            SELECT COUNT(*) AS row_count, MAX(updated_at) AS max_updated_at
            FROM {table}
            {where_clause}
            """
        )
        params: dict[str, Any] = {}
        if user_id:
            params["user_id"] = user_id
        if conversation_id:
            params["conversation_id"] = conversation_id
        for index, record_type in enumerate(indexed_record_types):
            params[f"record_type_{index}"] = record_type
        with self.engine.begin() as connection:
            row = connection.execute(statement, params).mappings().one()
        return {
            "row_count": int(row["row_count"] or 0),
            "max_updated_at": str(row["max_updated_at"]) if row["max_updated_at"] else None,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "record_types": indexed_record_types,
            "table": self.table_name,
        }

    def _best_effort_migrate_schema(self, connection: Any) -> None:
        """Add columns expected by the new boundary split when tables already exist."""
        from sqlalchemy import text

        table = quote_identifier(self.table_name)
        migrations = [
            f"ALTER TABLE {table} ADD COLUMN user_id VARCHAR(255) NULL AFTER memory_id",
            (
                f"ALTER TABLE {table} ADD COLUMN index_status VARCHAR(32) "
                "NOT NULL DEFAULT 'pending' AFTER record_type"
            ),
            f"ALTER TABLE {table} ADD INDEX idx_user_id (user_id)",
            f"ALTER TABLE {table} ADD INDEX idx_index_status (index_status)",
        ]
        for statement in migrations:
            try:
                connection.execute(text(statement))
            except Exception:
                pass


def records_to_documents(
    records: Sequence[ChatMemoryRecord],
    index_record_types: Sequence[str] | None = INDEXABLE_MEMORY_TYPES,
) -> list["RAGDocument"]:
    """Compatibility wrapper for the RAG-layer MySQL memory converter."""
    from open_deep_research.rag.loaders.mysql_memory import (
        records_to_documents as _records_to_documents,
    )

    return _records_to_documents(
        records,
        index_record_types=index_record_types,
    )


def load_memory_documents_from_mysql(
    *,
    database_url: str,
    table_name: str,
    conversation_id: Optional[str] = None,
    limit: int = 1000,
    record_types: Sequence[str] | None = INDEXABLE_MEMORY_TYPES,
    user_id: Optional[str] = None,
) -> list["RAGDocument"]:
    """Compatibility wrapper for the RAG-layer MySQL memory loader."""
    from open_deep_research.rag.loaders.mysql_memory import (
        load_memory_documents_from_mysql as _load_memory_documents_from_mysql,
    )

    return _load_memory_documents_from_mysql(
        database_url=database_url,
        table_name=table_name,
        conversation_id=conversation_id,
        limit=limit,
        record_types=record_types,
        user_id=user_id,
    )


def fingerprint_mysql_memory(
    *,
    database_url: Optional[str],
    table_name: str,
    conversation_id: Optional[str] = None,
    record_types: Sequence[str] | None = INDEXABLE_MEMORY_TYPES,
    user_id: Optional[str] = None,
) -> dict[str, Any]:
    """Compatibility wrapper for the RAG-layer MySQL memory fingerprint."""
    from open_deep_research.rag.loaders.mysql_memory import (
        fingerprint_mysql_memory as _fingerprint_mysql_memory,
    )

    return _fingerprint_mysql_memory(
        database_url=database_url,
        table_name=table_name,
        conversation_id=conversation_id,
        record_types=record_types,
        user_id=user_id,
    )


def load_memory_record_by_id(
    *,
    database_url: str,
    table_name: str,
    memory_id: str,
    conversation_id: Optional[str] = None,
) -> ChatMemoryRecord | None:
    """Load the complete raw MySQL memory row by memory id."""
    store = MySQLChatMemoryStore(database_url=database_url, table_name=table_name)
    return store.get_record_by_memory_id(
        memory_id=memory_id,
        conversation_id=conversation_id,
    )


def load_memory_record_by_source(
    *,
    database_url: str,
    table_name: str,
    source: str,
) -> ChatMemoryRecord | None:
    """Load the complete raw MySQL memory row from a citation/vector chunk source."""
    store = MySQLChatMemoryStore(database_url=database_url, table_name=table_name)
    return store.get_record_by_source(source)


def load_memory_record_by_source_id(
    *,
    database_url: str,
    table_name: str,
    source_id: str,
) -> ChatMemoryRecord | None:
    """Alias for callers that name the citation source field `source_id`."""
    return load_memory_record_by_source(
        database_url=database_url,
        table_name=table_name,
        source=source_id,
    )


def parse_mysql_memory_source(source: str) -> tuple[str, str] | None:
    """Parse `memory://mysql/{conversation_id}/{memory_id}` into lookup keys."""
    prefix = "memory://mysql/"
    if not source or not source.startswith(prefix):
        return None
    payload = source[len(prefix) :].strip("/")
    if "/" not in payload:
        return None
    conversation_id, memory_id = payload.rsplit("/", 1)
    if not conversation_id or not memory_id:
        return None
    return conversation_id, memory_id


def normalize_mysql_url(database_url: str) -> str:
    """Fill in the default SQLAlchemy MySQL driver prefix."""
    if database_url.startswith("mysql://"):
        return "mysql+pymysql://" + database_url[len("mysql://") :]
    return database_url


def validate_table_name(table_name: str) -> str:
    """Validate a MySQL identifier that will be used in DDL strings."""
    if not TABLE_NAME_PATTERN.match(table_name or ""):
        raise ValueError(
            "MySQL memory table name must contain only letters, numbers, and underscores, "
            "start with a letter or underscore, and be at most 64 characters."
        )
    return table_name


def quote_identifier(identifier: str) -> str:
    """Quote an already validated MySQL identifier."""
    return f"`{validate_table_name(identifier)}`"


def parse_metadata(value: Any) -> dict[str, Any]:
    """Handle MySQL JSON values returned as dicts, strings, bytes, or null."""
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def memory_record_from_row(row: Any) -> ChatMemoryRecord:
    """Convert a SQLAlchemy mapping row into a `ChatMemoryRecord`."""
    return ChatMemoryRecord(
        memory_id=row["memory_id"],
        user_id=_row_get(row, "user_id"),
        conversation_id=row["conversation_id"],
        record_type=row["record_type"],
        index_status=_row_get(row, "index_status", "indexed") or "indexed",
        title=row["title"],
        content=row["content"],
        metadata=parse_metadata(row["metadata_json"]),
        created_at=row["created_at"] or datetime.now(timezone.utc).replace(tzinfo=None),
    )


def stable_memory_id(
    conversation_id: str,
    record_type: str,
    content: str,
    user_id: Optional[str] = None,
) -> str:
    """Generate a deterministic memory id so repeated writes upsert the same row."""
    digest = hashlib.sha256(
        f"{user_id or ''}\n{conversation_id}\n{record_type}\n{content}".encode("utf-8")
    ).hexdigest()
    return f"{record_type}-{digest[:24]}"


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        return default
