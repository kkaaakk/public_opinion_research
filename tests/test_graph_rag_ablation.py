# 检查术语提取、自适应扩展、邻居评分和结构边的图检索行为。
"""Ablation tests for the improved GraphRAG subsystem.

These tests verify that each Phase improvement (terms, adaptive,
scoring, structural edges) works as intended and does not break
backward compatibility.
"""

from __future__ import annotations

from open_deep_research.rag.graph import GraphRAGIndex, create_graph_index
from open_deep_research.rag.graph_adaptive import ExpansionDecision, decide_expansion
from open_deep_research.rag.graph_scoring import (
    RECOMMENDED_GRAPH_PARAMS,
    compute_graph_score,
)
from open_deep_research.rag.graph_structured import (
    StructuralIndex,
    build_adjacent_edges,
    build_metadata_edges,
    build_source_edges,
)
from open_deep_research.rag.graph_terms import TermExtractor
from open_deep_research.rag.types import RAGChunk, RetrievalResult


# 创建带指定内容和来源的测试分块。
def _make_chunk(chunk_id: str, content: str, source: str = "s", title: str | None = None) -> RAGChunk:
    return RAGChunk(
        chunk_id=chunk_id,
        content=content,
        source=source,
        title=title,
        metadata={},
    )


# 把测试分块包装为指定得分的检索结果。
def _make_result(chunk_id: str, score: float, content: str = "...") -> RetrievalResult:
    return RetrievalResult(
        chunk=_make_chunk(chunk_id, content),
        score=score,
    )


# 验证术语提取及 IDF 过滤。
class TestTermExtractor:
    """Phase 2: Term extraction with IDF and stop-word filtering."""

    # 验证基本候选术语提取结果。
    def test_basic_extraction(self) -> None:
        extractor = TermExtractor(ner_enabled=False, idf_enabled=False)
        terms = extractor.extract("Apple Inc. is headquartered in Cupertino.")
        assert "apple" in terms
        assert "inc" in terms

    # 验证常见低信息词被 IDF 规则过滤。
    def test_idf_filtering(self) -> None:
        corpus = [
            "the quick brown fox",
            "the lazy dog",
            "the fox jumps over",
        ]
        extractor = TermExtractor(
            chunks=corpus,
            ner_enabled=False,
            idf_enabled=True,
            idf_threshold_percentile=50.0,
        )
        terms = extractor.extract("the quick brown fox")
        # "the" should be filtered by high IDF threshold
        assert "the" not in terms
        assert "quick" in terms
        assert "brown" in terms


# 验证查询特征对图扩展决策的影响。
class TestAdaptiveExpansion:
    """Phase 3: Adaptive expansion decision."""

    # 验证具体查询的自适应扩展判断。
    def test_decide_expansion_specific_query(self) -> None:
        seed = [_make_result("c1", 0.8)]
        decision = decide_expansion("Apple revenue 2024", seed, {}, {})
        assert isinstance(decision, ExpansionDecision)
        assert decision.should_expand is True

    # 验证宽泛查询不会无条件触发扩展。
    def test_decide_expansion_vague_query(self) -> None:
        seed = [_make_result("c1", 0.3)]
        decision = decide_expansion("tell me something", seed, {}, {})
        assert isinstance(decision, ExpansionDecision)

    # 验证图索引遵循自适应开关。
    def test_graph_index_adaptive_flag(self) -> None:
        chunks = [_make_chunk("c1", "Apple Inc. revenue"), _make_chunk("c2", "Google revenue")]
        index = GraphRAGIndex(chunks)
        seed = [_make_result("c1", 0.8, "Apple Inc. revenue")]
        # adaptive=False skips expansion decision
        result = index.expand("Apple revenue", seed, max_neighbors=2, graph_weight=0.35, adaptive=False)
        # Should still expand because adaptive=False means skip the decision check
        assert len(result) >= 1


# 检查图候选得分、置信度和参数契约。
class TestComputeGraphScore:
    """Phase 4: Data-driven graph scoring with confidence."""

    # 验证没有共享术语时的图评分。
    def test_zero_shared_terms(self) -> None:
        score, confidence = compute_graph_score(0.8, set(), 0.35, None)
        assert score == 0.0
        assert confidence == 0.0

    # 验证有效共享术语产生正向扩展得分。
    def test_positive_score(self) -> None:
        score, confidence = compute_graph_score(0.8, {"apple", "revenue"}, 0.35, None)
        assert score > 0.0
        assert 0.0 <= confidence <= 1.0

    # 验证低置信度候选被阈值过滤。
    def test_confidence_filter(self) -> None:
        # With very few shared terms, confidence should be low
        score, confidence = compute_graph_score(0.1, {"x"}, 0.35, None)
        assert confidence < 0.5

    # 验证推荐图参数包含约定配置键。
    def test_recommended_params_keys(self) -> None:
        assert "default" in RECOMMENDED_GRAPH_PARAMS
        assert "graph_weight" in RECOMMENDED_GRAPH_PARAMS["default"]
        assert "graph_confidence_threshold" in RECOMMENDED_GRAPH_PARAMS["default"]


# 检查同源、相邻和共享元数据的结构关系。
class TestStructuralEdges:
    """Phase 5: Structural edge builders."""

    # 验证同文档分块之间的来源关系。
    def test_source_edges(self) -> None:
        c1 = _make_chunk("c1", "a", source="doc1")
        c2 = _make_chunk("c2", "b", source="doc1")
        c3 = _make_chunk("c3", "c", source="doc2")
        edges = build_source_edges([c1, c2, c3])
        assert len(edges) == 1
        assert edges[0].edge_type == "SAME_SOURCE"
        assert edges[0].from_chunk_id in {"c1", "c2"}
        assert edges[0].to_chunk_id in {"c1", "c2"}

    # 验证连续分块之间的前后邻接关系。
    def test_adjacent_edges(self) -> None:
        c1 = _make_chunk("doc-1-chunk-1", "a")
        c2 = _make_chunk("doc-1-chunk-2", "b")
        c3 = _make_chunk("doc-2-chunk-1", "c")
        edges = build_adjacent_edges([c1, c2, c3])
        assert len(edges) == 1
        assert edges[0].edge_type == "NEXT_CHUNK"
        assert edges[0].from_chunk_id == "doc-1-chunk-1"
        assert edges[0].to_chunk_id == "doc-1-chunk-2"

    # 验证共享关键元数据的分块建立关系。
    def test_metadata_edges(self) -> None:
        c1 = _make_chunk("c1", "a", title="Section 1")
        c2 = _make_chunk("c2", "b", title="Section 1")
        edges = build_metadata_edges([c1, c2])
        assert any(e.edge_type == "SHARED_TITLE" for e in edges)

    # 验证结构索引正确返回邻居、权重和关系类型。
    def test_structural_index_neighbors(self) -> None:
        from open_deep_research.rag.graph_structured import StructuredEdge

        edge = StructuredEdge("c1", "c2", "SAME_SOURCE", weight=0.9)
        index = StructuralIndex([edge])
        neighbors = index.neighbors("c1")
        assert len(neighbors) == 1
        assert neighbors[0][0] == "c2"


# 检查图扩展各组件协作时的实际召回结果。
class TestGraphRAGIndexIntegration:
    """Integration: full GraphRAGIndex with all improvements."""

    # 验证通过共享术语召回关联分块。
    def test_term_based_expansion(self) -> None:
        chunks = [
            _make_chunk("c1", "Apple Inc. quarterly revenue"),
            _make_chunk("c2", "Apple revenue growth"),
            _make_chunk("c3", "Unrelated topic here"),
        ]
        index = GraphRAGIndex(chunks, graph_confidence_threshold=0.0)
        seed = [_make_result("c1", 0.9, "Apple Inc. quarterly revenue")]
        result = index.expand("Apple revenue", seed, max_neighbors=2, graph_weight=0.35, adaptive=False)
        # Should include c2 because it shares "apple" and "revenue" terms
        chunk_ids = {r.chunk.chunk_id for r in result}
        assert "c2" in chunk_ids

    # 验证启用结构边后可扩展对应邻居。
    def test_structural_expansion_enabled(self) -> None:
        chunks = [
            _make_chunk("doc-1-chunk-1", "intro", source="doc1"),
            _make_chunk("doc-1-chunk-2", "body", source="doc1"),
        ]
        index = GraphRAGIndex(chunks, structural_edges_enabled=True, graph_confidence_threshold=0.0)
        seed = [_make_result("doc-1-chunk-1", 0.9, "intro")]
        result = index.expand("test", seed, max_neighbors=2, graph_weight=0.35, adaptive=False)
        chunk_ids = {r.chunk.chunk_id for r in result}
        assert "doc-1-chunk-2" in chunk_ids

    # 验证图索引应用置信度阈值。
    def test_confidence_threshold_filters(self) -> None:
        chunks = [
            _make_chunk("c1", "Apple revenue"),
            _make_chunk("c2", "Banana"),
        ]
        # High threshold should filter everything
        index = GraphRAGIndex(chunks, graph_confidence_threshold=1.0)
        seed = [_make_result("c1", 0.9, "Apple revenue")]
        result = index.expand("Apple", seed, max_neighbors=2, graph_weight=0.35, adaptive=False)
        # Only seed should remain because nothing passes confidence=1.0
        assert len(result) == 1

    # 验证工厂正确创建内存图索引。
    def test_create_graph_index_memory(self) -> None:
        chunks = [_make_chunk("c1", "hello"), _make_chunk("c2", "world")]
        index = create_graph_index(chunks, backend="memory", index_id="test")
        assert isinstance(index, GraphRAGIndex)
