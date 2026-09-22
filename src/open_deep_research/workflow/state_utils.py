"""Shared state access and coercion helpers for workflow nodes.

One authoritative implementation for the state-shape helpers that were
previously duplicated between the main researcher module and the business-agent
runtime.  These are intentionally plain functions: no manager/service layer.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig

from open_deep_research.state import (
    ResearchReview,
    ResearchTask,
    research_task_identity,
)

__all__ = [
    "agents_for_new_run",
    "agents_state",
    "business_context",
    "coerce_research_review",
    "coerce_research_task",
    "coerce_research_tasks",
    "is_followup",
    "report_state",
    "research_state",
    "research_task_identity",
    "research_task_payload",
    "resolve_research_run_id",
    "role_context",
    "role_memories",
    "role_reports",
    "runtime_state",
    "tool_name",
    "workflow_state",
]


def workflow_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the workflow domain of the graph state."""
    return dict(state.get("workflow", {}) or {})


def agents_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the per-role agent domain of the graph state."""
    return dict(state.get("agents", {}) or {})


def research_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the research domain of the graph state."""
    return dict(state.get("research", {}) or {})


def report_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the report domain of the graph state."""
    return dict(state.get("report", {}) or {})


def runtime_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the runtime accounting domain of the graph state."""
    return dict(state.get("runtime", {}) or {})


def role_reports(state: Mapping[str, Any]) -> dict[str, str]:
    """Map each role to its formal report, skipping roles without one."""
    return {
        str(role): str(value.get("report") or "")
        for role, value in agents_state(state).items()
        if isinstance(value, Mapping) and value.get("report")
    }


def role_memories(state: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Map each role to its private memory list."""
    return {
        str(role): list(value.get("memory", []) or [])
        for role, value in agents_state(state).items()
        if isinstance(value, Mapping)
    }


def agents_for_new_run(state: Mapping[str, Any]) -> dict[str, Any]:
    """Carry private memory forward while clearing run-scoped reports/summaries."""
    return {
        role: {"memory": list(value.get("memory", []) or [])}
        for role, value in agents_state(state).items()
        if isinstance(value, Mapping) and value.get("memory")
    }


def is_followup(state: Mapping[str, Any]) -> bool:
    """Return whether the workflow is in a follow-up research round."""
    return int(workflow_state(state).get("round", 1) or 1) > 1


def business_context(configurable: Any) -> str:
    """Build business context for business-scenario prompts."""
    context = (configurable.organization_context or "").strip()
    if context:
        return context
    return (
        "No additional organization context was configured. Use the user's request, "
        "local RAG evidence, and cited public sources without inventing company facts."
    )


def tool_name(available_tool: Any) -> str:
    """Return a stable name for LangChain tools and provider-native tool dicts."""
    if isinstance(available_tool, dict):
        return str(available_tool.get("name") or "web_search")
    return str(getattr(available_tool, "name", ""))


def coerce_research_task(value: Any) -> ResearchTask | None:
    """Normalize a task from a Pydantic or serialized LangGraph state value."""
    if isinstance(value, ResearchTask):
        return value
    if isinstance(value, dict):
        try:
            return ResearchTask.model_validate(value)
        except Exception:
            return None
    return None


def coerce_research_tasks(value: Any) -> list[ResearchTask]:
    """Normalize a list-like task channel and ignore malformed task values."""
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    return [
        task
        for item in values
        if (task := coerce_research_task(item)) is not None
    ]


def coerce_research_review(value: Any) -> ResearchReview | None:
    """Normalize a review from a Pydantic or serialized LangGraph state value."""
    if isinstance(value, ResearchReview):
        return value
    if isinstance(value, dict):
        try:
            return ResearchReview.model_validate(value)
        except Exception:
            return None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return ResearchReview.model_validate(model_dump())
        except Exception:
            return None
    return None


def research_task_payload(task: ResearchTask) -> dict[str, Any]:
    """Serialize a task for a dynamic Send payload."""
    return task.model_dump()


def resolve_research_run_id(
    state: Mapping[str, Any] | dict[str, Any],
    config: RunnableConfig | None = None,
) -> str:
    """Resolve one stable run scope without exposing credentials or raw history."""
    state_run_id = str(research_state(state).get("run_id") or "").strip()
    if state_run_id:
        return state_run_id
    configurable: Mapping[str, Any] = (
        config.get("configurable", {}) if isinstance(config, Mapping) else {}
    )
    for key in ("research_run_id", "thread_id", "run_id"):
        value = str(configurable.get(key) or "").strip()
        if value:
            return value
    metadata = config.get("metadata", {}) if isinstance(config, Mapping) else {}
    for key in ("research_run_id", "thread_id", "run_id"):
        value = str(metadata.get(key) or "").strip() if isinstance(metadata, Mapping) else ""
        if value:
            return value
    # A new root invocation without a LangGraph thread id still gets a unique
    # scope.  research_phase writes it into state before parallel Sends begin.
    return f"run_{uuid.uuid4().hex}"


def role_context(
    role_reports: dict[str, str],
    roles: tuple[str, ...] | None = None,
) -> str:
    """Format upstream role reports for downstream public-opinion agents."""
    if not role_reports:
        return "No upstream role reports are available yet."

    role_names = roles if roles is not None else tuple(role_reports)
    formatted_reports = [
        f"## {role}\n{report}"
        for role in role_names
        for report in [role_reports.get(role, "")]
        if report
    ]
    return "\n\n".join(formatted_reports) or "No upstream role reports are available yet."
