"""Apply State patch contracts explicitly in a deterministic order."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, cast
from uuid import uuid4

from langchain_core.messages import RemoveMessage, convert_to_messages

from open_deep_research.budget import merge_budget_usage
from open_deep_research.state import (
    DeepResearchState,
    ResearchRoleResult,
    agents_reducer,
    report_reducer,
    runtime_reducer,
    workflow_reducer,
)
from open_deep_research.workflow.state_utils import coerce_research_tasks

_DOMAIN_UPDATES = {
    "workflow": workflow_reducer,
    "agents": agents_reducer,
    "report": report_reducer,
    "runtime": runtime_reducer,
}
RESEARCH_ROLES = ("public_signal", "internal_knowledge")
ANALYSIS_ROLES = ("risk_assessment", "response_strategy")


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
    """Expose shared reports while keeping each role's own report role-local."""
    snapshot = deepcopy(state)
    snapshot["agents"] = {
        name: value if name == role else {"report": value.get("report", "")}
        for name, value in snapshot.get("agents", {}).items()
    }
    return snapshot


def read_research_result(
    artifact: Mapping[str, Any], role: str, packet: Mapping[str, Any]
) -> ResearchRoleResult | None:
    """Read the producer result or an explicit disabled-role receipt, without I/O."""
    if "research_result" in artifact:
        if "patch" in artifact:
            raise ValueError("Ambiguous research Artifact")
        return ResearchRoleResult.model_validate(artifact["research_result"])
    patch = artifact.get("patch")
    if not isinstance(patch, Mapping) or patch:
        raise ValueError("Unsupported research Artifact")
    if artifact.get("observation") != {"skipped": True, "reason": "role_disabled"}:
        raise ValueError("Missing research result")
    return None


def assemble_research_results(
    state: DeepResearchState,
    results: Mapping[str, ResearchRoleResult | None],
    packets: Mapping[str, Mapping[str, Any]],
) -> None:
    """Join a complete dispatched batch in role order, then commit atomically.

    None is an explicit disabled-role receipt. Progress comes from packets, not
    producers. Copy only changed aggregates; reports/messages stay put.
    """
    if set(results) != set(packets) or set(results) - set(RESEARCH_ROLES):
        raise ValueError("Research results must match the dispatched roles")
    agents = state.get("agents", {})
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
        value = result.model_dump(mode="json")  # detach nested mutable result data
        agent = {"report": value["report"]}
        if result.replace_report:
            agent["report"] = {"type": "override", "value": result.report}
        agents = agents_reducer(agents, {role: agent})
        runtime = {
            **runtime,
            "budget": merge_budget_usage(runtime.get("budget"), result.budget_delta),
        }
        tasks = coerce_research_tasks(
            packet.get("workflow", {}).get("pending_tasks", [])
        )
        # Only usable evidence completes follow-up tasks. A degraded placeholder
        # must not filter the same gap out of the next Review's dispatch.
        workflow = workflow_reducer(
            workflow,
            {
                "round": result.round,
                "completed_tasks": [t for t in tasks if t.target_role == role]
                if result.round > 1 and result.evidence_collected
                else [],
            },
        )
    state.update(agents=agents, runtime=runtime, workflow=workflow)
