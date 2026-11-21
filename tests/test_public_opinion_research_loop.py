# 用确定性模型与角色替身验证证据复核、动态 Send 和补充任务合并。
"""Tests for dynamic Plan-and-Execute research inside the public-opinion subgraph."""

import asyncio

from reference_workflow import PythonWorkflow
from sdk_model_support import install_sequence

import open_deep_research.runtime.business_agent as business_agent_module
import open_deep_research.workflow.research as workflow_research
from open_deep_research.prompts import response_strategy_prompt, risk_assessment_prompt
from open_deep_research.state import ResearchReview, ResearchTask, agents_reducer
from open_deep_research.workflow import reporting


# 构造启用指定研究轮数的隔离配置。
def _config(*, max_research_rounds: int = 2) -> dict:
    """Build a small deterministic subgraph configuration for loop tests."""
    return {
        "configurable": {
            "enabled_business_agents": [
                "public_signal",
                "internal_knowledge",
                "risk_assessment",
                "response_strategy",
            ],
            "max_research_rounds": max_research_rounds,
            "research_model": "openai:fixture-model",
        }
    }


# 创建带角色、目标和证据要求的标准研究任务。
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


# 创建记录角色执行次数和轮次的智能体替身。
def _fake_agent(calls: list[tuple[str, str, list[str]]]):
    """Return a fake formal agent implementation while preserving loop state."""

    # 生成当前角色的测试报告与任务完成增量。
    async def run(*, state, config, role):
        del config
        workflow = state.get("workflow", {})
        mode = "followup" if workflow.get("round", 1) > 1 else "initial"
        tasks = workflow.get("pending_tasks", []) or []
        task_ids = [
            task.task_id if isinstance(task, ResearchTask) else task["task_id"]
            for task in tasks
        ]
        calls.append((role, mode, task_ids))
        result = {
            "agents": {role: {"report": f"{role} {mode} report"}},
            "workflow": {"completed_tasks": tasks if mode == "followup" else []},
            "runtime": {"budget": {}},
        }
        if mode == "followup":
            result["workflow"]["round"] = workflow.get("round", 2)
        return result

    return run


# 构造首轮研究开始所需的状态。
def _initial_state() -> dict:
    """Build the complete public-opinion state input used by the graph."""
    return {
        "messages": [],
        "workflow": {
            "brief": "brand risk fixture",
            "round": 1,
            "review": None,
            "pending_tasks": [],
            "completed_tasks": [],
        },
        "agents": {},
        "report": {},
        "runtime": {"budget": {}},
    }


# 安装确定性模型和角色后调用研究子图。
def _invoke_with_fixtures(
    monkeypatch,
    reviews: list[ResearchReview],
    *,
    max_rounds: int = 2,
):
    """Invoke the compiled subgraph with deterministic review and agent fixtures."""
    requests = install_sequence(monkeypatch, *reviews, "risk analysis", "response strategy")
    calls: list[tuple[str, str, list[str]]] = []
    monkeypatch.setattr(
        workflow_research,
        "run_business_agent",
        _fake_agent(calls),
    )

    async def no_report(*args, **kwargs):
        return {}

    for name in ("section_writer", "write_final_sections", "compile_final_report"):
        monkeypatch.setattr(reporting, name, no_report)
    result = asyncio.run(
        PythonWorkflow().ainvoke(
            _initial_state(),
            _config(max_research_rounds=max_rounds),
            start_at="research",
        )
    )
    assert [request.schema_name for request in requests] == [
        *(["ResearchReview"] * len(reviews)),
        None,
        None,
    ]
    assert [request.prompt for request in requests[-2:]] == [
        risk_assessment_prompt,
        response_strategy_prompt,
    ]
    return result, calls, requests[:-2]


# 验证首轮证据充分时直接进入风险评估。
def test_sufficient_initial_research_goes_directly_to_risk(monkeypatch) -> None:
    """A complete first review does not launch a follow-up round."""
    result, calls, reviewer = _invoke_with_fixtures(
        monkeypatch,
        [ResearchReview(research_complete=True, confirmed_findings=["enough"])],
    )

    assert len(reviewer) == 1
    assert {(role, mode) for role, mode, _ in calls} == {
        ("public_signal", "initial"),
        ("internal_knowledge", "initial"),
    }
    assert result["workflow"]["round"] == 1
    assert result["agents"]["risk_assessment"]["report"] == "risk analysis"
    assert result["agents"]["response_strategy"]["report"] == "response strategy"


# 验证仅公众信号补充后回到复核并合并报告。
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

    assert len(reviewer) == 2
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
    assert (
        "public_signal followup report" in result["agents"]["public_signal"]["report"]
    )


# 验证内部缺口只调度内部知识角色。
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

    assert len(reviewer) == 2
    assert not [call for call in calls if call[:2] == ("public_signal", "followup")]
    assert [call for call in calls if call[:2] == ("internal_knowledge", "followup")]


# 验证两个角色的补充输出都汇合后再执行下一次复核。
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

    assert len(reviewer) == 2
    assert {
        (role, mode)
        for role, mode, _ in calls
        if role in {"public_signal", "internal_knowledge"} and mode == "followup"
    } == {
        ("public_signal", "followup"),
        ("internal_knowledge", "followup"),
    }
    assert {task.task_id for task in result["workflow"]["completed_tasks"]} == {
        "public-gap",
        "internal-gap",
    }


# 验证同角色多个任务合并为一次动态 Send。
def test_multiple_same_role_tasks_are_batched_into_one_send() -> None:
    """Three public tasks result in one public-agent Send with three tasks."""
    tasks = [_task(f"public-{index}", "public_signal") for index in range(3)]
    sends = workflow_research.followup_assignments(
        {
            "workflow": {
                "round": 1,
                "review": ResearchReview(
                    research_complete=False,
                    next_tasks=tasks,
                ),
                "completed_tasks": [],
            },
        },
        _config(),
    )

    assert len(sends) == 1
    assert set(sends) == {"public_signal"}
    assert [
        task["task_id"] for task in sends["public_signal"]["workflow"]["pending_tasks"]
    ] == [
        "public-0",
        "public-1",
        "public-2",
    ]


# 验证已完成任务和轮数上限阻止无效循环。
def test_completed_task_and_max_round_prevent_another_loop(monkeypatch) -> None:
    """Completed tasks and the workflow safety limit both stop re-planning."""
    task = _task("already-done", "public_signal")
    completed_route = workflow_research.followup_assignments(
        {
            "workflow": {
                "round": 1,
                "review": ResearchReview(
                    research_complete=False,
                    next_tasks=[task],
                ),
                "completed_tasks": [task],
            },
        },
        _config(),
    )
    assert completed_route == {}

    _result, calls, reviewer = _invoke_with_fixtures(
        monkeypatch,
        [ResearchReview(research_complete=False, next_tasks=[task])],
        max_rounds=1,
    )
    assert len(reviewer) == 1
    assert not [call for call in calls if call[1] == "followup"]


# 验证预算统计本身不改变证据复核路由规则。
def test_budget_usage_does_not_change_review_routing() -> None:
    """Budget counters remain observational and do not suppress a valid Send."""
    task = _task("budget-independent-gap", "public_signal")
    sends = workflow_research.followup_assignments(
        {
            "workflow": {
                "round": 1,
                "review": ResearchReview(
                    research_complete=False,
                    next_tasks=[task],
                ),
                "completed_tasks": [],
            },
            "runtime": {
                "budget": {
                    "model_calls": 999,
                    "tool_calls": 999,
                    "search_calls": 999,
                }
            },
        },
        _config(),
    )

    assert set(sends) == {"public_signal"}


# 验证报告合并保留首轮与补充轮次证据。
def test_role_report_reducer_preserves_initial_and_followup_reports() -> None:
    """A follow-up report is appended instead of replacing the initial report."""
    merged = agents_reducer(
        {"public_signal": {"report": "round one evidence"}},
        {"public_signal": {"report": "round two evidence"}},
    )

    assert "round one evidence" in merged["public_signal"]["report"]
    assert "round two evidence" in merged["public_signal"]["report"]


# 验证补充任务说明聚焦缺口，不重新要求全面调查。
def test_followup_assignment_contains_only_gap_tasks() -> None:
    """Follow-up assignments explain the gap and avoid a generic repeat survey."""
    task = _task("regulator-notice", "public_signal")
    assignment = business_agent_module._build_business_agent_assignment(
        {
            "workflow": {"brief": "brand risk", "round": 2, "pending_tasks": [task]},
            "agents": {"public_signal": {"report": "round one"}},
        },
        "public_signal",
    )

    assert "regulator-notice" in assignment
    assert task.objective in assignment
    assert "Do not repeat the first-round comprehensive survey" in assignment
