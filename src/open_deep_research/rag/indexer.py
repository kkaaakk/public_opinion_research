"""RAG indexing boundary.

The indexer owns data ingestion: local documents, optional file memories, and
indexable MySQL memory are all normalized to `RAGDocument`, split to chunks, and
written to the configured vector store. The query pipeline only consumes the
ready retriever.
"""

import threading
from typing import TYPE_CHECKING, Any, Sequence

from open_deep_research.memory.store import MySQLChatMemoryStore
from open_deep_research.rag.elasticsearch_bm25 import ElasticsearchBM25Index
from open_deep_research.rag.embeddings import create_embedding_backend
from open_deep_research.rag.loaders import (
    fingerprint_knowledge_base_paths,
    load_documents_from_paths,
)
from open_deep_research.rag.loaders.mysql_memory import (
    fingerprint_mysql_memory,
    load_memory_documents_from_mysql,
)
from open_deep_research.rag.memory import (
    fingerprint_memory_paths,
    load_memory_documents_from_paths,
)
from open_deep_research.rag.retriever import BM25Index, HybridChunkRetriever
from open_deep_research.rag.splitter import split_documents
from open_deep_research.rag.types import RAGChunk, RAGDocument
from open_deep_research.rag.vectorstore import create_vectorstore_backend

if TYPE_CHECKING:
    from open_deep_research.rag.service import RAGPipelineConfig


class RAGIndexer:
    """Build and refresh the retrieval index for a RAG pipeline."""

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

    def ensure_ready(self, force: bool = False) -> None:
        """Load sources, build chunk indexes, and sync pending MySQL memory."""
        source_fingerprint = self.source_fingerprint()
        if self._ready and not force and source_fingerprint == self._source_fingerprint:
            return

        with self._lock:
            source_fingerprint = self.source_fingerprint()
            if self._ready and not force and source_fingerprint == self._source_fingerprint:
                return

            chunking = self.config.chunking
            hybrid = self.config.hybrid_retrieval
            graph = self.config.graph_rag
            keyword = self.config.keyword_search

            self.documents = self.load_indexable_documents()
            pending_memory_ids = self._pending_memory_ids(self.documents)
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
                graph_enabled=graph.enabled,
                graph_backend=graph.backend,
                graph_index_id=self.index_id,
                graph_max_neighbors=graph.max_neighbors,
                graph_weight=graph.weight,
                graph_ner_enabled=graph.ner_enabled,
                graph_idf_enabled=graph.idf_enabled,
                graph_idf_threshold_percentile=graph.idf_threshold_percentile,
                graph_confidence_threshold=graph.confidence_threshold,
                structural_edges_enabled=graph.structural_edges_enabled,
                neo4j_uri=graph.neo4j_uri,
                neo4j_username=graph.neo4j_username,
                neo4j_password=graph.neo4j_password,
                neo4j_database=graph.neo4j_database,
            )
            self._mark_pending_memories_indexed(pending_memory_ids)
            self._source_fingerprint = self.source_fingerprint()
            self._ready = True

    def index_pending_memories(self) -> None:
        """Refresh the full index for a separate indexing job.

        The implementation refreshes the full local index so file documents and
        MySQL memories remain consistent in one collection.
        """
        self.ensure_ready(force=True)

    def load_indexable_documents(self) -> list[RAGDocument]:
        """Load all data sources and normalize them to RAG documents."""
        chunking = self.config.chunking
        multimodal = self.config.multimodal
        memory = self.config.memory
        documents: list[RAGDocument] = []
        if chunking.knowledge_base_paths:
            documents.extend(
                load_documents_from_paths(
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
            )
        if memory.enabled and memory.paths:
            documents.extend(
                load_memory_documents_from_paths(
                    memory.paths,
                    json_text_fields=memory.json_text_fields,
                )
            )
        if memory.enabled and memory.mysql_url:
            documents.extend(
                load_memory_documents_from_mysql(
                    database_url=memory.mysql_url,
                    table_name=memory.mysql_table,
                    conversation_id=memory.conversation_id,
                    user_id=memory.user_id,
                    limit=memory.mysql_limit,
                    record_types=memory.mysql_index_record_types,
                )
            )
        return documents

    def source_fingerprint(self) -> dict[str, Any]:
        """Fingerprint source content for refresh decisions, not index naming."""
        chunking = self.config.chunking
        multimodal = self.config.multimodal
        memory = self.config.memory
        return {
            "knowledge_base": fingerprint_knowledge_base_paths(
                chunking.knowledge_base_paths,
                include_multimodal=multimodal.enabled,
            ),
            "file_memory": fingerprint_memory_paths(
                memory.paths if memory.enabled else []
            ),
            "mysql_memory": fingerprint_mysql_memory(
                database_url=(
                    memory.mysql_url if memory.enabled else None
                ),
                table_name=memory.mysql_table,
                conversation_id=memory.conversation_id,
                user_id=memory.user_id,
                record_types=memory.mysql_index_record_types,
            ),
        }

    @staticmethod
    def _pending_memory_ids(documents: Sequence[RAGDocument]) -> list[str]:
        """Snapshot pending MySQL memory ids included in the current document batch."""
        memory_ids = [
            str(document.metadata["memory_id"])
            for document in documents
            if document.metadata.get("memory_backend") == "mysql"
            and document.metadata.get("index_status") == "pending"
            and document.metadata.get("memory_id")
        ]
        return list(dict.fromkeys(memory_ids))

    def _mark_pending_memories_indexed(self, memory_ids: Sequence[str]) -> None:
        """Mark only the MySQL memory ids that were indexed in this batch."""
        if not memory_ids:
            return
        memory = self.config.memory
        if not (memory.enabled and memory.mysql_url):
            return
        store = MySQLChatMemoryStore(
            database_url=memory.mysql_url,
            table_name=memory.mysql_table,
        )
        store.mark_records_indexed(memory_ids)
