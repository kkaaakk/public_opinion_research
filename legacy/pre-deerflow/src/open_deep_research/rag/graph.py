# 为混合检索结果提供内存或 Neo4j 共现图邻居扩展。
"""Lightweight GraphRAG expansion for local RAG chunks.

This module builds an in-process chunk graph from shared competitor/product
terms. It is intentionally small: the graph does not replace vector/BM25
retrieval, it only expands strong seed hits to neighboring evidence that shares
important entities or phrases.
"""

from collections import defaultdict
from collections.abc import Callable
from typing import Any

from open_deep_research.rag.graph_terms import TermExtractor
from open_deep_research.rag.types import RAGChunk, RetrievalResult


# 延迟导入自适应扩展判断，避免模块初始化时的循环依赖。
def _adaptive_decision(
    query: str,
    seed_results: list[RetrievalResult],
    chunk_terms: dict[str, set[str]],
    term_to_chunk_ids: dict[str, set[str]],
) -> Any:
    """Lazy import to avoid circular dependency on startup."""
    from open_deep_research.rag.graph_adaptive import decide_expansion

    return decide_expansion(query, seed_results, chunk_terms, term_to_chunk_ids)


# 基于分块共享术语构建内存共现图，并扩展初始召回结果。
class GraphRAGIndex:
    """Co-occurrence graph over RAG chunks."""

    # 建立分块、术语和结构边索引，保存扩展与评分参数。
    def __init__(
        self,
        chunks: list[RAGChunk],
        *,
        term_extractor: TermExtractor | None = None,
        graph_confidence_threshold: float = 0.15,
        structural_edges_enabled: bool = False,
    ):
        self.chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        if term_extractor is None:
            term_extractor = TermExtractor(ner_enabled=False, idf_enabled=False)
        self.term_extractor = term_extractor
        self.graph_confidence_threshold = graph_confidence_threshold
        self.chunk_terms = {
            chunk.chunk_id: self.term_extractor.extract(
                f"{chunk.title or ''}\n{chunk.content}"
            )
            for chunk in chunks
        }
        self.term_to_chunk_ids: dict[str, set[str]] = defaultdict(set)
        for chunk_id, terms in self.chunk_terms.items():
            for term in terms:
                self.term_to_chunk_ids[term].add(chunk_id)
        self._last_expansion_decision: Any = None
        self._structural_index: Any = None
        if structural_edges_enabled:
            from open_deep_research.rag.graph_structured import (
                StructuralIndex,
                build_adjacent_edges,
                build_metadata_edges,
                build_source_edges,
            )

            all_edges = []
            all_edges.extend(build_source_edges(chunks))
            all_edges.extend(build_adjacent_edges(chunks))
            all_edges.extend(build_metadata_edges(chunks))
            self._structural_index = StructuralIndex(all_edges)

    # 从种子分块寻找图邻居，计算扩展分数并按分块 ID 合并去重。
    def expand(
        self,
        query: str,
        seed_results: list[RetrievalResult],
        max_neighbors: int,
        graph_weight: float,
        *,
        adaptive: bool = True,
    ) -> list[RetrievalResult]:
        """Expand seed results to graph neighbors and merge them by chunk id."""
        if not seed_results or max_neighbors <= 0 or graph_weight <= 0:
            return seed_results

        if adaptive:
            decision = _adaptive_decision(
                query, seed_results, self.chunk_terms, self.term_to_chunk_ids
            )
            self._last_expansion_decision = decision
            if not decision.should_expand:
                return seed_results

        query_terms = self.term_extractor.extract(query)
        merged = {result.chunk.chunk_id: result for result in seed_results}
        candidate_scores: dict[str, tuple[float, set[str]]] = {}

        for seed in seed_results:
            if seed.score <= 0:
                continue
            seed_id = seed.chunk.chunk_id
            seed_terms = self.chunk_terms.get(seed_id, set())
            expansion_terms = seed_terms | (seed_terms & query_terms)
            for term in expansion_terms:
                for neighbor_id in self.term_to_chunk_ids.get(term, set()):
                    if neighbor_id == seed_id:
                        continue
                    neighbor_terms = self.chunk_terms.get(neighbor_id, set())
                    shared_terms = seed_terms & neighbor_terms
                    if not shared_terms:
                        continue
                    from open_deep_research.rag.graph_scoring import compute_graph_score

                    final_score, confidence = compute_graph_score(
                        seed_score=seed.score,
                        shared_terms=shared_terms,
                        graph_weight=graph_weight,
                        term_extractor=self.term_extractor,
                    )
                    if confidence < self.graph_confidence_threshold:
                        continue
                    previous_score, previous_terms = candidate_scores.get(
                        neighbor_id,
                        (0.0, set()),
                    )
                    if final_score > previous_score:
                        candidate_scores[neighbor_id] = (final_score, shared_terms)
                    else:
                        previous_terms.update(shared_terms)

        # Structural edge expansion (supplementary, lower weight)
        if self._structural_index is not None:
            for seed in seed_results:
                seed_id = seed.chunk.chunk_id
                for neighbor_id, struct_weight, _edge_type in self._structural_index.neighbors(seed_id):
                    if neighbor_id == seed_id:
                        continue
                    # Structural edges get a smaller fraction of the graph weight
                    struct_score = graph_weight * max(seed.score, 0.1) * struct_weight * 0.5
                    if struct_score < self.graph_confidence_threshold:
                        continue
                    previous_score, previous_terms = candidate_scores.get(
                        neighbor_id,
                        (0.0, set()),
                    )
                    if struct_score > previous_score:
                        candidate_scores[neighbor_id] = (struct_score, set())

        ranked_neighbors = sorted(
            candidate_scores.items(),
            key=lambda item: (item[1][0], len(item[1][1])),
            reverse=True,
        )[:max_neighbors]

        for chunk_id, (graph_score, _shared_terms) in ranked_neighbors:
            existing = merged.get(chunk_id)
            if existing is not None:
                merged[chunk_id] = existing.model_copy(
                    update={
                        "score": max(existing.score, graph_score),
                        "graph_score": graph_score,
                    }
                )
                continue
            chunk = self.chunks_by_id.get(chunk_id)
            if chunk is None:
                continue
            merged[chunk_id] = RetrievalResult(
                chunk=chunk,
                score=graph_score,
                graph_score=graph_score,
            )

        expanded_results = list(merged.values())
        expanded_results.sort(key=lambda item: item.score, reverse=True)
        return expanded_results


# 保留旧术语提取接口，委托默认 TermExtractor 处理。
def extract_graph_terms(text: str) -> set[str]:
    """Backward-compatible wrapper: extract terms using default TermExtractor.

    Deprecated: use ``TermExtractor.extract()`` directly for better control.
    """
    extractor = TermExtractor(ner_enabled=False, idf_enabled=False)
    return extractor.extract(text)


# 将分块与共现关系存入 Neo4j，供持久化图扩展检索。
class Neo4jGraphRAGIndex:
    """Neo4j-backed co-occurrence graph over RAG chunks."""

    # 初始化图连接和术语配置，并写入当前分块关系。
    def __init__(
        self,
        chunks: list[RAGChunk],
        *,
        index_id: str,
        uri: str,
        username: str | None = None,
        password: str | None = None,
        database: str | None = None,
        driver_factory: Callable[..., Any] | None = None,
        term_extractor: TermExtractor | None = None,
        graph_confidence_threshold: float = 0.15,
    ):
        if not uri:
            raise ValueError("neo4j_uri is required when graph_backend is neo4j.")
        self.chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        self.index_id = index_id
        self.database = database
        if term_extractor is None:
            term_extractor = TermExtractor(ner_enabled=False, idf_enabled=False)
        self.term_extractor = term_extractor
        self.graph_confidence_threshold = graph_confidence_threshold
        self.chunk_terms: dict[str, set[str]] = {}
        self.term_to_chunk_ids: dict[str, set[str]] = defaultdict(set)
        self.driver = self._create_driver(
            uri=uri,
            username=username,
            password=password,
            driver_factory=driver_factory,
        )
        self._write_graph(chunks)
        self._last_expansion_decision: Any = None

    # 查询与种子共享术语的 Neo4j 邻居，转换为统一检索结果。
    def expand(
        self,
        query: str,
        seed_results: list[RetrievalResult],
        max_neighbors: int,
        graph_weight: float,
        *,
        adaptive: bool = True,
    ) -> list[RetrievalResult]:
        """Expand seed results with Neo4j neighbors sharing chunk terms."""
        if not seed_results or max_neighbors <= 0 or graph_weight <= 0:
            return seed_results

        if adaptive:
            decision = _adaptive_decision(
                query, seed_results, self.chunk_terms, self.term_to_chunk_ids
            )
            self._last_expansion_decision = decision
            if not decision.should_expand:
                return seed_results

        seed_ids = [result.chunk.chunk_id for result in seed_results]
        base_seed_score = max((result.score for result in seed_results), default=0.0)
        merged = {result.chunk.chunk_id: result for result in seed_results}
        with self._session() as session:
            rows = list(
                session.run(
                    """
                    MATCH (seed:RagChunk {index_id: $index_id})
                        -[:MENTIONS]->(term:RagTerm)
                        <-[:MENTIONS]-(neighbor:RagChunk {index_id: $index_id})
                    WHERE seed.chunk_id IN $seed_ids
                        AND NOT neighbor.chunk_id IN $seed_ids
                    RETURN neighbor.chunk_id AS chunk_id,
                        collect(DISTINCT term.term) AS shared_terms
                    """,
                    index_id=self.index_id,
                    seed_ids=seed_ids,
                )
            )

        from open_deep_research.rag.graph_scoring import compute_graph_score

        for row in rows:
            chunk_id = row["chunk_id"]
            shared_term_list = row["shared_terms"]
            shared_terms = set(shared_term_list) if shared_term_list else set()
            chunk = self.chunks_by_id.get(chunk_id)
            if chunk is None:
                continue
            graph_score, confidence = compute_graph_score(
                seed_score=base_seed_score,
                shared_terms=shared_terms,
                graph_weight=graph_weight,
                term_extractor=self.term_extractor,
            )
            if confidence < self.graph_confidence_threshold:
                continue
            existing = merged.get(chunk_id)
            if existing is not None:
                merged[chunk_id] = existing.model_copy(
                    update={
                        "score": max(existing.score, graph_score),
                        "graph_score": graph_score,
                    }
                )
                continue
            merged[chunk_id] = RetrievalResult(
                chunk=chunk,
                score=graph_score,
                graph_score=graph_score,
            )

        expanded_results = list(merged.values())
        expanded_results.sort(key=lambda item: item.score, reverse=True)
        return expanded_results

    # 根据图连接参数构建 Neo4j 驱动。
    def _create_driver(
        self,
        *,
        uri: str,
        username: str | None,
        password: str | None,
        driver_factory: Callable[..., Any] | None,
    ) -> Any:
        if driver_factory is None:
            try:
                from neo4j import GraphDatabase
            except ImportError as exc:
                raise ImportError(
                    "Install neo4j to use graph_backend='neo4j'."
                ) from exc
            driver_factory = GraphDatabase.driver
        auth = (username, password) if username and password else None
        return driver_factory(uri, auth=auth)

    # 打开配置数据库对应的图会话。
    def _session(self) -> Any:
        if self.database:
            return self.driver.session(database=self.database)
        return self.driver.session()

    # 将分块、术语及关联关系同步到 Neo4j。
    def _write_graph(self, chunks: list[RAGChunk]) -> None:
        with self._session() as session:
            session.run(
                """
                CREATE CONSTRAINT rag_chunk_node_key IF NOT EXISTS
                FOR (chunk:RagChunk) REQUIRE chunk.node_key IS UNIQUE
                """
            )
            session.run(
                """
                CREATE CONSTRAINT rag_term_node_key IF NOT EXISTS
                FOR (term:RagTerm) REQUIRE term.node_key IS UNIQUE
                """
            )
            for chunk in chunks:
                terms = sorted(
                    self.term_extractor.extract(
                        f"{chunk.title or ''}\n{chunk.content}"
                    )
                )
                # Keep in-memory copy for adaptive expansion
                self.chunk_terms[chunk.chunk_id] = set(terms)
                for term in terms:
                    self.term_to_chunk_ids[term].add(chunk.chunk_id)
                session.run(
                    """
                    MERGE (chunk:RagChunk {node_key: $chunk_node_key})
                    SET chunk.index_id = $index_id,
                        chunk.chunk_id = $chunk_id,
                        chunk.source = $source,
                        chunk.title = $title,
                        chunk.content = $content
                    WITH chunk
                    UNWIND $terms AS term
                    MERGE (graph_term:RagTerm {
                        node_key: $term_node_key_prefix + term
                    })
                    SET graph_term.index_id = $index_id,
                        graph_term.term = term
                    MERGE (chunk)-[:MENTIONS]->(graph_term)
                    """,
                    chunk_node_key=f"{self.index_id}:chunk:{chunk.chunk_id}",
                    term_node_key_prefix=f"{self.index_id}:term:",
                    index_id=self.index_id,
                    chunk_id=chunk.chunk_id,
                    source=chunk.source,
                    title=chunk.title,
                    content=chunk.content,
                    terms=terms,
                )


# 按图开关与后端配置创建内存或 Neo4j 扩展索引。
def create_graph_index(
    chunks: list[RAGChunk],
    *,
    backend: str,
    index_id: str,
    neo4j_uri: str | None = None,
    neo4j_username: str | None = None,
    neo4j_password: str | None = None,
    neo4j_database: str | None = None,
    driver_factory: Callable[..., Any] | None = None,
    term_extractor: TermExtractor | None = None,
    graph_confidence_threshold: float = 0.15,
    structural_edges_enabled: bool = False,
) -> GraphRAGIndex | Neo4jGraphRAGIndex:
    """Create the configured graph expansion index."""
    normalized_backend = backend.strip().lower()
    if normalized_backend in {"memory", "local", "inmemory"}:
        return GraphRAGIndex(
            chunks,
            term_extractor=term_extractor,
            graph_confidence_threshold=graph_confidence_threshold,
            structural_edges_enabled=structural_edges_enabled,
        )
    if normalized_backend == "neo4j":
        return Neo4jGraphRAGIndex(
            chunks,
            index_id=index_id,
            uri=neo4j_uri or "",
            username=neo4j_username,
            password=neo4j_password,
            database=neo4j_database,
            driver_factory=driver_factory,
            term_extractor=term_extractor,
            graph_confidence_threshold=graph_confidence_threshold,
        )
    raise ValueError("graph_backend must be memory or neo4j.")
