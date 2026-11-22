# 验证四个状态域的归属、并发合并、动态任务载荷及角色上下文。
"""Regression tests for the domain-owned research state architecture."""

import asyncio

from sdk_model_support import install_sequence

from open_deep_research.prompts import risk_assessment_prompt
from open_deep_research.state import (
    DeepResearchState,
    ReportState,
    ResearchReview,
    ResearchTask,
    agents_reducer,
    report_reducer,
    runtime_reducer,
    workflow_reducer,
)
from open_deep_research.workflow.research import (
    _build_followup_send_payload,
    risk_assessment,
)


# 构造标准研究任务供状态合并测试使用。
def _task(task_id: str, role: str = "public_signal") -> ResearchTask:
    return ResearchTask(
        task_id=task_id,
        objective="Resolve evidence gap",
        target_role=role,
        evidence_needed="Primary evidence",
        reason="May change risk judgment",
    )


# 验证顶层状态只包含约定状态域和原生消息。
def test_top_level_state_contains_only_owned_domains_and_messages() -> None:
    """The checkpoint schema exposes four stable domains plus messages."""
    assert set(DeepResearchState.__annotations__) == {
        "messages",
        "workflow",
        "agents",
        "report",
        "runtime",
    }
    assert set(ReportState.__annotations__) == {"final"}
    assert report_reducer({"final": "previous"}, {}) == {"final": "previous"}
    assert report_reducer({"final": "previous"}, {"final": "new"}) == {"final": "new"}


# 验证旧状态别名和兼容域已删除。
def test_legacy_state_aliases_and_domains_are_gone() -> None:
    import open_deep_research.state as state_module

    assert not hasattr(state_module, "AgentState")
    assert not hasattr(state_module, "PublicOpinionState")
    assert not hasattr(state_module, "ResearchState")
    assert not hasattr(state_module, "research_reducer")


# 验证并行角色的报告不会互相覆盖。
def test_parallel_agents_merge_reports_by_role() -> None:
    """Parallel producer updates cannot overwrite another role's state."""
    merged = agents_reducer(
        {}, {"public_signal": {"report": "public report"}}
    )
    merged = agents_reducer(
        merged, {"internal_knowledge": {"report": "internal report"}}
    )
    assert merged["public_signal"]["report"] == "public report"
    assert merged["internal_knowledge"]["report"] == "internal report"
    assert set(merged["public_signal"]) == {"report"}


# 验证预算合并保留各角色的用量。
def test_runtime_budget_merges_without_losing_usage() -> None:
    runtime = runtime_reducer(
        {"budget": {"model_calls": 1}},
        {"budget": {"model_calls": 2}},
    )
    assert runtime["budget"]["model_calls"] == 3
    assert "metrics" not in runtime


# 验证补充 Send 按完整状态域传输数据。
def test_followup_send_transports_domain_packets() -> None:
    """Dynamic Send payloads carry aggregates and no removed domains."""
    task = _task("gap-1")
    state = {
        "messages": [],
        "workflow": {
            "brief": "brief",
            "round": 1,
            "review": ResearchReview(research_complete=False, next_tasks=[task]),
        },
        "agents": {"public_signal": {"report": "round one"}},
        "report": {},
        "runtime": {"budget": {}},
    }
    payload = _build_followup_send_payload(state, [task], 2)
    assert set(payload) == {
        "messages",
        "workflow",
        "agents",
        "report",
        "runtime",
    }
    assert payload["workflow"]["pending_tasks"][0]["task_id"] == "gap-1"
    assert "research_mode" not in payload


# 验证显式清空待办时仍保留已完成任务历史。
def test_workflow_reducer_can_clear_pending_tasks_without_losing_history() -> None:
    """Review can clear transient work while completed tasks remain deduplicated."""
    task = _task("gap-1")
    merged = workflow_reducer(
        {"round": 2, "pending_tasks": [task], "completed_tasks": [task]},
        {"pending_tasks": {"type": "override", "value": []}},
    )
    assert merged["round"] == 2
    assert merged["pending_tasks"] == []
    assert [item.task_id for item in merged["completed_tasks"]] == ["gap-1"]


# 验证角色任务包含上游报告且提示词不再声明私有记忆。
def test_analysis_uses_formal_role_reports_only(monkeypatch) -> None:
    """Downstream analysis receives public role text and excludes private fields."""
    state = {
        "workflow": {"brief": "brief", "round": 1},
        "agents": {
            "public_signal": {"report": "PUBLIC_SENTINEL", "memory": "PRIVATE_SENTINEL"},
            "internal_knowledge": {"report": "INTERNAL_SENTINEL"},
        },
    }
    requests = install_sequence(monkeypatch, "risk assessment")
    asyncio.run(risk_assessment(state, {}))
    [request] = requests
    assert request.prompt == risk_assessment_prompt
    data = request.messages[-1]["content"]
    assert "PUBLIC_SENTINEL" in data
    assert "INTERNAL_SENTINEL" in data
    assert "PRIVATE_SENTINEL" not in data
    assert "<Memory Policy>" not in request.prompt
