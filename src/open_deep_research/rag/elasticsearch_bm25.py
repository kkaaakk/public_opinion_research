"""Elasticsearch-backed keyword/BM25 index for RAG chunks."""

from typing import TYPE_CHECKING, Any

from open_deep_research.rag.types import RAGChunk, RetrievalResult

if TYPE_CHECKING:
    from open_deep_research.rag.config import KeywordSearchConfig


class ElasticsearchBM25Index:
    """Small adapter that exposes the same `search(query, top_k)` shape as BM25Index."""

    def __init__(self, chunks: list[RAGChunk], config: "KeywordSearchConfig"):
        self.chunks = chunks
        self.url = config.elasticsearch_url
        self.index_name = config.elasticsearch_index
        try:
            from elasticsearch import Elasticsearch
        except ImportError as exc:  # pragma: no cover - optional dependency guard
            raise ImportError(
                "Install the official `elasticsearch` Python package to use "
                "keyword_backend='elasticsearch'."
            ) from exc

        try:
            self.client = Elasticsearch(self.url)
            if not self.client.ping():
                raise ConnectionError("Elasticsearch ping returned false.")
            self._ensure_index()
            self._index_chunks(chunks)
        except Exception as exc:
            raise ConnectionError(
                f"Failed to initialize Elasticsearch keyword index "
                f"'{self.index_name}' at {self.url}: {exc}"
            ) from exc

    def search(self, query: str, top_k: int) -> list[RetrievalResult]:
        """Search chunks with Elasticsearch BM25 over the `content` field."""
        if not query.strip() or top_k <= 0:
            return []
        response = self.client.search(
            index=self.index_name,
            size=top_k,
            query={"match": {"content": query}},
        )
        results = []
        for hit in response.get("hits", {}).get("hits", []):
            source = hit.get("_source", {})
            score = float(hit.get("_score") or 0.0)
            chunk = RAGChunk(
                chunk_id=source.get("chunk_id") or hit.get("_id"),
                source=source.get("source") or source.get("source_id") or "",
                title=source.get("title"),
                content=source.get("content") or "",
                metadata=source.get("metadata") or {},
            )
            results.append(
                RetrievalResult(chunk=chunk, score=score, keyword_score=score)
            )
        return results

    def delete_by_source_id(self, source_id: str) -> None:
        """Delete indexed chunks for one source id."""
        if not source_id:
            return
        self.client.delete_by_query(
            index=self.index_name,
            query={"term": {"source_id": source_id}},
            conflicts="proceed",
            refresh=True,
        )

    def _ensure_index(self) -> None:
        if self.client.indices.exists(index=self.index_name):
            return
        self.client.indices.create(
            index=self.index_name,
            mappings={
                "properties": {
                    "chunk_id": {"type": "keyword"},
                    "source_id": {"type": "keyword"},
                    "source": {"type": "keyword"},
                    "title": {"type": "text"},
                    "content": {"type": "text"},
                    "metadata": {"type": "object", "enabled": False},
                }
            },
        )

    def _index_chunks(self, chunks: list[RAGChunk]) -> None:
        if not chunks:
            return
        try:
            from elasticsearch import helpers
        except ImportError as exc:  # pragma: no cover - guarded in __init__
            raise ImportError("Install `elasticsearch` to use Elasticsearch BM25.") from exc

        source_ids = {chunk.source for chunk in chunks if chunk.source}
        for source_id in source_ids:
            self.delete_by_source_id(source_id)

        actions = [
            {
                "_op_type": "index",
                "_index": self.index_name,
                "_id": chunk.chunk_id,
                "_source": {
                    "chunk_id": chunk.chunk_id,
                    "source_id": chunk.source,
                    "source": chunk.source,
                    "title": chunk.title,
                    "content": chunk.content,
                    "metadata": _jsonable_metadata(chunk.metadata),
                },
            }
            for chunk in chunks
        ]
        helpers.bulk(self.client, actions, refresh=True)


def _jsonable_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Keep metadata storable even when callers put non-JSON objects in it."""
    import json

    return json.loads(json.dumps(metadata or {}, ensure_ascii=False, default=str))

