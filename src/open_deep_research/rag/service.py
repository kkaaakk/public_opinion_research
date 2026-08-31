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
from pathlib import Path
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field, model_validator

from open_deep_research.configuration import Configuration
from open_deep_research.memory.context import get_conversation_id, get_user_id
from open_deep_research.memory.types import INDEXABLE_MEMORY_TYPES
from open_deep_research.rag.citations import build_answer_ready_context
from open_deep_research.rag.config import (
    ChunkingConfig,
    EmbeddingConfig,
    GraphRAGConfig,
    HybridRetrievalConfig,
    KeywordSearchConfig,
    MemoryConfig,
    MultimodalConfig,
    RerankerConfig,
    VectorstoreConfig,
)
from open_deep_research.rag.indexer import RAGIndexer
from open_deep_research.rag.loaders import (
    DEFAULT_RAG_VISION_MODEL,
    DEFAULT_RAG_VISION_PROMPT,
)
from open_deep_research.rag.reranker import create_reranker
from open_deep_research.rag.types import AnswerReadyContext, RetrievalResult


class RAGPipelineConfig(BaseModel):
    """Complete config needed by the local RAG subsystem."""

    knowledge_base_paths: list[str] = Field(default_factory=lambda: ["data/knowledge"])
    chunk_size: int = 1200
    chunk_overlap: int = 200
    top_k: int = 4
    rerank_top_n: int = 20
    embedding_provider: str = "sentence_transformers"
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_device: Optional[str] = None
    vectorstore_provider: str = "milvus"
    vectorstore_path: str = "data/indexes/rag"
    collection_name: str = "open_deep_research"
    milvus_uri: Optional[str] = "data/indexes/rag/milvus.db"
    milvus_token: Optional[str] = None
    milvus_db_name: Optional[str] = None
    milvus_metric_type: str = "COSINE"
    reranker_provider: str = "cross_encoder"
    reranker_model: str = "BAAI/bge-reranker-base"
    reranker_device: Optional[str] = None
    json_text_fields: Optional[list[str]] = None
    multimodal_enabled: bool = True
    multimodal_provider: str = "ocr"
    ocr_languages: str = "eng+chi_sim"
    vision_enabled: bool = True
    vision_model: str = DEFAULT_RAG_VISION_MODEL
    vision_prompt: str = DEFAULT_RAG_VISION_PROMPT
    vision_max_tokens: int = 512
    memory_enabled: bool = False
    memory_paths: Optional[list[str]] = Field(
        default_factory=lambda: ["data/memory/chat_memory.jsonl"]
    )
    memory_json_text_fields: Optional[list[str]] = None
    memory_mysql_url: Optional[str] = None
    memory_mysql_table: str = "rag_chat_memories"
    memory_mysql_limit: int = 1000
    memory_mysql_index_record_types: Optional[list[str]] = Field(
        default_factory=lambda: list(INDEXABLE_MEMORY_TYPES)
    )
    memory_conversation_id: Optional[str] = None
    memory_user_id: Optional[str] = None
    hash_embedding_dimensions: int = 256
    keyword_top_k: int = 12
    keyword_backend: str = "memory"
    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_index: str = "rag_chunks"
    rrf_rank_constant: int = 60
    structured_metadata_weight: float = 0.15
    graph_enabled: bool = False
    graph_backend: str = "neo4j"
    graph_max_neighbors: int = 4
    graph_weight: float = 0.35
    graph_ner_enabled: bool = True
    graph_idf_enabled: bool = True
    graph_idf_threshold_percentile: float = 85.0
    graph_confidence_threshold: float = 0.15
    structural_edges_enabled: bool = False
    neo4j_uri: Optional[str] = "bolt://localhost:7687"
    neo4j_username: Optional[str] = "neo4j"
    neo4j_password: Optional[str] = None
    neo4j_database: Optional[str] = None
    authority_rerank_enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def _unpack_sub_configs(cls, data: Any) -> Any:
        """Unpack nested sub-config objects into flat fields (flat fields win).

        Only fields explicitly set on the sub-config are unpacked, so that
        default values from the sub-config do not pollute model_fields_set
        and break downstream validators (e.g. milvus_uri auto-derivation).
        """
        if not isinstance(data, dict):
            return data

        sub_config_map = {
            "embedding": EmbeddingConfig,
            "vectorstore": VectorstoreConfig,
            "reranker": RerankerConfig,
            "multimodal": MultimodalConfig,
            "memory": MemoryConfig,
            "keyword_search": KeywordSearchConfig,
            "hybrid_retrieval": HybridRetrievalConfig,
            "graph_rag": GraphRAGConfig,
            "chunking": ChunkingConfig,
        }

        for sub_key, sub_cls in sub_config_map.items():
            sub_obj = data.get(sub_key)
            if not isinstance(sub_obj, BaseModel):
                continue
            data.pop(sub_key)
            for field_name in sub_obj.model_fields_set:
                flat_name = _sub_to_flat_field_name(sub_key, field_name)
                if flat_name is not None:
                    data.setdefault(flat_name, getattr(sub_obj, field_name))

        return data

    @model_validator(mode="after")
    def apply_path_defaults(self) -> "RAGPipelineConfig":
        """Keep Milvus Lite colocated with custom vectorstore paths."""
        if "milvus_uri" not in self.model_fields_set and "vectorstore_path" in self.model_fields_set:
            self.milvus_uri = _milvus_uri_from_vectorstore_path(self.vectorstore_path)
        if not 0 <= self.structured_metadata_weight <= 1:
            raise ValueError("structured_metadata_weight must be between 0 and 1.")
        return self

    # ------------------------------------------------------------------
    # Read-only sub-config views (derived from flat fields in real-time).
    # These are @property, NOT model fields, so model_fields / model_dump
    # / model_json_schema are completely unaffected.
    # ------------------------------------------------------------------

    @property
    def embedding(self) -> EmbeddingConfig:
        """Embedding subsystem configuration (read-only view)."""
        return EmbeddingConfig(
            provider=self.embedding_provider,
            model=self.embedding_model,
            device=self.embedding_device,
            hash_dimensions=self.hash_embedding_dimensions,
        )

    @property
    def vectorstore(self) -> VectorstoreConfig:
        """Vector store subsystem configuration (read-only view)."""
        return VectorstoreConfig(
            provider=self.vectorstore_provider,
            persist_path=self.vectorstore_path,
            collection_name=self.collection_name,
            milvus_uri=self.milvus_uri,
            milvus_token=self.milvus_token,
            milvus_db_name=self.milvus_db_name,
            milvus_metric_type=self.milvus_metric_type,
        )

    @property
    def reranker(self) -> RerankerConfig:
        """Reranker subsystem configuration (read-only view)."""
        return RerankerConfig(
            provider=self.reranker_provider,
            model=self.reranker_model,
            device=self.reranker_device,
        )

    @property
    def multimodal(self) -> MultimodalConfig:
        """Multimodal processing configuration (read-only view)."""
        return MultimodalConfig(
            enabled=self.multimodal_enabled,
            provider=self.multimodal_provider,
            ocr_languages=self.ocr_languages,
            vision_enabled=self.vision_enabled,
            vision_model=self.vision_model,
            vision_prompt=self.vision_prompt,
            vision_max_tokens=self.vision_max_tokens,
        )

    @property
    def memory(self) -> MemoryConfig:
        """Memory subsystem configuration (read-only view)."""
        return MemoryConfig(
            enabled=self.memory_enabled,
            paths=self.memory_paths,
            json_text_fields=self.memory_json_text_fields,
            mysql_url=self.memory_mysql_url,
            mysql_table=self.memory_mysql_table,
            mysql_limit=self.memory_mysql_limit,
            mysql_index_record_types=self.memory_mysql_index_record_types,
            conversation_id=self.memory_conversation_id,
            user_id=self.memory_user_id,
        )

    @property
    def keyword_search(self) -> KeywordSearchConfig:
        """Keyword search configuration (read-only view)."""
        return KeywordSearchConfig(
            top_k=self.keyword_top_k,
            backend=self.keyword_backend,
            elasticsearch_url=self.elasticsearch_url,
            elasticsearch_index=self.elasticsearch_index,
        )

    @property
    def hybrid_retrieval(self) -> HybridRetrievalConfig:
        """Hybrid retrieval configuration (read-only view)."""
        return HybridRetrievalConfig(
            rrf_rank_constant=self.rrf_rank_constant,
            structured_metadata_weight=self.structured_metadata_weight,
        )

    @property
    def graph_rag(self) -> GraphRAGConfig:
        """Graph RAG configuration (read-only view)."""
        return GraphRAGConfig(
            enabled=self.graph_enabled,
            backend=self.graph_backend,
            max_neighbors=self.graph_max_neighbors,
            weight=self.graph_weight,
            ner_enabled=self.graph_ner_enabled,
            idf_enabled=self.graph_idf_enabled,
            idf_threshold_percentile=self.graph_idf_threshold_percentile,
            confidence_threshold=self.graph_confidence_threshold,
            structural_edges_enabled=self.structural_edges_enabled,
            neo4j_uri=self.neo4j_uri,
            neo4j_username=self.neo4j_username,
            neo4j_password=self.neo4j_password,
            neo4j_database=self.neo4j_database,
        )

    @property
    def chunking(self) -> ChunkingConfig:
        """Document chunking configuration (read-only view)."""
        return ChunkingConfig(
            knowledge_base_paths=self.knowledge_base_paths,
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            top_k=self.top_k,
            rerank_top_n=self.rerank_top_n,
            json_text_fields=self.json_text_fields,
            authority_rerank_enabled=self.authority_rerank_enabled,
        )


class RAGPipeline:
    """End-to-end query facade for local RAG."""

    def __init__(self, config: RAGPipelineConfig, index_id: Optional[str] = None):
        self.config = config
        self.index_id = index_id or build_rag_index_id(config)
        self.indexer = RAGIndexer(config=config, index_id=self.index_id)
        reranker_config = config.reranker
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

        query_vector = self.indexer.embedding_backend.embed_query(query)
        candidate_count = max(self.config.top_k, self.config.rerank_top_n)
        retrieval_results = self.indexer.retriever.retrieve(
            query=query,
            query_vector=query_vector,
            top_k=candidate_count,
            keyword_top_k=max(candidate_count, self.config.keyword_top_k),
        )
        retrieval_results = self.reranker.rerank(
            query=query,
            results=retrieval_results,
            top_k=candidate_count,
        )
        if self.config.authority_rerank_enabled:
            retrieval_results = apply_authority_adjustment(retrieval_results)
        filtered_results = self._filter_results(retrieval_results)
        return build_answer_ready_context(
            query=display_query,
            matched_chunks=filtered_results[: self.config.top_k],
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
        if not self.config.authority_rerank_enabled:
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


def _milvus_uri_from_vectorstore_path(vectorstore_path: str) -> str:
    stripped = str(vectorstore_path).strip()
    if "://" in stripped or stripped.endswith(".db"):
        return stripped
    return str(Path(stripped).expanduser() / "milvus.db").replace("\\", "/")


# Maps sub-config field names to RAGPipelineConfig flat field names.
# Most are identity (e.g. embedding.provider -> embedding_provider), but
# some differ (e.g. vectorstore.persist_path -> vectorstore_path).
_SUB_TO_FLAT: dict[str, dict[str, str]] = {
    "embedding": {
        "provider": "embedding_provider",
        "model": "embedding_model",
        "device": "embedding_device",
        "hash_dimensions": "hash_embedding_dimensions",
    },
    "vectorstore": {
        "provider": "vectorstore_provider",
        "persist_path": "vectorstore_path",
        "collection_name": "collection_name",
        "milvus_uri": "milvus_uri",
        "milvus_token": "milvus_token",
        "milvus_db_name": "milvus_db_name",
        "milvus_metric_type": "milvus_metric_type",
    },
    "reranker": {
        "provider": "reranker_provider",
        "model": "reranker_model",
        "device": "reranker_device",
    },
    "multimodal": {
        "enabled": "multimodal_enabled",
        "provider": "multimodal_provider",
        "ocr_languages": "ocr_languages",
        "vision_enabled": "vision_enabled",
        "vision_model": "vision_model",
        "vision_prompt": "vision_prompt",
        "vision_max_tokens": "vision_max_tokens",
    },
    "memory": {
        "enabled": "memory_enabled",
        "paths": "memory_paths",
        "json_text_fields": "memory_json_text_fields",
        "mysql_url": "memory_mysql_url",
        "mysql_table": "memory_mysql_table",
        "mysql_limit": "memory_mysql_limit",
        "mysql_index_record_types": "memory_mysql_index_record_types",
        "conversation_id": "memory_conversation_id",
        "user_id": "memory_user_id",
    },
    "keyword_search": {
        "top_k": "keyword_top_k",
        "backend": "keyword_backend",
        "elasticsearch_url": "elasticsearch_url",
        "elasticsearch_index": "elasticsearch_index",
    },
    "hybrid_retrieval": {
        "rrf_rank_constant": "rrf_rank_constant",
        "structured_metadata_weight": "structured_metadata_weight",
    },
    "graph_rag": {
        "enabled": "graph_enabled",
        "backend": "graph_backend",
        "max_neighbors": "graph_max_neighbors",
        "weight": "graph_weight",
        "ner_enabled": "graph_ner_enabled",
        "idf_enabled": "graph_idf_enabled",
        "idf_threshold_percentile": "graph_idf_threshold_percentile",
        "confidence_threshold": "graph_confidence_threshold",
        "structural_edges_enabled": "structural_edges_enabled",
        "neo4j_uri": "neo4j_uri",
        "neo4j_username": "neo4j_username",
        "neo4j_password": "neo4j_password",
        "neo4j_database": "neo4j_database",
    },
    "chunking": {
        "knowledge_base_paths": "knowledge_base_paths",
        "chunk_size": "chunk_size",
        "chunk_overlap": "chunk_overlap",
        "top_k": "top_k",
        "rerank_top_n": "rerank_top_n",
        "json_text_fields": "json_text_fields",
        "authority_rerank_enabled": "authority_rerank_enabled",
    },
}


def _sub_to_flat_field_name(sub_key: str, field_name: str) -> str | None:
    """Return the flat field name for a sub-config field, or None if unknown."""
    return _SUB_TO_FLAT.get(sub_key, {}).get(field_name)


_CONFIG_TO_PIPELINE_FIELDS: dict[str, str] = {
    "knowledge_base_paths": "rag_knowledge_base_paths",
    "chunk_size": "rag_chunk_size",
    "chunk_overlap": "rag_chunk_overlap",
    "top_k": "rag_top_k",
    "rerank_top_n": "rag_rerank_top_n",
    "embedding_provider": "rag_embedding_provider",
    "embedding_model": "rag_embedding_model",
    "embedding_device": "rag_embedding_device",
    "vectorstore_provider": "rag_vectorstore_provider",
    "vectorstore_path": "rag_vectorstore_path",
    "collection_name": "rag_collection_name",
    "milvus_uri": "rag_milvus_uri",
    "milvus_token": "rag_milvus_token",
    "milvus_db_name": "rag_milvus_db_name",
    "milvus_metric_type": "rag_milvus_metric_type",
    "reranker_provider": "rag_reranker_provider",
    "reranker_model": "rag_reranker_model",
    "reranker_device": "rag_reranker_device",
    "json_text_fields": "rag_json_text_fields",
    "multimodal_enabled": "rag_multimodal_enabled",
    "multimodal_provider": "rag_multimodal_provider",
    "ocr_languages": "rag_ocr_languages",
    "vision_enabled": "rag_vision_enabled",
    "vision_model": "rag_vision_model",
    "vision_prompt": "rag_vision_prompt",
    "vision_max_tokens": "rag_vision_max_tokens",
    "memory_enabled": "rag_memory_enabled",
    "memory_paths": "rag_memory_paths",
    "memory_json_text_fields": "rag_memory_json_text_fields",
    "memory_mysql_url": "rag_memory_mysql_url",
    "memory_mysql_table": "rag_memory_mysql_table",
    "memory_mysql_limit": "rag_memory_mysql_limit",
    "memory_mysql_index_record_types": "rag_memory_mysql_index_record_types",
    "hash_embedding_dimensions": "rag_hash_embedding_dimensions",
    "keyword_top_k": "rag_keyword_top_k",
    "keyword_backend": "rag_keyword_backend",
    "elasticsearch_url": "rag_elasticsearch_url",
    "elasticsearch_index": "rag_elasticsearch_index",
    "rrf_rank_constant": "rag_rrf_rank_constant",
    "structured_metadata_weight": "rag_structured_metadata_weight",
    "graph_enabled": "rag_graph_enabled",
    "graph_backend": "rag_graph_backend",
    "graph_max_neighbors": "rag_graph_max_neighbors",
    "graph_weight": "rag_graph_weight",
    "graph_ner_enabled": "rag_graph_ner_enabled",
    "graph_idf_enabled": "rag_graph_idf_enabled",
    "graph_idf_threshold_percentile": "rag_graph_idf_threshold_percentile",
    "graph_confidence_threshold": "rag_graph_confidence_threshold",
    "structural_edges_enabled": "rag_structural_edges_enabled",
    "neo4j_uri": "rag_neo4j_uri",
    "neo4j_username": "rag_neo4j_username",
    "neo4j_password": "rag_neo4j_password",
    "neo4j_database": "rag_neo4j_database",
    "authority_rerank_enabled": "rag_authority_rerank_enabled",
}


def build_rag_pipeline_config(
    configurable: Configuration | Mapping[str, Any] | RAGPipelineConfig | None = None,
    runtime_config: RunnableConfig | Mapping[str, Any] | None = None,
    *,
    memory_user_id: str | None = None,
    memory_conversation_id: str | None = None,
    memory_enabled: bool | None = None,
) -> RAGPipelineConfig:
    """Build one effective RAG config for queries and indexing jobs.

    ``Configuration`` supplies the application-level ``rag_*`` settings while
    mappings support the direct and ``rag_``-prefixed shapes used by the RAG MCP.
    Memory identity and the forced indexing mode are explicit overrides so the
    memory refresh path cannot drift from normal query configuration.
    """
    if isinstance(configurable, RAGPipelineConfig):
        payload = configurable.model_dump()
    elif isinstance(configurable, Configuration):
        payload = {
            pipeline_field: getattr(configurable, configuration_field)
            for pipeline_field, configuration_field in _CONFIG_TO_PIPELINE_FIELDS.items()
        }
    else:
        raw_config = dict(configurable or {})
        nested_config = raw_config.get("configurable")
        if isinstance(nested_config, Mapping):
            raw_config = dict(nested_config)
        payload = {}
        for pipeline_field in RAGPipelineConfig.model_fields:
            prefixed_field = f"rag_{pipeline_field}"
            if pipeline_field in raw_config:
                payload[pipeline_field] = raw_config[pipeline_field]
            elif prefixed_field in raw_config:
                payload[pipeline_field] = raw_config[prefixed_field]
        for sub_config_name in _SUB_TO_FLAT:
            if sub_config_name in raw_config:
                payload[sub_config_name] = raw_config[sub_config_name]

    if runtime_config is not None:
        if memory_conversation_id is None:
            memory_conversation_id = get_conversation_id(runtime_config)
        if memory_user_id is None:
            memory_user_id = get_user_id(runtime_config)
    if "knowledge_base_paths" in payload and payload["knowledge_base_paths"] is None:
        # Configuration normalizes an explicitly empty path list to None, while
        # the pipeline keeps the historical empty-list meaning for this field.
        payload["knowledge_base_paths"] = []
    if memory_conversation_id is not None:
        payload["memory_conversation_id"] = memory_conversation_id
    if memory_user_id is not None:
        payload["memory_user_id"] = memory_user_id
    if memory_enabled is not None:
        payload["memory_enabled"] = memory_enabled

    return RAGPipelineConfig(**payload)


_PIPELINE_CACHE: dict[str, RAGPipeline] = {}
_PIPELINE_CACHE_LOCK = threading.Lock()


def build_rag_index_id(config: RAGPipelineConfig) -> str:
    """Build a stable index id from config, not source-content fingerprints."""
    payload = config.model_dump()
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def get_or_create_rag_pipeline(config: RAGPipelineConfig) -> RAGPipeline:
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
