"""RAG 子系统功能域配置类。

每个子配置类对应一个 RAG 功能域（embedding、vectorstore、reranker 等），
字段名和默认值与 RAGPipelineConfig 中的平铺字段一一对应。

这些类既可以独立构造和传递（例如直接传给 create_embedding_backend），
也可以通过 RAGPipelineConfig 的只读 @property 视图访问。
"""

from typing import Optional

from pydantic import BaseModel, Field

from open_deep_research.memory.types import INDEXABLE_MEMORY_TYPES
from open_deep_research.rag.loaders import (
    DEFAULT_RAG_VISION_MODEL,
    DEFAULT_RAG_VISION_PROMPT,
)


class EmbeddingConfig(BaseModel):
    provider: str = "sentence_transformers"
    model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    device: Optional[str] = None
    hash_dimensions: int = 256


class VectorstoreConfig(BaseModel):
    provider: str = "milvus"
    persist_path: str = "data/indexes/rag"
    collection_name: str = "open_deep_research"
    milvus_uri: Optional[str] = "data/indexes/rag/milvus.db"
    milvus_token: Optional[str] = None
    milvus_db_name: Optional[str] = None
    milvus_metric_type: str = "COSINE"


class RerankerConfig(BaseModel):
    provider: str = "cross_encoder"
    model: str = "BAAI/bge-reranker-base"
    device: Optional[str] = None


class MultimodalConfig(BaseModel):
    enabled: bool = True
    provider: str = "ocr"
    ocr_languages: str = "eng+chi_sim"
    vision_enabled: bool = True
    vision_model: str = DEFAULT_RAG_VISION_MODEL
    vision_prompt: str = DEFAULT_RAG_VISION_PROMPT
    vision_max_tokens: int = 512


class MemoryConfig(BaseModel):
    enabled: bool = False
    paths: Optional[list[str]] = Field(default_factory=lambda: ["data/memory/chat_memory.jsonl"])
    json_text_fields: Optional[list[str]] = None
    mysql_url: Optional[str] = None
    mysql_table: str = "rag_chat_memories"
    mysql_limit: int = 1000
    mysql_index_record_types: Optional[list[str]] = Field(default_factory=lambda: list(INDEXABLE_MEMORY_TYPES))
    conversation_id: Optional[str] = None
    user_id: Optional[str] = None


class KeywordSearchConfig(BaseModel):
    top_k: int = 12
    backend: str = "memory"
    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_index: str = "rag_chunks"


class HybridRetrievalConfig(BaseModel):
    rrf_rank_constant: int = 60
    structured_metadata_weight: float = 0.15


class GraphRAGConfig(BaseModel):
    enabled: bool = False
    backend: str = "neo4j"
    max_neighbors: int = 4
    weight: float = 0.35
    ner_enabled: bool = True
    idf_enabled: bool = True
    idf_threshold_percentile: float = 85.0
    confidence_threshold: float = 0.15
    structural_edges_enabled: bool = False
    neo4j_uri: Optional[str] = "bolt://localhost:7687"
    neo4j_username: Optional[str] = "neo4j"
    neo4j_password: Optional[str] = None
    neo4j_database: Optional[str] = None


class ChunkingConfig(BaseModel):
    knowledge_base_paths: list[str] = Field(default_factory=lambda: ["data/knowledge"])
    chunk_size: int = 1200
    chunk_overlap: int = 200
    top_k: int = 4
    rerank_top_n: int = 20
    json_text_fields: Optional[list[str]] = None
    authority_rerank_enabled: bool = True
