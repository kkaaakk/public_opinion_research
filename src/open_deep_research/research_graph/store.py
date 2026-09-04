"""Research Graph storage adapters.

The in-memory adapter is intentionally feature-complete enough for unit and
integration tests.  The Neo4j adapter keeps the same contract and scopes every
query by ``run_id`` so separate research runs cannot leak evidence into one
another.
"""

from __future__ import annotations

import json
import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from open_deep_research.research_graph.models import (
    GraphEdge,
    GraphNode,
    RawResearchDocument,
    RelevantSubgraph,
    ResearchDeltaGraph,
    ResearchGraphScope,
    WriteReceipt,
)
from open_deep_research.research_graph.schema import (
    RESEARCH_NODE_TYPES,
    content_hash,
    normalize_text,
    official_graph_schema,
    schema_cypher,
)

LOGGER = logging.getLogger(__name__)


class ResearchGraphError(RuntimeError):
    """Raised when an explicitly enabled Research Graph cannot be used."""


class ResearchGraphStore(ABC):
    """Minimal storage contract used by graph strategies and tests."""

    @abstractmethod
    def write_delta(self, delta: ResearchDeltaGraph) -> WriteReceipt:
        """Persist one related delta graph and return a compact receipt."""

    @abstractmethod
    def source_is_persisted(
        self,
        scope: ResearchGraphScope,
        document: RawResearchDocument,
    ) -> bool:
        """Return whether this exact source version is already persisted."""

    @abstractmethod
    def retrieve(
        self,
        query: str,
        *,
        run_id: str,
        max_nodes: int = 24,
        max_edges: int = 48,
        seed_ids: Iterable[str] = (),
    ) -> RelevantSubgraph:
        """Return a bounded, provenance-preserving relevant subgraph."""

    def close(self) -> None:
        """Release external resources when the adapter owns any."""


def _scope_value(scope: ResearchGraphScope | str, name: str, default: Any = "") -> Any:
    if isinstance(scope, ResearchGraphScope):
        return getattr(scope, name, default)
    return scope if name == "run_id" else default


def _node_text(node: GraphNode) -> str:
    return " ".join(
        (
            node.node_id,
            node.node_type,
            node.role,
            node.task_id,
            json.dumps(node.properties, ensure_ascii=False, default=str),
        )
    ).casefold()


def _query_terms(query: str) -> list[str]:
    # This is only a cheap retrieval pre-filter.  Semantic decisions are made
    # by the Context Manager, never by this tokenization step.
    terms = [normalize_text(part) for part in str(query or "").split()]
    return [term for term in terms if len(term) >= 2]


class InMemoryResearchGraphStore(ResearchGraphStore):
    """Deterministic graph store used by tests and local development."""

    def __init__(self) -> None:
        """Initialize empty scoped node, edge, and source-version indexes."""
        self.nodes: dict[tuple[str, str], GraphNode] = {}
        self.edges: dict[tuple[str, str], GraphEdge] = {}
        self._source_versions: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()

    def write_delta(self, delta: ResearchDeltaGraph) -> WriteReceipt:
        """Upsert a delta into the in-memory scoped graph."""
        nodes, edges = delta.materialize()
        with self._lock:
            for node in nodes:
                self.nodes[(delta.scope.run_id, node.node_id)] = node
            for edge in edges:
                if (
                    (delta.scope.run_id, edge.source_id) in self.nodes
                    and (delta.scope.run_id, edge.target_id) in self.nodes
                ):
                    self.edges[(delta.scope.run_id, edge.edge_id)] = edge
            for source in delta.sources:
                source_key = source.url or source.source_id
                self._source_versions[(delta.scope.run_id, source_key)] = source.content_hash

        return WriteReceipt(
            run_id=delta.scope.run_id,
            role=delta.scope.role,
            research_round=delta.scope.research_round,
            task_id=delta.scope.task_id,
            source_ids=[source.source_id for source in delta.sources],
            evidence_ids=[item.evidence_id for item in delta.evidences],
            claim_ids=[item.claim_id for item in delta.claims],
            finding_ids=[item.finding_id for item in delta.findings],
            node_ids=[node.node_id for node in nodes],
            edge_ids=[edge.edge_id for edge in edges],
        )

    def source_is_persisted(
        self,
        scope: ResearchGraphScope,
        document: RawResearchDocument,
    ) -> bool:
        """Check whether the exact URL/source content version is stored."""
        source_key = document.url or document.source_id
        version = document.content_hash or content_hash(document.content)
        with self._lock:
            return self._source_versions.get((scope.run_id, source_key)) == version

    def retrieve(
        self,
        query: str,
        *,
        run_id: str,
        max_nodes: int = 24,
        max_edges: int = 48,
        seed_ids: Iterable[str] = (),
    ) -> RelevantSubgraph:
        """Retrieve a scored and one-hop-expanded subgraph for one run."""
        max_nodes = max(0, int(max_nodes))
        max_edges = max(0, int(max_edges))
        seed_set = {str(value) for value in seed_ids if value}
        terms = _query_terms(query)
        with self._lock:
            scoped_nodes = {
                node_id: node
                for (scope_id, node_id), node in self.nodes.items()
                if scope_id == run_id
            }
            scoped_edges = [
                edge
                for (scope_id, _), edge in self.edges.items()
                if scope_id == run_id
            ]

        scored: list[tuple[int, str, GraphNode]] = []
        for node_id, node in scoped_nodes.items():
            text = _node_text(node)
            score = sum(1 for term in terms if term in text)
            if node_id in seed_set:
                score += len(terms) + 1
            if score > 0:
                scored.append((score, node_id, node))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected_ids = {node_id for _, node_id, _ in scored[:max_nodes]}
        selected_ids.update(seed_id for seed_id in seed_set if seed_id in scoped_nodes)

        # Expand one hop from selected nodes, retaining the relationship type.
        if selected_ids and len(selected_ids) < max_nodes:
            for edge in scoped_edges:
                if edge.source_id in selected_ids or edge.target_id in selected_ids:
                    selected_ids.add(edge.source_id)
                    selected_ids.add(edge.target_id)
                    if len(selected_ids) >= max_nodes:
                        break

        nodes = [scoped_nodes[node_id] for node_id in selected_ids if node_id in scoped_nodes]
        nodes.sort(key=lambda node: (node.node_type, node.node_id))
        nodes = nodes[:max_nodes]
        selected_ids = {node.node_id for node in nodes}
        edges = [
            edge
            for edge in scoped_edges
            if edge.source_id in selected_ids and edge.target_id in selected_ids
        ][:max_edges]
        return RelevantSubgraph(run_id=run_id, query=query, nodes=nodes, edges=edges)

    def close(self) -> None:
        """Clear no external resources; retained for adapter symmetry."""


class Neo4jResearchGraphStore(ResearchGraphStore):
    """Neo4j adapter for the Research Graph schema.

    The project uses the official Neo4j Python driver for deterministic
    provenance writes.  ``neo4j-graphrag`` types and retrievers are optional
    enhancements at the boundary; the batch adapter avoids the experimental KG
    Builder's one-LLM-call-per-chunk default.
    """

    def __init__(
        self,
        *,
        uri: str,
        username: str | None = None,
        password: str | None = None,
        database: str | None = None,
        driver_factory: Callable[..., Any] | None = None,
        verify_connectivity: bool = True,
    ) -> None:
        """Initialize the driver and fail clearly if Neo4j is unavailable."""
        if not uri:
            raise ValueError("research_graph_uri is required when Research Graph is enabled.")
        self.uri = uri
        self.database = database
        self.driver = self._create_driver(
            uri=uri,
            username=username,
            password=password,
            driver_factory=driver_factory,
        )
        try:
            self.official_schema = official_graph_schema()
        except Exception:  # pragma: no cover - protects against future API drift
            LOGGER.warning(
                "neo4j-graphrag GraphSchema adapter is unavailable; using project schema adapter.",
                exc_info=True,
            )
            self.official_schema = None
        try:
            if verify_connectivity and hasattr(self.driver, "verify_connectivity"):
                self.driver.verify_connectivity()
            self.ensure_schema()
        except Exception as exc:
            self.close()
            raise ResearchGraphError(
                "Research Graph is enabled but the Neo4j connection/schema setup failed. "
                "Check research_graph_uri, credentials, database, and Neo4j availability."
            ) from exc

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
            except ImportError as exc:  # pragma: no cover - dependency guard
                raise ImportError(
                    "Install neo4j to use research_graph_backend='neo4j'."
                ) from exc
            driver_factory = GraphDatabase.driver
        auth = (username, password) if username and password else None
        return driver_factory(uri, auth=auth)

    def _execute(self, query: str, **params: Any) -> list[Any]:
        if hasattr(self.driver, "execute_query"):
            result = self.driver.execute_query(
                query,
                parameters_=params,
                database_=self.database,
            )
            if isinstance(result, tuple):
                return list(result[0])
            return list(result or [])
        session_factory = getattr(self.driver, "session", None)
        if not callable(session_factory):
            raise ResearchGraphError("Configured Neo4j driver has no execute_query or session API.")
        session = session_factory(database=self.database) if self.database else session_factory()
        try:
            result = session.run(query, **params)
            return list(result or [])
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()

    def ensure_schema(self) -> None:
        """Create only idempotent project-owned constraints and indexes."""
        for query in schema_cypher():
            self._execute(query)

    def write_delta(self, delta: ResearchDeltaGraph) -> WriteReceipt:
        """Upsert a scoped delta with deterministic node and edge keys."""
        nodes, edges = delta.materialize()
        # Validate/represent the delta through the official GraphRAG types when
        # the package is installed.  The project Cypher below remains the write
        # boundary because it preserves run-scoped provenance keys explicitly.
        self.to_official_graph(delta)
        node_rows = [
            {
                "run_id": node.run_id,
                "node_id": node.node_id,
                "entity_type": node.node_type,
                "role": node.role,
                "research_round": node.research_round,
                "task_id": node.task_id,
                "properties_json": json.dumps(node.properties, ensure_ascii=False, default=str),
                "url": node.properties.get("url"),
                "content_hash": node.properties.get("content_hash"),
            }
            for node in nodes
        ]
        edge_rows = [
            {
                "run_id": edge.run_id,
                "edge_id": edge.edge_id,
                "relation_type": edge.relation_type,
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "properties_json": json.dumps(edge.properties, ensure_ascii=False, default=str),
            }
            for edge in edges
        ]
        if node_rows:
            self._execute(
                """
                UNWIND $rows AS row
                MERGE (node:ResearchNode {run_id: row.run_id, node_id: row.node_id})
                SET node.entity_type = row.entity_type,
                    node.role = row.role,
                    node.research_round = row.research_round,
                    node.task_id = row.task_id,
                    node.properties_json = row.properties_json,
                    node.url = row.url,
                    node.content_hash = row.content_hash
                """,
                rows=node_rows,
            )
            # Add the typed labels from the validated project schema.  Labels
            # are selected only from the constant allow-list, never from raw
            # model text, so the Cypher identifier remains deterministic.
            for node_type in RESEARCH_NODE_TYPES:
                typed_rows = [row for row in node_rows if row["entity_type"] == node_type]
                if not typed_rows:
                    continue
                self._execute(
                    f"""
                    UNWIND $rows AS row
                    MATCH (node:ResearchNode {{run_id: row.run_id, node_id: row.node_id}})
                    SET node:{node_type}
                    """,
                    rows=typed_rows,
                )
        if edge_rows:
            self._execute(
                """
                UNWIND $rows AS row
                MATCH (source:ResearchNode {run_id: row.run_id, node_id: row.source_id})
                MATCH (target:ResearchNode {run_id: row.run_id, node_id: row.target_id})
                MERGE (source)-[edge:ResearchEdge {run_id: row.run_id, edge_id: row.edge_id}]->(target)
                SET edge.relation_type = row.relation_type,
                    edge.properties_json = row.properties_json
                """,
                rows=edge_rows,
            )
        return WriteReceipt(
            run_id=delta.scope.run_id,
            role=delta.scope.role,
            research_round=delta.scope.research_round,
            task_id=delta.scope.task_id,
            source_ids=[source.source_id for source in delta.sources],
            evidence_ids=[item.evidence_id for item in delta.evidences],
            claim_ids=[item.claim_id for item in delta.claims],
            finding_ids=[item.finding_id for item in delta.findings],
            node_ids=[node.node_id for node in nodes],
            edge_ids=[edge.edge_id for edge in edges],
        )

    @staticmethod
    def to_official_graph(delta: ResearchDeltaGraph) -> Any:
        """Convert a delta to official ``neo4j-graphrag`` graph types if present."""
        try:
            from neo4j_graphrag.components.types import (
                Neo4jGraph,
                Neo4jNode,
                Neo4jRelationship,
            )
        except ImportError:  # pragma: no cover - optional boundary guard
            return None
        try:
            nodes, edges = delta.materialize()
            official_nodes = [
                Neo4jNode(
                    id=node.node_id,
                    label=node.node_type,
                    properties={
                        "id": node.node_id,
                        "run_id": node.run_id,
                        "entity_type": node.node_type,
                        "properties_json": json.dumps(node.properties, ensure_ascii=False, default=str),
                    },
                )
                for node in nodes
            ]
            official_relationships = [
                Neo4jRelationship(
                    start_node_id=edge.source_id,
                    end_node_id=edge.target_id,
                    type=edge.relation_type,
                    properties={
                        "run_id": edge.run_id,
                        "relation_type": edge.relation_type,
                        "properties_json": json.dumps(edge.properties, ensure_ascii=False, default=str),
                    },
                )
                for edge in edges
            ]
            return Neo4jGraph(nodes=official_nodes, relationships=official_relationships)
        except Exception:  # pragma: no cover - future GraphRAG API drift
            LOGGER.debug("neo4j-graphrag graph type conversion failed", exc_info=True)
            return None

    def source_is_persisted(
        self,
        scope: ResearchGraphScope,
        document: RawResearchDocument,
    ) -> bool:
        """Check all stored versions of one URL inside the current run."""
        source_key = document.url or document.source_id
        version = document.content_hash or content_hash(document.content)
        rows = self._execute(
            """
            MATCH (source:ResearchNode {run_id: $run_id, entity_type: 'Source'})
            WHERE coalesce(source.url, source.node_id) = $source_key
            RETURN source.content_hash AS content_hash
            """,
            run_id=scope.run_id,
            source_key=source_key,
        )
        return any(
            (row.get("content_hash") if hasattr(row, "get") else None) == version
            for row in rows
        )

    def retrieve(
        self,
        query: str,
        *,
        run_id: str,
        max_nodes: int = 24,
        max_edges: int = 48,
        seed_ids: Iterable[str] = (),
    ) -> RelevantSubgraph:
        """Retrieve nodes and provenance edges scoped to ``run_id``."""
        terms = _query_terms(query)
        rows = self._execute(
            """
            MATCH (node:ResearchNode {run_id: $run_id})
            WITH node,
                 reduce(score = 0, term IN $terms |
                    score + CASE WHEN toLower(coalesce(node.properties_json, '')) CONTAINS term
                                      OR toLower(coalesce(node.node_id, '')) CONTAINS term
                                 THEN 1 ELSE 0 END) AS score
            WHERE score > 0 OR size($terms) = 0 OR node.node_id IN $seed_ids
            RETURN node.node_id AS node_id,
                   node.entity_type AS entity_type,
                   node.role AS role,
                   node.research_round AS research_round,
                   node.task_id AS task_id,
                   node.properties_json AS properties_json,
                   score
            ORDER BY score DESC, node.node_id
            LIMIT $max_nodes
            """,
            run_id=run_id,
            terms=terms,
            seed_ids=list(seed_ids),
            max_nodes=max(0, int(max_nodes)),
        )
        nodes: list[GraphNode] = []
        for row in rows:
            data = dict(row) if isinstance(row, Mapping) else {}
            try:
                properties = json.loads(data.get("properties_json") or "{}")
            except (TypeError, ValueError):
                properties = {"text": str(data.get("properties_json") or "")}
            nodes.append(
                GraphNode(
                    node_id=str(data.get("node_id") or ""),
                    node_type=str(data.get("entity_type") or "Finding"),  # type: ignore[arg-type]
                    role=str(data.get("role") or ""),
                    research_round=int(data.get("research_round") or 1),
                    task_id=str(data.get("task_id") or ""),
                    properties=properties,
                    run_id=run_id,
                )
            )
        node_ids = [node.node_id for node in nodes]
        edge_rows = self._execute(
            """
            MATCH (source:ResearchNode {run_id: $run_id})
                  -[edge:ResearchEdge {run_id: $run_id}]->
                  (target:ResearchNode {run_id: $run_id})
            WHERE source.node_id IN $node_ids AND target.node_id IN $node_ids
            RETURN edge.edge_id AS edge_id,
                   edge.relation_type AS relation_type,
                   source.node_id AS source_id,
                   target.node_id AS target_id,
                   edge.properties_json AS properties_json
            LIMIT $max_edges
            """,
            run_id=run_id,
            node_ids=node_ids,
            max_edges=max(0, int(max_edges)),
        )
        edges: list[GraphEdge] = []
        for row in edge_rows:
            data = dict(row) if isinstance(row, Mapping) else {}
            try:
                properties = json.loads(data.get("properties_json") or "{}")
            except (TypeError, ValueError):
                properties = {}
            edges.append(
                GraphEdge(
                    edge_id=str(data.get("edge_id") or ""),
                    relation_type=str(data.get("relation_type") or "DERIVED_FROM"),  # type: ignore[arg-type]
                    source_id=str(data.get("source_id") or ""),
                    target_id=str(data.get("target_id") or ""),
                    run_id=run_id,
                    properties=properties,
                )
            )
        return RelevantSubgraph(run_id=run_id, query=query, nodes=nodes, edges=edges)

    def close(self) -> None:
        """Close the owned Neo4j driver."""
        close = getattr(self.driver, "close", None)
        if callable(close):
            close()


_STORE_CACHE: dict[tuple[Any, ...], ResearchGraphStore] = {}
_STORE_CACHE_LOCK = threading.RLock()


def create_research_graph_store(
    configurable: Any,
    *,
    run_id: str,
    driver_factory: Callable[..., Any] | None = None,
    store_factory: Callable[..., ResearchGraphStore] | None = None,
) -> ResearchGraphStore:
    """Create/cache a store for one configured run.

    Caching is scoped by the connection and run identifier.  Neo4j queries
    still carry ``run_id`` explicitly, so the cache cannot widen retrieval
    scope accidentally.
    """
    backend = str(getattr(configurable, "research_graph_backend", "neo4j") or "neo4j").strip().lower()
    uri = getattr(configurable, "research_graph_uri", None)
    database = getattr(configurable, "research_graph_database", None)
    username = getattr(configurable, "research_graph_username", None)
    key = (backend, uri, database, username, run_id)
    with _STORE_CACHE_LOCK:
        cached = _STORE_CACHE.get(key)
        if cached is not None:
            return cached
        if store_factory is not None:
            store = store_factory(
                configurable=configurable,
                run_id=run_id,
                driver_factory=driver_factory,
            )
        elif backend in {"memory", "inmemory", "local"}:
            store = InMemoryResearchGraphStore()
        elif backend == "neo4j":
            store = Neo4jResearchGraphStore(
                uri=str(uri or ""),
                username=username,
                password=getattr(configurable, "research_graph_password", None),
                database=database,
                driver_factory=driver_factory,
            )
        else:
            raise ValueError("research_graph_backend must be either 'neo4j' or 'memory'.")
        _STORE_CACHE[key] = store
        return store


def clear_research_graph_store_cache() -> None:
    """Close and clear cached adapters, primarily for tests and shutdown."""
    with _STORE_CACHE_LOCK:
        stores = list(_STORE_CACHE.values())
        _STORE_CACHE.clear()
    for store in stores:
        try:
            store.close()
        except Exception:  # pragma: no cover - defensive shutdown guard
            LOGGER.debug("Failed to close Research Graph store", exc_info=True)


__all__ = [
    "InMemoryResearchGraphStore",
    "Neo4jResearchGraphStore",
    "ResearchGraphError",
    "ResearchGraphStore",
    "clear_research_graph_store_cache",
    "create_research_graph_store",
]
