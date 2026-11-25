"""Exercise the SDK context harness without any Research Graph dependency."""

import asyncio

import pytest
from agents import Agent, function_tool
from agents.exceptions import MaxTurnsExceeded, UserError
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext
from test_sdk_runtime import TOKENS, _runtime, _tool

from open_deep_research.budget import ModelBudgetExceeded
from open_deep_research.configuration import Configuration
from open_deep_research.runtime import tool_executor
from open_deep_research.runtime.sdk_context import (
    NOT_INVOKED,
    BatchSettlement,
    ObservedToolResult,
    SDKContextHarness,
)
from open_deep_research.runtime.sdk_runtime import _RunBudget
from open_deep_research.runtime.tool_executor import ToolExecutor
from open_deep_research.tools import register_tool
from open_deep_research.web.run_data import digest
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


def _declared(name="search"):
    async def body(query: str = ""):
        return f"evidence {query}"

    return register_tool(
        function_tool(
            body, name_override=name, strict_mode=True, failure_error_function=None
        ),
        domain="web_search",
        effect="read",
    )


def _tool_context(harness, call_id, name="search", arguments='{"query":"q"}'):
    return ToolContext(
        context=harness,
        tool_name=name,
        tool_call_id=call_id,
        tool_arguments=arguments,
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
    # The executor entered this call, so it may still be running a body that
    # settlement has no right to declare finished.
    harness.mark_executing("running")
    settlement = asyncio.run(harness.settle("test"))
    assert settlement == BatchSettlement(
        reason="test", in_flight_call_ids=("running",)
    )
    assert not settlement.succeeded


def test_settlement_closes_a_call_the_executor_never_entered():
    """A hook-only observation has no invocation behind it and must not stay live."""
    tool = _declared()
    harness = _harness()
    harness.tool_executor = _executor(tool)
    context = _tool_context(harness, "ghost")

    asyncio.run(harness.on_tool_start(context, None, tool.sdk_tool))
    assert harness.results["ghost"].status == "running"

    settlement = asyncio.run(harness.settle("cancelled"))
    assert settlement.succeeded
    record = harness.results["ghost"]
    assert (record.status, record.observation, record.error_type) == (
        "cancelled",
        None,
        NOT_INVOKED,
    )


def test_observation_announced_during_settlement_is_born_terminal():
    """A late SDK hook must not reintroduce a live record after settlement."""
    tool = _declared()
    harness = _harness()
    harness.tool_executor = _executor(tool)
    asyncio.run(harness.settle("exception"))

    asyncio.run(harness.on_tool_start(_tool_context(harness, "late"), None, tool.sdk_tool))

    assert harness.results["late"].status == "cancelled"
    assert harness.results["late"].error_type == NOT_INVOKED
    assert asyncio.run(harness.settle("exception")).succeeded


def test_cancelled_tool_step_closes_its_record_without_running_the_body(monkeypatch):
    """A Step cancelled before the body ran leaves no live record and no usage."""
    harness = _harness()
    ran = []

    async def body(query: str = ""):
        ran.append(query)
        return "unexpected"

    tool = register_tool(
        function_tool(
            body, name_override="search", strict_mode=True, failure_error_function=None
        ),
        domain="web_search",
        effect="read",
    )
    executor = _executor(tool)
    harness.tool_executor = executor

    async def cancelled_step(options, step):
        assert options["name"] == "por.agent_tool"
        raise asyncio.CancelledError()

    monkeypatch.setattr(tool_executor, "native_step_available", lambda: True)
    monkeypatch.setattr(tool_executor.DBOS, "run_step_async", cancelled_step)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            executor.invoke(tool.sdk_tool, _tool_context(harness, "call-1"), '{"query":"q"}')
        )

    assert ran == []
    record = harness.results["call-1"]
    assert (record.status, record.observation, record.error_type) == (
        "cancelled",
        None,
        "CancelledError",
    )
    assert asyncio.run(harness.settle("cancelled")).succeeded


@pytest.mark.parametrize("attempt", range(20))
def test_parallel_failing_tool_settles_every_record_terminal(attempt):
    """Repeat the sibling race: no record may stay live, no allowance is refunded."""
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def search(query: str, limit: int = 3):
        if query == "blocked":
            started.set()
            await blocked.wait()
            return "never returned"
        # Bounded so a serial SDK loop fails loudly instead of hanging.
        await asyncio.wait_for(started.wait(), timeout=10)
        raise ModelBudgetExceeded("Tool summary ran out of allowance")

    tool = _tool(coroutine=search, effect="read")
    model = ScriptedModel(
        [
            [
                function_call(tool.name, {"query": "blocked"}, call_id="call-blocked"),
                function_call(tool.name, {"query": "failing"}, call_id="call-failing"),
            ]
        ],
        default_usage=TOKENS,
    )
    runtime = _runtime(tools=[tool], model=model)

    with pytest.raises(UserError) as caught:
        asyncio.run(runtime.run("collect evidence"))
    assert isinstance(caught.value.__cause__, ModelBudgetExceeded)

    harness = runtime.harness
    assert harness.last_settlement is not None
    assert harness.last_settlement.succeeded
    assert {
        call_id: record.status for call_id, record in harness.results.items()
    } == {"call-blocked": "cancelled", "call-failing": "failed"}
    assert harness.results["call-blocked"].error_type == "CancelledError"
    # Both calls were admitted before either body ran, so neither is refunded.
    assert caught.value.budget_usage["tool_calls"] == 2


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
