# 读取文件中的对话记忆记录，并转换为带会话范围的 RAG 文档。
"""聊天记忆 RAG 数据源加载器。

这个模块把“用户长期记忆 / 聊天记录摘要”转换成普通的 `RAGDocument`，
让记忆可以和本地文档共用同一套 RAG 流程：

```text
memory record
  -> RAGDocument
  -> RAGChunk
  -> embedding
  -> vectorstore + BM25
  -> rerank
  -> citation context
```

推荐存入 RAG 的不是完整聊天流水，而是抽取后的长期记忆，例如用户偏好、
项目约定、历史决策、常用背景。原始消息也支持加载，但应谨慎使用，避免把
临时噪声和误解写进长期知识库。
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Iterable, Sequence

from open_deep_research.rag.types import RAGDocument

LOGGER = logging.getLogger(__name__)
SUPPORTED_MEMORY_EXTENSIONS = {".json", ".jsonl"}
TEXT_KEYS = ("content", "text", "summary", "memory", "value", "note")
TITLE_KEYS = ("title", "name", "subject", "summary")


def load_memory_documents_from_paths(
    memory_paths: Sequence[str],
    json_text_fields: Sequence[str] | None = None,
) -> list[RAGDocument]:
    """从记忆文件加载 `RAGDocument`。

    支持：

    - `.jsonl`：每行一个记忆 record。
    - `.json`：单个 record、record 数组，或带 `memories/messages/items`
      字段的对象。

    每条 record 会转换成一个 `RAGDocument`，source 使用 `memory://...`
    协议，方便在最终引用里和本地文件区分开。
    """
    loaded_documents: list[RAGDocument] = []
    for configured_path in memory_paths:
        path = Path(configured_path).expanduser()
        if not path.exists():
            LOGGER.warning("Skipping missing RAG memory path: %s", configured_path)
            continue
        for candidate_path in _iter_memory_paths(path):
            try:
                loaded_documents.extend(
                    _load_memory_file(
                        candidate_path,
                        json_text_fields=json_text_fields,
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive logging path
                LOGGER.warning("Failed to load RAG memory file %s: %s", candidate_path, exc)
    return loaded_documents


def fingerprint_memory_paths(memory_paths: Sequence[str] | None) -> dict[str, Any]:
    """为记忆文件生成 fingerprint，用于 RAG 索引失效判断。

    和普通文档一样，记忆文件的 path、mtime、size、sha256 都会进入 index id。
    只要记忆文件内容变化，RAG 会使用新的索引命名空间。
    """
    files = []
    for configured_path in memory_paths or []:
        path = Path(configured_path).expanduser()
        if not path.exists():
            files.append({"path": str(path), "missing": True})
            continue
        for candidate_path in _iter_memory_paths(path):
            stat = candidate_path.stat()
            files.append(
                {
                    "path": candidate_path.as_posix(),
                    "mtime_ns": stat.st_mtime_ns,
                    "size": stat.st_size,
                    "sha256": _file_sha256(candidate_path),
                }
            )
    return {"files": sorted(files, key=lambda item: item["path"])}


def _iter_memory_paths(path: Path) -> Iterable[Path]:
    """枚举记忆文件路径。

    记忆源目前只接受 JSON/JSONL，避免把任意聊天导出文本误当成长期记忆。
    如果确实要索引普通文本，可以继续通过 `rag_knowledge_base_paths` 加载。
    """
    if path.is_file():
        if path.suffix.lower() in SUPPORTED_MEMORY_EXTENSIONS:
            yield path.resolve()
        return

    for candidate in sorted(path.rglob("*")):
        if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_MEMORY_EXTENSIONS:
            yield candidate.resolve()


def _load_memory_file(
    path: Path,
    json_text_fields: Sequence[str] | None = None,
) -> list[RAGDocument]:
    """根据扩展名加载记忆文件。"""
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return _load_jsonl_memory(path, json_text_fields=json_text_fields)
    if suffix == ".json":
        return _load_json_memory(path, json_text_fields=json_text_fields)
    return []


def _load_jsonl_memory(
    path: Path,
    json_text_fields: Sequence[str] | None = None,
) -> list[RAGDocument]:
    """加载 JSONL 记忆文件，每一行是一个独立 record。"""
    documents = []
    for line_index, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        record = json.loads(stripped)
        document = _memory_record_to_document(
            record,
            path=path,
            item_index=line_index,
            json_text_fields=json_text_fields,
        )
        if document is not None:
            documents.append(document)
    return documents


def _load_json_memory(
    path: Path,
    json_text_fields: Sequence[str] | None = None,
) -> list[RAGDocument]:
    """加载 JSON 记忆文件。

    兼容几种常见形状：

    - `[{...}, {...}]`
    - `{"memories": [{...}]}`
    - `{"messages": [{...}]}`
    - `{"items": [{...}]}`
    - 单个 `{...}` record
    """
    raw_data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    records = _extract_records(raw_data)
    documents = []
    for item_index, record in enumerate(records):
        document = _memory_record_to_document(
            record,
            path=path,
            item_index=item_index,
            json_text_fields=json_text_fields,
        )
        if document is not None:
            documents.append(document)
    return documents


def _extract_records(raw_data: Any) -> list[Any]:
    """从常见 JSON 结构中抽出记忆 record 列表。"""
    if isinstance(raw_data, list):
        return raw_data
    if isinstance(raw_data, dict):
        for key in ("memories", "messages", "items", "records"):
            value = raw_data.get(key)
            if isinstance(value, list):
                return value
        return [raw_data]
    return []


def _memory_record_to_document(
    record: Any,
    path: Path,
    item_index: int,
    json_text_fields: Sequence[str] | None = None,
) -> RAGDocument | None:
    """把一条记忆 record 转成 `RAGDocument`。

    record 可以是字符串，也可以是对象。对象会优先提取指定字段、常见文本字段，
    或 `messages` 对话数组。
    """
    text, field_paths = _extract_memory_text(record, json_text_fields=json_text_fields)
    if not text:
        return None

    metadata = _memory_metadata(record)
    metadata.update(
        {
            "source_type": "memory",
            "extension": path.suffix.lower(),
            "path": path.as_posix(),
            "item_index": item_index,
            "field_paths": field_paths,
        }
    )
    title = _memory_title(record, fallback=f"{path.stem} memory {item_index + 1}")
    source = _memory_source(record, path=path, item_index=item_index)
    return RAGDocument(content=text, source=source, title=title, metadata=metadata)


def _extract_memory_text(
    record: Any,
    json_text_fields: Sequence[str] | None = None,
) -> tuple[str, list[str]]:
    """从记忆 record 中提取用于索引的正文和字段路径。"""
    if isinstance(record, str):
        return record.strip(), ["$"]

    if not isinstance(record, dict):
        return "", []

    fragments: list[tuple[str, str]] = []
    if json_text_fields:
        for field_path in json_text_fields:
            fragments.extend(
                _collect_text_fragments(
                    _extract_field_path(record, field_path),
                    prefix=field_path,
                )
            )
    else:
        for key in TEXT_KEYS:
            if key in record:
                fragments.extend(_collect_text_fragments(record.get(key), prefix=f"$.{key}"))
        if not fragments and isinstance(record.get("messages"), list):
            fragments.extend(_collect_message_fragments(record["messages"]))
        if not fragments:
            fragments.extend(_collect_text_fragments(record, prefix="$"))

    return _deduplicate_fragments(fragments)


def _collect_message_fragments(messages: list[Any]) -> list[tuple[str, str]]:
    """将聊天 messages 数组转成可索引文本片段。"""
    fragments = []
    for index, message in enumerate(messages):
        if isinstance(message, str):
            fragments.append((f"$.messages.{index}", message))
            continue
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "message")
        content = message.get("content") or message.get("text")
        if isinstance(content, str) and content.strip():
            fragments.append((f"$.messages.{index}.content", f"{role}: {content.strip()}"))
    return fragments


def _deduplicate_fragments(fragments: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """去掉空片段和重复片段，返回正文与字段路径。"""
    deduplicated_text = []
    field_paths = []
    seen = set()
    for field_path, fragment in fragments:
        normalized_fragment = str(fragment).strip()
        if not normalized_fragment or normalized_fragment in seen:
            continue
        seen.add(normalized_fragment)
        deduplicated_text.append(normalized_fragment)
        field_paths.append(field_path)
    return "\n\n".join(deduplicated_text), field_paths


def _memory_metadata(record: Any) -> dict[str, Any]:
    """提取记忆 record 中常见的结构化 metadata。"""
    if not isinstance(record, dict):
        return {"memory_type": "raw_text"}

    metadata = {}
    for key in (
        "memory_id",
        "conversation_id",
        "message_id",
        "role",
        "created_at",
        "updated_at",
        "memory_type",
        "type",
        "confidence",
    ):
        value = record.get(key)
        if value is not None:
            metadata[key] = value
    if "memory_type" not in metadata and "type" in metadata:
        metadata["memory_type"] = metadata["type"]
    return metadata


def _memory_title(record: Any, fallback: str) -> str:
    """从记忆 record 中推断 citation 标题。"""
    if isinstance(record, dict):
        for key in TITLE_KEYS:
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:120]
        memory_type = record.get("memory_type") or record.get("type")
        if memory_type:
            return f"Memory: {memory_type}"
    return fallback


def _memory_source(record: Any, path: Path, item_index: int) -> str:
    """生成 memory:// source，便于和本地文件引用区分。"""
    if isinstance(record, dict):
        conversation_id = record.get("conversation_id") or path.stem
        memory_id = record.get("memory_id") or record.get("message_id") or f"item-{item_index + 1}"
        return f"memory://{conversation_id}/{memory_id}"
    return f"memory://{path.stem}/item-{item_index + 1}"


def _extract_field_path(data: Any, field_path: str) -> Any:
    """解析 JSON dotted path。"""
    current_value = data
    for part in field_path.split("."):
        if isinstance(current_value, list):
            if part.isdigit():
                index = int(part)
                if 0 <= index < len(current_value):
                    current_value = current_value[index]
                    continue
            return None
        if not isinstance(current_value, dict):
            return None
        current_value = current_value.get(part)
        if current_value is None:
            return None
    return current_value


def _collect_text_fragments(value: Any, prefix: str = "$") -> list[tuple[str, str]]:
    """递归收集 JSON 标量文本，保留字段路径。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [(prefix, value)]
    if isinstance(value, (int, float, bool)):
        return [(prefix, str(value))]
    if isinstance(value, list):
        fragments = []
        for index, item in enumerate(value):
            fragments.extend(_collect_text_fragments(item, prefix=f"{prefix}.{index}"))
        return fragments
    if isinstance(value, dict):
        fragments = []
        for key, nested_value in value.items():
            fragments.extend(_collect_text_fragments(nested_value, prefix=f"{prefix}.{key}"))
        return fragments
    return []


def _file_sha256(path: Path) -> str:
    """流式计算文件 sha256。"""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
