"""Build structured memory records from conversation artifacts.

This module decides what should be stored. It does not write MySQL and does not
touch the vector index.
"""

from typing import Any, Mapping, Optional, Sequence

from open_deep_research.memory.store import stable_memory_id
from open_deep_research.memory.types import (
    ChatMemoryRecord,
    MemoryType,
    normalize_memory_type,
)


def build_chat_memory_records(
    *,
    conversation_id: str,
    chat_content: str,
    summary: str,
    memories: Sequence[str | Mapping[str, Any]] | None = None,
    metadata: Optional[dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> list[ChatMemoryRecord]:
    """Convert transcript, summary, and extracted facts into MySQL memory rows.

    Plain string `memories` are treated as `project_fact` for compatibility with
    the earlier generic `memory` type.
    """
    base_metadata = dict(metadata or {})
    candidates: list[tuple[str, str, str]] = [
        (MemoryType.CHAT_RAW.value, "Chat Transcript", chat_content),
        (MemoryType.SUMMARY.value, "Conversation Summary", summary),
    ]
    for index, memory in enumerate(memories or [], start=1):
        record_type, title, content = _normalize_memory_item(memory, index)
        candidates.append((record_type, title, content))

    records = []
    for record_type, title, content in candidates:
        cleaned = (content or "").strip()
        if not cleaned:
            continue
        memory_id = stable_memory_id(
            conversation_id=conversation_id,
            record_type=record_type,
            content=cleaned,
            user_id=user_id,
        )
        metadata_payload = {
            **base_metadata,
            "source_type": "memory",
            "memory_type": record_type,
            "memory_backend": "mysql",
        }
        if record_type == MemoryType.DEPRECATED.value:
            metadata_payload["memory_usage"] = "do_not_adopt"
        records.append(
            ChatMemoryRecord(
                memory_id=memory_id,
                user_id=user_id,
                conversation_id=conversation_id,
                record_type=record_type,
                title=title,
                content=cleaned,
                metadata=metadata_payload,
                index_status="pending",
            )
        )
    return records


def extract_conversation_memories(
    *,
    conversation_id: str,
    chat_content: str,
    summary: str,
    memories: Sequence[str | Mapping[str, Any]] | None = None,
    metadata: Optional[dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> list[ChatMemoryRecord]:
    """Extract conversation memory records for final report generation."""
    return build_chat_memory_records(
        conversation_id=conversation_id,
        user_id=user_id,
        chat_content=chat_content,
        summary=summary,
        memories=memories,
        metadata=metadata,
    )


def _normalize_memory_item(
    memory: str | Mapping[str, Any],
    index: int,
) -> tuple[str, str, str]:
    if isinstance(memory, Mapping):
        raw_type = (
            memory.get("record_type")
            or memory.get("memory_type")
            or memory.get("type")
            or MemoryType.PROJECT_FACT.value
        )
        record_type = normalize_memory_type(str(raw_type))
        content = str(memory.get("content") or memory.get("text") or memory.get("memory") or "")
        title = str(memory.get("title") or _default_title(record_type, index))
        return record_type, title, content
    return (
        MemoryType.PROJECT_FACT.value,
        _default_title(MemoryType.PROJECT_FACT.value, index),
        str(memory),
    )


def _default_title(record_type: str, index: int) -> str:
    titles = {
        MemoryType.PREFERENCE.value: "User Preference",
        MemoryType.PROJECT_FACT.value: "Project Fact",
        MemoryType.DECISION.value: "Decision",
        MemoryType.CONSTRAINT.value: "Constraint",
        MemoryType.DEPRECATED.value: "Deprecated Memory",
        MemoryType.SUMMARY.value: "Conversation Summary",
        MemoryType.CHAT_RAW.value: "Chat Transcript",
    }
    return f"{titles.get(record_type, 'Memory')} {index}"
