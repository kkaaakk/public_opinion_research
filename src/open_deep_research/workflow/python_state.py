"""Apply existing State patch contracts explicitly in a deterministic order."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, cast
from uuid import uuid4

from langchain_core.messages import RemoveMessage, convert_to_messages

from open_deep_research.budget import empty_budget_usage, merge_budget_usage
from open_deep_research.research_graph.metrics import merge_research_graph_metrics
from open_deep_research.state import (
    DeepResearchState,
    ResearchRoleResult,
    ResearchTask,
    agents_reducer,
    report_reducer,
    research_reducer,
    runtime_reducer,
    workflow_reducer,
)
from open_deep_research.workflow.state_utils import coerce_research_tasks

_DOMAIN_UPDATES = {
    "workflow": workflow_reducer,
    "agents": agents_reducer,
    "research": research_reducer,
    "report": report_reducer,
    "runtime": runtime_reducer,
}
RESEARCH_ROLES = ("public_signal", "internal_knowledge")


def apply_patch(state: DeepResearchState, patch: Mapping[str, Any]) -> None:
    """Commit one delta using the shared pure merge rules, without graph channels."""
    unknown = set(patch) - {*_DOMAIN_UPDATES, "messages"}
    if unknown:
        raise ValueError(f"Unknown State domains: {sorted(unknown)}")
    updates: dict[str, Any] = {
        name: merge(state.get(name, {}), deepcopy(patch[name]))
        for name, merge in _DOMAIN_UPDATES.items()
        if name in patch
    }
    if "messages" in patch:
        messages = deepcopy(list(state.get("messages", [])))
        for message in convert_to_messages(deepcopy(patch["messages"])):
            if message.id is None:
                message.id = str(uuid4())
            index = next(
                (i for i, old in enumerate(messages) if old.id == message.id), None
            )
            if isinstance(message, RemoveMessage):
                if index is None:
                    raise ValueError(f"Cannot remove absent message {message.id}")
                messages.pop(index)
            elif index is None:
                messages.append(message)
            else:
                messages[index] = message
        updates["messages"] = messages
    state.update(cast(DeepResearchState, updates))


def copy_state(value: Mapping[str, Any]) -> DeepResearchState:
    """Copy caller-owned values and normalize the existing message input contract."""
    state = cast(DeepResearchState, {name: {} for name in _DOMAIN_UPDATES})
    state["messages"] = []
    apply_patch(state, value)
    return state


def role_snapshot(state: DeepResearchState, role: str) -> DeepResearchState:
    """Expose shared reports while keeping memory and rolling summaries role-local."""
    snapshot = deepcopy(state)
    snapshot["agents"] = {
        name: value if name == role else {"report": value.get("report", "")}
        for name, value in snapshot.get("agents", {}).items()
    }
    return snapshot


def read_research_result(
    artifact: Mapping[str, Any], role: str, packet: Mapping[str, Any]
) -> ResearchRoleResult | None:
    """Read current JSON or the exact historical producer Artifact, without I/O.

    Unsupported historical fields fail closed: drain them on their original
    release instead of silently discarding data or replaying an opaque unit.
    """
    if "research_result" in artifact:
        if "patch" in artifact:
            raise ValueError("Ambiguous research Artifact")
        return ResearchRoleResult.model_validate(artifact["research_result"])
    patch = artifact.get("patch")
    if not isinstance(patch, Mapping):
        raise ValueError("Missing legacy research Patch")
    if artifact.get("observation") == {"skipped": True, "reason": "role_disabled"}:
        if patch not in ({}, {"runtime": {"budget": empty_budget_usage()}}):
            raise ValueError("Skipped research role contains unexpected result data")
        return None
    allowed = {
        "agents": {role},
        "workflow": {"round", "completed_tasks"},
        "research": {"run_id", "working_contexts", "working_context_scopes"},
        "runtime": {"budget", "metrics"},
    }
    if set(patch) - allowed.keys() or any(
        not isinstance(patch.get(k, {}), Mapping) or set(patch.get(k, {})) - keys
        for k, keys in allowed.items()
    ):
        raise ValueError("Unsupported legacy research Patch fields")
    agent = patch.get("agents", {}).get(role, {})
    if not agent or set(agent) - {"report", "memory", "rolling_summary"}:
        raise ValueError("Invalid legacy research role result")
    workflow = packet.get("workflow", {})
    round_ = workflow.get("round", 1)
    progress = patch.get("workflow", {})
    tasks = [
        ResearchTask.model_validate(t) for t in progress.get("completed_tasks", [])
    ]
    expected = (
        coerce_research_tasks(workflow.get("pending_tasks", [])) if round_ > 1 else []
    )
    if progress.get("round", round_) != round_ or tasks != expected:
        raise ValueError("Legacy research progress differs from dispatched work")
    graph = patch.get("research", {})
    if graph.get("run_id", packet.get("research", {}).get("run_id")) != packet.get(
        "research", {}
    ).get("run_id"):
        raise ValueError("Legacy research run mismatch")
    for key in ("working_contexts", "working_context_scopes"):
        if set(graph.get(key, {})) - {role}:
            raise ValueError("Legacy Working Context ownership mismatch")
    report = agent.get("report", "")
    replace = isinstance(report, Mapping)
    if replace and (set(report) != {"type", "value"} or report["type"] != "override"):
        raise ValueError("Unsupported legacy report replacement")
    runtime = patch.get("runtime", {})
    return ResearchRoleResult(
        role=role,
        round=round_,
        report=report["value"] if replace else report,
        replace_report=replace,
        memory=agent.get("memory", []),
        rolling_summary=agent.get("rolling_summary", ""),
        working_context=graph.get("working_contexts", {}).get(role),
        working_context_scope=graph.get("working_context_scopes", {}).get(role),
        budget_delta=runtime.get("budget", {}),
        metrics_delta=runtime.get("metrics", {}),
    )


def assemble_research_results(
    state: DeepResearchState,
    results: Mapping[str, ResearchRoleResult | None],
    packets: Mapping[str, Mapping[str, Any]],
) -> None:
    """Join a complete dispatched batch in role order, then commit atomically.

    None is an explicit disabled-role receipt. Progress comes from packets, not
    producers. Copy only changed aggregates; reports/messages/evidence stay put.
    """
    if set(results) != set(packets) or set(results) - set(RESEARCH_ROLES):
        raise ValueError("Research results must match the dispatched roles")
    agents = state.get("agents", {})
    graph = dict(state.get("research", {}))
    runtime = state.get("runtime", {})
    workflow = state.get("workflow", {})
    for role in RESEARCH_ROLES:
        if (result := results.get(role)) is None:
            continue
        result = ResearchRoleResult.model_validate(result.model_dump())
        packet = packets[role]
        if result.role != role or result.round != packet.get("workflow", {}).get(
            "round", 1
        ):
            raise ValueError("Research result role or round mismatch")
        if (
            result.working_context_scope
            and result.working_context_scope.run_id != graph.get("run_id")
        ):
            raise ValueError("Research result run mismatch")
        value = result.model_dump(mode="json")  # detach nested mutable result data
        agent = {key: value[key] for key in ("report", "memory", "rolling_summary")}
        if result.replace_report:
            agent["report"] = {"type": "override", "value": result.report}
        agents = agents_reducer(agents, {role: agent})
        if result.working_context is not None:
            for field, key in (
                ("working_contexts", "working_context"),
                ("working_context_scopes", "working_context_scope"),
            ):
                graph[field] = {**graph.get(field, {}), role: value[key]}
        runtime = {
            **runtime,
            "budget": merge_budget_usage(runtime.get("budget"), result.budget_delta),
        }
        if result.metrics_delta:
            runtime["metrics"] = merge_research_graph_metrics(
                runtime.get("metrics"), result.metrics_delta
            )
        tasks = coerce_research_tasks(
            packet.get("workflow", {}).get("pending_tasks", [])
        )
        workflow = workflow_reducer(
            workflow,
            {
                "round": result.round,
                "completed_tasks": [t for t in tasks if t.target_role == role]
                if result.round > 1
                else [],
            },
        )
    state.update(agents=agents, research=graph, runtime=runtime, workflow=workflow)
