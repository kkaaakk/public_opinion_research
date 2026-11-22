"""Exercise the SDK context harness without any Research Graph dependency."""

import asyncio
from dataclasses import replace

import pytest
from agents import Agent, function_tool
from agents.exceptions import MaxTurnsExceeded
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

from open_deep_research.configuration import Configuration
from open_deep_research.runtime import sdk_context
from open_deep_research.runtime.sdk_context import (
    BatchSettlement,
    ObservedToolResult,
    SDKContextHarness,
)
from open_deep_research.runtime.sdk_runtime import _RunBudget
from open_deep_research.runtime.tool_executor import ToolExecutor
from open_deep_research.tools import register_tool
from open_deep_research.web.run_data import RunJSON, digest
from open_deep_research.workflow.execution import ExecutionControlError


def _executor(tool, name="search", domain="web_search"):
    return ToolExecutor(
        "public_signal",
        frozenset({domain}),
        Configuration(research_model="openai:fixture"),
        {},
        _RunBudget(initial={}),
        {name: tool},
    )


def _harness(**kwargs):
    return SDKContextHarness(
        role="public_signal", task_id="task-1", research_round=1, **kwargs
    )


def test_history_units_partition_at_observed_response_boundaries():
    harness = _harness()
    items = [
        {"role": "user", "content": "task"},
        {"type": "function_call", "call_id": "a", "name": "search", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "a", "output": "first"},
        {"role": "assistant", "content": "analysis"},
        {"type": "function_call", "call_id": "b", "name": "search", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "b", "output": "second"},
        {"role": "assistant", "content": "done"},
    ]
    starts = [items[1], items[3], items[6]]
    harness._response_starts = [digest(item) for item in starts]
    head, units = harness.history_units(items)
    assert head == [items[0]]
    assert [turn for turn, _ in units] == [0, 1, 2]
    assert units[0][1] == items[1:3]
    assert units[1][1] == items[3:6]
    assert units[2][1] == items[6:]


def test_history_units_preserve_unknown_history_for_capacity_error():
    harness = _harness()
    items = [{"role": "user", "content": "task"}]
    head, units = harness.history_units(items)
    assert head == items
    assert units == []

    first = {"role": "assistant", "content": "first"}
    second = {"role": "assistant", "content": "second"}
    harness._response_starts = [digest(first), digest(second)]
    for ambiguous in ([second, first], [first, second, first], [second]):
        assert harness.history_units(ambiguous) == (ambiguous, [])


def test_can_compact_unit_requires_complete_call_output_pairs():
    complete = [
        {"type": "reasoning", "summary": []},
        {"type": "function_call", "call_id": "a", "name": "search", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "a", "output": "result"},
    ]
    assert SDKContextHarness.can_compact_unit(complete)
    assert not SDKContextHarness.can_compact_unit(complete[:-1])
    assert not SDKContextHarness.can_compact_unit(
        [*complete, {"role": "user", "content": "new task"}]
    )
    assert not SDKContextHarness.can_compact_unit(
        [
            {
                "type": "function_call",
                "call_id": "a",
                "name": "search",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "b",
                "output": "result",
            },
        ]
    )


@pytest.mark.parametrize(
    "arguments", ['{"query":3}', '{"limit":100}', "[]", "{"]
)
def test_tool_validation_is_recorded_as_failure(arguments):
    harness = _harness()

    async def search(query: str, limit: int = 3):
        return "unexpected"

    tool = register_tool(
        function_tool(
            search, name_override="search", strict_mode=True, failure_error_function=None
        ),
        domain="web_search",
        effect="read",
    )
    executor = _executor(tool)
    harness.tool_executor = executor

    def ask(_call):
        return [function_call("search", arguments, call_id="invalid")]

    model = ScriptedModel(
        [ModelStep.respond(ask), [assistant_message("validation handled")]]
    )
    agent = Agent(
        name="fixture",
        instructions="fixture",
        model=model,
        tools=[executor.bind(tool.sdk_tool)],
    )
    asyncio.run(harness.run(agent, [{"role": "user", "content": "task"}]))
    assert harness.results["invalid"].status == "failed"
    assert harness.last_settlement.succeeded


def test_tool_records_are_restored_and_conflicts_rejected():
    harness = _harness()
    record = ObservedToolResult(
        sdk_run_id="sdk-run",
        tool_call_id="call-1",
        tool_name="search",
        arguments_json='{"query":"q"}',
        args={"query": "q"},
        model_turn=1,
        status="succeeded",
        observation="result",
    )
    harness.results["call-1"] = record
    harness.restore_tool(
        {
            "sdk_run_id": "sdk-run",
            "tool_call_id": "call-1",
            "tool_name": "search",
            "arguments_json": '{"query":"q"}',
            "args": {"query": "q"},
            "model_turn": 1,
            "status": "succeeded",
            "observation": None,
            "error_type": None,
            "operation_id": "op-1",
            "effect_status": "returned",
        }
    )
    assert harness.results["call-1"].operation_id == "op-1"
    with pytest.raises(ExecutionControlError, match="does not match"):
        harness.restore_tool(
            {
                "sdk_run_id": "sdk-run",
                "tool_call_id": "call-1",
                "tool_name": "other",
                "arguments_json": '{"query":"q"}',
                "args": {"query": "q"},
                "model_turn": 1,
            }
        )


def test_settlement_checkpoint_only_restores_tools_without_a_committed_output(
    monkeypatch,
):
    serializer = RunJSON()
    snapshot = None

    async def step(options, settle):
        nonlocal snapshot
        assert options["name"] == "por.agent_settlement"
        if snapshot is None:
            snapshot = serializer.serialize(await settle())
        return serializer.deserialize(snapshot)

    monkeypatch.setattr(sdk_context, "native_step_available", lambda: True)
    monkeypatch.setattr(sdk_context.DBOS, "run_step_async", step)
    harness = _harness()
    completed = ObservedToolResult(
        sdk_run_id="sdk-run",
        tool_call_id="call-1",
        tool_name="search",
        arguments_json='{"query":"secret"}',
        args={"query": "secret"},
        model_turn=0,
        status="succeeded",
        observation={"raw": "large payload"},
    )
    pending = replace(completed, tool_call_id="call-2", status="running", observation=None)
    failed = replace(
        pending, status="failed", error_type="ExecutionControlError",
        operation_id="op-2", effect_status="uncertain",
    )
    harness.results = {"call-1": completed, "call-2": failed}
    expected = asyncio.run(harness._settle_on_exit("exception"))
    assert expected.succeeded
    assert set(serializer.deserialize(snapshot)["failed_tools"]) == {"call-2"}
    assert "raw" not in snapshot and "secret" not in snapshot

    replayed = _harness()
    replayed.results = {"call-1": replace(completed), "call-2": pending}
    assert asyncio.run(replayed._settle_on_exit("exception")) == expected
    assert replayed.results["call-1"] == completed
    assert replayed.results["call-2"] == failed


def test_settlement_reports_live_and_pending_calls():
    harness = _harness()
    harness.results["done"] = ObservedToolResult(
        sdk_run_id="sdk-run",
        tool_call_id="done",
        tool_name="search",
        arguments_json="{}",
        args={},
        model_turn=0,
        status="succeeded",
    )
    harness.results["running"] = ObservedToolResult(
        sdk_run_id="sdk-run",
        tool_call_id="running",
        tool_name="search",
        arguments_json="{}",
        args={},
        model_turn=0,
        status="running",
    )
    settlement = asyncio.run(harness.settle("test"))
    assert settlement == BatchSettlement(
        reason="test", in_flight_call_ids=("running",)
    )
    assert not settlement.succeeded


def test_runner_rejects_handoffs_and_tool_guardrails():
    harness = _harness()
    agent = Agent(name="fixture", instructions="fixture", model=ScriptedModel([]))
    agent.handoffs.append(
        Agent(name="other", instructions="other", model=ScriptedModel([]))
    )
    with pytest.raises(ValueError, match="without handoffs"):
        asyncio.run(harness.run(agent, "task"))

    harness = _harness()
    tool = function_tool(lambda: "ok", name_override="guarded")
    tool.tool_output_guardrails = [object()]
    agent = Agent(
        name="fixture",
        instructions="fixture",
        model=ScriptedModel([]),
        tools=[tool],
    )
    with pytest.raises(ValueError, match="guardrails"):
        asyncio.run(harness.run(agent, "task"))


def test_max_turns_is_raised_by_the_sdk_loop():
    harness = _harness()

    async def loop() -> str:
        return "again"

    tool = register_tool(
        function_tool(
            loop, name_override="loop", strict_mode=True, failure_error_function=None
        ),
        domain="web_search",
        effect="read",
    )
    executor = _executor(tool, name="loop")
    harness.tool_executor = executor
    agent = Agent(
        name="fixture",
        instructions="fixture",
        model=ScriptedModel(
            [
                [function_call("loop", "{}", call_id=f"call-{index}")]
                for index in range(3)
            ]
        ),
        tools=[executor.bind(tool.sdk_tool)],
    )
    with pytest.raises(MaxTurnsExceeded):
        asyncio.run(harness.run(agent, "task", max_turns=2))
    assert harness.last_settlement is not None
