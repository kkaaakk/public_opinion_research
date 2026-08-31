"""Conversation memory write workflow.

This path owns raw MySQL writes. When configured, it also triggers a background
index refresh after the write; query-time `RAGIndexer.ensure_ready()` still
remains the fallback if that background refresh has not completed.
"""

import logging
import threading
from typing import Any, Mapping, Sequence

from langchain_core.runnables import RunnableConfig

from open_deep_research.configuration import Configuration
from open_deep_research.memory.context import get_conversation_id, get_user_id
from open_deep_research.memory.extractor import extract_conversation_memories
from open_deep_research.memory.store import MySQLChatMemoryStore

LOGGER = logging.getLogger(__name__)


def persist_conversation_memory(
    *,
    configurable: Configuration,
    runtime_config: RunnableConfig,
    chat_content: str,
    summary: str,
    memories: Sequence[str | Mapping[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Persist chat memory to MySQL and mark rows as pending for indexing."""
    if not configurable.rag_memory_write_enabled:
        return 0
    if not configurable.rag_memory_mysql_url:
        raise ValueError("rag_memory_mysql_url is required when memory writing is enabled.")

    conversation_id = get_conversation_id(runtime_config)
    user_id = get_user_id(runtime_config)
    records = extract_conversation_memories(
        conversation_id=conversation_id,
        user_id=user_id,
        chat_content=chat_content,
        summary=summary,
        memories=memories,
        metadata=metadata,
    )
    if not records:
        return 0

    store = MySQLChatMemoryStore(
        database_url=configurable.rag_memory_mysql_url,
        table_name=configurable.rag_memory_mysql_table,
    )
    written_count = store.upsert_records(records)
    if written_count and configurable.rag_memory_write_sync_index:
        trigger_memory_index_refresh(
            configurable=configurable,
            conversation_id=conversation_id,
            user_id=user_id,
        )
    return written_count


def trigger_memory_index_refresh(
    *,
    configurable: Configuration,
    conversation_id: str,
    user_id: str | None = None,
) -> None:
    """Start a non-blocking refresh that syncs pending memory into the vector store."""
    thread = threading.Thread(
        target=_refresh_memory_index,
        kwargs={
            "configurable": configurable,
            "conversation_id": conversation_id,
            "user_id": user_id,
        },
        name="rag-memory-index-refresh",
        daemon=True,
    )
    thread.start()


def _refresh_memory_index(
    *,
    configurable: Configuration,
    conversation_id: str,
    user_id: str | None = None,
) -> None:
    try:
        from open_deep_research.rag.service import (
            build_rag_pipeline_config,
            get_or_create_rag_pipeline,
        )

        pipeline = get_or_create_rag_pipeline(
            build_rag_pipeline_config(
                configurable,
                memory_enabled=True,
                memory_conversation_id=conversation_id,
                memory_user_id=user_id,
            )
        )
        pipeline.index_pending_memories()
    except Exception:  # pragma: no cover - depends on external DB/vector DB
        LOGGER.exception("Failed to refresh memory RAG index; continuing in background.")
