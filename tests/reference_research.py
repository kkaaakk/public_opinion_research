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
    patch = {
        "agents": {
            role: {
                "report": {"type": "override", "value": result.report}
                if result.replace_report
                else result.report,
                "memory": deepcopy(result.memory),
                "rolling_summary": result.rolling_summary,
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
    if result.working_context is not None:
        patch["research"] = {
            "run_id": result.working_context_scope.run_id,
            "working_contexts": {role: result.working_context.model_dump(mode="json")},
            "working_context_scopes": {
                role: result.working_context_scope.model_dump(mode="json")
            },
        }
        patch["runtime"]["metrics"] = deepcopy(result.metrics_delta)
    return patch


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
