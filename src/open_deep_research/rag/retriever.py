"""本地 RAG hybrid retriever。

本模块解决“召回”问题：给定 query，先找一批可能相关的 chunk。

当前采用 hybrid retrieval：

1. 向量召回：适合语义相近但字面不完全匹配的内容。
2. BM25 关键词召回：适合精确词、编号、术语、文件名、接口名等。
3. RRF 排名融合：避免不同召回分数尺度不一致，也减少单一路径漏召回。

rerank 不在这里做；本模块只负责生成候选和初步排序。
"""

import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from typing import Any

from open_deep_research.rag.graph import create_graph_index
from open_deep_research.rag.graph_terms import TermExtractor
from open_deep_research.rag.metadata import build_structured_context_text
from open_deep_research.rag.types import RAGChunk, RetrievalResult
from open_deep_research.rag.vectorstore import VectorStoreBackend

TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)


class BM25Index:
    """轻量 BM25 关键词索引。

    这里没有引入额外搜索引擎依赖，而是用一份小型内存索引覆盖本地 RAG
    的关键词召回需求。对于中小型本地知识库，这样足够轻量。
    """

    def __init__(self, chunks: list[RAGChunk], k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        # BM25 同时索引标题和正文。标题往往包含章节名/政策名/对象名，
        # 对关键词召回很有帮助。
        self.docs = [
            _tokenize(f"{chunk.title or ''} {chunk.content}") for chunk in chunks
        ]
        self.avgdl = sum(len(doc) for doc in self.docs) / max(1, len(self.docs))
        self.idf = self._build_idf()

    def search(self, query: str, top_k: int) -> list[RetrievalResult]:
        """用 BM25 分数检索关键词相关 chunk。"""
        query_terms = _tokenize(query)
        scored = []
        for chunk, doc_terms in zip(self.chunks, self.docs):
            score = self._score(query_terms, doc_terms)
            if score > 0:
                scored.append(
                    RetrievalResult(chunk=chunk, score=score, keyword_score=score)
                )
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:top_k]

    def _build_idf(self) -> dict[str, float]:
        """计算每个 term 的 IDF。

        使用 BM25 常见的平滑公式，避免高频词权重过大，也避免除零。
        """
        doc_freq: dict[str, int] = defaultdict(int)
        for doc in self.docs:
            for term in set(doc):
                doc_freq[term] += 1

        total_docs = max(1, len(self.docs))
        return {
            term: math.log(1 + (total_docs - freq + 0.5) / (freq + 0.5))
            for term, freq in doc_freq.items()
        }

    def _score(self, query_terms: list[str], doc_terms: list[str]) -> float:
        """计算单个文档相对 query 的 BM25 分数。"""
        if not query_terms or not doc_terms:
            return 0.0
        term_counts = Counter(doc_terms)
        doc_len = len(doc_terms)
        score = 0.0
        for term in query_terms:
            freq = term_counts.get(term, 0)
            if freq == 0:
                continue
            denominator = freq + self.k1 * (1 - self.b + self.b * doc_len / self.avgdl)
            score += self.idf.get(term, 0.0) * freq * (self.k1 + 1) / denominator
        return score


class HybridChunkRetriever:
    """融合向量召回和 BM25 召回的检索器。

    Dense 和 BM25 的候选使用 RRF 融合，后续再交给 reranker 精排。
    """

    def __init__(
        self,
        vectorstore: VectorStoreBackend,
        keyword_index: Any,
        rrf_rank_constant: int = 60,
        structured_metadata_weight: float = 0.15,
        graph_enabled: bool = False,
        graph_backend: str = "memory",
        graph_index_id: str = "default",
        graph_max_neighbors: int = 4,
        graph_weight: float = 0.35,
        graph_ner_enabled: bool = True,
        graph_idf_enabled: bool = True,
        graph_idf_threshold_percentile: float = 85.0,
        graph_confidence_threshold: float = 0.15,
        structural_edges_enabled: bool = False,
        neo4j_uri: str | None = None,
        neo4j_username: str | None = None,
        neo4j_password: str | None = None,
        neo4j_database: str | None = None,
        graph_driver_factory: Callable[..., Any] | None = None,
    ):
        self.vectorstore = vectorstore
        self.keyword_index = keyword_index
        self.rrf_rank_constant = max(1, rrf_rank_constant)
        self.structured_metadata_weight = max(0.0, structured_metadata_weight)
        self.graph_max_neighbors = max(0, graph_max_neighbors)
        self.graph_weight = max(0.0, graph_weight)
        chunks = getattr(keyword_index, "chunks", [])
        term_extractor: TermExtractor | None = None
        if graph_enabled and chunks:
            term_extractor = TermExtractor(
                chunks=[f"{c.title or ''}\n{c.content}" for c in chunks],
                ner_enabled=graph_ner_enabled,
                idf_enabled=graph_idf_enabled,
                idf_threshold_percentile=graph_idf_threshold_percentile,
            )
        self.graph_index = (
            create_graph_index(
                chunks,
                backend=graph_backend,
                index_id=graph_index_id,
                neo4j_uri=neo4j_uri,
                neo4j_username=neo4j_username,
                neo4j_password=neo4j_password,
                neo4j_database=neo4j_database,
                driver_factory=graph_driver_factory,
                term_extractor=term_extractor,
                graph_confidence_threshold=graph_confidence_threshold,
                structural_edges_enabled=structural_edges_enabled,
            )
            if graph_enabled and chunks
            else None
        )

    def retrieve(
        self,
        query: str,
        query_vector: list[float],
        top_k: int,
        keyword_top_k: int,
    ) -> list[RetrievalResult]:
        """分别召回向量候选和关键词候选，并融合排序。"""
        vector_results = self.vectorstore.search(query_vector, top_k=top_k)
        keyword_results = self.keyword_index.search(query, top_k=keyword_top_k)
        fused_results = reciprocal_rank_fusion(
            vector_results,
            keyword_results,
            rank_constant=self.rrf_rank_constant,
        )
        if self.graph_index is not None:
            fused_results = self.graph_index.expand(
                query=query,
                seed_results=fused_results,
                max_neighbors=self.graph_max_neighbors,
                graph_weight=self.graph_weight,
            )
        fused_results = apply_structured_metadata_boost(
            query,
            fused_results,
            weight=self.structured_metadata_weight,
        )
        return fused_results[:top_k]


def reciprocal_rank_fusion(
    vector_results: list[RetrievalResult],
    keyword_results: list[RetrievalResult],
    rank_constant: int = 60,
) -> list[RetrievalResult]:
    """Use RRF to fuse dense vector and BM25 rankings by rank position."""
    rank_constant = max(1, rank_constant)
    chunks = {
        item.chunk.chunk_id: item.chunk
        for item in [*vector_results, *keyword_results]
    }
    rrf_scores: dict[str, float] = defaultdict(float)
    vector_scores: dict[str, float] = {}
    keyword_scores: dict[str, float] = {}

    for rank, item in enumerate(vector_results, start=1):
        chunk_id = item.chunk.chunk_id
        rrf_scores[chunk_id] += 1 / (rank_constant + rank)
        vector_scores[chunk_id] = (
            item.vector_score if item.vector_score is not None else item.score
        )

    for rank, item in enumerate(keyword_results, start=1):
        chunk_id = item.chunk.chunk_id
        rrf_scores[chunk_id] += 1 / (rank_constant + rank)
        keyword_scores[chunk_id] = (
            item.keyword_score if item.keyword_score is not None else item.score
        )

    fused = []
    for chunk_id, chunk in chunks.items():
        fused.append(
            RetrievalResult(
                chunk=chunk,
                score=rrf_scores[chunk_id],
                vector_score=vector_scores.get(chunk_id),
                keyword_score=keyword_scores.get(chunk_id),
            )
        )

    fused.sort(key=lambda item: item.score, reverse=True)
    return fused


def apply_structured_metadata_boost(
    query: str,
    results: list[RetrievalResult],
    *,
    weight: float,
) -> list[RetrievalResult]:
    """Add a small score boost when query terms match chunk metadata."""
    weight = max(0.0, weight)
    if weight <= 0 or not results:
        return results
    query_terms = set(_tokenize(query))
    if not query_terms:
        return results

    boosted_results = []
    for result in results:
        structured_score = _structured_metadata_score(query_terms, result.chunk)
        boosted_results.append(
            result.model_copy(
                update={
                    "score": result.score + (weight * structured_score),
                    "structured_score": structured_score,
                }
            )
        )
    boosted_results.sort(key=lambda item: item.score, reverse=True)
    return boosted_results


def _structured_metadata_score(query_terms: set[str], chunk: RAGChunk) -> float:
    structured_context = build_structured_context_text(
        source=chunk.source,
        title=chunk.title,
        metadata=chunk.metadata,
    )
    metadata_terms = set(_tokenize(structured_context))
    if not query_terms or not metadata_terms:
        return 0.0
    return len(query_terms & metadata_terms) / len(query_terms)


def _tokenize(text: str) -> list[str]:
    """用于 BM25 和 simple reranker 的轻量 tokenizer。

    英文等空格语言保留完整 word token；中文/CJK token 会拆成单字，
    这样即使没有中文分词器，也能获得基础关键词召回能力。
    """
    tokens = []
    for token in TOKEN_PATTERN.findall(text.lower()):
        if any(_is_cjk(char) for char in token):
            tokens.extend(char for char in token if _is_cjk(char))
        else:
            tokens.append(token)
    return tokens


def _is_cjk(char: str) -> bool:
    """判断字符是否属于常见 CJK 统一表意文字范围。"""
    return "\u4e00" <= char <= "\u9fff"
