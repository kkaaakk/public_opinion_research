# 按 Markdown、JSON、代码和普通文本结构切块，并保留来源跨度与可信度元数据。
"""本地 RAG 文本切块工具.

loader 负责把文件变成 `RAGDocument`，而 splitter 负责把 document 切成
适合检索的 `RAGChunk`。切块时不仅要控制长度，还要保留引用所需的位置信息：

- 字符起止位置。
- 1-based 行号范围。
- Markdown 标题路径。
- 文档级 metadata，例如 PDF 页码、JSON 字段路径等。

这些 metadata 最终会出现在 `citations.py` 生成的工具结果里，帮助 LLM 和用户
确认答案依据。
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from langchain_text_splitters import (
    Language,
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

from open_deep_research.rag.code_languages import (
    language_enum_from_name,
    language_for_extension,
)
from open_deep_research.rag.types import RAGChunk, RAGDocument

_MARKDOWN_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3")]
_TEXT_SPLIT_SEPARATORS = [
    "\n\n",
    "\n",
    "。",
    "！",
    "？",
    "；",
    "、",
    ". ",
    "! ",
    "? ",
    "; ",
    ".",
    "!",
    "?",
    ";",
    "，",
    ",",
    " ",
    "",
]


@dataclass(frozen=True)
class TextSpan:
    """切块中间结构：文本片段及其在原文中的字符范围.

    splitter 先产生 `TextSpan`，再结合 document metadata 组装成 `RAGChunk`。
    这样可以把“如何切文本”和“如何构造 RAG 数据结构”分开。
    """

    content: str
    start: int
    end: int


# 保存转换为 RAGChunk 前的内部切块文本及元数据。
@dataclass(frozen=True)
class ChunkDocument:
    """Internal LangChain-like chunk representation used before `RAGChunk`."""

    page_content: str
    metadata: dict[str, Any]


# 表示一个 JSON 标量叶子及其路径、来源位置。
@dataclass(frozen=True)
class JSONLeaf:
    """One scalar JSON value with its JSON path and source span."""

    json_path: str
    parent_path: str
    label: str
    value: Any
    text: str
    start: int
    end: int


def split_documents(
    documents: list[RAGDocument],
    chunk_size: int,
    chunk_overlap: int,
) -> list[RAGChunk]:
    """将多个 `RAGDocument` 切成带稳定 id 和丰富 metadata 的 chunk.

    `chunk_size` 和 `chunk_overlap` 使用字符数计量。当前实现不依赖 tokenizer，
    因此对不同模型都保持轻量，但 token 数只近似受控。
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than 0.")
    if chunk_overlap < 0:
        raise ValueError("chunk_overlap must be greater than or equal to 0.")
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be smaller than chunk_size.")

    chunked_documents: list[RAGChunk] = []
    for document_index, document in enumerate(documents):
        document_key = _stable_document_key(document)
        file_type = _infer_file_type(document)
        chunk_documents = _split_document(
            document,
            file_type=file_type,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        for chunk_index, chunk_document in enumerate(chunk_documents):
            chunk_text = chunk_document.page_content.strip()
            if not chunk_text:
                continue

            metadata = {
                **document.metadata,
                **chunk_document.metadata,
                "source": document.source,
                "file_type": chunk_document.metadata.get("file_type", file_type),
                "document_index": document_index,
                "chunk_index": chunk_index,
            }
            char_start = _coerce_non_negative_int(metadata.get("char_start"), default=0)
            char_end = _coerce_non_negative_int(
                metadata.get("char_end"),
                default=char_start + len(chunk_text),
            )
            metadata["char_start"] = char_start
            metadata["char_end"] = char_end

            # 行号和标题路径都是 citation 友好的 metadata。它们不会参与检索分数，
            # 但会帮助最终报告解释“这段证据来自哪里”。
            line_start, line_end = _line_range(document.content, char_start, char_end)
            heading_path = _heading_path_from_metadata(metadata)
            if not heading_path and file_type != "json":
                heading_path = _heading_path_at(document.content, char_end)
            content_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
            authority_metadata = infer_authority_metadata(
                source=document.source,
                heading_path=heading_path,
                content=chunk_text,
                metadata=metadata,
            )
            chunked_documents.append(
                RAGChunk(
                    content=chunk_text,
                    source=document.source,
                    title=document.title,
                    chunk_id=f"{document_key}-{chunk_index + 1}-{content_hash[:12]}",
                    metadata={
                        **metadata,
                        "content_hash": content_hash,
                        "line_start": line_start,
                        "line_end": line_end,
                        "heading_path": heading_path,
                        **authority_metadata,
                    },
                )
            )
    return chunked_documents


# 按文档类型分派到对应切块策略。
def _split_document(
    document: RAGDocument,
    *,
    file_type: str,
    chunk_size: int,
    chunk_overlap: int,
) -> list[ChunkDocument]:
    if file_type == "markdown":
        return _split_markdown_document(
            document,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
    if file_type == "json":
        return _split_json_document(
            document,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
    if file_type == "code":
        return _split_code_document(
            document,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
    return _split_plain_text_document(
        document,
        file_type=file_type,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


# 按标题层级组织 Markdown，再将较长段落切块并保留标题路径。
def _split_markdown_document(
    document: RAGDocument,
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> list[ChunkDocument]:
    normalized_text = _normalize_text(document.content)
    if not normalized_text:
        return []

    markdown_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=_MARKDOWN_HEADERS,
        strip_headers=True,
    )
    sections = markdown_splitter.split_text(normalized_text)
    if not sections:
        return _split_plain_text_document(
            document,
            file_type="markdown",
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    chunk_documents: list[ChunkDocument] = []
    search_start = 0
    for section in sections:
        section_text = section.page_content.strip()
        if not section_text:
            continue
        section_start = normalized_text.find(section_text, search_start)
        if section_start == -1:
            section_start = normalized_text.find(section_text)
        if section_start == -1:
            section_start = search_start

        section_metadata = {
            key: value
            for key, value in section.metadata.items()
            if key in {"h1", "h2", "h3"} and value
        }
        for span in _split_text(
            section_text,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        ):
            chunk_documents.append(
                ChunkDocument(
                    page_content=span.content,
                    metadata={
                        "file_type": "markdown",
                        "char_start": section_start + span.start,
                        "char_end": section_start + span.end,
                        **section_metadata,
                    },
                )
            )
        search_start = section_start + len(section_text)

    return chunk_documents


# 按 JSON 叶子路径和字段组织切块，保留结构定位信息。
def _split_json_document(
    document: RAGDocument,
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> list[ChunkDocument]:
    normalized_text = _normalize_text(document.content)
    if not normalized_text:
        return []

    try:
        parsed_json = json.loads(normalized_text)
    except json.JSONDecodeError:
        return _split_plain_text_document(
            document,
            file_type="json",
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    json_text_fields = document.metadata.get("json_text_fields")
    allowed_paths = (
        {_normalize_json_filter_path(str(path)) for path in json_text_fields}
        if isinstance(json_text_fields, list)
        else set()
    )

    leaves: list[JSONLeaf] = []
    search_start = 0
    for json_path, value, parent_path, label in _iter_json_leaf_values(parsed_json):
        if allowed_paths and not _json_path_matches(json_path, allowed_paths):
            continue
        text = _json_leaf_to_text(value)
        if not text:
            continue
        value_start = _find_json_value_offset(
            normalized_text,
            value=value,
            text=text,
            search_start=search_start,
        )
        leaves.append(
            JSONLeaf(
                json_path=json_path,
                parent_path=parent_path,
                label=label,
                value=value,
                text=text,
                start=value_start,
                end=value_start + len(text),
            )
        )
        search_start = max(search_start, value_start + len(text))

    chunk_documents: list[ChunkDocument] = []
    grouped_leaves: dict[str, list[JSONLeaf]] = {}
    for leaf in leaves:
        grouped_leaves.setdefault(leaf.parent_path, []).append(leaf)
    for group_path, group_leaves in grouped_leaves.items():
        chunk_documents.extend(
            _split_json_group(
                group_path=group_path,
                leaves=group_leaves,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
        )
    return chunk_documents


# 对普通文本递归切块，并附加原文跨度元数据。
def _split_plain_text_document(
    document: RAGDocument,
    *,
    file_type: str,
    chunk_size: int,
    chunk_overlap: int,
) -> list[ChunkDocument]:
    return [
        ChunkDocument(
            page_content=span.content,
            metadata={
                "file_type": file_type,
                "char_start": span.start,
                "char_end": span.end,
            },
        )
        for span in _split_text(
            document.content,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
    ]


# 根据代码语言选择分隔规则，尽量保留代码结构边界。
def _split_code_document(
    document: RAGDocument,
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> list[ChunkDocument]:
    language = _infer_code_language(document)
    if language is None:
        return _split_plain_text_document(
            document,
            file_type="code",
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    return [
        ChunkDocument(
            page_content=span.content,
            metadata={
                "file_type": "code",
                "language": language.value,
                "char_start": span.start,
                "char_end": span.end,
            },
        )
        for span in _split_code_text(
            document.content,
            language=language,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
    ]


# 从来源及现有元数据推导可信度字段，供权威性重排使用。
def infer_authority_metadata(
    *,
    source: str,
    heading_path: list[str],
    content: str,
    metadata: dict,
) -> dict[str, float | str]:
    """Infer source trust metadata used by authority-aware reranking."""
    explicit_status = metadata.get("source_status")
    status = str(explicit_status).strip().lower() if explicit_status else ""
    if not status:
        status = _infer_source_status(
            source=source,
            heading_path=heading_path,
            content=content,
        )
    weight, penalty = _authority_values(status)
    return {
        "source_status": status,
        "authority_weight": weight,
        "authority_score_penalty": penalty,
    }


# 从文档信息推断来源状态，如正式、草稿或过期等标记。
def _infer_source_status(source: str, heading_path: list[str], content: str) -> str:
    normalized_source = source.replace("\\", "/").lower()
    normalized_heading = " ".join(heading_path).lower()
    normalized_content = content.lower()

    if "misleading_archive" in normalized_source:
        if (
            "negative misleading notes" in normalized_heading
            or "unanswerable_trap" in normalized_content
        ):
            return "unanswerable_trap"
        return "misleading"
    if any(marker in normalized_heading for marker in ("deprecated", "retired")):
        return "deprecated"
    if any(
        marker in normalized_content
        for marker in (
            "[deprecated]",
            "[retired]",
            "[do_not_use]",
            "[misleading]",
            "[false_claim]",
            "do not use this draft",
            "do not use this snippet",
        )
    ):
        return "deprecated"
    return "authoritative"


# 归一化权威性判断使用的候选元数据值。
def _authority_values(status: str) -> tuple[float, float]:
    if status == "authoritative":
        return 1.0, 0.0
    if status == "deprecated":
        return 0.35, -4.0
    if status == "misleading":
        return 0.1, -8.0
    if status == "unanswerable_trap":
        return 0.0, -10.0
    return 0.8, -1.0


# 使用递归字符分块器由粗到细选择边界，并保留来源位置。
def _split_text(text: str, chunk_size: int, chunk_overlap: int) -> list[TextSpan]:
    """Use LangChain's recursive character splitter and keep source spans.

    `RecursiveCharacterTextSplitter` still uses character length, but it
    recursively tries coarse-to-fine separators before falling back to raw
    character splits. We keep the resulting start offsets so citation metadata
    can still point back to the original document text.
    """
    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized_text:
        return []

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=len,
        separators=_TEXT_SPLIT_SEPARATORS,
        keep_separator="end",
        add_start_index=True,
        strip_whitespace=True,
    )
    return _split_with_langchain_splitter(
        normalized_text,
        text_splitter=text_splitter,
        chunk_overlap=chunk_overlap,
    )


# 使用语言感知的递归分块规则拆分代码文本。
def _split_code_text(
    text: str,
    *,
    language: Language,
    chunk_size: int,
    chunk_overlap: int,
) -> list[TextSpan]:
    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized_text:
        return []

    text_splitter = RecursiveCharacterTextSplitter.from_language(
        language=language,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=len,
        keep_separator="start",
        add_start_index=True,
        strip_whitespace=True,
    )
    return _split_with_langchain_splitter(
        normalized_text,
        text_splitter=text_splitter,
        chunk_overlap=chunk_overlap,
    )


# 统一执行 LangChain 分块并恢复每块在原文中的位置。
def _split_with_langchain_splitter(
    normalized_text: str,
    *,
    text_splitter: RecursiveCharacterTextSplitter,
    chunk_overlap: int,
) -> list[TextSpan]:
    langchain_documents = text_splitter.create_documents([normalized_text])

    spans: list[TextSpan] = []
    search_start = 0
    for langchain_document in langchain_documents:
        raw_content = langchain_document.page_content
        chunk_text = raw_content.strip()
        if not chunk_text:
            continue

        raw_start = langchain_document.metadata.get("start_index")
        start = raw_start if isinstance(raw_start, int) and raw_start >= 0 else None
        if start is None:
            start = normalized_text.find(chunk_text, search_start)
            if start == -1:
                start = search_start

        start += len(raw_content) - len(raw_content.lstrip())
        end = start + len(chunk_text)
        spans.append(TextSpan(content=chunk_text, start=start, end=end))
        search_start = max(start + 1, end - chunk_overlap)

    return spans


# 按 JSON 字段组构造分块，必要时继续细分长内容。
def _split_json_group(
    *,
    group_path: str,
    leaves: list[JSONLeaf],
    chunk_size: int,
    chunk_overlap: int,
) -> list[ChunkDocument]:
    long_leaves = [leaf for leaf in leaves if len(leaf.text) > chunk_size]
    if not long_leaves:
        return _split_json_combined_fields(
            group_path=group_path,
            leaves=leaves,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    context_leaves = [leaf for leaf in leaves if leaf not in long_leaves]
    chunk_documents: list[ChunkDocument] = []
    for long_leaf in long_leaves:
        chunk_documents.extend(
            _split_json_long_field(
                group_path=group_path,
                context_leaves=context_leaves,
                primary_leaf=long_leaf,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
        )
    return chunk_documents


# 将可以合并的 JSON 字段整理为同一块，控制总长度。
def _split_json_combined_fields(
    *,
    group_path: str,
    leaves: list[JSONLeaf],
    chunk_size: int,
    chunk_overlap: int,
) -> list[ChunkDocument]:
    combined_text = "\n".join(_json_field_line(leaf) for leaf in leaves)
    metadata = _json_group_metadata(group_path=group_path, leaves=leaves)
    if len(combined_text) <= chunk_size:
        return [ChunkDocument(page_content=combined_text, metadata=metadata)]

    return [
        ChunkDocument(page_content=span.content, metadata=metadata)
        for span in _split_text(
            combined_text,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
    ]


# 单独拆分超长 JSON 字段，并保留字段路径关联。
def _split_json_long_field(
    *,
    group_path: str,
    context_leaves: list[JSONLeaf],
    primary_leaf: JSONLeaf,
    chunk_size: int,
    chunk_overlap: int,
) -> list[ChunkDocument]:
    context_lines = [_json_field_line(leaf) for leaf in context_leaves]
    field_prefix = f"{primary_leaf.label}: "
    prefix_length = len("\n".join([*context_lines, field_prefix]))
    primary_chunk_size = max(1, chunk_size - prefix_length)
    primary_chunk_overlap = min(chunk_overlap, max(0, primary_chunk_size - 1))
    field_paths = [leaf.json_path for leaf in [*context_leaves, primary_leaf]]

    chunk_documents: list[ChunkDocument] = []
    for span in _split_text(
        primary_leaf.text,
        chunk_size=primary_chunk_size,
        chunk_overlap=primary_chunk_overlap,
    ):
        page_content = _json_long_field_content(
            context_lines=context_lines,
            label=primary_leaf.label,
            value=span.content,
        )
        chunk_documents.append(
            ChunkDocument(
                page_content=page_content,
                metadata={
                    "file_type": "json",
                    "json_path": group_path,
                    "field_paths": field_paths,
                    "primary_json_path": primary_leaf.json_path,
                    "char_start": primary_leaf.start + span.start,
                    "char_end": primary_leaf.start + span.end,
                },
            )
        )
    return chunk_documents


# 构造长字段切片的文本表示，保留识别字段所需的上下文。
def _json_long_field_content(
    *,
    context_lines: list[str],
    label: str,
    value: str,
) -> str:
    primary_line = f"{label}: {value}"
    if not context_lines:
        return primary_line
    return "\n".join([*context_lines, primary_line])


# 为同组 JSON 字段构造路径和来源范围元数据。
def _json_group_metadata(group_path: str, leaves: list[JSONLeaf]) -> dict[str, Any]:
    return {
        "file_type": "json",
        "json_path": group_path,
        "field_paths": [leaf.json_path for leaf in leaves],
        "field_char_ranges": [
            {
                "json_path": leaf.json_path,
                "char_start": leaf.start,
                "char_end": leaf.end,
            }
            for leaf in leaves
        ],
        "char_start": min(leaf.start for leaf in leaves),
        "char_end": max(leaf.end for leaf in leaves),
    }


# 将一个 JSON 叶子格式化为路径与值组成的文本行。
def _json_field_line(leaf: JSONLeaf) -> str:
    return f"{leaf.label}: {leaf.text}"


# 递归遍历对象和数组，输出标量叶子及对应 JSON 路径。
def _iter_json_leaf_values(
    value: Any,
    json_path: str = "$",
    parent_path: str = "$",
    label: str = "$",
) -> list[tuple[str, Any, str, str]]:
    if isinstance(value, dict):
        leaves: list[tuple[str, Any, str, str]] = []
        for key, child in value.items():
            child_path = _append_json_object_key(json_path, key)
            child_parent_path = child_path if json_path == "$" else json_path
            leaves.extend(
                _iter_json_leaf_values(
                    child,
                    json_path=child_path,
                    parent_path=child_parent_path,
                    label=str(key),
                )
            )
        return leaves
    if isinstance(value, list):
        leaves = []
        for index, child in enumerate(value):
            child_path = f"{json_path}[{index}]"
            leaves.extend(
                _iter_json_leaf_values(
                    child,
                    json_path=child_path,
                    parent_path=child_path,
                    label=f"[{index}]",
                )
            )
        return leaves
    return [(json_path, value, parent_path, label)]


# 为对象键追加规范 JSON 路径，处理需要转义的键名。
def _append_json_object_key(json_path: str, key: Any) -> str:
    key_text = str(key)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_text):
        return f"{json_path}.{key_text}"
    escaped_key = key_text.replace("\\", "\\\\").replace("'", "\\'")
    return f"{json_path}['{escaped_key}']"


# 将 JSON 标量转换为可索引文本。
def _json_leaf_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False)


# 在原始 JSON 文本中定位字段值，供来源跨度记录使用。
def _find_json_value_offset(
    raw_text: str,
    *,
    value: Any,
    text: str,
    search_start: int,
) -> int:
    if isinstance(value, str):
        serialized = json.dumps(value, ensure_ascii=False)
        serialized_start = raw_text.find(serialized, search_start)
        if serialized_start != -1:
            text_start = raw_text.find(text, serialized_start, serialized_start + len(serialized))
            if text_start != -1:
                return text_start
            return serialized_start + 1

    serialized = json.dumps(value, ensure_ascii=False)
    start = raw_text.find(serialized, search_start)
    if start != -1:
        return start
    start = raw_text.find(text, search_start)
    if start != -1:
        return start
    return search_start


# 判断叶子路径是否符合配置的字段筛选范围。
def _json_path_matches(json_path: str, allowed_paths: set[str]) -> bool:
    normalized_without_indexes = re.sub(r"\[\d+\]", "", json_path)
    return json_path in allowed_paths or normalized_without_indexes in allowed_paths


# 统一 JSON 字段筛选路径的表示方式。
def _normalize_json_filter_path(path: str) -> str:
    stripped = path.strip()
    if not stripped:
        return "$"
    if stripped.startswith("$"):
        return stripped
    return "$." + stripped


# 根据来源名称与元数据推断文档类型。
def _infer_file_type(document: RAGDocument) -> str:
    metadata = document.metadata or {}
    raw_file_type = str(metadata.get("file_type") or "").strip().lower()
    if raw_file_type:
        return _normalize_file_type(raw_file_type)

    extension = str(metadata.get("extension") or "").strip().lower()
    if extension:
        return _normalize_file_type(extension)

    source = document.source.split("#", maxsplit=1)[0].replace("\\", "/").lower()
    match = re.search(r"(\.[a-z0-9]+)$", source)
    return _normalize_file_type(match.group(1) if match else "")


# 统一文档类型名称，供切块策略分派使用。
def _normalize_file_type(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {".md", ".markdown", "md", "markdown"}:
        return "markdown"
    if normalized in {".json", "json"}:
        return "json"
    if normalized in {".txt", "txt", "text", ""}:
        return "text"
    if normalized == "code":
        return "code"
    if language_for_extension(normalized) or language_enum_from_name(normalized):
        return "code"
    if normalized.startswith("."):
        return normalized[1:]
    return normalized


# 从显式元数据或扩展名推断代码语言。
def _infer_code_language(document: RAGDocument) -> Language | None:
    metadata = document.metadata or {}
    metadata_language = metadata.get("language")
    language = language_enum_from_name(str(metadata_language or ""))
    if language is not None:
        return language

    extension = str(metadata.get("extension") or "").strip().lower()
    language = language_enum_from_name(language_for_extension(extension))
    if language is not None:
        return language

    source = document.source.split("#", maxsplit=1)[0].replace("\\", "/").lower()
    match = re.search(r"(\.[a-z0-9]+)$", source)
    if match:
        return language_enum_from_name(language_for_extension(match.group(1)))
    return None


# 读取并规范化 Markdown 标题层级路径。
def _heading_path_from_metadata(metadata: dict[str, Any]) -> list[str]:
    return [
        str(metadata[key])
        for key in ("h1", "h2", "h3")
        if metadata.get(key)
    ]


# 容错转换非负整数，供偏移和序号元数据使用。
def _coerce_non_negative_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value >= 0:
        return value
    return default


# 规范化文本表示，供分块和稳定定位使用。
def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _heading_path_at(text: str, position: int) -> list[str]:
    """计算某个字符位置之前的 Markdown 标题层级.

    例如：

    ```text
    # A
    ## B
    ### C
    ```

    位于 C 之后的 chunk 会得到 `["A", "B", "C"]`。
    """
    headings: list[str] = []
    for line in text[:position].splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        level = len(stripped) - len(stripped.lstrip("#"))
        if level < 1 or level > 6:
            continue
        heading = stripped[level:].strip()
        if not heading:
            continue
        headings = headings[: level - 1]
        headings.append(heading)
    return headings


def _line_range(text: str, start: int, end: int) -> tuple[int, int]:
    """根据字符范围计算 1-based 行号范围."""
    line_start = text.count("\n", 0, start) + 1
    line_end = text.count("\n", 0, end) + 1
    return line_start, line_end


# 由文档身份生成短且稳定的键，避免不同来源的分块 ID 冲突。
def _stable_document_key(document: RAGDocument) -> str:
    """Create a short stable key from the document identity."""
    identity = f"{document.source}\n{document.title or ''}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
