"""Run-level tool admission replaces Producer-local budget races."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError
from test_sdk_runtime import _tool

from open_deep_research.configuration import Configuration
from open_deep_research.runtime import tool_executor
from open_deep_research.runtime.sdk_runtime import _RunBudget
from open_deep_research.runtime.tool_executor import ToolExecutor, _ToolRejected
from open_deep_research.workflow.execution import ExecutionControlError, stage_context


def _executor(config, *, initial=None):
    tool = _tool("public_signal")
    return ToolExecutor(
        "public_signal",
        frozenset({"core", "web_search"}),
        config,
        {"configurable": {}},
        _RunBudget(initial=initial or {}),
        {tool.name: tool},
    )


def _in_scope(executor, call_id="call-1"):
    token = stage_context.set(SimpleNamespace(run_id="run-1"))
    try:
        asyncio.run(executor._admit("web_search", call_id))
    finally:
        stage_context.reset(token)


def test_run_admission_accepts_and_charges_one_call(monkeypatch):
    calls = []

    def allow(*args):
        calls.append(args)
        return True

    monkeypatch.setattr(tool_executor, "admit_tool_call", allow)
    executor = _executor(
        Configuration(
            research_model="openai:fixture",
            budget_enabled=True,
            max_tool_calls=1,
            max_search_calls=1,
        )
    )
    _in_scope(executor)

    assert calls[0][1] == "run-1"
    assert calls[0][-1] is True
    assert executor.budget.delta["tool_calls"] == 1
    assert executor.budget.delta["search_calls"] == 1


def test_run_admission_rejection_returns_an_ordinary_tool_error(monkeypatch):
    def deny(*args):
        return False

    monkeypatch.setattr(tool_executor, "admit_tool_call", deny)
    executor = _executor(
        Configuration(research_model="openai:fixture", budget_enabled=True, max_tool_calls=1)
    )

    with pytest.raises(_ToolRejected, match="Budget guard skipped"):
        _in_scope(executor)

    assert executor.budget.delta["tool_calls"] == 0
    assert executor.budget.delta["degradation_reasons"]


def test_admission_storage_failure_is_a_control_error(monkeypatch):
    def fail(*args):
        raise SQLAlchemyError("admission storage unavailable")

    monkeypatch.setattr(tool_executor, "admit_tool_call", fail)
    executor = _executor(
        Configuration(research_model="openai:fixture", budget_enabled=True, max_tool_calls=1)
    )

    with pytest.raises(ExecutionControlError) as caught:
        _in_scope(executor)

    assert isinstance(caught.value.__cause__, SQLAlchemyError)
    assert executor.budget.delta.get("tool_calls", 0) == 0


def test_unbounded_tool_limits_do_not_touch_the_run_ledger(monkeypatch):
    def unexpected(*args):
        pytest.fail("run ledger must not be used without tool/search limits")

    monkeypatch.setattr(tool_executor, "admit_tool_call", unexpected)
    executor = _executor(
        Configuration(
            research_model="openai:fixture",
            budget_enabled=True,
            max_model_calls=10,
        )
    )
    _in_scope(executor)

    assert executor.budget.delta["tool_calls"] == 1
