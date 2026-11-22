"""Tool quotas and model budgets must be enforced independently."""

import asyncio

from agents.testing import ScriptedModel, assistant_message, function_call
from test_sdk_runtime import _runtime, _tool

from open_deep_research.configuration import Configuration
from open_deep_research.runtime.sdk_context import ObservedToolResult
from open_deep_research.runtime.sdk_runtime import _RunBudget
from open_deep_research.runtime.tool_executor import ToolExecutor


def test_exhausted_tool_quota_does_not_stop_model_reasoning():
    """A spent tool/search allowance must not end an Agent with model budget left."""

    async def search(query, limit=3):
        return "must not execute"

    tool = _tool("public_signal", coroutine=search)
    runtime = _runtime(
        "public_signal",
        tools=[tool],
        initial_budget={"tool_calls": 1},
        config=Configuration(
            research_model="openai:fixture",
            budget_enabled=True,
            max_model_calls=10,
            max_tool_calls=1,
        ),
        model=ScriptedModel(
            [
                [function_call(tool.name, '{"query":"q"}', call_id="call-1")],
                [assistant_message("reasoning continued")],
            ]
        ),
    )
    result = asyncio.run(runtime.run("task"))

    # The tool is rejected by its own quota, but the next model request still runs.
    assert runtime.harness.results["call-1"].status == "failed"
    assert len(runtime.model.calls) == 2
    assert "reasoning continued" in result.report


def test_exhausted_model_budget_does_not_reject_tool_admission():
    """A spent model budget must not reject a tool call with tool quota left."""
    tool = _tool("public_signal")
    executor = ToolExecutor(
        "public_signal",
        frozenset({"core", "web_search"}),
        Configuration(
            research_model="openai:fixture",
            budget_enabled=True,
            max_model_calls=1,
            max_tool_calls=10,
        ),
        {"configurable": {}},
        _RunBudget(initial={"model_calls": 1}),
        {tool.name: tool},
    )

    asyncio.run(
        executor._admit(
            ObservedToolResult(
                sdk_run_id="SDK_run",
                tool_call_id="call-1",
                tool_name="web_search",
                arguments_json='{"query":"q"}',
                args={"query": "q"},
                model_turn=0,
            )
        )
    )

    assert executor.budget.delta["tool_calls"] == 1
    assert executor.budget.delta["search_calls"] == 1
