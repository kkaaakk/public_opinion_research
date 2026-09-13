"""Serializable LangGraph state grouped by data ownership."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from langgraph.graph import MessagesState
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from open_deep_research.budget import empty_budget_usage, merge_budget_usage
from open_deep_research.research_graph.metrics import merge_research_graph_metrics

BusinessAgentRole = Literal[
    "public_signal", "internal_knowledge", "risk_assessment", "response_strategy"
]


class ResearchComplete(BaseModel):
    """Call this tool to indicate that the research is complete."""


class Summary(BaseModel):
    """Research summary with representative excerpts."""
    summary: str
    key_excerpts: str


class ClarifyWithUser(BaseModel):
    """Structured clarification decision."""
    need_clarification: bool = Field(description="Whether clarification is required.")
    question: str = Field(description="Question used to clarify the report scope.")
    verification: str = Field(description="Confirmation shown before research starts.")


class ResearchQuestion(BaseModel):
    """Structured research brief output."""
    research_brief: str = Field(description="Research question used to guide research.")


class ResearchTask(BaseModel):
    """One evidence-gap task assigned to a producer role."""

    task_id: str = Field(description="Stable task identifier within the run.")
    objective: str
    target_role: Literal["public_signal", "internal_knowledge"]
    evidence_needed: str
    reason: str
    priority: Literal["high", "medium", "low"] = "medium"


class ResearchReview(BaseModel):
    """Evidence sufficiency review and optional follow-up tasks."""
    research_complete: bool
    confirmed_findings: list[str] = Field(default_factory=list)
    unresolved_claims: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    research_gaps: list[str] = Field(default_factory=list)
    next_tasks: list[ResearchTask] = Field(default_factory=list)


class SearchQuery(BaseModel):
    """One follow-up search query."""
    search_query: str


class Section(BaseModel):
    """One planned report section."""

    name: str
    description: str
    research: bool = True
    content: str = ""
    agent_role: str = ""
    status: Literal["pending", "done"] = "pending"


class Sections(BaseModel):
    """Structured section plan output."""
    sections: list[Section]


class Feedback(BaseModel):
    """Section quality feedback."""
    grade: Literal["pass", "fail"]
    follow_up_queries: list[SearchQuery]


class WorkflowState(TypedDict, total=False):
    """Where the public-opinion workflow is in the current run."""

    brief: str
    round: int
    review: ResearchReview | None
    pending_tasks: list[ResearchTask]
    completed_tasks: list[ResearchTask]


class AgentRoleState(TypedDict, total=False):
    """State privately owned by one business agent role."""

    report: str
    memory: list[dict[str, Any]]
    rolling_summary: str


class ResearchState(TypedDict, total=False):
    """Serializable references and Working Context for a research run."""

    run_id: str
    working_contexts: dict[str, dict[str, Any]]


class ReportState(TypedDict, total=False):
    """Plan, section outputs, feedback, and final report."""
    sections: list[Section]
    completed_sections: list[Section]
    plan_feedback: list[str]
    final: str


class RuntimeState(TypedDict, total=False):
    """Non-business execution accounting and observability data."""

    budget: dict[str, Any]
    metrics: dict[str, Any]


def _is_override(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("type") == "override"


def _override_value(value: Any, default: Any) -> Any:
    return value.get("value", default) if _is_override(value) else value


def _coerce_research_task(value: Any) -> ResearchTask | None:
    if isinstance(value, ResearchTask):
        return value
    if isinstance(value, Mapping):
        try:
            return ResearchTask.model_validate(value)
        except Exception:
            return None
    return None


def _task_values(value: Any) -> list[Any]:
    value = _override_value(value, [])
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def research_tasks_reducer(current_value: Any, new_value: Any) -> list[ResearchTask]:
    """Merge and de-duplicate task values; an override token clears/replaces."""
    values = _task_values(new_value) if _is_override(new_value) else (
        _task_values(current_value) + _task_values(new_value)
    )
    merged: list[ResearchTask] = []
    seen: set[str] = set()
    for value in values:
        task = _coerce_research_task(value)
        if task is None:
            continue
        identity = task.task_id.strip() or (
            f"{task.target_role}:{task.objective.strip()}:{task.evidence_needed.strip()}"
        )
        if identity not in seen:
            seen.add(identity)
            merged.append(task)
    return merged


def _merge_round(current: Any, incoming: Any) -> int:
    incoming = _override_value(incoming, 1)
    try:
        left = int(current or 1)
    except (TypeError, ValueError):
        left = 1
    try:
        right = int(incoming or 1)
    except (TypeError, ValueError):
        right = 1
    return max(1, left, right)


def workflow_reducer(current_value: Any, new_value: Any) -> dict[str, Any]:
    """Merge workflow progress safely across parallel producer updates."""
    if _is_override(new_value):
        replacement = _override_value(new_value, {})
        return dict(replacement) if isinstance(replacement, Mapping) else {}
    current = dict(current_value) if isinstance(current_value, Mapping) else {}
    incoming = dict(new_value) if isinstance(new_value, Mapping) else {}
    merged: dict[str, Any] = dict(current)
    for key in ("brief", "review"):
        if key in incoming:
            merged[key] = incoming[key]
    if "round" in incoming:
        merged["round"] = _merge_round(current.get("round"), incoming["round"])
    if "pending_tasks" in incoming:
        merged["pending_tasks"] = research_tasks_reducer(
            current.get("pending_tasks", []), incoming["pending_tasks"]
        )
    if "completed_tasks" in incoming:
        merged["completed_tasks"] = research_tasks_reducer(
            current.get("completed_tasks", []), incoming["completed_tasks"]
        )
    return merged


def _merge_role_state(current: Any, incoming: Any) -> dict[str, Any]:
    left = dict(current) if isinstance(current, Mapping) else {}
    right = dict(incoming) if isinstance(incoming, Mapping) else {}
    merged: dict[str, Any] = dict(left)
    if "report" in right:
        report_update = right["report"]
        if _is_override(report_update):
            merged["report"] = str(_override_value(report_update, "") or "")
        else:
            previous = str(left.get("report") or "")
            report = str(report_update or "")
            merged["report"] = (
                f"{previous}\n\n--- Additional role research report ---\n{report}"
                if previous and report and report != previous
                else report or previous
            )
    if "memory" in right:
        memory_update = right["memory"]
        if _is_override(memory_update):
            merged["memory"] = list(_override_value(memory_update, []) or [])
        else:
            additions = memory_update if isinstance(memory_update, list) else [memory_update]
            merged["memory"] = list(left.get("memory", []) or []) + list(additions)
    if "rolling_summary" in right:
        merged["rolling_summary"] = str(right["rolling_summary"] or "")
    return merged


def agents_reducer(current_value: Any, new_value: Any) -> dict[str, Any]:
    """Merge per-role outputs without parallel-role overwrite."""
    if _is_override(new_value):
        replacement = _override_value(new_value, {})
        return dict(replacement) if isinstance(replacement, Mapping) else {}
    merged = dict(current_value) if isinstance(current_value, Mapping) else {}
    if isinstance(new_value, Mapping):
        for role, update in new_value.items():
            if role != "type":
                merged[str(role)] = _merge_role_state(merged.get(str(role)), update)
    return merged


def research_reducer(current_value: Any, new_value: Any) -> dict[str, Any]:
    """Merge per-run references and role-scoped Working Context values."""
    if _is_override(new_value):
        replacement = _override_value(new_value, {})
        return dict(replacement) if isinstance(replacement, Mapping) else {}
    current = dict(current_value) if isinstance(current_value, Mapping) else {}
    incoming = dict(new_value) if isinstance(new_value, Mapping) else {}
    merged: dict[str, Any] = dict(current)
    if incoming.get("run_id"):
        merged["run_id"] = str(incoming["run_id"])
    if "working_contexts" in incoming:
        contexts = dict(current.get("working_contexts", {}) or {})
        update = incoming["working_contexts"]
        if _is_override(update):
            contexts = dict(_override_value(update, {}) or {})
        elif isinstance(update, Mapping):
            contexts.update({str(role): value for role, value in update.items()})
        merged["working_contexts"] = contexts
    return merged


def report_reducer(current_value: Any, new_value: Any) -> dict[str, Any]:
    """Merge plan and section results, preserving parallel section output."""
    if _is_override(new_value):
        replacement = _override_value(new_value, {})
        return dict(replacement) if isinstance(replacement, Mapping) else {}
    current = dict(current_value) if isinstance(current_value, Mapping) else {}
    incoming = dict(new_value) if isinstance(new_value, Mapping) else {}
    merged: dict[str, Any] = dict(current)
    for key in ("sections", "final"):
        if key in incoming:
            merged[key] = _override_value(incoming[key], [] if key == "sections" else "")
    for key in ("completed_sections", "plan_feedback"):
        if key in incoming:
            value = incoming[key]
            merged[key] = (
                list(_override_value(value, []) or [])
                if _is_override(value)
                else list(current.get(key, []) or []) + list(value or [])
            )
    return merged


def runtime_reducer(current_value: Any, new_value: Any) -> dict[str, Any]:
    """Accumulate budget and graph metrics independently of routing."""
    if _is_override(new_value):
        replacement = _override_value(new_value, {})
        return dict(replacement) if isinstance(replacement, Mapping) else {}
    current = dict(current_value) if isinstance(current_value, Mapping) else {}
    incoming = dict(new_value) if isinstance(new_value, Mapping) else {}
    merged: dict[str, Any] = dict(current)
    if "budget" in incoming:
        budget = incoming["budget"]
        merged["budget"] = (
            dict(_override_value(budget, empty_budget_usage()))
            if _is_override(budget)
            else merge_budget_usage(current.get("budget", {}), budget)
        )
    if "metrics" in incoming:
        metrics = incoming["metrics"]
        merged["metrics"] = (
            dict(_override_value(metrics, {}))
            if _is_override(metrics)
            else merge_research_graph_metrics(current.get("metrics", {}), metrics)
        )
    return merged


class AgentInputState(MessagesState):
    """External input remains LangGraph's native messages channel."""


class DeepResearchState(MessagesState):
    """Top-level graph state: five owned domains plus native messages."""

    workflow: Annotated[WorkflowState, workflow_reducer]
    agents: Annotated[dict[str, AgentRoleState], agents_reducer]
    research: Annotated[ResearchState, research_reducer]
    report: Annotated[ReportState, report_reducer]
    runtime: Annotated[RuntimeState, runtime_reducer]


# Compatibility and semantic aliases only; DeepResearchState is the sole schema.
AgentState = DeepResearchState
PublicOpinionState = DeepResearchState


__all__ = [
    "AgentInputState", "AgentRoleState", "AgentState", "BusinessAgentRole",
    "ClarifyWithUser", "DeepResearchState", "Feedback", "PublicOpinionState",
    "ReportState", "ResearchComplete", "ResearchQuestion", "ResearchReview",
    "ResearchState", "ResearchTask", "RuntimeState", "SearchQuery", "Section",
    "Sections", "Summary", "WorkflowState", "agents_reducer", "report_reducer",
    "research_reducer", "research_tasks_reducer", "runtime_reducer",
    "workflow_reducer",
]
