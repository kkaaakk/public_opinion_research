"""Apply existing State patch contracts explicitly in a deterministic order."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, cast
from uuid import uuid4

from langchain_core.messages import RemoveMessage, convert_to_messages

from open_deep_research.state import (
    DeepResearchState,
    agents_reducer,
    report_reducer,
    research_reducer,
    runtime_reducer,
    workflow_reducer,
)

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


def merge_research_patches(
    state: DeepResearchState, patches: Mapping[str, Mapping[str, Any]]
) -> None:
    """Validate ownership, then atomically join producer deltas in role order.

    Budgets/metrics add; tasks deduplicate; rounds take max. Role reports append
    unless explicitly replaced; role memory appends. Shared replacement fields
    must agree. Section additions retain role order, then report compilation
    selects the last valid version by name in the original plan order.
    """
    candidate = deepcopy(state)
    replacements: dict[str, Any] = {}
    for role in RESEARCH_ROLES:
        if role not in patches:
            continue
        patch = patches[role]
        for domain in _DOMAIN_UPDATES:
            update = patch.get(domain, {})
            if not isinstance(update, Mapping) or update.get("type") == "override":
                raise ValueError(f"Producer cannot replace shared {domain}")
        if set(patch.get("agents", {})) - {role}:
            raise ValueError(
                f"Producer {role} cannot write another role's private state"
            )
        workflow = patch.get("workflow", {})
        if set(workflow) - {"round", "completed_tasks"}:
            raise ValueError("Only Workflow owns brief, review and pending tasks")
        for task in workflow_reducer({}, workflow).get("completed_tasks", []):
            if task.target_role != role:
                raise ValueError(f"Producer {role} cannot complete another role's task")
        research = patch.get("research", {})
        if research.get("run_id", state.get("research", {}).get("run_id")) != state.get(
            "research", {}
        ).get("run_id"):
            raise ValueError("Producer cannot change the research run")
        for field in ("working_contexts", "working_context_scopes"):
            if set(research.get(field, {})) - {role}:
                raise ValueError(f"Producer {role} cannot write another role's {field}")
        for field in ("sections", "final"):
            if field in patch.get("report", {}):
                value = patch["report"][field]
                if field in replacements and replacements[field] != value:
                    raise ValueError(
                        f"Conflicting parallel report replacement: {field}"
                    )
                replacements[field] = value
        apply_patch(candidate, patch)
    state.clear()
    state.update(candidate)
