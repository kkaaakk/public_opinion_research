# 将企业知识检索包装为研究智能体可调用的引用上下文工具。
"""Expose RAG retrieval as an agent tool."""

import asyncio

from agents import function_tool
from agents.tool_context import ToolContext
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
from open_deep_research.tools import register_tool

RAG_SEARCH_DESCRIPTION = (
    "Search the configured enterprise knowledge base, including local txt/md/json/pdf/image "
    "files. Use this when the answer may exist in company documents, playbooks, notices, "
    "manuals or project files."
)


class RAGRetrievalError(RuntimeError):
    """Expose a required local retrieval failure to the durable workflow."""


# 根据运行配置执行本地 RAG 检索，仅返回带来源引用的上下文。
async def _rag_search(query: str, config: RunnableConfig = None) -> str:
    """Search local enterprise knowledge, returning cited context only.

    Disabled or empty knowledge configurations return guidance text. A failure
    to build the index or retrieve from it raises instead of pretending the
    search returned no evidence.
    """
    configurable = Configuration.from_runnable_config(config)
    if not configurable.rag_enabled:
        return "Local RAG retrieval is disabled in configuration."
    if not configurable.rag_knowledge_base_paths:
        return "No local RAG document paths are configured."

    try:
        pipeline_config = build_rag_pipeline_config(configurable)
        pipeline = get_or_create_rag_pipeline(pipeline_config)
        retrieval_query = await asyncio.to_thread(
            maybe_rewrite_query_with_model,
            query,
            enabled=configurable.rag_query_rewrite_enabled,
            model_name=configurable.rag_query_rewrite_model,
            max_tokens=configurable.rag_query_rewrite_max_tokens,
            api_key=api_key_for_model(
                configurable.rag_query_rewrite_model, config or {}
            ),
        )
        answer_ready_context = await asyncio.to_thread(
            pipeline.query,
            retrieval_query,
            original_query=query,
        )
        return answer_ready_context.context
    except Exception as exc:
        raise RAGRetrievalError(
            f"Local RAG search failed ({type(exc).__name__}); "
            "the knowledge index or retrieval backend is unavailable."
        ) from exc


# 运行配置须在受保护操作登记后读取，故由 Executor 在校验通过后调用。
async def _run_rag_search(context: ToolContext, query: str) -> str:
    executor = context.context.tool_executor
    return await _rag_search(query, config=executor.tool_config(context))


@function_tool(
    name_override="rag_search",
    description_override=RAG_SEARCH_DESCRIPTION,
    use_docstring_info=False,
    strict_mode=False,
    failure_error_function=None,
)
async def rag_search_tool(context: ToolContext, query: str) -> str:
    return await context.context.tool_executor.execute(
        context, _run_rag_search, context, query
    )


rag_search_tool = register_tool(rag_search_tool, domain="rag", effect="idempotent")


# Public RAG imports still support direct LangChain callers.
rag_search = tool("rag_search", _rag_search, description=RAG_SEARCH_DESCRIPTION)
