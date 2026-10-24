# 汇总研究工作流节点与图入口，保留对外稳定导出。
"""Workflow composition helpers for the public-opinion research graph."""

from open_deep_research.workflow.state_utils import (
    agents_for_new_run,
    agents_state,
    business_context,
    coerce_research_review,
    coerce_research_task,
    coerce_research_tasks,
    is_followup,
    report_state,
    research_state,
    resolve_research_run_id,
    role_context,
    role_memories,
    role_reports,
    runtime_state,
    tool_name,
    workflow_state,
)


def get_workflow(config=None):
    """Reject the retired in-process factory with an actionable migration error."""
    raise ValueError(
        "Use the durable Run API for DBOS execution; no in-process workflow factory"
    )


__all__ = [
    "get_workflow",
    "agents_for_new_run",
    "agents_state",
    "business_context",
    "coerce_research_review",
    "coerce_research_task",
    "coerce_research_tasks",
    "is_followup",
    "report_state",
    "research_state",
    "resolve_research_run_id",
    "role_context",
    "role_memories",
    "role_reports",
    "runtime_state",
    "tool_name",
    "workflow_state",
]
