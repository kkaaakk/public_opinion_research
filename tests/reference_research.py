"""Frozen 31e8ad8 producer Patch behavior, used only by the historical oracle.

The result-to-Patch bridge preserves its input boundary; it does not call the
new assembler or the production legacy reader. Keep the historical merge checks.
"""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from open_deep_research.state import (
    DeepResearchState,
    ResearchRoleResult,
    workflow_reducer,
)
from open_deep_research.workflow.python_state import (
    _DOMAIN_UPDATES,
    RESEARCH_ROLES,
    apply_patch,
)
from open_deep_research.workflow.state_utils import coerce_research_tasks, is_followup


def legacy_research_patch(result, state):
    if not isinstance(result, ResearchRoleResult):
        return result
    role = result.role
    return {
        "agents": {
            role: {
                "report": {"type": "override", "value": result.report}
                if result.replace_report
                else result.report,
            }
        },
        "workflow": {
            "completed_tasks": coerce_research_tasks(
                state.get("workflow", {}).get("pending_tasks", [])
            )
            if is_followup(state)
            else [],
            **({"round": result.round} if is_followup(state) else {}),
        },
        "runtime": {"budget": deepcopy(result.budget_delta)},
    }


def merge_research_patches(
    state: DeepResearchState, patches: Mapping[str, Mapping[str, Any]]
) -> None:
    """Validate ownership, then atomically join producer deltas in role order.

    Budgets add; tasks deduplicate; rounds take max. Role reports append unless
    explicitly replaced. Shared report replacements must agree.
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
        report = patch.get("report", {})
        if "final" in report:
            if "final" in replacements and replacements["final"] != report["final"]:
                raise ValueError("Conflicting parallel report replacement: final")
            replacements["final"] = report["final"]
        apply_patch(candidate, patch)
    state.clear()
    state.update(candidate)
