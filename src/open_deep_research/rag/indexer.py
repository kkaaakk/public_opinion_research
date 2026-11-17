# 负责加载企业知识文档、分块并建立混合召回索引。
"""RAG indexing boundary.

The indexer owns data ingestion: knowledge documents are normalized to
`RAGDocument`, split to chunks, and written to the configured vector store. The
query pipeline only consumes the ready retriever.
"""

import threading
from typing import TYPE_CHECKING, Any

from open_deep_research.rag.elasticsearch_bm25 import ElasticsearchBM25Index
from open_deep_research.rag.embeddings import create_embedding_backend
from open_deep_research.rag.loaders import (
    fingerprint_knowledge_base_paths,
    load_documents_from_paths,
)
from open_deep_research.rag.retriever import BM25Index, HybridChunkRetriever
from open_deep_research.rag.splitter import split_documents
from open_deep_research.rag.types import RAGChunk, RAGDocument
from open_deep_research.rag.vectorstore import create_vectorstore_backend

if TYPE_CHECKING:
    from open_deep_research.rag.service import RAGPipelineConfig


# 封装本地 RAG 索引初始化和刷新，供查询管线复用。
class RAGIndexer:
    """Build and refresh the retrieval index for a RAG pipeline."""

    # 绑定索引依赖并初始化空的文档与检索状态。
    def __init__(self, config: "RAGPipelineConfig", index_id: str):
        """Initialize index dependencies and empty in-memory index state."""
        self.config = config
        self.index_id = index_id
        emb_config = config.embedding
        self.embedding_backend = create_embedding_backend(
            provider=emb_config.provider,
            model_name=emb_config.model,
            device=emb_config.device,
            hash_dimensions=emb_config.hash_dimensions,
        )
        vs_config = config.vectorstore
        self.vectorstore = create_vectorstore_backend(
            provider=vs_config.provider,
            persist_path=vs_config.persist_path,
            collection_name=vs_config.collection_name,
            index_id=index_id,
            milvus_uri=vs_config.milvus_uri,
            milvus_token=vs_config.milvus_token,
            milvus_db_name=vs_config.milvus_db_name,
            milvus_metric_type=vs_config.milvus_metric_type,
        )
        self.documents: list[RAGDocument] = []
        self.chunks: list[RAGChunk] = []
        self.keyword_index: Any | None = None
        self.retriever: HybridChunkRetriever | None = None
        self.last_vector_count = 0
        self._source_fingerprint: dict[str, Any] | None = None
        self._ready = False
        self._lock = threading.Lock()

    # 检查来源变化并构建分块索引。
    def ensure_ready(self, force: bool = False) -> None:
        """Load sources and build chunk indexes."""
        source_fingerprint = self.source_fingerprint()
        if self._ready and not force and source_fingerprint == self._source_fingerprint:
            return

        with self._lock:
            # 获取锁后再次检查，避免多个查询同时发现变化而重复重建索引。
            source_fingerprint = self.source_fingerprint()
            if self._ready and not force and source_fingerprint == self._source_fingerprint:
                return

            chunking = self.config.chunking
            hybrid = self.config.hybrid_retrieval
            keyword = self.config.keyword_search

            self.documents = self.load_indexable_documents()
            self.chunks = split_documents(
                self.documents,
                chunk_size=chunking.chunk_size,
                chunk_overlap=chunking.chunk_overlap,
            )

            if self.chunks:
                vectors = self.embedding_backend.embed_texts(
                    [chunk.content for chunk in self.chunks]
                )
                self.vectorstore.add(self.chunks, vectors)
                self.last_vector_count = len(vectors)
            else:
                self.last_vector_count = 0

            if keyword.backend.lower().strip() == "elasticsearch":
                self.keyword_index = ElasticsearchBM25Index(self.chunks, self.config)
            else:
                self.keyword_index = BM25Index(self.chunks)
            self.retriever = HybridChunkRetriever(
                vectorstore=self.vectorstore,
                keyword_index=self.keyword_index,
                rrf_rank_constant=hybrid.rrf_rank_constant,
                structured_metadata_weight=hybrid.structured_metadata_weight,
            )
            self._source_fingerprint = self.source_fingerprint()
            self._ready = True

    # 加载全部配置的企业知识来源，并转换为标准 RAG 文档。
    def load_indexable_documents(self) -> list[RAGDocument]:
        """Load configured knowledge sources and normalize them to RAG documents."""
        chunking = self.config.chunking
        multimodal = self.config.multimodal
        if not chunking.knowledge_base_paths:
            return []
        return load_documents_from_paths(
            chunking.knowledge_base_paths,
            json_text_fields=chunking.json_text_fields,
            multimodal_enabled=multimodal.enabled,
            multimodal_provider=multimodal.provider,
            ocr_languages=multimodal.ocr_languages,
            vision_enabled=multimodal.vision_enabled,
            vision_model=multimodal.vision_model,
            vision_prompt=multimodal.vision_prompt,
            vision_max_tokens=multimodal.vision_max_tokens,
        )

    # 计算来源内容指纹，用于判断是否刷新，不作为索引名称。
    def source_fingerprint(self) -> dict[str, Any]:
        """Fingerprint source content for refresh decisions, not index naming."""
        chunking = self.config.chunking
        multimodal = self.config.multimodal
        return {
            "knowledge_base": fingerprint_knowledge_base_paths(
                chunking.knowledge_base_paths,
                include_multimodal=multimodal.enabled,
            ),
        }
