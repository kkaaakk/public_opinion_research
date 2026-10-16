"""本地 RAG 向量库后端。

本模块负责保存 chunk 向量并根据 query 向量做相似度搜索。当前支持：

- `InMemoryVectorStore`：内存向量库，适合测试和临时实验。
- `FaissVectorStore`：本地持久化 FAISS index，加 JSON sidecar 保存 chunk。
- `ChromaVectorStore`：本地持久化 Chroma collection，作为兼容性备选后端。

统一接口是 `VectorStoreBackend`，这样 service/retriever 不需要关心底层使用
哪种向量库。
"""

import json
import math
import re
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from open_deep_research.rag.types import RAGChunk, RetrievalResult

if TYPE_CHECKING:
    from open_deep_research.rag.config import VectorstoreConfig


class VectorStoreBackend(ABC):
    """向量库抽象接口。"""

    @abstractmethod
    def is_ready(self) -> bool:
        """判断当前后端是否已有可查询索引。"""

    @abstractmethod
    def add(self, chunks: list[RAGChunk], vectors: list[list[float]]) -> None:
        """写入 chunk 和对应向量。"""

    @abstractmethod
    def search(self, query_vector: list[float], top_k: int) -> list[RetrievalResult]:
        """根据 query 向量返回 top_k 个相似 chunk。"""


class InMemoryVectorStore(VectorStoreBackend):
    """简单内存向量库。

    不做持久化，进程结束即丢失。优点是无依赖、速度快、行为确定，
    所以单测默认使用它。
    """

    def __init__(self):
        self._records: list[tuple[RAGChunk, list[float]]] = []

    def is_ready(self) -> bool:
        """判断内存中是否已有记录。"""
        return bool(self._records)

    def add(self, chunks: list[RAGChunk], vectors: list[list[float]]) -> None:
        """将 chunk 和向量以 tuple 形式存入内存。"""
        if len(chunks) != len(vectors):
            raise ValueError("Chunk and vector counts must match.")
        self._records = list(zip(chunks, vectors))

    def search(self, query_vector: list[float], top_k: int) -> list[RetrievalResult]:
        """用余弦相似度扫描所有内存记录并排序。"""
        if not self._records or not query_vector:
            return []

        scored_results = []
        for chunk, vector in self._records:
            vector_score = _cosine_similarity(query_vector, vector)
            scored_results.append(
                RetrievalResult(chunk=chunk, score=vector_score, vector_score=vector_score)
            )
        scored_results.sort(key=lambda result: result.score, reverse=True)
        return scored_results[:top_k]


class FaissVectorStore(VectorStoreBackend):
    """持久化 FAISS 向量库。

    FAISS 只保存向量 index，不保存完整 chunk 对象。因此这里额外维护
    `chunks.json` sidecar，用相同顺序保存 chunk。查询时 FAISS 返回向量下标，
    再用下标从 sidecar 加载的 `_chunks` 中找回原文和 metadata。
    """

    def __init__(self, persist_path: str, index_id: str):
        # 每个 index_id 使用独立目录。index_id 已包含配置和文件 fingerprint，
        # 因此知识库变化后不会误读旧索引。
        self.store_dir = Path(persist_path).expanduser() / "faiss" / index_id
        self.index_path = self.store_dir / "index.faiss"
        self.chunks_path = self.store_dir / "chunks.json"
        self._index = None
        self._chunks: list[RAGChunk] = []
        self._load()

    def is_ready(self) -> bool:
        """判断 FAISS index 和 sidecar chunk 是否都可用。"""
        return self._index is not None and bool(self._chunks)

    def add(self, chunks: list[RAGChunk], vectors: list[list[float]]) -> None:
        """将向量写入 FAISS，并将 chunk 写入 JSON sidecar。"""
        if not chunks:
            return
        import faiss
        import numpy as np

        # embedding backend 默认会归一化向量，因此 IndexFlatIP 等价于 cosine 排序。
        matrix = np.asarray(vectors, dtype="float32")
        index = faiss.IndexFlatIP(matrix.shape[1])
        index.add(matrix)

        self.store_dir.mkdir(parents=True, exist_ok=True)
        faiss.write_index(index, str(self.index_path))
        self.chunks_path.write_text(
            json.dumps([chunk.model_dump() for chunk in chunks], ensure_ascii=False),
            encoding="utf-8",
        )
        self._index = index
        self._chunks = chunks

    def search(self, query_vector: list[float], top_k: int) -> list[RetrievalResult]:
        """查询 FAISS index，并把返回下标映射回 chunk。"""
        if not self.is_ready() or not query_vector:
            return []
        import numpy as np

        query = np.asarray([query_vector], dtype="float32")
        scores, indexes = self._index.search(query, min(top_k, len(self._chunks)))
        results = []
        for score, index in zip(scores[0], indexes[0]):
            if index < 0:
                continue
            vector_score = float(score)
            results.append(
                RetrievalResult(
                    chunk=self._chunks[int(index)],
                    score=vector_score,
                    vector_score=vector_score,
                )
            )
        return results

    def _load(self) -> None:
        """尝试从磁盘加载 FAISS index 和 chunk sidecar。

        加载失败时重置为空状态，后续 `_ensure_indexed()` 会重新写入索引。
        """
        if not self.index_path.exists() or not self.chunks_path.exists():
            return
        try:
            import faiss

            self._index = faiss.read_index(str(self.index_path))
            raw_chunks = json.loads(self.chunks_path.read_text(encoding="utf-8"))
            self._chunks = [RAGChunk.model_validate(chunk) for chunk in raw_chunks]
        except Exception:
            self._index = None
            self._chunks = []


class ChromaVectorStore(VectorStoreBackend):
    """持久化 Chroma 向量库。

    Chroma 自带持久化和 metadata 存储。这里将完整 chunk 序列化到 metadata
    的 `chunk` 字段中，查询返回后再反序列化成 `RAGChunk`。
    """

    def __init__(self, persist_path: str, collection_name: str, index_id: str):
        try:
            # Chroma 也是可选重依赖，只在选中该 provider 时导入。
            import chromadb
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ImportError("Install chromadb to use the Chroma RAG vector store.") from exc

        self.client = chromadb.PersistentClient(path=str(Path(persist_path).expanduser()))
        self.collection = self.client.get_or_create_collection(
            name=_clean_collection_name(f"{collection_name}_{index_id[:16]}"),
            metadata={"hnsw:space": "cosine"},
        )

    def is_ready(self) -> bool:
        """判断 Chroma collection 中是否已有记录。"""
        return self.collection.count() > 0

    def add(self, chunks: list[RAGChunk], vectors: list[list[float]]) -> None:
        """将向量、文本和序列化 chunk upsert 到 Chroma。"""
        if not chunks:
            return
        self._delete_existing_records()
        ids = [chunk.chunk_id for chunk in chunks]
        self.collection.upsert(
            ids=ids,
            embeddings=vectors,
            documents=[chunk.content for chunk in chunks],
            metadatas=[{"chunk": json.dumps(chunk.model_dump(), ensure_ascii=False)} for chunk in chunks],
        )

    def search(self, query_vector: list[float], top_k: int) -> list[RetrievalResult]:
        """查询 Chroma collection 并转换成统一 RetrievalResult。"""
        if not self.is_ready() or not query_vector:
            return []
        response = self.collection.query(
            query_embeddings=[query_vector],
            n_results=top_k,
            include=["metadatas", "distances"],
        )
        metadatas = (response.get("metadatas") or [[]])[0]
        distances = (response.get("distances") or [[]])[0]
        results = []
        for metadata, distance in zip(metadatas, distances):
            chunk = RAGChunk.model_validate(json.loads(metadata["chunk"]))
            vector_score = 1.0 - float(distance)
            results.append(
                RetrievalResult(chunk=chunk, score=vector_score, vector_score=vector_score)
            )
        return results

    def _delete_existing_records(self) -> None:
        """Clear stale chunks before writing a refreshed chunk set."""
        if self.collection.count() <= 0:
            return
        existing = self.collection.get()
        ids = existing.get("ids") or []
        if ids:
            self.collection.delete(ids=ids)


class MilvusVectorStore(VectorStoreBackend):
    """Milvus-backed vector store for persistent local RAG indexes."""

    def __init__(
        self,
        persist_path: str,
        collection_name: str,
        index_id: str,
        uri: str | None = None,
        token: str | None = None,
        db_name: str | None = None,
        metric_type: str = "COSINE",
    ):
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ImportError("Install pymilvus to use the Milvus RAG vector store.") from exc

        self._client_class = MilvusClient
        self.uri = uri or _milvus_uri_from_path(persist_path)
        self.collection_name = _clean_collection_name(f"{collection_name}_{index_id[:16]}")
        self.metric_type = metric_type.upper().strip() or "COSINE"
        _ensure_local_milvus_parent(self.uri)

        client_kwargs = {"uri": self.uri}
        if token:
            client_kwargs["token"] = token
        if db_name:
            client_kwargs["db_name"] = db_name
        self.client = MilvusClient(**client_kwargs)

    def is_ready(self) -> bool:
        """Return whether the Milvus collection exists and has rows."""
        if not self.client.has_collection(self.collection_name):
            return False
        try:
            stats = self.client.get_collection_stats(self.collection_name)
            row_count = stats.get("row_count", 0) if isinstance(stats, dict) else 0
            return int(row_count) > 0
        except Exception:  # pragma: no cover - older client fallback
            return True

    def add(self, chunks: list[RAGChunk], vectors: list[list[float]]) -> None:
        """Refresh the Milvus collection with chunk vectors and serialized chunks."""
        if len(chunks) != len(vectors):
            raise ValueError("Chunk and vector counts must match.")
        if not chunks:
            return
        dimension = len(vectors[0])
        if dimension <= 0:
            raise ValueError("Milvus vectors must have at least one dimension.")
        if any(len(vector) != dimension for vector in vectors):
            raise ValueError("All Milvus vectors must have the same dimension.")

        self._drop_existing_collection()
        self._create_collection(dimension=dimension)
        rows = [
            {
                "id": index,
                "chunk_id": chunk.chunk_id,
                "embedding": vector,
                "chunk_json": json.dumps(chunk.model_dump(), ensure_ascii=False),
                "content": chunk.content[:65535],
                "source": chunk.source[:2048],
                "title": (chunk.title or "")[:2048],
            }
            for index, (chunk, vector) in enumerate(zip(chunks, vectors))
        ]
        self.client.insert(collection_name=self.collection_name, data=rows)

    def search(self, query_vector: list[float], top_k: int) -> list[RetrievalResult]:
        """Search Milvus and convert hits into RetrievalResult objects."""
        if not self.is_ready() or not query_vector:
            return []
        response = self.client.search(
            collection_name=self.collection_name,
            data=[query_vector],
            anns_field="embedding",
            limit=top_k,
            output_fields=["chunk_json"],
            search_params={"metric_type": self.metric_type},
        )
        hits = response[0] if response else []
        results = []
        for hit in hits:
            entity = _milvus_hit_entity(hit)
            chunk_json = entity.get("chunk_json")
            if not chunk_json:
                continue
            vector_score = float(_milvus_hit_score(hit))
            results.append(
                RetrievalResult(
                    chunk=RAGChunk.model_validate(json.loads(chunk_json)),
                    score=vector_score,
                    vector_score=vector_score,
                )
            )
        return results

    def _drop_existing_collection(self) -> None:
        if self.client.has_collection(self.collection_name):
            self.client.drop_collection(self.collection_name)

    def _create_collection(self, dimension: int) -> None:
        self._cleanup_partial_collection()
        if "://" not in self.uri:
            self._create_collection_quick(dimension=dimension)
            return
        try:
            schema = self._client_class.create_schema(
                auto_id=False,
                enable_dynamic_field=False,
            )
            from pymilvus import DataType

            schema.add_field(field_name="id", datatype=DataType.INT64, is_primary=True)
            schema.add_field(
                field_name="embedding",
                datatype=DataType.FLOAT_VECTOR,
                dim=dimension,
            )
            schema.add_field(
                field_name="chunk_id",
                datatype=DataType.VARCHAR,
                max_length=512,
            )
            schema.add_field(
                field_name="chunk_json",
                datatype=DataType.VARCHAR,
                max_length=65535,
            )
            schema.add_field(
                field_name="content",
                datatype=DataType.VARCHAR,
                max_length=65535,
            )
            schema.add_field(
                field_name="source",
                datatype=DataType.VARCHAR,
                max_length=2048,
            )
            schema.add_field(
                field_name="title",
                datatype=DataType.VARCHAR,
                max_length=2048,
            )
            index_params = self.client.prepare_index_params()
            index_params.add_index(
                field_name="embedding",
                index_type="AUTOINDEX",
                metric_type=self.metric_type,
            )
            self.client.create_collection(
                collection_name=self.collection_name,
                schema=schema,
                index_params=index_params,
            )
        except Exception:
            # Milvus Lite / older pymilvus clients support the quick-create API.
            self._cleanup_partial_collection()
            self._create_collection_quick(dimension=dimension)

    def _create_collection_quick(self, dimension: int) -> None:
        self.client.create_collection(
            collection_name=self.collection_name,
            dimension=dimension,
            primary_field_name="id",
            vector_field_name="embedding",
            metric_type=self.metric_type,
            auto_id=False,
        )

    def _cleanup_partial_collection(self) -> None:
        if "://" in self.uri:
            return
        collection_dir = Path(self.uri).expanduser() / "collections" / self.collection_name
        if collection_dir.exists():
            shutil.rmtree(collection_dir, ignore_errors=True)


def create_vectorstore_backend(
    provider: str,
    persist_path: str,
    collection_name: str,
    index_id: str,
    milvus_uri: str | None = None,
    milvus_token: str | None = None,
    milvus_db_name: str | None = None,
    milvus_metric_type: str = "COSINE",
    *,
    config: "VectorstoreConfig | None" = None,
) -> VectorStoreBackend:
    """根据配置创建向量库后端。

    可以传入 VectorstoreConfig 对象，也可以传入分散的参数。当 ``config``
    不为 ``None`` 时，会用 config 的值覆盖位置参数传入的值。``index_id``
    仍需由调用方显式传入，因为它不属于向量库自身的配置。
    """
    if config is not None:
        provider = config.provider
        persist_path = config.persist_path
        collection_name = config.collection_name
        milvus_uri = config.milvus_uri
        milvus_token = config.milvus_token
        milvus_db_name = config.milvus_db_name
        milvus_metric_type = config.milvus_metric_type
    normalized_provider = provider.lower().strip()
    if normalized_provider == "memory":
        return InMemoryVectorStore()
    if normalized_provider == "faiss":
        return FaissVectorStore(persist_path=persist_path, index_id=index_id)
    if normalized_provider == "chroma":
        return ChromaVectorStore(
            persist_path=persist_path,
            collection_name=collection_name,
            index_id=index_id,
        )
    if normalized_provider == "milvus":
        return MilvusVectorStore(
            persist_path=persist_path,
            collection_name=collection_name,
            index_id=index_id,
            uri=milvus_uri,
            token=milvus_token,
            db_name=milvus_db_name,
            metric_type=milvus_metric_type,
        )
    raise ValueError(
        f"Unsupported RAG vector store provider '{provider}'. "
        "Use one of: memory, faiss, milvus, chroma."
    )


def _cosine_similarity(left_vector: list[float], right_vector: list[float]) -> float:
    """计算两个向量的余弦相似度。"""
    if len(left_vector) != len(right_vector):
        raise ValueError("Vector dimensions must match for similarity search.")

    left_norm = math.sqrt(sum(component * component for component in left_vector))
    right_norm = math.sqrt(sum(component * component for component in right_vector))
    if left_norm == 0 or right_norm == 0:
        return 0.0

    dot_product = sum(left * right for left, right in zip(left_vector, right_vector))
    return dot_product / (left_norm * right_norm)


def _clean_collection_name(value: str) -> str:
    """Clean a vector-store collection name.

    The same conservative shape works for Milvus and Chroma.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", value)
    return cleaned[:63].strip("_") or "open_deep_research"


def _milvus_uri_from_path(persist_path: str) -> str:
    """Interpret the existing vectorstore path as a Milvus URI.

    Server deployments can pass `http://host:19530`; local development uses
    the directory-like `data/indexes/rag`, which becomes
    `data/indexes/rag/milvus.db` for Milvus Lite. Legacy `.rag_index` paths
    still work when explicitly configured.
    """
    stripped = str(persist_path).strip()
    if "://" in stripped or stripped.endswith(".db"):
        return stripped
    return str(Path(stripped).expanduser() / "milvus.db")


def _ensure_local_milvus_parent(uri: str) -> None:
    if "://" in uri:
        return
    Path(uri).expanduser().parent.mkdir(parents=True, exist_ok=True)


def _milvus_hit_entity(hit) -> dict:
    if isinstance(hit, dict):
        return hit.get("entity") or {}
    return getattr(hit, "entity", {}) or {}


def _milvus_hit_score(hit) -> float:
    if isinstance(hit, dict):
        return hit.get("distance", hit.get("score", 0.0))
    return getattr(hit, "distance", getattr(hit, "score", 0.0))
