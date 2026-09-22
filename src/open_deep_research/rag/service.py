"""RAG query pipeline.

`RAGPipeline` is now only the query entry point:
1. ask `RAGIndexer` to make the retrieval index ready;
2. run hybrid retrieval;
3. run reranking;
4. format answer-ready cited context.

Memory-specific loading and MySQL indexing details live behind the indexer.
"""

import hashlib
import json
import threading
from collections.abc import Mapping
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from open_deep_research.configuration import Configuration
from open_deep_research.memory.context import get_conversation_id, get_user_id
from open_deep_research.observability.langsmith import trace_span
from open_deep_research.rag.citations import build_answer_ready_context
from open_deep_research.rag.config import (
    RAGConfig,
    flat_config_path,
    rag_config_from_mapping,
    rag_config_to_flat_dict,
)
from open_deep_research.rag.indexer import RAGIndexer
from open_deep_research.rag.reranker import create_reranker
from open_deep_research.rag.types import AnswerReadyContext, RetrievalResult


class RAGPipelineConfig(RAGConfig):
    """Backward-compatible flat constructor and attribute facade for RAGConfig."""

    def __init__(self, **data: Any):
        canonical = rag_config_from_mapping(data)
        super().__init__(**BaseModel.model_dump(canonical))

    @classmethod
    def model_validate(cls, obj: Any, **_: Any) -> "RAGPipelineConfig":
        """Preserve flat mapping validation used by legacy Pydantic callers."""
        if isinstance(obj, RAGConfig):
            return cls.from_rag_config(obj)
        if isinstance(obj, Mapping):
            return cls(**dict(obj))
        return super().model_validate(obj, **_)

    @classmethod
    def from_rag_config(cls, config: RAGConfig) -> "RAGPipelineConfig":
        """Wrap canonical config without another field mapping step."""
        return cls(**BaseModel.model_dump(config))

    def to_rag_config(self) -> RAGConfig:
        """Return the plain canonical model consumed by runtime components."""
        return RAGConfig(**BaseModel.model_dump(self))

    def __getattr__(self, name: str) -> Any:
        path = flat_config_path(name)
        if path is not None:
            section_name, field_name = path
            return getattr(getattr(self, section_name), field_name)
        return super().__getattr__(name)

    def __setattr__(self, name: str, value: Any) -> None:
        path = flat_config_path(name)
        if path is not None and name not in type(self).model_fields:
            section_name, field_name = path
            setattr(getattr(self, section_name), field_name, value)
            return
        super().__setattr__(name, value)

    def model_dump(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        exclude_none: bool = False,
        **_: Any,
    ) -> dict[str, Any]:
        """Preserve the legacy flat serialization shape."""
        dumped = rag_config_to_flat_dict(
            self,
            include_non_pipeline=False,
            exclude_none=exclude_none,
        )
        if include is not None:
            included = set(include)
            dumped = {key: value for key, value in dumped.items() if key in included}
        if exclude is not None:
            excluded = set(exclude)
            dumped = {key: value for key, value in dumped.items() if key not in excluded}
        return dumped

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> "RAGPipelineConfig":
        """Preserve flat-key updates used by legacy callers."""
        if not update:
            copied = self.to_rag_config().model_copy(deep=deep)
            return type(self).from_rag_config(copied)
        current = rag_config_to_flat_dict(self, include_non_pipeline=True)
        current.update(update)
        return type(self)(**current)


class RAGPipeline:
    """End-to-end query facade for local RAG."""

    def __init__(self, config: RAGConfig, index_id: Optional[str] = None):
        self.config = config.to_rag_config() if isinstance(config, RAGPipelineConfig) else config
        self.index_id = index_id or build_rag_index_id(self.config)
        self.indexer = RAGIndexer(config=self.config, index_id=self.index_id)
        reranker_config = self.config.reranker
        self.reranker = create_reranker(
            provider=reranker_config.provider,
            model_name=reranker_config.model,
            device=reranker_config.device,
        )

    def query(self, query: str, *, original_query: str | None = None) -> AnswerReadyContext:
        """Retrieve cited local context for a natural-language query."""
        display_query = original_query or query
        self.indexer.ensure_ready()

        if not self.indexer.documents or not self.indexer.chunks:
            return AnswerReadyContext(
                query=display_query,
                context=(
                    "No local RAG documents or memory records were loaded. "
                    "Check rag_knowledge_base_paths, rag_memory_paths, and "
                    "supported file types before retrying."
                ),
            )
        if self.indexer.retriever is None:
            return AnswerReadyContext(
                query=display_query,
                context="Local RAG index is not ready.",
            )

        with trace_span(
            "embed_query",
            "chain",
            metadata={
                "embedding_provider": self.config.embedding.provider,
                "embedding_model": self.config.embedding.model,
            },
        ):
            query_vector = self.indexer.embedding_backend.embed_query(query)
        candidate_count = max(self.config.chunking.top_k, self.config.chunking.rerank_top_n)
        with trace_span(
            "rag_retrieval",
            "retriever",
            metadata={
                "retriever_type": "hybrid",
                "top_k": candidate_count,
                "keyword_top_k": max(candidate_count, self.config.keyword_search.top_k),
                "vectorstore_provider": self.config.vectorstore.provider,
            },
        ) as retrieval_run:
            retrieval_results = self.indexer.retriever.retrieve(
                query=query,
                query_vector=query_vector,
                top_k=candidate_count,
                keyword_top_k=max(candidate_count, self.config.keyword_search.top_k),
            )
            if retrieval_run is not None:
                retrieval_run.add_outputs({"result_count": len(retrieval_results)})
        with trace_span(
            "rerank",
            "chain",
            metadata={
                "reranker_provider": self.config.reranker.provider,
                "reranker_model": self.config.reranker.model,
                "input_count": len(retrieval_results),
                "top_k": candidate_count,
            },
        ) as rerank_run:
            retrieval_results = self.reranker.rerank(
                query=query,
                results=retrieval_results,
                top_k=candidate_count,
            )
            if rerank_run is not None:
                rerank_run.add_outputs({"output_count": len(retrieval_results)})
        if self.config.chunking.authority_rerank_enabled:
            retrieval_results = apply_authority_adjustment(retrieval_results)
        with trace_span(
            "result_selection",
            "chain",
            metadata={
                "authority_rerank_enabled": self.config.chunking.authority_rerank_enabled,
                "input_count": len(retrieval_results),
                "top_k": self.config.chunking.top_k,
            },
        ) as selection_run:
            filtered_results = self._filter_results(retrieval_results)
            selected = filtered_results[: self.config.chunking.top_k]
            if selection_run is not None:
                selection_run.add_outputs({"result_count": len(selected)})
        return build_answer_ready_context(
            query=display_query,
            matched_chunks=selected,
        )

    def ensure_indexed(self) -> None:
        """Compatibility hook for explicit indexing jobs."""
        self.indexer.ensure_ready()

    def index_pending_memories(self) -> None:
        """Refresh the index and mark pending MySQL memory rows as indexed."""
        self.indexer.index_pending_memories()

    def _filter_results(self, results: list[RetrievalResult]) -> list[RetrievalResult]:
        """Keep semantically or lexically relevant retrieval results."""
        return [
            result
            for result in results
            if not self._is_blocked_by_authority(result)
            and (
                (result.rerank_score or 0.0) > 0.0
                or result.score > 0.10
                or (result.keyword_score or 0.0) > 0.0
            )
        ]

    def _is_blocked_by_authority(self, result: RetrievalResult) -> bool:
        if not self.config.chunking.authority_rerank_enabled:
            return False
        metadata = result.chunk.metadata or {}
        source_status = str(metadata.get("source_status", "")).lower().strip()
        return source_status in {"misleading", "unanswerable_trap"}


def apply_authority_adjustment(results: list[RetrievalResult]) -> list[RetrievalResult]:
    """Apply source authority penalties after semantic reranking."""
    adjusted_results = []
    for result in results:
        metadata = dict(result.chunk.metadata or {})
        base_score = float(
            result.rerank_score if result.rerank_score is not None else result.score
        )
        penalty = float(metadata.get("authority_score_penalty", 0.0) or 0.0)
        adjusted_score = base_score + penalty
        metadata["authority_base_score"] = base_score
        metadata["authority_adjusted_score"] = adjusted_score
        adjusted_chunk = result.chunk.model_copy(update={"metadata": metadata})
        adjusted_results.append(
            result.model_copy(update={"chunk": adjusted_chunk, "score": adjusted_score})
        )
    adjusted_results.sort(key=lambda item: item.score, reverse=True)
    return adjusted_results


def rag_config_from_configuration(configurable: Configuration) -> RAGConfig:
    """Convert external flat Configuration at the RAG subsystem boundary."""
    raw = {
        field_name: getattr(configurable, field_name)
        for field_name in type(configurable).model_fields
        if field_name.startswith("rag_")
    }
    if raw.get("rag_knowledge_base_paths") is None:
        raw["rag_knowledge_base_paths"] = []
    return rag_config_from_mapping(raw)


def build_rag_config(
    configurable: Configuration | Mapping[str, Any] | RAGConfig | None = None,
    runtime_config: RunnableConfig | Mapping[str, Any] | None = None,
    *,
    memory_user_id: str | None = None,
    memory_conversation_id: str | None = None,
    memory_enabled: bool | None = None,
) -> RAGConfig:
    """Build the one canonical RAG config used by queries and indexing jobs."""
    if isinstance(configurable, RAGPipelineConfig):
        config = configurable.to_rag_config()
    elif isinstance(configurable, RAGConfig):
        config = configurable.model_copy(deep=True)
    elif isinstance(configurable, Configuration):
        config = rag_config_from_configuration(configurable)
    else:
        raw = dict(configurable or {})
        nested_config = raw.get("configurable")
        if isinstance(nested_config, Mapping):
            raw = dict(nested_config)
        for key in ("knowledge_base_paths", "rag_knowledge_base_paths"):
            if key in raw and raw[key] is None:
                raw[key] = []
        config = rag_config_from_mapping(raw)

    if runtime_config is not None:
        if memory_conversation_id is None:
            memory_conversation_id = get_conversation_id(runtime_config)
        if memory_user_id is None:
            memory_user_id = get_user_id(runtime_config)
    if memory_conversation_id is not None:
        config.memory.conversation_id = memory_conversation_id
    if memory_user_id is not None:
        config.memory.user_id = memory_user_id
    if memory_enabled is not None:
        config.memory.enabled = memory_enabled
    return config


def build_rag_pipeline_config(
    configurable: Configuration | Mapping[str, Any] | RAGConfig | None = None,
    runtime_config: RunnableConfig | Mapping[str, Any] | None = None,
    *,
    memory_user_id: str | None = None,
    memory_conversation_id: str | None = None,
    memory_enabled: bool | None = None,
) -> RAGPipelineConfig:
    """Compatibility wrapper returning the legacy public config type."""
    return RAGPipelineConfig.from_rag_config(
        build_rag_config(
            configurable,
            runtime_config,
            memory_user_id=memory_user_id,
            memory_conversation_id=memory_conversation_id,
            memory_enabled=memory_enabled,
        )
    )


_PIPELINE_CACHE: dict[str, RAGPipeline] = {}
_PIPELINE_CACHE_LOCK = threading.Lock()


def build_rag_index_id(config: RAGConfig) -> str:
    """Build a stable index id from config, not source-content fingerprints."""
    payload = rag_config_to_flat_dict(config, include_non_pipeline=False)
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def get_or_create_rag_pipeline(config: RAGConfig) -> RAGPipeline:
    """Return a cached pipeline for the effective RAG configuration."""
    index_id = build_rag_index_id(config)
    with _PIPELINE_CACHE_LOCK:
        cached_pipeline = _PIPELINE_CACHE.get(index_id)
        if cached_pipeline is not None:
            return cached_pipeline

        created_pipeline = RAGPipeline(config, index_id=index_id)
        _PIPELINE_CACHE[index_id] = created_pipeline
        return created_pipeline


def reset_rag_pipeline_cache() -> None:
    """Clear in-process RAG pipeline cache."""
    with _PIPELINE_CACHE_LOCK:
        _PIPELINE_CACHE.clear()
