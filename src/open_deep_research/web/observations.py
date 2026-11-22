"""Read-only, owner-scoped Studio views. No execution commands or raw trace data."""

from __future__ import annotations

from open_deep_research.budget import merge_budget_usage
from open_deep_research.web.run_data import (
    RunNotFound,
    public_payload,
    read_request,
    result_budget,
    role_deliverables,
    stage_budget,
)
from open_deep_research.workflow.python_state import ANALYSIS_ROLES


def execution_view(store, run_id, owner, workflow_id, offset, limit, workflow_offset=0):
    """Read public DBOS catalog APIs, explicitly excluding inputs/outputs/errors."""
    read_request(run_id, owner)
    workflow_id = workflow_id or run_id
    if workflow_id != run_id and not workflow_id.startswith(run_id + ":"):
        raise RunNotFound("Workflow not found")
    workflows = store.client.list_workflows(
        workflow_id_prefix=run_id,
        load_input=False,
        load_output=False,
        limit=limit + 1,
        offset=workflow_offset,
    )
    selected = store.client.list_workflows(
        workflow_ids=[workflow_id],
        load_input=False,
        load_output=False,
    )
    if not selected:
        raise RunNotFound("Workflow not found")
    steps = store.client.list_workflow_steps(
        workflow_id,
        load_output=False,
        offset=offset,
        limit=limit + 1,
    )
    workflow_fields = (
        "workflow_id",
        "name",
        "status",
        "queue_name",
        "app_version",
        "parent_workflow_id",
        "created_at",
        "updated_at",
        "completed_at",
        "dequeued_at",
    )
    step_fields = (
        "function_id",
        "function_name",
        "child_workflow_id",
        "started_at_epoch_ms",
        "completed_at_epoch_ms",
    )
    return public_payload(
        {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "workflows": [
                {key: getattr(w, key, None) for key in workflow_fields}
                for w in workflows[:limit]
                if w.workflow_id == run_id or w.workflow_id.startswith(run_id + ":")
            ],
            "workflows_has_more": len(workflows) > limit,
            "next_workflow_offset": workflow_offset + min(len(workflows), limit),
            "steps": [{key: s.get(key) for key in step_fields} for s in steps[:limit]],
            "steps_has_more": len(steps) > limit,
            "next_offset": offset + min(len(steps), limit),
            "note": "Native journal commands; absent completion time is not proof of failure. Payloads are withheld.",
        }
    )


def stage_errors(client, run_id):
    """Read safe error summaries from failed native business workflows."""
    failed = client.list_workflows(
        workflow_id_prefix=run_id + ":",
        status="ERROR",
        name=["por.plan", "por.research", "por.review", "por.role"],
        load_input=False,
        load_output=False,
    )
    return [
        error
        for workflow in sorted(
            failed, key=lambda wf: wf.completed_at or 0, reverse=True
        )
        if (error := client.get_event(workflow.workflow_id, "failure", 0))
    ]


def stage_results(client, run_id):
    """Extract public deliverables and progress from the workflows that own them."""
    completed, roles, active = [], [], []
    outputs = []
    other_stages = []
    usage = {}
    for workflow in client.list_workflows(
        workflow_id_prefix=run_id + ":",
        name=["por.role", "por.review"],
        load_input=False,
        load_output=True,
    ):
        is_role = workflow.name == "por.role"
        role = workflow.workflow_id[len(run_id) + 1 :].split(":")[0]
        step = role + "_agent" if is_role else "research_review"
        if workflow.status == "PENDING":
            active.append((workflow.updated_at or 0, step))
        if workflow.status != "SUCCESS" or not workflow.output:
            continue
        outputs.append((workflow.workflow_id, step, workflow.output))

    for workflow in client.list_workflows(
        workflow_id_prefix=run_id + ":",
        name=["por.plan", "por.research"],
        load_input=False,
        load_output=False,
    ):
        stages = client.get_event(workflow.workflow_id, "completed_stages", 0) or {}
        other_stages.extend(stages.items())
        if workflow.name == "por.research":
            for role in ANALYSIS_ROLES:
                result = client.get_event(workflow.workflow_id, f"analysis:{role}", 0)
                if result is None:
                    continue
                step = role + "_agent"
                if result.get("running"):
                    if workflow.status == "PENDING":
                        active.append((workflow.updated_at or 0, step))
                else:
                    outputs.append((f"{run_id}:{role}", step, result))
            if (
                workflow.status == "PENDING"
                and "generate_final_report" not in stages
                and client.get_event(workflow.workflow_id, "report_started", 0)
            ):
                active.append((workflow.updated_at or 0, "generate_final_report"))
        elif workflow.status == "PENDING":
            active.append(
                (workflow.updated_at or 0, workflow.name.removeprefix("por."))
            )

    for stage_id, step, result in outputs:
        if not result.get("observation", {}).get("skipped"):
            completed.append(step)
        roles.extend(
            dict(
                role=role,
                content=content,
                stage_id=stage_id,
                completed_at="",
            )
            for role, content in role_deliverables(result).items()
        )
        budget = result_budget(result)
        usage = merge_budget_usage(
            usage,
            stage_budget(result)
            | {
                key: budget[key]
                for key in ("tool_calls", "search_calls")
                if key in budget
            },
        )
    for step, notes in other_stages:
        completed.append(step)
        usage = merge_budget_usage(usage, notes)

    return {
        "completed_steps": completed,
        "current_step": max(active, default=(0, ""))[1],
        "role_reports": roles,
        "report": client.get_event(run_id + ":research", "report", 0) or "",
        "stage_usage": usage,
    }


__all__ = ["execution_view", "stage_errors", "stage_results"]
