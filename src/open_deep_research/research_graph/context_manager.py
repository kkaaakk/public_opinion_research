"""Bounded Working Context updates for the Research Graph harness."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage

from open_deep_research.budget import budget_from_model_response
from open_deep_research.observability import observe_model_ainvoke
from open_deep_research.research_graph.models import (
    ContextConflict,
    ContextFinding,
    ContextGap,
    RelevantSubgraph,
    ResearchDeltaGraph,
    ResearchReviewContext,
    WorkingContext,
    WorkingContextDelta,
)
from open_deep_research.research_graph.retriever import format_relevant_subgraph


@dataclass
class ContextManagerResult:
    """Result of one structured Context Manager update."""

    context: WorkingContext
    delta: WorkingContextDelta
    budget_usage: dict[str, Any] = field(default_factory=dict)


class ContextManager:
    """Structured-output updater; it cannot call tools or schedule work."""

    def __init__(
        self,
        *,
        model: Any,
        model_name: str,
        max_retries: int = 3,
        max_active_findings: int = 8,
        max_active_claims: int = 16,
        max_active_evidence: int = 24,
        max_open_gaps: int = 8,
        max_conflicts: int = 8,
    ) -> None:
        """Configure the LLM updater and Working Context capacity limits."""
        self.model = model
        self.model_name = model_name
        self.max_retries = max_retries
        self.capacity = {
            "findings": max(0, int(max_active_findings)),
            "claims": max(0, int(max_active_claims)),
            "evidence": max(0, int(max_active_evidence)),
            "gaps": max(0, int(max_open_gaps)),
            "conflicts": max(0, int(max_conflicts)),
        }

    async def update(
        self,
        *,
        task: Any,
        current: WorkingContext,
        relevant_subgraph: RelevantSubgraph,
        research_delta: ResearchDeltaGraph | Iterable[ResearchDeltaGraph],
    ) -> ContextManagerResult:
        """Update the bounded work area from a graph delta and relevant subgraph."""
        if isinstance(research_delta, ResearchDeltaGraph):
            deltas = [research_delta]
        else:
            deltas = list(research_delta)
        prompt = build_context_manager_prompt(
            task=task,
            current=current,
            relevant_subgraph=relevant_subgraph,
            research_delta=deltas,
            capacity=self.capacity,
        )
        model = self.model
        if hasattr(model, "with_structured_output"):
            model = model.with_structured_output(WorkingContextDelta)
        if hasattr(model, "with_retry"):
            model = model.with_retry(stop_after_attempt=self.max_retries)
        response = await observe_model_ainvoke(
            model,
            [HumanMessage(content=prompt)],
            observer_model=self.model_name,
            observer_structured_output=True,
            observer_component="context_manager",
        )
        delta = _coerce_context_delta(response)
        available_ids = _available_graph_ids(current, relevant_subgraph, deltas)
        context = apply_working_context_delta(
            current,
            delta,
            available_graph_ids=available_ids,
            capacity=self.capacity,
        )
        return ContextManagerResult(
            context=context,
            delta=delta,
            budget_usage=budget_from_model_response(response),
        )


def initial_working_context(task: Any) -> WorkingContext:
    """Create a small context before the first ReAct model call."""
    objective = str(getattr(task, "objective", "") or task or "").strip()
    return WorkingContext(current_objective=objective)


def apply_working_context_delta(
    current: WorkingContext,
    delta: WorkingContextDelta,
    *,
    available_graph_ids: set[str] | None = None,
    capacity: dict[str, int] | None = None,
) -> WorkingContext:
    """Apply an LLM-selected delta while enforcing bounded/provenance-safe state."""
    limits = capacity or {
        "findings": 8,
        "claims": 16,
        "evidence": 24,
        "gaps": 8,
        "conflicts": 8,
    }
    available = set(available_graph_ids or ())
    available.update(current.active_claim_ids)
    available.update(current.active_evidence_ids)
    available.update(current.active_event_ids)
    for item in [*current.confirmed_findings, *current.conflicts, *current.open_gaps]:
        for attribute in ("finding_ids", "claim_ids", "evidence_ids", "source_ids"):
            available.update(getattr(item, attribute, []) or [])

    findings = [_coerce_finding(item) for item in current.confirmed_findings]
    findings = [
        item
        for item in findings
        if item is not None and _has_provenance(item, available)
    ]
    for item in delta.confirmed_findings_add:
        normalized = _coerce_finding(item)
        if normalized is not None and _has_provenance(normalized, available):
            findings.append(normalized)
    for item in delta.confirmed_findings_update:
        normalized = _coerce_finding(item)
        if normalized is None or not _has_provenance(normalized, available):
            continue
        replaced = False
        for index, existing in enumerate(findings):
            if _same_context_item(existing, normalized):
                findings[index] = normalized
                replaced = True
                break
        if not replaced:
            findings.append(normalized)

    conflicts = [_coerce_conflict(item) for item in current.conflicts]
    conflicts = [item for item in conflicts if item is not None]
    for item in delta.conflicts_add:
        normalized = _coerce_conflict(item)
        if normalized is not None and _has_provenance(normalized, available):
            conflicts.append(normalized)
    conflicts = [
        item
        for item in conflicts
        if not _is_resolved(item, delta.conflicts_resolved)
    ]
    conflicts = _take_last(conflicts, limits["conflicts"])

    gaps = [_coerce_gap(item) for item in current.open_gaps]
    gaps = [item for item in gaps if item is not None]
    for item in delta.open_gaps_add:
        normalized = _coerce_gap(item)
        if normalized is not None and _has_provenance(normalized, available):
            gaps.append(normalized)
    gaps = [item for item in gaps if not _is_resolved(item, delta.open_gaps_resolved)]
    gaps = _take_last(gaps, limits["gaps"])

    findings = _take_last(_dedupe_context_items(findings), limits["findings"])
    active_claims = _bounded_ids(delta.active_claim_ids, available, limits["claims"])
    active_evidence = _bounded_ids(delta.active_evidence_ids, available, limits["evidence"])
    active_events = _bounded_ids(delta.active_event_ids, available, limits.get("events", limits["evidence"]))

    return WorkingContext(
        current_objective=current.current_objective,
        confirmed_findings=findings,
        active_claim_ids=active_claims or _take_last(current.active_claim_ids, limits["claims"]),
        active_evidence_ids=active_evidence or _take_last(current.active_evidence_ids, limits["evidence"]),
        active_event_ids=active_events or _take_last(
            current.active_event_ids,
            limits.get("events", limits["evidence"]),
        ),
        conflicts=conflicts,
        open_gaps=gaps,
        recent_progress=(delta.recent_progress or current.recent_progress).strip(),
    )


def build_context_manager_prompt(
    *,
    task: Any,
    current: WorkingContext,
    relevant_subgraph: RelevantSubgraph,
    research_delta: list[ResearchDeltaGraph],
    capacity: dict[str, int],
) -> str:
    """Build a bounded, ID-preserving Context Manager prompt."""
    delta_text = []
    for delta in research_delta:
        nodes, edges = delta.materialize()
        delta_text.append(
            {
                "nodes": [
                    {"id": node.node_id, "type": node.node_type, "properties": node.properties}
                    for node in nodes
                ],
                "edges": [
                    {"id": edge.edge_id, "type": edge.relation_type, "from": edge.source_id, "to": edge.target_id}
                    for edge in edges
                ],
            }
        )
    objective = str(getattr(task, "objective", "") or task or "")
    return (
        "You are a structured Working Context updater inside a research harness. "
        "Do not call tools, make a final risk assessment, create follow-up tasks, "
        "or write a report. Compare the current context with this step's new graph "
        "delta and the retrieved subgraph. Decide what findings, conflicts, gaps, "
        "and active IDs should remain useful for the next ReAct step. Every semantic "
        "finding, conflict, and gap must cite graph IDs from the supplied material; "
        "do not invent IDs. Keep the output within the capacity limits.\n\n"
        f"Current task/objective:\n{objective}\n\n"
        f"Current Working Context:\n{json.dumps(current.model_dump(mode='json'), ensure_ascii=False)}\n\n"
        f"Relevant retrieved subgraph:\n{format_relevant_subgraph(relevant_subgraph)}\n\n"
        f"This step's Research Delta:\n{json.dumps(delta_text, ensure_ascii=False)}\n\n"
        f"Capacity limits:\n{json.dumps(capacity, ensure_ascii=False)}\n\n"
        "Return only WorkingContextDelta structured output."
    )


def build_research_review_context(
    *,
    run_id: str,
    working_contexts: dict[str, WorkingContext],
    relevant_subgraph: RelevantSubgraph,
) -> ResearchReviewContext:
    """Construct bounded Research Review input without full role reports."""
    confirmed: list[ContextFinding | str] = []
    conflicts: list[ContextConflict | str] = []
    gaps: list[ContextGap | str] = []
    for context in working_contexts.values():
        confirmed.extend(context.confirmed_findings)
        conflicts.extend(context.conflicts)
        gaps.extend(context.open_gaps)
    node_ids = {node.node_id for node in relevant_subgraph.nodes}
    claims = [node for node in relevant_subgraph.nodes if node.node_type == "Claim"]
    uncertainties = [node for node in relevant_subgraph.nodes if node.node_type == "Uncertainty"]
    return ResearchReviewContext(
        run_id=run_id,
        task_coverage="Working Context and graph subgraph are scoped to the current run.",
        confirmed_findings=confirmed[-16:],
        unresolved_claims=[
            str(node.properties.get("statement") or node.properties.get("summary") or node.node_id)
            for node in claims[-16:]
        ],
        conflicts=conflicts[-12:],
        uncertainties=[
            str(node.properties.get("summary") or node.node_id)
            for node in uncertainties[-12:]
        ],
        evidence_gaps=gaps[-12:],
        relevant_node_ids=sorted(node_ids),
        relevant_edge_ids=sorted(relevant_subgraph.edge_ids),
    )


def render_working_context(context: WorkingContext, *, max_chars: int = 10_000) -> str:
    """Render the bounded context for agent prompts."""
    text = json.dumps(context.model_dump(mode="json"), ensure_ascii=False, indent=2)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n[working context bounded]"
    return text


def _coerce_context_delta(response: Any) -> WorkingContextDelta:
    if isinstance(response, WorkingContextDelta):
        return response
    if isinstance(response, dict):
        return WorkingContextDelta.model_validate(response)
    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        return WorkingContextDelta.model_validate(model_dump())
    raise TypeError("Context Manager returned an invalid WorkingContextDelta.")


def _available_graph_ids(
    current: WorkingContext,
    subgraph: RelevantSubgraph,
    deltas: list[ResearchDeltaGraph],
) -> set[str]:
    available = set(subgraph.node_ids)
    available.update(current.active_claim_ids)
    available.update(current.active_evidence_ids)
    available.update(current.active_event_ids)
    for delta in deltas:
        nodes, _ = delta.materialize()
        available.update(node.node_id for node in nodes)
    return available


def _coerce_finding(item: Any) -> ContextFinding | None:
    if isinstance(item, ContextFinding):
        return item
    # A free-form finding cannot satisfy the provenance contract.  The
    # structured Context Manager schema should emit a ContextFinding instead.
    if isinstance(item, str):
        return None
    if isinstance(item, dict):
        try:
            return ContextFinding.model_validate(item)
        except Exception:
            return None
    return None


def _coerce_conflict(item: Any) -> ContextConflict | None:
    if isinstance(item, ContextConflict):
        return item
    if isinstance(item, dict):
        try:
            return ContextConflict.model_validate(item)
        except Exception:
            return None
    return None


def _coerce_gap(item: Any) -> ContextGap | None:
    if isinstance(item, ContextGap):
        return item
    if isinstance(item, dict):
        try:
            return ContextGap.model_validate(item)
        except Exception:
            return None
    return None


def _same_context_item(left: ContextFinding, right: ContextFinding) -> bool:
    return bool(
        set(left.finding_ids) & set(right.finding_ids)
        or set(left.claim_ids) & set(right.claim_ids)
        or left.summary.casefold() == right.summary.casefold()
    )


def _dedupe_context_items(items: list[ContextFinding]) -> list[ContextFinding]:
    result: list[ContextFinding] = []
    for item in items:
        if any(_same_context_item(item, existing) for existing in result):
            continue
        result.append(item)
    return result


def _has_provenance(item: Any, available: set[str]) -> bool:
    ids: set[str] = set()
    for attribute in ("finding_ids", "claim_ids", "evidence_ids", "source_ids"):
        ids.update(getattr(item, attribute, []) or [])
    return bool(ids & available) or not available


def _is_resolved(item: Any, resolutions: list[str]) -> bool:
    normalized = {str(value).casefold() for value in resolutions}
    item_ids: set[str] = set()
    for attribute in ("finding_ids", "claim_ids", "evidence_ids", "source_ids"):
        item_ids.update(str(value).casefold() for value in getattr(item, attribute, []) or [])
    return bool(item_ids & normalized or str(getattr(item, "summary", "")).casefold() in normalized)


def _bounded_ids(values: Iterable[str], available: set[str], limit: int) -> list[str]:
    values_list = list(dict.fromkeys(str(value) for value in values if str(value) in available))
    return _take_last(values_list, limit)


def _take_last(values: Iterable[Any], limit: int) -> list[Any]:
    """Take at most ``limit`` trailing values, including the zero case."""
    normalized_limit = max(0, int(limit))
    values_list = list(values)
    if normalized_limit == 0:
        return []
    return values_list[-normalized_limit:]


__all__ = [
    "ContextManager",
    "ContextManagerResult",
    "apply_working_context_delta",
    "build_context_manager_prompt",
    "build_research_review_context",
    "initial_working_context",
    "render_working_context",
]
