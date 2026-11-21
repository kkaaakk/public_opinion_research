# 验证摘要层沿异常链传播预算、执行控制和数据库故障。
"""Summarization must not degrade wrapped control failures into webpage excerpts."""

import asyncio

import pytest
from dbos import error as dbos_error
from sdk_model_support import Reply, install_sequence
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.llm import generation
from open_deep_research.llm.errors import ModelInvocationError
from open_deep_research.search import tavily
from open_deep_research.workflow import execution
from open_deep_research.workflow.execution import ExecutionControlError


def wrapped(cause):
    try:
        raise ModelInvocationError("wrapped") from cause
    except ModelInvocationError as error:
        return error


def _budget_spy(monkeypatch):
    calls = {"begin": 0, "end": []}

    async def begin():
        calls["begin"] += 1
        return f"call-{calls['begin']}"

    async def end(call_id, usage):
        calls["end"].append((call_id, usage))

    monkeypatch.setattr(generation, "begin_model_call", begin)
    monkeypatch.setattr(generation, "end_model_call", end)
    # This test exercises the inline path; no DBOS Workflow is running.
    monkeypatch.setattr(execution, "native_step_available", lambda: False)
    return calls


def _summarize(**kwargs):
    return asyncio.run(
        tavily.summarize_webpage(
            "openai:fixture",
            "page content " * 200,
            fallback_content="bounded source excerpt",
            max_excerpt_chars=64,
            **kwargs,
        )
    )


def test_wrapped_sqlalchemy_error_propagates(monkeypatch):
    calls = _budget_spy(monkeypatch)
    install_sequence(monkeypatch, wrapped(SQLAlchemyError("connection pool unavailable")))

    with pytest.raises(ModelInvocationError) as caught:
        _summarize()

    assert isinstance(caught.value.__cause__, SQLAlchemyError)
    assert calls["begin"] == 1
    assert [call_id for call_id, _usage in calls["end"]] == ["call-1"]
    usage = calls["end"][0][1]
    assert usage["model_calls"] == 1
    assert usage["usage_complete"] is False


def test_wrapped_execution_control_error_propagates(monkeypatch):
    calls = _budget_spy(monkeypatch)
    control = ExecutionControlError("executor no longer owns the effect")
    install_sequence(monkeypatch, wrapped(control))

    with pytest.raises(ModelInvocationError):
        _summarize()

    assert calls["begin"] == 1
    assert len(calls["end"]) == 1


def test_wrapped_dbos_cancellation_propagates(monkeypatch):
    calls = _budget_spy(monkeypatch)
    cancellation = dbos_error.DBOSWorkflowCancelledError("workflow cancelled")
    install_sequence(monkeypatch, wrapped(cancellation))

    with pytest.raises(ModelInvocationError):
        _summarize()

    assert calls["begin"] == 1
    assert len(calls["end"]) == 1


def test_wrapped_provider_failure_still_degrades(monkeypatch):
    calls = _budget_spy(monkeypatch)
    install_sequence(monkeypatch, ValueError("provider unavailable"))

    result = _summarize()

    assert result.startswith("[SUMMARY UNAVAILABLE:")
    assert "bounded source excerpt" in result
    assert calls["begin"] == 1
    assert len(calls["end"]) == 1


def test_structured_output_parsing_failure_still_degrades(monkeypatch):
    calls = _budget_spy(monkeypatch)
    install_sequence(monkeypatch, Reply({"wrong_field": "x"}, 4, 2), Reply({"wrong_field": "y"}, 4, 2))
    result = _summarize(max_attempts=2)

    assert result.startswith("[SUMMARY UNAVAILABLE:")
    assert "bounded source excerpt" in result
    assert calls["begin"] == 2
    assert [identity for identity, _ in calls["end"]] == ["call-1", "call-2"]
    assert all(usage["model_calls"] == 1 and usage["total_tokens"] == 6 for _, usage in calls["end"])
