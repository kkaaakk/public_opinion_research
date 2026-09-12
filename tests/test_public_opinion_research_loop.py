"""Tests for dynamic Plan-and-Execute research inside the public-opinion subgraph."""

import asyncio

from langgraph.types import Send

import open_deep_research.deep_researcher as deep_researcher_module
from open_deep_research.state import ResearchReview, ResearchTask, agents_reducer


def _config(*, max_research_rounds: int = 2) -> dict:
    """Build a small deterministic subgraph configuration for loop tests."""
    return {
        "configurable": {
            "agent_observer_enabled": False,
            "enabled_business_agents": [
                "public_signal",
                "internal_knowledge",
                "risk_assessment",
                "response_strategy",
            ],
            "max_research_rounds": max_research_rounds,
            "research_model": "fixture:model",
        }
    }


def _task(task_id: str, role: str) -> ResearchTask:
    """Create a valid fixture task."""
    return ResearchTask(
        task_id=task_id,
        objective=f"Resolve {task_id}",
        target_role=role,
        evidence_needed=f"Evidence for {task_id}",
        reason=f"{task_id} could change the risk judgment",
        priority="high",
    )


class _ReviewModel:
    """Minimal structured-output model fixture used by the review node."""

    def __init__(self, reviews: list[ResearchReview]) -> None:
        self.reviews = iter(reviews)
        self.calls = 0

    def with_structured_output(self, _schema):
        return self

    def with_retry(self, **_kwargs):
        return self

    def with_config(self, _config):
        return self

    async def ainvoke(self, _messages):
        self.calls += 1
        return next(self.reviews)


def _fake_agent(calls: list[tuple[str, str, list[str]]]):
    """Return a fake formal agent implementation while preserving loop state."""

    async def run(state, _config, role):
        workflow = state.get("workflow", {})
        mode = "followup" if workflow.get("round", 1) > 1 else "initial"
        tasks = workflow.get("pending_tasks", []) or []
        task_ids = [
            task.task_id if isinstance(task, ResearchTask) else task["task_id"]
            for task in tasks
        ]
        calls.append((role, mode, task_ids))
        result = {
            "agents": {role: {"report": f"{role} {mode} report", "memory": []}},
            "workflow": {"completed_tasks": tasks if mode == "followup" else []},
            "runtime": {"budget": {}},
        }
        if mode == "followup":
            result["workflow"]["round"] = workflow.get("round", 2)
        return result

    return run


def _initial_state() -> dict:
    """Build the complete public-opinion state input used by the graph."""
    return {
        "messages": [],
        "workflow": {"brief": "brand risk fixture", "round": 1, "review": None,
                     "pending_tasks": [], "completed_tasks": []},
        "agents": {},
        "research": {"run_id": "fixture-run", "working_contexts": {}},
        "report": {},
        "runtime": {"budget": {}, "metrics": {}},
    }


def _invoke_with_fixtures(
    monkeypatch,
    reviews: list[ResearchReview],
    *,
    max_rounds: int = 2,
):
    """Invoke the compiled subgraph with deterministic review and agent fixtures."""
    reviewer = _ReviewModel(reviews)
    calls: list[tuple[str, str, list[str]]] = []
    monkeypatch.setattr(deep_researcher_module, "configurable_model", reviewer)
    monkeypatch.setattr(
        deep_researcher_module,
        "_run_public_opinion_agent",
        _fake_agent(calls),
    )
    result = asyncio.run(
        deep_researcher_module.public_opinion_subgraph.ainvoke(
            _initial_state(),
            _config(max_research_rounds=max_rounds),
        )
    )
    return result, calls, reviewer


def test_sufficient_initial_research_goes_directly_to_risk(monkeypatch) -> None:
    """A complete first review does not launch a follow-up round."""
    result, calls, reviewer = _invoke_with_fixtures(
        monkeypatch,
        [ResearchReview(research_complete=True, confirmed_findings=["enough"])],
    )

    assert reviewer.calls == 1
    assert {(role, mode) for role, mode, _ in calls} == {
        ("public_signal", "initial"),
        ("internal_knowledge", "initial"),
        ("risk_assessment", "initial"),
        ("response_strategy", "initial"),
    }
    assert result["workflow"]["round"] == 1


def test_public_only_followup_returns_to_review_and_merges_reports(monkeypatch) -> None:
    """A public-only gap re-enters the formal public agent and then review."""
    task = _task("public-baseline", "public_signal")
    result, calls, reviewer = _invoke_with_fixtures(
        monkeypatch,
        [
            ResearchReview(research_complete=False, next_tasks=[task]),
            ResearchReview(research_complete=True, confirmed_findings=["resolved"]),
        ],
    )

    assert reviewer.calls == 2
    assert [(role, mode) for role, mode, _ in calls].count(
        ("public_signal", "followup")
    ) == 1
    assert not [
        call for call in calls if call[:2] == ("internal_knowledge", "followup")
    ]
    assert result["workflow"]["round"] == 2
    assert [item.task_id for item in result["workflow"]["completed_tasks"]] == [
        "public-baseline"
    ]
    assert "public_signal initial report" in result["agents"]["public_signal"]["report"]
    assert "public_signal followup report" in result["agents"]["public_signal"]["report"]


def test_internal_only_followup_uses_internal_agent(monkeypatch) -> None:
    """An internal-only gap does not launch the public-signal agent again."""
    task = _task("policy-check", "internal_knowledge")
    _result, calls, reviewer = _invoke_with_fixtures(
        monkeypatch,
        [
            ResearchReview(research_complete=False, next_tasks=[task]),
            ResearchReview(research_complete=True),
        ],
    )

    assert reviewer.calls == 2
    assert not [call for call in calls if call[:2] == ("public_signal", "followup")]
    assert [call for call in calls if call[:2] == ("internal_knowledge", "followup")]


def test_both_followups_are_joined_before_the_next_review(monkeypatch) -> None:
    """Two dynamic Sends complete before the next review runs."""
    public_task = _task("public-gap", "public_signal")
    internal_task = _task("internal-gap", "internal_knowledge")
    result, calls, reviewer = _invoke_with_fixtures(
        monkeypatch,
        [
            ResearchReview(
                research_complete=False,
                next_tasks=[public_task, internal_task],
            ),
            ResearchReview(research_complete=True),
        ],
    )

    assert reviewer.calls == 2
    assert {
        (role, mode)
        for role, mode, _ in calls
        if role in {"public_signal", "internal_knowledge"}
        and mode == "followup"
    } == {
        ("public_signal", "followup"),
        ("internal_knowledge", "followup"),
    }
    assert {task.task_id for task in result["workflow"]["completed_tasks"]} == {
        "public-gap",
        "internal-gap",
    }


def test_multiple_same_role_tasks_are_batched_into_one_send() -> None:
    """Three public tasks result in one public-agent Send with three tasks."""
    tasks = [_task(f"public-{index}", "public_signal") for index in range(3)]
    sends = deep_researcher_module.route_after_research_review(
        {
            "workflow": {"round": 1, "review": ResearchReview(
                research_complete=False,
                next_tasks=tasks,
            ),
            "completed_tasks": []},
        },
        _config(),
    )

    assert len(sends) == 1
    assert isinstance(sends[0], Send)
    assert sends[0].node == "public_signal_agent"
    assert [task["task_id"] for task in sends[0].arg["workflow"]["pending_tasks"]] == [
        "public-0",
        "public-1",
        "public-2",
    ]


def test_completed_task_and_max_round_prevent_another_loop(monkeypatch) -> None:
    """Completed tasks and the workflow safety limit both stop re-planning."""
    task = _task("already-done", "public_signal")
    completed_route = deep_researcher_module.route_after_research_review(
        {
            "workflow": {"round": 1, "review": ResearchReview(
                research_complete=False,
                next_tasks=[task],
            ),
            "completed_tasks": [task]},
        },
        _config(),
    )
    assert completed_route == ["risk_assessment_agent"]

    _result, calls, reviewer = _invoke_with_fixtures(
        monkeypatch,
        [ResearchReview(research_complete=False, next_tasks=[task])],
        max_rounds=1,
    )
    assert reviewer.calls == 1
    assert not [call for call in calls if call[1] == "followup"]


def test_budget_usage_does_not_change_review_routing() -> None:
    """Budget counters remain observational and do not suppress a valid Send."""
    task = _task("budget-independent-gap", "public_signal")
    sends = deep_researcher_module.route_after_research_review(
        {
            "workflow": {"round": 1, "review": ResearchReview(
                research_complete=False,
                next_tasks=[task],
            ),
            "completed_tasks": []},
            "runtime": {"budget": {
                "model_calls": 999,
                "tool_calls": 999,
                "search_calls": 999,
            }},
        },
        _config(),
    )

    assert isinstance(sends[0], Send)
    assert sends[0].node == "public_signal_agent"


def test_role_report_reducer_preserves_initial_and_followup_reports() -> None:
    """A follow-up report is appended instead of replacing the initial report."""
    merged = agents_reducer(
        {"public_signal": {"report": "round one evidence"}},
        {"public_signal": {"report": "round two evidence"}},
    )

    assert "round one evidence" in merged["public_signal"]["report"]
    assert "round two evidence" in merged["public_signal"]["report"]


def test_followup_assignment_contains_only_gap_tasks() -> None:
    """Follow-up assignments explain the gap and avoid a generic repeat survey."""
    task = _task("regulator-notice", "public_signal")
    assignment = deep_researcher_module._build_public_opinion_agent_assignment(
        {
            "workflow": {"brief": "brand risk", "round": 2, "pending_tasks": [task]},
            "agents": {"public_signal": {"report": "round one", "memory": []}},
        },
        "public_signal",
    )

    assert "regulator-notice" in assignment
    assert task.objective in assignment
    assert "Do not repeat the first-round comprehensive survey" in assignment
