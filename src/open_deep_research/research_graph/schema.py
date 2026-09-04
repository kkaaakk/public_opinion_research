"""Research Graph schema and deterministic identifier helpers."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from open_deep_research.research_graph.models import (
    ResearchNodeType,
    ResearchRelationType,
)

RESEARCH_NODE_TYPES: tuple[ResearchNodeType, ...] = (
    "ResearchRun",
    "ResearchTask",
    "Source",
    "Evidence",
    "Claim",
    "SourceFinding",
    "Event",
    "Entity",
    "Uncertainty",
    "Finding",
    "Coverage",
)

RESEARCH_RELATION_TYPES: tuple[ResearchRelationType, ...] = (
    "EXTRACTED_FROM",
    "SUPPORTS",
    "CONTRADICTS",
    "CONTEXTUALIZES",
    "FROM_SOURCE",
    "DERIVED_FROM",
    "ABOUT_CLAIM",
    "RELATES_TO",
    "INVOLVES",
    "APPLIES_TO",
    "BASED_ON",
    "SUPPORTED_BY",
    "ASSESSES",
)


def stable_id(prefix: str, *parts: Any, length: int = 20) -> str:
    """Create a deterministic, opaque ID from code-owned values."""
    payload = "\x1f".join(str(part or "") for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()
    return f"{prefix}_{digest[:length]}"


def content_hash(content: str) -> str:
    """Return the canonical hash used for source-version deduplication."""
    return hashlib.sha256((content or "").encode("utf-8", errors="replace")).hexdigest()


def normalize_text(value: Any) -> str:
    """Normalize text for cheap exact-deduplication only.

    This helper is intentionally not used as a semantic claim resolver.  It is
    only used to keep an identical extraction from creating duplicate IDs.
    """
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def official_graph_schema() -> Any:
    """Return an optional ``neo4j-graphrag`` GraphSchema.

    The package is a required production dependency, but this import remains
    lazy so light unit tests can use the in-memory adapter without importing
    the experimental KG Builder modules.  The schema is supplied to clients
    that opt into the official KG components; deterministic project writes do
    not depend on this experimental API.
    """
    try:
        from neo4j_graphrag.components.schema import (
            GraphSchema,
            NodeType,
            Pattern,
            PropertyType,
            RelationshipType,
        )
    except ImportError:
        return None

    string = lambda name, description="": PropertyType(  # noqa: E731
        name=name,
        type="STRING",
        description=description,
    )
    node_types = [
        NodeType(
            label=node_type,
            description=f"Per-run public-opinion research {node_type} node.",
            properties=[string("id"), string("run_id"), string("properties_json")],
            additional_properties=True,
        )
        for node_type in RESEARCH_NODE_TYPES
    ]
    relationship_types = [
        RelationshipType(
            label=relation_type,
            description=f"Research provenance relation {relation_type}.",
            properties=[string("run_id"), string("properties_json")],
            additional_properties=True,
        )
        for relation_type in RESEARCH_RELATION_TYPES
    ]
    patterns = [
        Pattern(source="Evidence", relationship="EXTRACTED_FROM", target="Source"),
        Pattern(source="Evidence", relationship="SUPPORTS", target="Claim"),
        Pattern(source="Evidence", relationship="CONTRADICTS", target="Claim"),
        Pattern(source="Finding", relationship="DERIVED_FROM", target="Claim"),
        Pattern(source="Finding", relationship="SUPPORTED_BY", target="Evidence"),
        Pattern(source="Coverage", relationship="ASSESSES", target="ResearchTask"),
    ]
    return GraphSchema(
        node_types=node_types,
        relationship_types=relationship_types,
        patterns=patterns,
    )


def schema_cypher() -> tuple[str, ...]:
    """Return idempotent Cypher statements for the project adapter."""
    return (
        "CREATE CONSTRAINT research_node_scope_key IF NOT EXISTS "
        "FOR (node:ResearchNode) REQUIRE (node.run_id, node.node_id) IS NODE KEY",
        "CREATE CONSTRAINT research_edge_scope_key IF NOT EXISTS "
        "FOR ()-[edge:ResearchEdge]-() REQUIRE (edge.run_id, edge.edge_id) IS RELATIONSHIP KEY",
        "CREATE INDEX research_node_type IF NOT EXISTS FOR (node:ResearchNode) ON (node.entity_type)",
        "CREATE INDEX research_source_url IF NOT EXISTS FOR (node:ResearchNode) ON (node.url)",
    )


__all__ = [
    "RESEARCH_NODE_TYPES",
    "RESEARCH_RELATION_TYPES",
    "content_hash",
    "normalize_text",
    "official_graph_schema",
    "schema_cypher",
    "stable_id",
]
