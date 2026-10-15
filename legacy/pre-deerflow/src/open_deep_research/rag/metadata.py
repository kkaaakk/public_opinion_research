# 将来源、标题及结构化元数据转换为检索和重排可用的短文本。
"""Helpers for using structured chunk metadata during retrieval."""

from typing import Any

STRUCTURED_METADATA_KEYS = (
    "source_type",
    "file_type",
    "language",
    "memory_backend",
    "memory_type",
    "conversation_id",
    "user_id",
    "h1",
    "h2",
    "h3",
    "heading_path",
    "section_title",
    "json_path",
    "page",
    "page_number",
    "source_status",
)


# 合并来源、标题及元数据行，为分块补充可搜索上下文。
def build_structured_context_text(
    *,
    source: str,
    title: str | None,
    metadata: dict[str, Any] | None,
) -> str:
    """Format source/title/metadata as short searchable context lines."""
    lines: list[str] = []
    if title:
        lines.append(f"title: {title}")
    if source:
        lines.append(f"source: {source}")
    lines.extend(build_structured_metadata_lines(metadata or {}))
    return "\n".join(lines)


# 按稳定顺序格式化对检索有帮助的元数据字段。
def build_structured_metadata_lines(metadata: dict[str, Any]) -> list[str]:
    """Return stable metadata lines for fields useful to retrieval/reranking."""
    lines: list[str] = []
    for key in STRUCTURED_METADATA_KEYS:
        value = metadata.get(key)
        if value is None or value == "":
            continue
        lines.append(f"{key}: {_metadata_value_to_text(value)}")
    return lines


# 将标量或复合元数据值转换为可比较的文本。
def _metadata_value_to_text(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return " / ".join(_metadata_value_to_text(item) for item in value)
    if isinstance(value, dict):
        return " / ".join(
            f"{key}={_metadata_value_to_text(item)}"
            for key, item in sorted(value.items())
            if item is not None and item != ""
        )
    return str(value)
