"""SDK ToolExecutor must propagate wrapped control failures, not degrade them."""

import asyncio
import builtins

import pytest
from agents.exceptions import MCPToolCancellationError, UserError
from agents.testing import ScriptedModel, assistant_message, function_call
from sqlalchemy.exc import SQLAlchemyError
from test_sdk_runtime import _runtime, _tool

from open_deep_research.budget import ModelBudgetExceeded
from open_deep_research.llm.errors import ModelInvocationError
from open_deep_research.workflow.execution import (
    ExecutionControlError,
    error_chain,
    failure_kind,
)


def _model(tool):
    return ScriptedModel(
        [
            [function_call(tool.name, '{"query":"brand"}', call_id="call-1")],
            [assistant_message("continued after tool")],
        ]
    )


def _run(tool):
    runtime = _runtime("public_signal", tools=[tool], model=_model(tool))
    return runtime, asyncio.run(runtime.run("collect evidence"))


def test_ordinary_tool_failure_returns_a_tool_result():
    async def fail(query, limit=3):
        raise RuntimeError("search provider unavailable")

    tool = _tool("public_signal", coroutine=fail)
    runtime, result = _run(tool)

    assert len(runtime.model.calls) == 2
    assert "continued after tool" in result.report
    assert runtime.harness.results["call-1"].status == "failed"
    assert "search provider unavailable" in str(
        runtime.harness.results["call-1"].observation
    )
    assert result.budget["tool_calls"] == 1


def test_mcp_cancellation_remains_a_tool_result():
    async def fail(query, limit=3):
        raise MCPToolCancellationError("tool call cancelled")

    tool = _tool("public_signal", coroutine=fail)
    runtime, result = _run(tool)

    assert len(runtime.model.calls) == 2
    assert "continued after tool" in result.report
    assert runtime.harness.results["call-1"].status == "cancelled"


def test_implicit_cancellation_context_stays_a_tool_error():
    """anyio reports an MCP timeout with an internal cancellation as context."""

    async def fail(query, limit=3):
        try:
            raise asyncio.CancelledError()
        except asyncio.CancelledError:
            try:
                raise TimeoutError("mcp request timed out")
            except TimeoutError:
                raise RuntimeError("McpError: timed out")

    tool = _tool("public_signal", coroutine=fail)
    runtime, result = _run(tool)

    assert len(runtime.model.calls) == 2
    assert "continued after tool" in result.report
    assert runtime.harness.results["call-1"].status == "failed"
    assert "timed out" in str(runtime.harness.results["call-1"].observation)


def test_explicitly_wrapped_cancellation_propagates():
    async def fail(query, limit=3):
        try:
            raise asyncio.CancelledError()
        except asyncio.CancelledError as exc:
            raise RuntimeError("wrapped cancellation") from exc

    tool = _tool("public_signal", coroutine=fail)
    runtime = _runtime("public_signal", tools=[tool], model=_model(tool))

    with pytest.raises(UserError) as caught:
        asyncio.run(runtime.run("collect evidence"))

    assert len(runtime.model.calls) == 1
    assert runtime.harness.results["call-1"].status == "failed"
    assert any(
        isinstance(cause, asyncio.CancelledError)
        for cause in error_chain(caught.value)
    )


def test_wrapped_budget_failure_keeps_its_public_kind():
    tool = _tool("public_signal", coroutine=_control_failure("budget"))
    runtime = _runtime("public_signal", tools=[tool], model=_model(tool))

    with pytest.raises(UserError) as caught:
        asyncio.run(runtime.run("collect evidence"))

    assert failure_kind(caught.value) == "budget_exhausted"


def _control_failure(kind):
    async def fail(query, limit=3):
        if kind == "direct":
            raise ExecutionControlError("executor no longer owns the effect")
        if kind == "wrapped":
            raise ModelInvocationError("wrapped tool failure") from ExecutionControlError(
                "executor no longer owns the effect"
            )
        if kind == "budget":
            raise ModelInvocationError("wrapped tool failure") from ModelBudgetExceeded(
                "physical model budget exhausted"
            )
        if kind == "group":
            raise builtins.ExceptionGroup(
                "wrapped tool failure", [ModelBudgetExceeded("budget exhausted")]
            )
        if kind == "database":
            raise SQLAlchemyError("storage unavailable")
        if kind == "cancel":
            raise asyncio.CancelledError()
        if kind == "wrapped_cancel":
            raise builtins.BaseExceptionGroup(
                "wrapped cancellation", [asyncio.CancelledError()]
            )
        raise AssertionError("unknown fixture kind")

    return fail


@pytest.mark.parametrize(
    "kind,outer,inner,status",
    [
        ("direct", UserError, ExecutionControlError, "failed"),
        ("wrapped", UserError, ExecutionControlError, "failed"),
        ("budget", UserError, ModelBudgetExceeded, "failed"),
        ("group", UserError, ModelBudgetExceeded, "failed"),
        ("database", UserError, SQLAlchemyError, "failed"),
        ("cancel", asyncio.CancelledError, asyncio.CancelledError, "cancelled"),
        (
            "wrapped_cancel",
            builtins.BaseExceptionGroup,
            asyncio.CancelledError,
            "cancelled",
        ),
    ],
)
def test_control_failure_propagates_and_stops_the_agent_loop(
    kind, outer, inner, status
):
    tool = _tool("public_signal", coroutine=_control_failure(kind))
    runtime = _runtime("public_signal", tools=[tool], model=_model(tool))

    with pytest.raises(outer) as caught:
        asyncio.run(runtime.run("collect evidence"))

    # The second model step must never run after a control failure.
    assert len(runtime.model.calls) == 1
    assert runtime.harness.results["call-1"].status == status
    assert runtime.harness.results["call-1"].error_type
    assert caught.value.budget_usage["tool_calls"] == 1
    # The SDK may wrap the boundary exception; the real cause stays traceable.
    assert any(isinstance(cause, inner) for cause in error_chain(caught.value))
