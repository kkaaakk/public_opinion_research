"""Regression guards for shared workflow state helpers."""

from open_deep_research.state import ResearchTask, research_tasks_reducer
from open_deep_research.workflow.state_utils import (
    agents_for_new_run,
    coerce_research_tasks,
    is_followup,
    resolve_research_run_id,
    role_context,
    role_memories,
    role_reports,
)


def _task(task_id: str = "task-1") -> ResearchTask:
    return ResearchTask(
        task_id=task_id,
        objective="Verify the claim",
        target_role="public_signal",
        evidence_needed="Two cited sources",
        reason="Evidence gap",
    )


def test_shared_role_helpers_preserve_reports_memories_and_new_run_state() -> None:
    state = {
        "agents": {
            "public_signal": {
                "report": "Public report",
                "memory": [{"finding": "signal"}],
            },
            "internal_knowledge": {"report": "", "memory": []},
        }
    }

    assert role_reports(state) == {"public_signal": "Public report"}
    assert role_memories(state) == {
        "public_signal": [{"finding": "signal"}],
        "internal_knowledge": [],
    }
    assert agents_for_new_run(state) == {
        "public_signal": {"memory": [{"finding": "signal"}]}
    }
    assert role_context(role_reports(state)) == "## public_signal\nPublic report"


def test_followup_and_run_id_resolution_priority_are_stable() -> None:
    config = {
        "configurable": {"research_run_id": "config-run", "thread_id": "thread"},
        "metadata": {"research_run_id": "metadata-run"},
    }
    assert is_followup({"workflow": {"round": 1}}) is False
    assert is_followup({"workflow": {"round": 2}}) is True
    assert resolve_research_run_id({"research": {"run_id": "state-run"}}, config) == "state-run"
    assert resolve_research_run_id({}, config) == "config-run"
    assert resolve_research_run_id({}, {"metadata": {"thread_id": "metadata-thread"}}) == "metadata-thread"


def test_task_coercion_and_reducer_share_the_same_identity_rule() -> None:
    first = _task("")
    duplicate = first.model_copy(update={"reason": "Another explanation"})
    distinct = first.model_copy(update={"evidence_needed": "One official source"})

    coerced = coerce_research_tasks([first.model_dump(), "invalid", duplicate])
    assert coerced == [first, duplicate]
    assert research_tasks_reducer([first], [duplicate, distinct]) == [first, distinct]
