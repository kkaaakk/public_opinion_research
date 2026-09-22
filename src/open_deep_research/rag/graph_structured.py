"""Structured edge builders for GraphRAG.

This module adds document-structure edges to the term-co-occurrence graph.
Structural edges (same source, adjacent chunks, shared metadata) are
complementary to term edges: they capture relationships that are not
visible at the term level but are strong signals for multi-hop retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass

from open_deep_research.rag.types import RAGChunk


@dataclass(frozen=True)
class StructuredEdge:
    """A single structural relationship between two chunks."""

    from_chunk_id: str
    to_chunk_id: str
    edge_type: str
    weight: float = 1.0


def build_source_edges(chunks: list[RAGChunk]) -> list[StructuredEdge]:
    """Return SAME_SOURCE edges between chunks that come from the same document.

    Weight decays with chunk distance to avoid over-connecting long docs.
    """
    source_groups: dict[str, list[RAGChunk]] = {}
    for chunk in chunks:
        source_groups.setdefault(chunk.source, []).append(chunk)

    edges: list[StructuredEdge] = []
    for source, group in source_groups.items():
        if len(group) < 2:
            continue
        sorted_group = sorted(group, key=lambda c: c.chunk_id)
        for i in range(len(sorted_group)):
            for j in range(i + 1, len(sorted_group)):
                distance = j - i
                weight = max(0.2, 1.0 - (distance - 1) * 0.15)
                edges.append(
                    StructuredEdge(
                        from_chunk_id=sorted_group[i].chunk_id,
                        to_chunk_id=sorted_group[j].chunk_id,
                        edge_type="SAME_SOURCE",
                        weight=round(weight, 4),
                    )
                )
    return edges


def build_adjacent_edges(chunks: list[RAGChunk]) -> list[StructuredEdge]:
    """Return NEXT_CHUNK / PREV_CHUNK edges for sequentially adjacent chunks.

    Only connects chunks whose chunk_id differ by exactly one sequential
    suffix, e.g. ``doc-1-chunk-3`` -> ``doc-1-chunk-4``.
    """
    edges: list[StructuredEdge] = []
    chunk_map = {c.chunk_id: c for c in chunks}

    for chunk in chunks:
        next_id = _next_chunk_id(chunk.chunk_id)
        if next_id and next_id in chunk_map:
            edges.append(
                StructuredEdge(
                    from_chunk_id=chunk.chunk_id,
                    to_chunk_id=next_id,
                    edge_type="NEXT_CHUNK",
                    weight=1.0,
                )
            )
    return edges


def build_metadata_edges(chunks: list[RAGChunk]) -> list[StructuredEdge]:
    """Return SHARED_METADATA edges when chunks share key metadata fields.

    Currently inspects ``title`` and ``metadata.json_path``.
    """
    edges: list[StructuredEdge] = []
    # Group by title
    title_groups: dict[str, list[RAGChunk]] = {}
    for chunk in chunks:
        if chunk.title:
            title_groups.setdefault(chunk.title, []).append(chunk)

    for title_group in title_groups.values():
        if len(title_group) < 2:
            continue
        for i, chunk_a in enumerate(title_group):
            for chunk_b in title_group[i + 1 :]:
                edges.append(
                    StructuredEdge(
                        from_chunk_id=chunk_a.chunk_id,
                        to_chunk_id=chunk_b.chunk_id,
                        edge_type="SHARED_TITLE",
                        weight=0.8,
                    )
                )

    # Group by json_path in metadata
    json_path_groups: dict[str, list[RAGChunk]] = {}
    for chunk in chunks:
        jp = (chunk.metadata or {}).get("json_path")
        if jp:
            json_path_groups.setdefault(str(jp), []).append(chunk)

    for jp_group in json_path_groups.values():
        if len(jp_group) < 2:
            continue
        for i, chunk_a in enumerate(jp_group):
            for chunk_b in jp_group[i + 1 :]:
                edges.append(
                    StructuredEdge(
                        from_chunk_id=chunk_a.chunk_id,
                        to_chunk_id=chunk_b.chunk_id,
                        edge_type="SHARED_JSON_PATH",
                        weight=0.6,
                    )
                )

    return edges


def _next_chunk_id(chunk_id: str) -> str | None:
    """Infer the next sequential chunk id, e.g. ``doc-1-chunk-3`` -> ``doc-1-chunk-4``.

    Returns None if the id does not follow the expected ``*-chunk-N`` pattern.
    """
    import re

    match = re.search(r"chunk-(\d+)$", chunk_id)
    if not match:
        return None
    next_num = int(match.group(1)) + 1
    prefix = chunk_id[: match.start()]
    return f"{prefix}chunk-{next_num}"


class StructuralIndex:
    """In-memory index of structural edges for fast lookup."""

    def __init__(self, edges: list[StructuredEdge] | None = None):
        self._outgoing: dict[str, list[StructuredEdge]] = {}
        self._incoming: dict[str, list[StructuredEdge]] = {}
        if edges:
            for edge in edges:
                self.add_edge(edge)

    def add_edge(self, edge: StructuredEdge) -> None:
        self._outgoing.setdefault(edge.from_chunk_id, []).append(edge)
        self._incoming.setdefault(edge.to_chunk_id, []).append(edge)

    def neighbors(
        self, chunk_id: str
    ) -> list[tuple[str, float, str]]:
        """Return (neighbor_chunk_id, weight, edge_type) for all edges touching *chunk_id*."""
        results: list[tuple[str, float, str]] = []
        for edge in self._outgoing.get(chunk_id, []):
            results.append((edge.to_chunk_id, edge.weight, edge.edge_type))
        for edge in self._incoming.get(chunk_id, []):
            results.append((edge.from_chunk_id, edge.weight, edge.edge_type))
        return results
