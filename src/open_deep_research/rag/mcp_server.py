"""MCP server wrapper for the local RAG pipeline."""

import argparse
import os
from typing import Any, Mapping

from mcp.server.fastmcp import FastMCP

from open_deep_research.rag.loaders import (
    load_documents_from_paths,
    load_memory_documents_from_mysql,
    load_memory_documents_from_paths,
)
from open_deep_research.rag.query_rewriter import (
    api_key_for_model,
    maybe_rewrite_query_with_model,
)
from open_deep_research.rag.service import (
    _PIPELINE_CACHE,
    _PIPELINE_CACHE_LOCK,
    RAGPipeline,
    RAGPipelineConfig,
    build_rag_index_id,
    build_rag_pipeline_config,
    get_or_create_rag_pipeline,
    reset_rag_pipeline_cache,
)
from open_deep_research.rag.types import Citation, RAGDocument, RetrievalResult

MCP_SERVER_NAME = "open-deep-research-rag"

mcp = FastMCP(
    MCP_SERVER_NAME,
    instructions=(
        "Expose Open Deep Research local RAG operations as MCP tools. "
        "Use rag_search for cited retrieval, rag_ensure_indexed before long runs, "
        "and rag_status/rag_list_sources to inspect configured sources."
    ),
)


def build_pipeline_config(config: Mapping[str, Any] | RAGPipelineConfig | None = None) -> RAGPipelineConfig:
    """Build a RAG pipeline config from direct or `rag_`-prefixed settings."""
    return build_rag_pipeline_config(_raw_config_dict(config))


@mcp.tool(
    name="rag_search",
    description=(
        "Search configured local RAG documents and memory. Returns answer-ready "
        "cited context plus structured citations."
    ),
)
def rag_search(query: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a local RAG query and return cited context."""
    try:
        raw_config = _raw_config_dict(config)
        pipeline_config = build_pipeline_config(config)
        pipeline = get_or_create_rag_pipeline(pipeline_config)
        rewrite_model = str(
            raw_config.get("rag_query_rewrite_model")
            or raw_config.get("query_rewrite_model")
            or "openai:gpt-4.1-mini"
        )
        retrieval_query = maybe_rewrite_query_with_model(
            query,
            enabled=bool(raw_config.get("rag_query_rewrite_enabled", False)),
            model_name=rewrite_model,
            max_tokens=int(
                raw_config.get("rag_query_rewrite_max_tokens")
                or raw_config.get("query_rewrite_max_tokens")
                or 256
            ),
            api_key=api_key_for_model(rewrite_model, raw_config),
        )
        answer_ready_context = pipeline.query(
            retrieval_query,
            original_query=query,
        )
        return {
            "ok": True,
            "query": answer_ready_context.query,
            "index_id": pipeline.index_id,
            "context": answer_ready_context.context,
            "citations": [
                _citation_payload(citation)
                for citation in answer_ready_context.citations
            ],
            "matched_chunks": [
                _retrieval_result_payload(result)
                for result in answer_ready_context.matched_chunks
            ],
        }
    except Exception as exc:
        return _error_payload("rag_search", exc)


@mcp.tool(
    name="rag_ensure_indexed",
    description="Build or refresh the configured local RAG index and return index status.",
)
def rag_ensure_indexed(
    config: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Ensure the configured RAG index is ready."""
    try:
        pipeline = get_or_create_rag_pipeline(build_pipeline_config(config))
        pipeline.indexer.ensure_ready(force=force)
        return _pipeline_status_payload(pipeline, cached=True)
    except Exception as exc:
        return _error_payload("rag_ensure_indexed", exc)


@mcp.tool(
    name="rag_index_pending_memories",
    description=(
        "Refresh the configured RAG index and mark pending MySQL memory rows as indexed."
    ),
)
def rag_index_pending_memories(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Refresh the full index and mark pending MySQL memories indexed."""
    try:
        pipeline = get_or_create_rag_pipeline(build_pipeline_config(config))
        pipeline.index_pending_memories()
        return _pipeline_status_payload(pipeline, cached=True)
    except Exception as exc:
        return _error_payload("rag_index_pending_memories", exc)


@mcp.tool(
    name="rag_status",
    description="Inspect the configured RAG index id and cached pipeline state.",
)
def rag_status(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return status for the configured RAG pipeline without forcing indexing."""
    try:
        pipeline_config = build_pipeline_config(config)
        index_id = build_rag_index_id(pipeline_config)
        cached_pipeline = _cached_pipeline(index_id)
        if cached_pipeline is None:
            return {
                "ok": True,
                "index_id": index_id,
                "cached": False,
                "ready": False,
                "document_count": 0,
                "chunk_count": 0,
                "vector_count": 0,
                "config": _config_summary(pipeline_config),
            }
        return _pipeline_status_payload(cached_pipeline, cached=True)
    except Exception as exc:
        return _error_payload("rag_status", exc)


@mcp.tool(
    name="rag_list_sources",
    description=(
        "List configured local RAG source documents and memory records without building embeddings."
    ),
)
def rag_list_sources(
    config: dict[str, Any] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Load and list configured RAG source documents without indexing."""
    try:
        pipeline_config = build_pipeline_config(config)
        documents = _load_configured_documents(pipeline_config)
        bounded_limit = max(0, int(limit))
        returned_documents = documents[:bounded_limit]
        return {
            "ok": True,
            "index_id": build_rag_index_id(pipeline_config),
            "document_count": len(documents),
            "returned_count": len(returned_documents),
            "truncated": len(documents) > len(returned_documents),
            "sources": [
                _document_payload(document)
                for document in returned_documents
            ],
            "config": _config_summary(pipeline_config),
        }
    except Exception as exc:
        return _error_payload("rag_list_sources", exc)


@mcp.tool(
    name="rag_reset_cache",
    description="Clear the in-process RAG pipeline cache.",
)
def rag_reset_cache() -> dict[str, Any]:
    """Clear the in-process RAG pipeline cache."""
    try:
        reset_rag_pipeline_cache()
        return {"ok": True, "message": "RAG pipeline cache cleared."}
    except Exception as exc:
        return _error_payload("rag_reset_cache", exc)


def _raw_config_dict(config: Mapping[str, Any] | Any | None) -> dict[str, Any]:
    if config is None:
        return {}
    if hasattr(config, "model_dump"):
        raw = dict(config.model_dump(exclude_none=True))
    else:
        raw = dict(config)
    configurable = raw.get("configurable")
    if isinstance(configurable, Mapping):
        raw = dict(configurable)
    return {key: value for key, value in raw.items() if value is not None}


def _cached_pipeline(index_id: str) -> RAGPipeline | None:
    with _PIPELINE_CACHE_LOCK:
        return _PIPELINE_CACHE.get(index_id)


def _pipeline_status_payload(pipeline: RAGPipeline, cached: bool) -> dict[str, Any]:
    indexer = pipeline.indexer
    ready = bool(getattr(indexer, "_ready", False))
    return {
        "ok": True,
        "index_id": pipeline.index_id,
        "cached": cached,
        "ready": ready,
        "document_count": len(indexer.documents),
        "chunk_count": len(indexer.chunks),
        "vector_count": indexer.last_vector_count,
        "vectorstore_ready": indexer.vectorstore.is_ready(),
        "keyword_backend": pipeline.config.keyword_backend,
        "config": _config_summary(pipeline.config),
    }


def _load_configured_documents(config: RAGPipelineConfig) -> list[RAGDocument]:
    documents: list[RAGDocument] = []
    if config.knowledge_base_paths:
        documents.extend(
            load_documents_from_paths(
                config.knowledge_base_paths,
                json_text_fields=config.json_text_fields,
                multimodal_enabled=config.multimodal_enabled,
                multimodal_provider=config.multimodal_provider,
                ocr_languages=config.ocr_languages,
                vision_enabled=config.vision_enabled,
                vision_model=config.vision_model,
                vision_prompt=config.vision_prompt,
                vision_max_tokens=config.vision_max_tokens,
            )
        )
    if config.memory_enabled and config.memory_paths:
        documents.extend(
            load_memory_documents_from_paths(
                config.memory_paths,
                json_text_fields=config.memory_json_text_fields,
            )
        )
    if config.memory_enabled and config.memory_mysql_url:
        documents.extend(
            load_memory_documents_from_mysql(
                database_url=config.memory_mysql_url,
                table_name=config.memory_mysql_table,
                conversation_id=config.memory_conversation_id,
                user_id=config.memory_user_id,
                limit=config.memory_mysql_limit,
                record_types=config.memory_mysql_index_record_types,
            )
        )
    return documents


def _config_summary(config: RAGPipelineConfig) -> dict[str, Any]:
    return {
        "knowledge_base_paths": config.knowledge_base_paths,
        "memory_enabled": config.memory_enabled,
        "memory_paths": config.memory_paths,
        "has_memory_mysql_url": bool(config.memory_mysql_url),
        "memory_mysql_table": config.memory_mysql_table,
        "embedding_provider": config.embedding_provider,
        "embedding_model": config.embedding_model,
        "vectorstore_provider": config.vectorstore_provider,
        "vectorstore_path": config.vectorstore_path,
        "collection_name": config.collection_name,
        "keyword_backend": config.keyword_backend,
        "reranker_provider": config.reranker_provider,
        "reranker_model": config.reranker_model,
        "top_k": config.top_k,
        "rerank_top_n": config.rerank_top_n,
        "chunk_size": config.chunk_size,
        "chunk_overlap": config.chunk_overlap,
        "multimodal_enabled": config.multimodal_enabled,
        "vision_enabled": config.vision_enabled,
        "graph_enabled": config.graph_enabled,
    }


def _document_payload(document: RAGDocument) -> dict[str, Any]:
    return {
        "source": document.source,
        "title": document.title,
        "metadata": document.metadata,
        "content_preview": document.content[:240],
        "content_length": len(document.content),
    }


def _citation_payload(citation: Citation) -> dict[str, Any]:
    return citation.model_dump()


def _retrieval_result_payload(result: RetrievalResult) -> dict[str, Any]:
    return {
        "chunk_id": result.chunk.chunk_id,
        "source": result.chunk.source,
        "title": result.chunk.title,
        "score": result.score,
        "vector_score": result.vector_score,
        "keyword_score": result.keyword_score,
        "graph_score": result.graph_score,
        "structured_score": result.structured_score,
        "rerank_score": result.rerank_score,
        "metadata": result.chunk.metadata,
        "content_preview": result.chunk.content[:240],
    }


def _error_payload(operation: str, exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "operation": operation,
        "error_type": exc.__class__.__name__,
        "error": str(exc),
    }


def main() -> None:
    """Run the RAG MCP server."""
    parser = argparse.ArgumentParser(description="Run the Open Deep Research RAG MCP server.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default=os.getenv("RAG_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument("--host", default=os.getenv("RAG_MCP_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("RAG_MCP_PORT", "8000")),
    )
    args = parser.parse_args()

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
