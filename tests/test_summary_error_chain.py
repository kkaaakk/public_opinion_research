# 验证摘要层沿异常链传播预算、执行控制和数据库故障。
"""Summarization must not degrade wrapped control failures into webpage excerpts."""

import asyncio
from typing import Any

import pytest
from dbos import error as dbos_error
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research import budget
from open_deep_research.budget import structured_output_chain
from open_deep_research.llm.errors import ModelInvocationError
from open_deep_research.search import tavily
from open_deep_research.state import Summary
from open_deep_research.workflow import execution
from open_deep_research.workflow.execution import ExecutionControlError

_STEPS: list = []


class _QueueChat(BaseChatModel):
    """Real LangChain chat model returning queued results or exceptions."""

    @property
    def _llm_type(self) -> str:
        return "queue"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if not _STEPS:
            raise AssertionError("queue chat has no queued response")
        return _STEPS.pop(0)()


def _script(*steps: Any) -> None:
    _STEPS[:] = list(steps)


def _error_step(error: BaseException, cause: BaseException | None = None):
    def step():
        if cause is not None:
            raise error from cause
        raise error

    return step


def _invalid_summary_step():
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "Summary", "args": {"wrong_field": "x"}, "id": "call-1"}
        ],
        response_metadata={"model_name": "fixture"},
        usage_metadata={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
    )
    return lambda: ChatResult(
        generations=[ChatGeneration(message=message)]
    )


def _budget_spy(monkeypatch):
    calls = {"begin": 0, "end": []}

    async def begin():
        calls["begin"] += 1
        return f"call-{calls['begin']}"

    async def end(call_id, usage):
        calls["end"].append((call_id, usage))

    monkeypatch.setattr(budget, "begin_model_call", begin)
    monkeypatch.setattr(budget, "end_model_call", end)
    # This test exercises the inline path; no DBOS Workflow is running.
    monkeypatch.setattr(execution, "native_step_available", lambda: False)
    return calls


def _summarize(**kwargs):
    return asyncio.run(
        tavily.summarize_webpage(
            _QueueChat(),
            "page content " * 200,
            fallback_content="bounded source excerpt",
            max_excerpt_chars=64,
            **kwargs,
        )
    )


def test_wrapped_sqlalchemy_error_propagates(monkeypatch):
    calls = _budget_spy(monkeypatch)
    _script(_error_step(SQLAlchemyError("connection pool unavailable")))

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
    _script(_error_step(ModelInvocationError("wrapped"), control))

    with pytest.raises(ModelInvocationError):
        _summarize()

    assert calls["begin"] == 1
    assert len(calls["end"]) == 1


def test_wrapped_dbos_cancellation_propagates(monkeypatch):
    calls = _budget_spy(monkeypatch)
    cancellation = dbos_error.DBOSWorkflowCancelledError("workflow cancelled")
    _script(_error_step(ModelInvocationError("wrapped"), cancellation))

    with pytest.raises(ModelInvocationError):
        _summarize()

    assert calls["begin"] == 1
    assert len(calls["end"]) == 1


def test_wrapped_provider_failure_still_degrades(monkeypatch):
    calls = _budget_spy(monkeypatch)
    _script(_error_step(ValueError("provider unavailable")))

    result = _summarize()

    assert result.startswith("[SUMMARY UNAVAILABLE:")
    assert "bounded source excerpt" in result
    assert calls["begin"] == 1
    assert len(calls["end"]) == 1


def test_structured_output_parsing_failure_still_degrades(monkeypatch):
    calls = _budget_spy(monkeypatch)
    _script(_invalid_summary_step(), _invalid_summary_step())
    model = structured_output_chain(_QueueChat(), Summary, max_attempts=2)

    result = asyncio.run(
        tavily.summarize_webpage(
            model,
            "page content " * 200,
            fallback_content="bounded source excerpt",
            max_excerpt_chars=64,
        )
    )

    assert result.startswith("[SUMMARY UNAVAILABLE:")
    assert "bounded source excerpt" in result
    assert calls["begin"] == 1
    assert len(calls["end"]) == 1
    assert calls["end"][0][1]["model_calls"] == 2
