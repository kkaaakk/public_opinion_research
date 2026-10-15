# 为检索片段生成稳定引用编号，并组装可直接提供给模型的证据上下文。
"""本地 RAG 引用格式化工具。

检索和 rerank 得到的是结构化 `RetrievalResult`，但 researcher LLM 接收到的是
ToolMessage 文本。本模块负责把结构化结果转换成“可直接回答但强制带引用”的
上下文字符串。

这里故意在输出中加入 grounding 规则：

```text
Use only the cited excerpts below...
```

原因是本地 RAG 结果后续还会经过 researcher、compression、final report 多个
LLM 阶段，越早把引用约束写进上下文，越能减少无依据扩写。
"""

from open_deep_research.rag.types import AnswerReadyContext, Citation, RetrievalResult


def build_answer_ready_context(
    query: str,
    matched_chunks: list[RetrievalResult],
) -> AnswerReadyContext:
    """将检索结果格式化为带 SOURCE 的上下文。

    返回的 `AnswerReadyContext.context` 会进入 researcher 的工具结果。
    每个 chunk 会被转成一个 SOURCE 块，包含：

    - SOURCE 序号和标题。
    - 本地文件路径或 PDF page source。
    - chunk id。
    - 可读 metadata。
    - 截断后的 excerpt。
    """
    if not matched_chunks:
        empty_context = (
            "No relevant local knowledge base results were found for this query. "
            "Try a broader query, add more local documents, or fall back to web search."
        )
        return AnswerReadyContext(query=query, context=empty_context)

    # 先构造结构化 Citation，再统一格式化成文本。这样测试和未来 UI 展示都可以
    # 复用 citations 字段，而不是重新解析 context 字符串。
    citations = [
        Citation(
            citation_id=index,
            title=result.chunk.title or result.chunk.source,
            source=result.chunk.source,
            chunk_id=result.chunk.chunk_id,
            excerpt=_build_excerpt(result.chunk.content),
            metadata=result.chunk.metadata,
        )
        for index, result in enumerate(matched_chunks, start=1)
    ]

    lines = [
        "Local knowledge base results:",
        (
            "Use only the cited excerpts below for claims about local knowledge "
            "or chat memory. If the excerpts do not support an answer, say the "
            "local knowledge base does not contain enough cited evidence. If a "
            "source is marked memory_type=deprecated or "
            "memory_usage=do_not_adopt, treat it as a prohibition or obsolete "
            "fact, not a recommendation.\n"
        ),
    ]
    for citation in citations:
        lines.append(f"--- SOURCE {citation.citation_id}: {citation.title} ---")
        lines.append(f"SOURCE: {citation.source}")
        lines.append(f"CHUNK ID: {citation.chunk_id}")
        metadata_line = _format_metadata(citation.metadata)
        if metadata_line:
            lines.append(f"METADATA: {metadata_line}")
        lines.append(f"EXCERPT:\n{citation.excerpt}\n")

    lines.append("### Sources")
    for citation in citations:
        lines.append(
            f"[{citation.citation_id}] {citation.title}: {citation.source} "
            f"(chunk: {citation.chunk_id})"
        )

    return AnswerReadyContext(
        query=query,
        context="\n".join(lines).strip(),
        citations=citations,
        matched_chunks=matched_chunks,
    )


def _build_excerpt(text: str, max_length: int = 500) -> str:
    """生成 citation 中展示的短摘录。

    RAG chunk 可能较长，直接展示完整 chunk 会消耗 prompt budget。
    这里压缩空白并截断到固定字符数，保留足够证据同时控制工具返回大小。
    """
    normalized_text = " ".join(text.split())
    if len(normalized_text) <= max_length:
        return normalized_text
    return normalized_text[: max_length - 3].rstrip() + "..."


def _format_metadata(metadata: dict) -> str:
    """将 chunk metadata 格式化为紧凑的一行。

    只展示对引用定位最有用的信息：标题路径、页码、JSON 字段路径和行号。
    其它 metadata 仍保留在结构化对象里，但不直接塞进 prompt，避免噪声过大。
    """
    parts = []
    if metadata.get("heading_path"):
        parts.append("heading=" + " > ".join(metadata["heading_path"]))
    if metadata.get("page_number"):
        parts.append(f"page={metadata['page_number']}")
    if metadata.get("field_paths"):
        parts.append("fields=" + ", ".join(metadata["field_paths"][:8]))
    if metadata.get("source_status"):
        parts.append(f"source_status={metadata['source_status']}")
    if metadata.get("authority_adjusted_score") is not None:
        parts.append(f"authority_score={float(metadata['authority_adjusted_score']):.4f}")
    if metadata.get("source_type") == "memory":
        parts.append("source_type=memory")
    if metadata.get("memory_type"):
        parts.append(f"memory_type={metadata['memory_type']}")
    if metadata.get("memory_usage"):
        parts.append(f"memory_usage={metadata['memory_usage']}")
    if metadata.get("conversation_id"):
        parts.append(f"conversation_id={metadata['conversation_id']}")
    if metadata.get("created_at"):
        parts.append(f"created_at={metadata['created_at']}")
    if metadata.get("confidence") is not None:
        parts.append(f"confidence={metadata['confidence']}")
    if metadata.get("line_start") and metadata.get("line_end"):
        parts.append(f"lines={metadata['line_start']}-{metadata['line_end']}")
    return "; ".join(parts)
