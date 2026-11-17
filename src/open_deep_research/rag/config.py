# 按嵌入、向量库、重排、多模态和分块拆分 RAG 子配置。
"""RAG 子系统功能域配置类。

每个子配置类对应一个 RAG 功能域（embedding、vectorstore、reranker 等），
字段名和默认值与 RAGPipelineConfig 中的平铺字段一一对应。
"""

from typing import Optional

from pydantic import BaseModel, Field

from open_deep_research.rag.loaders import (
    DEFAULT_RAG_VISION_MODEL,
    DEFAULT_RAG_VISION_PROMPT,
)


# 配置嵌入后端、模型、设备及哈希向量维数。
class EmbeddingConfig(BaseModel):
    provider: str = "sentence_transformers"
    model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    device: Optional[str] = None
    hash_dimensions: int = 256


# 配置向量存储后端、持久化路径、集合及连接参数。
class VectorstoreConfig(BaseModel):
    provider: str = "milvus"
    persist_path: str = "data/indexes/rag"
    collection_name: str = "open_deep_research"
    milvus_uri: Optional[str] = "data/indexes/rag/milvus.db"
    milvus_token: Optional[str] = None
    milvus_db_name: Optional[str] = None
    milvus_metric_type: str = "COSINE"


# 配置候选重排后端、模型和运行设备。
class RerankerConfig(BaseModel):
    provider: str = "cross_encoder"
    model: str = "BAAI/bge-reranker-base"
    device: Optional[str] = None


# 配置图片 OCR、视觉理解与输出限制。
class MultimodalConfig(BaseModel):
    enabled: bool = True
    provider: str = "ocr"
    ocr_languages: str = "eng+chi_sim"
    vision_enabled: bool = True
    vision_model: str = DEFAULT_RAG_VISION_MODEL
    vision_prompt: str = DEFAULT_RAG_VISION_PROMPT
    vision_max_tokens: int = 512


# 配置关键词检索后端及 Elasticsearch 连接。
class KeywordSearchConfig(BaseModel):
    top_k: int = 12
    backend: str = "memory"
    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_index: str = "rag_chunks"


# 配置混合召回数量、排名融合和结构化元数据加权。
class HybridRetrievalConfig(BaseModel):
    rrf_rank_constant: int = 60
    structured_metadata_weight: float = 0.15


# 配置文档切块长度、重叠量等分块参数。
class ChunkingConfig(BaseModel):
    knowledge_base_paths: list[str] = Field(default_factory=lambda: ["data/knowledge"])
    chunk_size: int = 1200
    chunk_overlap: int = 200
    top_k: int = 4
    rerank_top_n: int = 20
    json_text_fields: Optional[list[str]] = None
    authority_rerank_enabled: bool = True
