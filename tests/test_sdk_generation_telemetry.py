"""One redacted SDK model span per physical bounded request."""

import asyncio

import httpx2
import pytest
from langchain_core.tools import tool
from test_sdk_models import _chat_text, _mock_http

from open_deep_research.budget import (
    ModelBudgetExceeded,
    budget_capture,
    model_budget_admission,
)
from open_deep_research.configuration import Configuration
from open_deep_research.llm.errors import ModelInvocationError
from open_deep_research.llm.generation import (
    ModelOutputError,
    generate_structured,
    generate_text,
    generate_text_sync,
)
from open_deep_research.observability.telemetry import business_span, correlation
from open_deep_research.state import ClarifyWithUser

MODEL = "deepseek:fixture"


def generations(trace_capture):
    return [
        s
        for s in trace_capture.spans()
        if s.attributes.get("openinference.span.kind") == "LLM"
    ]


def assert_redacted(trace_capture):
    assert not any("PRIVATE_" in s.to_json() for s in trace_capture.spans())


@pytest.mark.parametrize("synchronous", [False, True])
def test_generation_preserves_usage_native_parent_and_privacy(
    monkeypatch, trace_capture, synchronous
):
    sent = []

    def respond(request):
        sent.append(request)
        body = _chat_text(request, content="PRIVATE_RESPONSE_SENTINEL")
        body["usage"]["prompt_tokens_details"] = {"cached_tokens": 2}
        return httpx2.Response(200, json=body)

    _mock_http(monkeypatch, respond)
    with correlation(run_id="trace-fixture"), business_span("report_generation"):
        output, used = (
            generate_text_sync(MODEL, "PRIVATE_PROMPT_SENTINEL")
            if synchronous
            else asyncio.run(generate_text(MODEL, "PRIVATE_PROMPT_SENTINEL"))
        )
    assert (
        output == "PRIVATE_RESPONSE_SENTINEL" and len(sent) == used["model_calls"] == 1
    )
    spans = {span.context.span_id: span for span in trace_capture.spans()}
    (llm,) = generations(trace_capture)
    assert llm.attributes["llm.model_name"] == "fixture"
    assert llm.attributes["llm.token_count.total"] == 15
    assert llm.attributes["llm.token_count.prompt_details.cache_read"] == 2
    assert llm.attributes["por.run_id"] == "trace-fixture"
    parent = spans[llm.parent.span_id]
    while parent.name != "report_generation":
        parent = spans[parent.parent.span_id]
    assert (
        parent.start_time // 1000
        <= llm.start_time // 1000
        <= llm.end_time // 1000
        <= parent.end_time // 1000
    )
    assert_redacted(trace_capture)


def test_structured_retries_trace_once_each_and_bill_invalid_output(
    monkeypatch, trace_capture
):
    outputs = iter(
        [
            '{"bad":"PRIVATE_SCHEMA"}',
            '{"need_clarification":false,"question":"","verification":"ok"}',
        ]
    )
    _mock_http(
        monkeypatch,
        lambda request: httpx2.Response(
            200,
            json=_chat_text(request, content=next(outputs)),
        ),
    )
    result, used = asyncio.run(
        generate_structured(MODEL, "PRIVATE_PROMPT", ClarifyWithUser, max_attempts=2)
    )
    assert result.verification == "ok"
    assert used["model_calls"] == 2 and used["total_tokens"] == 30
    assert (
        sum(
            span.attributes["llm.token_count.total"]
            for span in generations(trace_capture)
        )
        == 30
    )
    assert len(generations(trace_capture)) == 2
    assert_redacted(trace_capture)


@pytest.mark.parametrize("mode", ["provider_error", "cancelled", "truncated"])
def test_failed_or_cancelled_request_closes_its_started_sdk_span(
    monkeypatch, trace_capture, mode
):
    sent = []

    async def respond(request):
        sent.append(1)
        if mode == "cancelled":
            raise asyncio.CancelledError("PRIVATE_CANCELLED")
        if mode == "truncated":
            return httpx2.Response(
                200, json=_chat_text(request, content="", finish="length")
            )
        return httpx2.Response(
            503, json={"error": {"message": "PRIVATE_PROVIDER_ERROR"}}
        )

    _mock_http(monkeypatch, respond)
    expected = {
        "provider_error": ModelInvocationError,
        "cancelled": asyncio.CancelledError,
        "truncated": ModelOutputError,
    }[mode]
    with budget_capture() as used, pytest.raises(expected):
        asyncio.run(generate_text(MODEL, "PRIVATE_PROMPT"))
    assert len(sent) == len(generations(trace_capture)) == used["model_calls"] == 1
    assert used["total_tokens"] == (15 if mode == "truncated" else 0)
    assert_redacted(trace_capture)


def test_parallel_async_and_sync_calls_preserve_identity(monkeypatch, trace_capture):
    _mock_http(
        monkeypatch,
        lambda request: httpx2.Response(
            200,
            json=_chat_text(request, content="PRIVATE_RESPONSE"),
        ),
    )

    async def work(role, asynchronous):
        with correlation(role=role, run_id="parallel-fixture"), business_span("stage"):
            if asynchronous:
                await generate_text(MODEL, "PRIVATE_PROMPT")
            else:
                await asyncio.to_thread(generate_text_sync, MODEL, "PRIVATE_PROMPT")

    async def run():
        await asyncio.gather(
            work("internal_knowledge", True), work("public_signal", False)
        )

    asyncio.run(run())
    llms = generations(trace_capture)
    assert len(llms) == 2 and {s.attributes["por.role"] for s in llms} == {
        "public_signal",
        "internal_knowledge",
    }
    assert all(s.attributes["por.run_id"] == "parallel-fixture" for s in llms)
    assert_redacted(trace_capture)


@pytest.mark.parametrize("inside_tool", [False, True])
@pytest.mark.parametrize("synchronous", [False, True])
def test_budget_admission_precedes_any_model_observation(
    monkeypatch, trace_capture, inside_tool, synchronous
):
    _mock_http(monkeypatch, lambda request: pytest.fail("unadmitted provider request"))
    cfg = Configuration(budget_enabled=True, max_model_calls=2)
    baseline = {"model_calls": 1}

    @tool
    def bounded_lookup(query: str) -> str:
        """Exercise the LangChain tool wrapper around an SDK call."""
        return generate_text_sync(MODEL, query)[0]

    async def run():
        if inside_tool:
            if synchronous:
                return bounded_lookup.invoke({"query": "PRIVATE_PROMPT"})
            return await bounded_lookup.ainvoke({"query": "PRIVATE_PROMPT"})
        if synchronous:
            return await asyncio.to_thread(generate_text_sync, MODEL, "PRIVATE_PROMPT")
        return await generate_text(MODEL, "PRIVATE_PROMPT")

    with budget_capture() as used, model_budget_admission(cfg, lambda: baseline):
        with pytest.raises(ModelBudgetExceeded):
            asyncio.run(run())
    assert baseline == {"model_calls": 1}
    assert used["model_calls"] == used["total_tokens"] == 0
    assert generations(trace_capture) == []
