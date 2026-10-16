"""Canonical configuration models for the RAG subsystem.

Application configuration remains flat and ``rag_``-prefixed for LangGraph and
environment compatibility. Inside the RAG subsystem, values live only in the
nested models below. A field's alias is its legacy flat pipeline name, which
lets the compatibility adapter be derived from the canonical schema instead of
maintaining separate mapping tables.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from open_deep_research.memory.types import INDEXABLE_MEMORY_TYPES
from open_deep_research.rag.loaders import (
    DEFAULT_RAG_VISION_MODEL,
    DEFAULT_RAG_VISION_PROMPT,
)


class _RAGSubConfig(BaseModel):
    """Base for canonical sections with legacy flat validation aliases."""

    model_config = ConfigDict(populate_by_name=True)


class EmbeddingConfig(_RAGSubConfig):
    provider: str = Field(default="sentence_transformers", alias="embedding_provider")
    model: str = Field(
        default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        alias="embedding_model",
    )
    device: Optional[str] = Field(default=None, alias="embedding_device")
    hash_dimensions: int = Field(default=256, alias="hash_embedding_dimensions")


class VectorstoreConfig(_RAGSubConfig):
    provider: str = Field(default="milvus", alias="vectorstore_provider")
    persist_path: str = Field(default="data/indexes/rag", alias="vectorstore_path")
    collection_name: str = "open_deep_research"
    milvus_uri: Optional[str] = "data/indexes/rag/milvus.db"
    milvus_token: Optional[str] = None
    milvus_db_name: Optional[str] = None
    milvus_metric_type: str = "COSINE"

    @model_validator(mode="after")
    def apply_path_defaults(self) -> "VectorstoreConfig":
        """Keep Milvus Lite colocated with an explicitly customized index path."""
        if "milvus_uri" not in self.model_fields_set and "persist_path" in self.model_fields_set:
            self.milvus_uri = _milvus_uri_from_vectorstore_path(self.persist_path)
        return self


class RerankerConfig(_RAGSubConfig):
    provider: str = Field(default="cross_encoder", alias="reranker_provider")
    model: str = Field(default="BAAI/bge-reranker-base", alias="reranker_model")
    device: Optional[str] = Field(default=None, alias="reranker_device")


class MultimodalConfig(_RAGSubConfig):
    enabled: bool = Field(default=True, alias="multimodal_enabled")
    provider: str = Field(default="ocr", alias="multimodal_provider")
    ocr_languages: str = "eng+chi_sim"
    vision_enabled: bool = True
    vision_model: str = DEFAULT_RAG_VISION_MODEL
    vision_prompt: str = DEFAULT_RAG_VISION_PROMPT
    vision_max_tokens: int = 512


class MemoryConfig(_RAGSubConfig):
    enabled: bool = Field(default=False, alias="memory_enabled")
    paths: Optional[list[str]] = Field(
        default_factory=lambda: ["data/memory/chat_memory.jsonl"],
        alias="memory_paths",
    )
    json_text_fields: Optional[list[str]] = Field(default=None, alias="memory_json_text_fields")
    mysql_url: Optional[str] = Field(default=None, alias="memory_mysql_url")
    mysql_table: str = Field(default="rag_chat_memories", alias="memory_mysql_table")
    mysql_limit: int = Field(default=1000, alias="memory_mysql_limit")
    mysql_index_record_types: Optional[list[str]] = Field(
        default_factory=lambda: list(INDEXABLE_MEMORY_TYPES),
        alias="memory_mysql_index_record_types",
    )
    write_enabled: bool = Field(default=False, alias="memory_write_enabled")
    write_sync_index: bool = Field(default=True, alias="memory_write_sync_index")
    conversation_id: Optional[str] = Field(default=None, alias="memory_conversation_id")
    user_id: Optional[str] = Field(default=None, alias="memory_user_id")


class KeywordSearchConfig(_RAGSubConfig):
    top_k: int = Field(default=12, alias="keyword_top_k")
    backend: str = Field(default="memory", alias="keyword_backend")
    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_index: str = "rag_chunks"


class HybridRetrievalConfig(_RAGSubConfig):
    rrf_rank_constant: int = 60
    structured_metadata_weight: float = 0.15

    @model_validator(mode="after")
    def validate_structured_metadata_weight(self) -> "HybridRetrievalConfig":
        if not 0 <= self.structured_metadata_weight <= 1:
            raise ValueError("structured_metadata_weight must be between 0 and 1.")
        return self


class GraphRAGConfig(_RAGSubConfig):
    enabled: bool = Field(default=False, alias="graph_enabled")
    backend: str = Field(default="neo4j", alias="graph_backend")
    max_neighbors: int = Field(default=4, alias="graph_max_neighbors")
    weight: float = Field(default=0.35, alias="graph_weight")
    ner_enabled: bool = Field(default=True, alias="graph_ner_enabled")
    idf_enabled: bool = Field(default=True, alias="graph_idf_enabled")
    idf_threshold_percentile: float = Field(default=85.0, alias="graph_idf_threshold_percentile")
    confidence_threshold: float = Field(default=0.15, alias="graph_confidence_threshold")
    structural_edges_enabled: bool = False
    neo4j_uri: Optional[str] = "bolt://localhost:7687"
    neo4j_username: Optional[str] = "neo4j"
    neo4j_password: Optional[str] = None
    neo4j_database: Optional[str] = None


class ChunkingConfig(_RAGSubConfig):
    knowledge_base_paths: list[str] = Field(default_factory=lambda: ["data/knowledge"])
    chunk_size: int = 1200
    chunk_overlap: int = 200
    top_k: int = 4
    rerank_top_n: int = 20
    json_text_fields: Optional[list[str]] = None
    authority_rerank_enabled: bool = True


class QueryConfig(_RAGSubConfig):
    """Query-time rewrite and attached-image behavior."""

    image_enabled: bool = Field(default=True, alias="query_image_enabled")
    image_max_images: int = Field(default=3, alias="query_image_max_images")
    image_max_bytes: int = Field(default=5_000_000, alias="query_image_max_bytes")
    rewrite_enabled: bool = Field(default=True, alias="query_rewrite_enabled")
    rewrite_model: str = Field(
        default="volcengine:doubao-seed-2-0-mini-260428",
        alias="query_rewrite_model",
    )
    rewrite_max_tokens: int = Field(default=256, alias="query_rewrite_max_tokens")


class RAGConfig(BaseModel):
    """Single internal source of truth for all ``rag_*`` settings."""

    enabled: bool = False
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    vectorstore: VectorstoreConfig = Field(default_factory=VectorstoreConfig)
    reranker: RerankerConfig = Field(default_factory=RerankerConfig)
    multimodal: MultimodalConfig = Field(default_factory=MultimodalConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    keyword_search: KeywordSearchConfig = Field(default_factory=KeywordSearchConfig)
    hybrid_retrieval: HybridRetrievalConfig = Field(default_factory=HybridRetrievalConfig)
    graph_rag: GraphRAGConfig = Field(default_factory=GraphRAGConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    query: QueryConfig = Field(default_factory=QueryConfig)


def rag_config_from_mapping(values: Mapping[str, Any] | None = None) -> RAGConfig:
    """Convert flat, ``rag_``-prefixed, or nested input at the sole boundary."""
    raw = dict(values or {})
    nested_config = raw.get("configurable")
    if isinstance(nested_config, Mapping):
        raw = dict(nested_config)

    payload: dict[str, Any] = {}
    if "enabled" in raw:
        payload["enabled"] = raw["enabled"]
    elif "rag_enabled" in raw:
        payload["enabled"] = raw["rag_enabled"]

    for section_name, section_cls in _section_types().items():
        section_payload = _explicit_section_values(raw.get(section_name))
        for field_name, field_info in section_cls.model_fields.items():
            flat_name = field_info.alias or field_name
            if flat_name in raw:
                section_payload[field_name] = raw[flat_name]
            elif f"rag_{flat_name}" in raw:
                section_payload[field_name] = raw[f"rag_{flat_name}"]
        if section_payload:
            payload[section_name] = section_payload
    return RAGConfig(**payload)


def rag_config_to_flat_dict(
    config: RAGConfig,
    *,
    include_non_pipeline: bool = True,
    exclude_none: bool = False,
) -> dict[str, Any]:
    """Serialize canonical values with legacy flat names derived from aliases."""
    flat: dict[str, Any] = {}
    if include_non_pipeline:
        flat["enabled"] = config.enabled
    for section_name in _section_types():
        section = getattr(config, section_name)
        for field_name, field_info in type(section).model_fields.items():
            if not include_non_pipeline and _is_non_pipeline_field(section_name, field_name):
                continue
            value = getattr(section, field_name)
            if exclude_none and value is None:
                continue
            flat[field_info.alias or field_name] = value
    return flat


def flat_config_path(flat_name: str) -> tuple[str, str] | None:
    """Resolve a legacy flat name from canonical field aliases."""
    for section_name, section_cls in _section_types().items():
        for field_name, field_info in section_cls.model_fields.items():
            if flat_name == (field_info.alias or field_name):
                return section_name, field_name
    return None


def _section_types() -> dict[str, type[_RAGSubConfig]]:
    """Derive section model types directly from the canonical model schema."""
    sections: dict[str, type[_RAGSubConfig]] = {}
    for name, field_info in RAGConfig.model_fields.items():
        annotation = field_info.annotation
        if isinstance(annotation, type) and issubclass(annotation, _RAGSubConfig):
            sections[name] = annotation
    return sections


def _explicit_section_values(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_unset=True)
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _is_non_pipeline_field(section_name: str, field_name: str) -> bool:
    return section_name == "query" or (
        section_name == "memory" and field_name in {"write_enabled", "write_sync_index"}
    )


def _milvus_uri_from_vectorstore_path(vectorstore_path: str) -> str:
    stripped = str(vectorstore_path).strip()
    if "://" in stripped or stripped.endswith(".db"):
        return stripped
    return str(Path(stripped).expanduser() / "milvus.db").replace("\\", "/")
