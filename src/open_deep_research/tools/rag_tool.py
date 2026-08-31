"""Expose RAG retrieval as an agent tool."""

import asyncio

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from open_deep_research.configuration import Configuration
from open_deep_research.rag.query_rewriter import (
    api_key_for_model,
    maybe_rewrite_query_with_model,
)
from open_deep_research.rag.service import (
    build_rag_pipeline_config,
    get_or_create_rag_pipeline,
)

RAG_SEARCH_DESCRIPTION = (
    "Search the configured local RAG sources, including local txt/md/json/pdf/image files "
    "and optional chat memory from json/jsonl or MySQL records. Use this when the answer may exist "
    "in local documents, notes, manuals, project files, or remembered user context."
)


@tool(description=RAG_SEARCH_DESCRIPTION)
async def rag_search(query: str, config: RunnableConfig = None) -> str:
    """Search local knowledge and memory, returning cited context only."""
    configurable = Configuration.from_runnable_config(config)
    if not configurable.rag_enabled:
        return "Local RAG retrieval is disabled in configuration."

    has_document_paths = bool(configurable.rag_knowledge_base_paths)
    has_memory_paths = bool(configurable.rag_memory_enabled and configurable.rag_memory_paths)
    has_mysql_memory = bool(configurable.rag_memory_enabled and configurable.rag_memory_mysql_url)
    if not has_document_paths and not has_memory_paths and not has_mysql_memory:
        return "No local RAG document paths or memory paths are configured."

    try:
        pipeline_config = build_rag_pipeline_config(configurable, config)
        pipeline = get_or_create_rag_pipeline(pipeline_config)
        retrieval_query = await asyncio.to_thread(
            maybe_rewrite_query_with_model,
            query,
            enabled=configurable.rag_query_rewrite_enabled,
            model_name=configurable.rag_query_rewrite_model,
            max_tokens=configurable.rag_query_rewrite_max_tokens,
            api_key=api_key_for_model(configurable.rag_query_rewrite_model, config or {}),
        )
        answer_ready_context = await asyncio.to_thread(
            pipeline.query,
            retrieval_query,
            original_query=query,
        )
        return answer_ready_context.context
    except Exception as exc:
        return f"Local RAG search failed: {exc}"
