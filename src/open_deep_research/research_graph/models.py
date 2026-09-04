"""Typed models for the per-run public-opinion research graph.

The graph is deliberately separate from LangGraph's message state.  These
models describe durable research memory and the bounded state that is allowed
back into an agent prompt.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

ResearchNodeType = Literal[
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
]

ResearchRelationType = Literal[
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
]


class ResearchGraphScope(BaseModel):
    """Scope attached to every graph write and retrieval."""

    run_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    research_round: int = Field(default=1, ge=1)
    task_id: str = Field(min_length=1)


class RawResearchDocument(BaseModel):
    """A tool result converted into a source-bound extraction input.

    URL and other source metadata are produced by code.  The extraction model
    only receives ``source_id`` as a reference and is never trusted to invent
    source metadata.
    """

    source_id: str = Field(min_length=1)
    content: str = ""
    url: str | None = None
    source_path: str | None = None
    title: str = ""
    source_type: str = "tool_result"
    tool_name: str = ""
    query: str = ""
    published_at: str | None = None
    retrieved_at: str | None = None
    content_hash: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class Source(BaseModel):
    """A source with deterministic provenance metadata."""

    source_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    url: str | None = None
    source_path: str | None = None
    title: str = ""
    source_type: str = "tool_result"
    tool_name: str = ""
    query: str = ""
    published_at: str | None = None
    retrieved_at: str | None = None
    content_hash: str = ""
    role: str = ""
    research_round: int = Field(default=1, ge=1)
    task_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    """A source-bound observation extracted from one or more source spans."""

    evidence_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    evidence_type: str = "fact"
    stance: Literal["supports", "contradicts", "neutral", "unknown"] = "unknown"
    confidence: float | None = Field(default=None, ge=0, le=1)
    quote: str = ""
    role: str = ""
    research_round: int = Field(default=1, ge=1)
    task_id: str = ""
    supports_claim_ids: list[str] = Field(default_factory=list)
    contradicts_claim_ids: list[str] = Field(default_factory=list)
    contextualizes_claim_ids: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    """A proposition that can be supported, contradicted, or contextualized."""

    claim_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    status: Literal["confirmed", "disputed", "unsupported", "open", "unknown"] = "open"
    confidence: float | None = Field(default=None, ge=0, le=1)
    role: str = ""
    research_round: int = Field(default=1, ge=1)
    task_id: str = ""
    event_ids: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)


class SourceFinding(BaseModel):
    """A source-level synthesis that retains all local graph references."""

    source_finding_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)


class Event(BaseModel):
    """A dated incident, announcement, investigation, or other event."""

    event_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    date: str | None = None
    description: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)


class Entity(BaseModel):
    """An entity mentioned by extracted evidence."""

    entity_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    entity_type: str = "Entity"
    aliases: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class Uncertainty(BaseModel):
    """An uncertainty explicitly attached to claims and evidence."""

    uncertainty_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    severity: Literal["low", "medium", "high", "unknown"] = "unknown"


class Finding(BaseModel):
    """An analysis conclusion derived from claims and supporting evidence."""

    finding_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    uncertainty_ids: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    finding_type: str = "research_finding"


class Coverage(BaseModel):
    """Evidence coverage for a research task or explicit gap."""

    coverage_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    status: Literal["covered", "partial", "open", "unknown"] = "unknown"
    summary: str = ""
    finding_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class GraphNode(BaseModel):
    """Storage-neutral node representation."""

    node_id: str = Field(min_length=1)
    node_type: ResearchNodeType
    properties: dict[str, Any] = Field(default_factory=dict)
    run_id: str = Field(min_length=1)
    role: str = ""
    research_round: int = Field(default=1, ge=1)
    task_id: str = ""


class GraphEdge(BaseModel):
    """Storage-neutral directed relationship representation."""

    edge_id: str = Field(min_length=1)
    relation_type: ResearchRelationType
    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    properties: dict[str, Any] = Field(default_factory=dict)


class ResearchDeltaGraph(BaseModel):
    """One related graph delta extracted from a batch of new material."""

    scope: ResearchGraphScope
    sources: list[Source] = Field(default_factory=list)
    evidences: list[Evidence] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    source_findings: list[SourceFinding] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    uncertainties: list[Uncertainty] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    coverages: list[Coverage] = Field(default_factory=list)
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_scope(self) -> ResearchDeltaGraph:
        """Reject accidental cross-run nodes before they reach a store."""
        for node in self.nodes:
            if node.run_id != self.scope.run_id:
                raise ValueError("ResearchDeltaGraph contains a node outside its scope.")
        for edge in self.edges:
            if edge.run_id != self.scope.run_id:
                raise ValueError("ResearchDeltaGraph contains an edge outside its scope.")
        return self

    def materialize(self) -> tuple[list[GraphNode], list[GraphEdge]]:
        """Return typed nodes and provenance edges for storage."""
        nodes = list(self.nodes)
        edges = list(self.edges)
        node_ids = {node.node_id for node in nodes}

        def add_node(node_id: str, node_type: ResearchNodeType, value: BaseModel, **meta: Any) -> None:
            if node_id in node_ids:
                return
            payload = value.model_dump(mode="json")
            payload.pop("run_id", None)
            payload.pop("role", None)
            payload.pop("research_round", None)
            payload.pop("task_id", None)
            nodes.append(
                GraphNode(
                    node_id=node_id,
                    node_type=node_type,
                    properties=payload,
                    run_id=self.scope.run_id,
                    role=str(meta.get("role") or self.scope.role),
                    research_round=int(meta.get("research_round") or self.scope.research_round),
                    task_id=str(meta.get("task_id") or self.scope.task_id),
                )
            )
            node_ids.add(node_id)

        for source in self.sources:
            add_node(source.source_id, "Source", source, role=source.role)
        for evidence in self.evidences:
            add_node(evidence.evidence_id, "Evidence", evidence, role=evidence.role)
        for claim in self.claims:
            add_node(claim.claim_id, "Claim", claim, role=claim.role)
        for source_finding in self.source_findings:
            add_node(source_finding.source_finding_id, "SourceFinding", source_finding)
        for event in self.events:
            add_node(event.event_id, "Event", event)
        for entity in self.entities:
            add_node(entity.entity_id, "Entity", entity)
        for uncertainty in self.uncertainties:
            add_node(uncertainty.uncertainty_id, "Uncertainty", uncertainty)
        for finding in self.findings:
            add_node(finding.finding_id, "Finding", finding)
        for coverage in self.coverages:
            add_node(coverage.coverage_id, "Coverage", coverage)

        def add_edge(edge_id: str, relation: ResearchRelationType, source_id: str, target_id: str, **props: Any) -> None:
            if not source_id or not target_id or source_id not in node_ids or target_id not in node_ids:
                return
            if any(edge.edge_id == edge_id for edge in edges):
                return
            edges.append(
                GraphEdge(
                    edge_id=edge_id,
                    relation_type=relation,
                    source_id=source_id,
                    target_id=target_id,
                    run_id=self.scope.run_id,
                    properties=props,
                )
            )

        def edge_key(relation: str, source_id: str, target_id: str) -> str:
            return f"edge:{relation}:{source_id}:{target_id}"

        for evidence in self.evidences:
            add_edge(edge_key("EXTRACTED_FROM", evidence.evidence_id, evidence.source_id), "EXTRACTED_FROM", evidence.evidence_id, evidence.source_id)
        for item in self.source_findings:
            add_edge(edge_key("FROM_SOURCE", item.source_finding_id, item.source_id), "FROM_SOURCE", item.source_finding_id, item.source_id)
            for evidence_id in item.evidence_ids:
                add_edge(edge_key("DERIVED_FROM", item.source_finding_id, evidence_id), "DERIVED_FROM", item.source_finding_id, evidence_id)
            for claim_id in item.claim_ids:
                add_edge(edge_key("ABOUT_CLAIM", item.source_finding_id, claim_id), "ABOUT_CLAIM", item.source_finding_id, claim_id)
        for evidence in self.evidences:
            for claim_id in evidence.supports_claim_ids:
                add_edge(edge_key("SUPPORTS", evidence.evidence_id, claim_id), "SUPPORTS", evidence.evidence_id, claim_id)
            for claim_id in evidence.contradicts_claim_ids:
                add_edge(edge_key("CONTRADICTS", evidence.evidence_id, claim_id), "CONTRADICTS", evidence.evidence_id, claim_id)
            for claim_id in evidence.contextualizes_claim_ids:
                add_edge(edge_key("CONTEXTUALIZES", evidence.evidence_id, claim_id), "CONTEXTUALIZES", evidence.evidence_id, claim_id)
        for claim in self.claims:
            for event_id in claim.event_ids:
                add_edge(edge_key("RELATES_TO", claim.claim_id, event_id), "RELATES_TO", claim.claim_id, event_id)
            for entity_id in claim.entity_ids:
                add_edge(edge_key("INVOLVES", claim.claim_id, entity_id), "INVOLVES", claim.claim_id, entity_id)
        for uncertainty in self.uncertainties:
            for claim_id in uncertainty.claim_ids:
                add_edge(edge_key("APPLIES_TO", uncertainty.uncertainty_id, claim_id), "APPLIES_TO", uncertainty.uncertainty_id, claim_id)
            for evidence_id in uncertainty.evidence_ids:
                add_edge(edge_key("BASED_ON", uncertainty.uncertainty_id, evidence_id), "BASED_ON", uncertainty.uncertainty_id, evidence_id)
        for finding in self.findings:
            for claim_id in finding.claim_ids:
                add_edge(edge_key("DERIVED_FROM", finding.finding_id, claim_id), "DERIVED_FROM", finding.finding_id, claim_id)
            for evidence_id in finding.evidence_ids:
                add_edge(edge_key("SUPPORTED_BY", finding.finding_id, evidence_id), "SUPPORTED_BY", finding.finding_id, evidence_id)
            for uncertainty_id in finding.uncertainty_ids:
                add_edge(edge_key("CONTEXTUALIZES", finding.finding_id, uncertainty_id), "CONTEXTUALIZES", finding.finding_id, uncertainty_id)
        for coverage in self.coverages:
            for finding_id in coverage.finding_ids:
                add_edge(edge_key("SUPPORTED_BY", coverage.coverage_id, finding_id), "SUPPORTED_BY", coverage.coverage_id, finding_id)
            for claim_id in coverage.claim_ids:
                add_edge(edge_key("SUPPORTED_BY", coverage.coverage_id, claim_id), "SUPPORTED_BY", coverage.coverage_id, claim_id)
        return nodes, edges


class GraphExtractionOutput(BaseModel):
    """LLM-only semantic extraction output.

    Source metadata and stable graph IDs are intentionally absent.  The code
    side maps these local references to deterministic IDs and binds them to the
    supplied ``RawResearchDocument`` objects.
    """

    evidences: list[dict[str, Any]] = Field(default_factory=list)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    source_findings: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    entities: list[dict[str, Any]] = Field(default_factory=list)
    uncertainties: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    coverages: list[dict[str, Any]] = Field(default_factory=list)
    relations: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_singular_aliases(cls, value: Any) -> Any:
        """Accept common singular keys from structured-output test doubles."""
        if not isinstance(value, dict):
            return value
        aliases = {
            "evidence": "evidences",
            "source_finding": "source_findings",
            "event": "events",
            "entity": "entities",
            "uncertainty": "uncertainties",
            "finding": "findings",
            "coverage": "coverages",
            "relationship": "relations",
        }
        normalized = dict(value)
        for source, target in aliases.items():
            if target not in normalized and source in normalized:
                normalized[target] = normalized[source]
        return normalized


class RelevantSubgraph(BaseModel):
    """Bounded graph retrieval result passed to the Context Manager/agent."""

    run_id: str = Field(min_length=1)
    query: str = ""
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)

    @property
    def node_ids(self) -> set[str]:
        """Return IDs available for provenance validation."""
        return {node.node_id for node in self.nodes}

    @property
    def edge_ids(self) -> set[str]:
        """Return edge IDs available for provenance validation."""
        return {edge.edge_id for edge in self.edges}


class WriteReceipt(BaseModel):
    """Deterministic acknowledgement used by Micro Compact."""

    run_id: str = Field(min_length=1)
    role: str = ""
    research_round: int = Field(default=1, ge=1)
    task_id: str = ""
    source_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    duplicate_source_ids: list[str] = Field(default_factory=list)

    def as_text(self) -> str:
        """Render a compact, provenance-preserving receipt."""
        lines = [
            "[Research result compacted.",
            "Persisted to Research Graph.",
            f"Run: {self.run_id}",
        ]
        for label, values in (
            ("Sources", self.source_ids),
            ("Evidence", self.evidence_ids),
            ("Claims", self.claim_ids),
            ("Findings", self.finding_ids),
            ("Duplicate sources skipped", self.duplicate_source_ids),
        ):
            if values:
                lines.append(f"{label}: {', '.join(values)}")
        lines.append("]")
        return "\n".join(lines)


class ContextFinding(BaseModel):
    """Bounded working-context finding with provenance."""

    summary: str = Field(min_length=1)
    finding_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)


class ContextConflict(BaseModel):
    """Conflict in working memory with explicit graph references."""

    summary: str = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class ContextGap(BaseModel):
    """Open research gap with explicit evidence provenance when known."""

    summary: str = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    priority: Literal["high", "medium", "low"] = "medium"


class WorkingContext(BaseModel):
    """Bounded short-term working memory for one role and task."""

    current_objective: str = ""
    confirmed_findings: list[ContextFinding | str] = Field(default_factory=list)
    active_claim_ids: list[str] = Field(default_factory=list)
    active_evidence_ids: list[str] = Field(default_factory=list)
    active_event_ids: list[str] = Field(default_factory=list)
    conflicts: list[ContextConflict | str] = Field(default_factory=list)
    open_gaps: list[ContextGap | str] = Field(default_factory=list)
    recent_progress: str = ""


class WorkingContextDelta(BaseModel):
    """Structured output contract for the Context Manager LLM."""

    confirmed_findings_add: list[ContextFinding | str] = Field(default_factory=list)
    confirmed_findings_update: list[ContextFinding | str] = Field(default_factory=list)
    conflicts_add: list[ContextConflict | str] = Field(default_factory=list)
    conflicts_resolved: list[str] = Field(default_factory=list)
    open_gaps_add: list[ContextGap | str] = Field(default_factory=list)
    open_gaps_resolved: list[str] = Field(default_factory=list)
    active_claim_ids: list[str] = Field(default_factory=list)
    active_evidence_ids: list[str] = Field(default_factory=list)
    active_event_ids: list[str] = Field(default_factory=list)
    recent_progress: str = ""


class RollingCompactOutput(BaseModel):
    """Structured output for incremental rolling history compaction."""

    rolling_summary: str = ""


class ResearchReviewContext(BaseModel):
    """Bounded state supplied to Research Review in Graph mode."""

    run_id: str = ""
    task_coverage: str = ""
    confirmed_findings: list[ContextFinding | str] = Field(default_factory=list)
    unresolved_claims: list[str] = Field(default_factory=list)
    conflicts: list[ContextConflict | str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    evidence_gaps: list[ContextGap | str] = Field(default_factory=list)
    relevant_node_ids: list[str] = Field(default_factory=list)
    relevant_edge_ids: list[str] = Field(default_factory=list)


__all__ = [
    "Claim",
    "ContextConflict",
    "ContextFinding",
    "ContextGap",
    "Coverage",
    "Entity",
    "Evidence",
    "Event",
    "Finding",
    "GraphEdge",
    "GraphExtractionOutput",
    "GraphNode",
    "RelevantSubgraph",
    "ResearchDeltaGraph",
    "ResearchGraphScope",
    "ResearchNodeType",
    "ResearchRelationType",
    "ResearchReviewContext",
    "RawResearchDocument",
    "RollingCompactOutput",
    "Source",
    "SourceFinding",
    "Uncertainty",
    "WorkingContext",
    "WorkingContextDelta",
    "WriteReceipt",
]
