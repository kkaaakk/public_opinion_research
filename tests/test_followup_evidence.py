"""Follow-up completion requires usable evidence, not just a non-empty report."""

import asyncio

from agents.testing import ScriptedModel, assistant_message, function_call
from test_sdk_runtime import _runtime, _tool

from open_deep_research.budget import budget_usage_with_reason
from open_deep_research.configuration import Configuration
from open_deep_research.state import ResearchReview, ResearchTask
from open_deep_research.workflow import research


# 正常压缩结果，保证测试只观察工具证据与降级信号。
async def _compress(messages, cumulative_budget):
    return "Role report", {}


# 模拟预算守卫生成的占位报告。
async def _degraded_compress(messages, cumulative_budget):
    return (
        "Budget guard skipped research compression before any research notes "
        "were collected.",
        budget_usage_with_reason(
            "Skipped research compression to preserve the final report model call."
        ),
    )


def _followup_runtime(tool, *, compress=_compress, budget=None, config=None):
    return _runtime(
        "public_signal",
        tools=[tool],
        research_round=2,
        compress=compress,
        initial_budget=budget or {},
        config=config
        or Configuration(research_model="openai:fixture", budget_enabled=True),
        model=ScriptedModel(
            [
                [function_call(tool.name, '{"query":"gap"}', call_id="gap-call")],
                [assistant_message("follow-up report")],
            ]
        ),
    )


def test_successful_tool_evidence_is_collected():
    async def search(query, limit=3):
        return "new dated evidence"

    runtime = _followup_runtime(_tool(coroutine=search))
    result = asyncio.run(runtime.run("resolve the gap"))

    assert result.evidence_collected is True
    assert runtime.harness.results["gap-call"].status == "succeeded"
    assert "Role report" in result.report


def test_placeholder_compression_is_not_evidence():
    async def search(query, limit=3):
        return "new dated evidence"

    runtime = _followup_runtime(_tool(coroutine=search), compress=_degraded_compress)
    result = asyncio.run(runtime.run("resolve the gap"))

    assert result.evidence_collected is False
    assert result.budget["degradation_reasons"]


def test_tool_failure_text_is_not_evidence():
    async def search(query, limit=3):
        raise RuntimeError("search provider unavailable")

    runtime = _followup_runtime(_tool(coroutine=search))
    result = asyncio.run(runtime.run("resolve the gap"))

    assert result.evidence_collected is False
    assert runtime.harness.results["gap-call"].status == "failed"
    assert "Role report" in result.report


def test_budget_rejection_degrades_the_followup():
    async def search(query, limit=3):
        return "only one call fits"

    tool = _tool(coroutine=search)
    runtime = _runtime(
        "public_signal",
        tools=[tool],
        research_round=2,
        config=Configuration(
            research_model="openai:fixture",
            budget_enabled=True,
            max_tool_calls=1,
        ),
        model=ScriptedModel(
            [
                [
                    function_call(tool.name, '{"query":"one"}', call_id="gap-1"),
                    function_call(tool.name, '{"query":"two"}', call_id="gap-2"),
                ],
                [assistant_message("follow-up report")],
            ]
        ),
    )
    result = asyncio.run(runtime.run("resolve the gap"))

    assert runtime.harness.results["gap-1"].status == "succeeded"
    assert runtime.harness.results["gap-2"].status == "failed"
    assert "Budget guard skipped" in str(runtime.harness.results["gap-2"].observation)
    assert result.budget["degradation_reasons"]
    assert result.evidence_collected is False


def _config(*, max_research_rounds=2):
    return {
        "configurable": {
            "enabled_business_agents": ["public_signal", "internal_knowledge"],
            "max_research_rounds": max_research_rounds,
            "research_model": "fixture:model",
        }
    }


def _gap():
    return ResearchTask(
        task_id="missing-date",
        target_role="public_signal",
        objective="Find the notice date",
        evidence_needed="Dated official notice",
        reason="The date changes the risk judgment",
    )


def test_unresolved_gap_is_redispatched_until_the_round_limit():
    """A degraded follow-up must not filter its task out of the next Review."""
    task = _gap()
    state = {
        "workflow": {
            "round": 1,
            "review": ResearchReview(research_complete=False, next_tasks=[task]),
            "completed_tasks": [],
        }
    }
    sends = research.followup_assignments(state, _config())
    assert [
        item["task_id"] for item in sends["public_signal"]["workflow"]["pending_tasks"]
    ] == ["missing-date"]

    state["workflow"]["round"] = 2
    assert research.followup_assignments(state, _config()) == {}
