"""Research-memory retrieval adapters.

This retriever returns a bounded subgraph.  It intentionally does not generate
an answer; the existing LangGraph business agent remains the answer/analysis
generator.
"""

from __future__ import annotations

from typing import Any

from open_deep_research.research_graph.models import (
    RelevantSubgraph,
    ResearchGraphScope,
    WorkingContext,
)
from open_deep_research.research_graph.store import ResearchGraphStore


class ResearchGraphRetriever:
    """Small project-side facade over an in-memory or Neo4j graph store."""

    def __init__(
        self,
        store: ResearchGraphStore,
        *,
        max_nodes: int = 24,
        max_edges: int = 48,
    ) -> None:
        """Configure the store and maximum subgraph size."""
        self.store = store
        self.max_nodes = max_nodes
        self.max_edges = max_edges

    def retrieve(
        self,
        task: Any,
        *,
        scope: ResearchGraphScope,
        working_context: WorkingContext | None = None,
        query_suffix: str = "",
    ) -> RelevantSubgraph:
        """Retrieve local research memory relevant to the current task/gap."""
        objective = str(getattr(task, "objective", "") or task or "")
        evidence_needed = str(getattr(task, "evidence_needed", "") or "")
        gaps = ""
        seed_ids: list[str] = []
        if working_context is not None:
            gaps = " ".join(_context_summaries(working_context))
            seed_ids.extend(working_context.active_claim_ids)
            seed_ids.extend(working_context.active_evidence_ids)
            seed_ids.extend(working_context.active_event_ids)
        query = " ".join(part for part in (objective, evidence_needed, gaps, query_suffix) if part).strip()
        return self.store.retrieve(
            query,
            run_id=scope.run_id,
            max_nodes=self.max_nodes,
            max_edges=self.max_edges,
            seed_ids=seed_ids,
        )


def format_relevant_subgraph(
    subgraph: RelevantSubgraph,
    *,
    max_property_chars: int = 2400,
) -> str:
    """Render only a bounded, ID-preserving subgraph for a model prompt."""
    if not subgraph.nodes:
        return "No relevant Research Graph subgraph was retrieved for this task."
    lines = [f"Research Graph subgraph (run_id={subgraph.run_id}):"]
    for node in subgraph.nodes:
        properties = str(node.properties)
        if len(properties) > max_property_chars:
            properties = properties[:max_property_chars].rstrip() + "…"
        lines.append(
            f"- NODE {node.node_id} [{node.node_type}] role={node.role} "
            f"round={node.research_round}: {properties}"
        )
    if subgraph.edges:
        lines.append("Relationships:")
        for edge in subgraph.edges:
            lines.append(
                f"- EDGE {edge.edge_id}: {edge.source_id} -[{edge.relation_type}]-> "
                f"{edge.target_id}"
            )
    lines.append("Use these graph IDs when making any evidence-backed statement.")
    return "\n".join(lines)


def _context_summaries(context: WorkingContext) -> list[str]:
    values: list[str] = []
    for item in context.confirmed_findings:
        values.append(str(getattr(item, "summary", item)))
    for item in context.conflicts:
        values.append(str(getattr(item, "summary", item)))
    for item in context.open_gaps:
        values.append(str(getattr(item, "summary", item)))
    return values[-8:]


__all__ = ["ResearchGraphRetriever", "format_relevant_subgraph"]
