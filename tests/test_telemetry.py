"""Offline native instrumentation, privacy, lifecycle and failure contracts."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from agents import (
    Agent,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
    RunConfig,
    Runner,
    function_tool,
)
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from openai import AsyncOpenAI
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as RealOTLPExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.trace import (
    Link,
    SpanContext,
    Status,
    StatusCode,
    TraceFlags,
    TraceState,
)
from test_sdk_context import _tool, _workspace

from open_deep_research.observability import telemetry
from open_deep_research.runtime.sdk_context import SDKContextHarness


def kinds(spans, kind):
    return [s for s in spans if s.attributes.get("openinference.span.kind") == kind]


def test_disabled_is_noop_and_does_not_initialize(monkeypatch):
    monkeypatch.setattr(telemetry, "_attempted", False)
    monkeypatch.setattr(telemetry, "_active", False)
    monkeypatch.setenv("POR_TRACING_ENABLED", "false")
    assert not telemetry.initialize_telemetry()
    with telemetry.business_span("disabled") as span:
        assert span is None
    assert telemetry.enqueue_context() == {}


@pytest.mark.parametrize(
    "field,value",
    [
        ("LANGFUSE_SECRET_KEY", ""),
        ("LANGFUSE_BASE_URL", "https://user:secret@example.test"),
        ("POR_TRACE_SAMPLE_RATE", "nan"),
        ("POR_TRACE_TIMEOUT_SECONDS", "0"),
        ("POR_TRACE_SHUTDOWN_SECONDS", "99"),
    ],
)
def test_invalid_configuration_fails_open(monkeypatch, field, value):
    monkeypatch.setattr(telemetry, "_attempted", False)
    monkeypatch.setattr(telemetry, "_active", False)
    monkeypatch.setenv("POR_TRACING_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv(field, value)
    assert not telemetry.initialize_telemetry()
    assert not telemetry.telemetry_enabled()


def test_one_initialization_and_no_default_openai_exporter(trace_capture):
    from agents.tracing import get_trace_provider

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(lambda _: telemetry.initialize_telemetry(), range(32)))
    assert telemetry._provider is trace_capture.provider
    processors = get_trace_provider()._multi_processor._processors
    assert [type(p).__name__ for p in processors] == [
        "SDKUsageEnrichment",
        "OpenInferenceTracingProcessor",
    ]


def test_initializer_disables_pipeline_if_global_provider_registration_loses_race(
    monkeypatch,
):
    from uuid import uuid4

    from agents import set_trace_processors, set_tracing_disabled
    from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http import trace_exporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    foreign = TracerProvider(shutdown_on_exit=False)
    current = {"provider": trace.ProxyTracerProvider()}
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: current["provider"])
    monkeypatch.setattr(
        trace, "set_tracer_provider", lambda _: current.update(provider=foreign)
    )
    monkeypatch.setattr(
        trace_exporter, "OTLPSpanExporter", lambda **kwargs: InMemorySpanExporter()
    )
    monkeypatch.setenv("POR_TRACING_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-race-test-" + uuid4().hex)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "race-test-only")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://127.0.0.1:1")
    for key, value in {
        "_attempted": False,
        "_active": False,
        "_provider": None,
        "_client": None,
        "_settings": None,
    }.items():
        monkeypatch.setattr(telemetry, key, value)
    try:
        assert not telemetry.initialize_telemetry()
        assert not telemetry.telemetry_enabled()
        assert trace.get_tracer_provider() is foreign
    finally:
        instrumentor = OpenAIAgentsInstrumentor()
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
        set_trace_processors([])
        set_tracing_disabled(True)
        telemetry.shutdown_telemetry()
        foreign.shutdown()


@pytest.mark.parametrize("through_harness", [False, True])
def test_runconfig_disabled_really_suppresses_openinference(
    trace_capture, tmp_path, through_harness
):
    async def run():
        model = ScriptedModel(
            [[assistant_message("private response")]], emit_traces=True
        )
        workspace, _, _ = _workspace(tmp_path, producer=False)
        run = SDKContextHarness(workspace).run if through_harness else Runner.run
        return await run(
            Agent(name="probe", model=model),
            "private prompt",
            run_config=RunConfig(
                tracing_disabled=True, trace_include_sensitive_data=False
            ),
        )

    assert asyncio.run(run()).final_output == "private response"
    spans = trace_capture.spans()
    assert not any(s.attributes.get("openinference.span.kind") for s in spans)
    if not through_harness:
        assert spans == ()


def test_parallel_harness_native_spans_are_distinct_and_private(
    trace_capture, tmp_path
):
    async def run():
        gate = asyncio.Event()
        entered = []

        async def invoke(label, producer):
            workspace, _, _ = _workspace(tmp_path / label, producer=producer)
            workspace.scope.research_round = 2
            harness = SDKContextHarness(workspace)

            async def first(call):
                entered.append(label)
                if len(entered) == 2:
                    gate.set()
                await asyncio.wait_for(gate.wait(), 3)
                return [
                    function_call(
                        "web_search",
                        {"label": "secret-tool-argument"},
                        call_id=label + "-1",
                    )
                ]

            model = ScriptedModel(
                [
                    ModelStep.respond(first),
                    [
                        function_call(
                            "web_search",
                            {"label": "secret-tool-argument-2"},
                            call_id=label + "-2",
                        )
                    ],
                    [assistant_message("secret-model-output")],
                ],
                emit_traces=True,
            )
            agent = Agent(
                name="public_signal" if producer else "risk_assessment",
                instructions="secret-instructions",
                model=model,
                tools=[_tool(harness)],
            )
            with telemetry.correlation(
                unit_id=label, execution_id=label + "-exec", generation=1
            ):
                return await harness.run(agent, "secret-private-memory")

        with telemetry.business_span("research_root", run_id="run-a"):
            return await asyncio.gather(
                invoke("producer", True), invoke("consumer", False)
            )

    results = asyncio.run(run())
    assert [r.final_output for r in results] == ["secret-model-output"] * 2
    spans = trace_capture.spans()
    assert len(kinds(spans, "LLM")) == 6
    assert len(kinds(spans, "TOOL")) == 4
    by_id = {s.context.span_id: s for s in spans}
    tools = kinds(spans, "TOOL")
    assert {s.attributes["por.tool_call_id"] for s in tools} == {
        "producer-1",
        "producer-2",
        "consumer-1",
        "consumer-2",
    }
    for tool in tools:
        parent = by_id[tool.parent.span_id]
        assert (
            parent.attributes["por.invocation_id"]
            == tool.attributes["por.invocation_id"]
        )
        assert tool.attributes["por.role"] == parent.attributes["por.role"]
        assert tool.attributes["por.generation"] == 1
        assert tool.attributes["por.research_round"] == 2
        assert tool.attributes["por.graph_call_id"].startswith("CALL_")
    assert len({s.attributes["por.invocation_id"] for s in tools}) == 2
    names = {s.name for s in spans}
    assert {
        "context_build",
        "persist_tool_results",
        "context_retrieval",
        "research_graph_write",
    } <= names
    exported = json.dumps([json.loads(s.to_json()) for s in spans])
    assert "secret-" not in exported
    assert "test-only-secret" not in exported
    assert all(not s.events and not s.status.description for s in spans)


def test_official_model_usage_without_duplicate_llm_or_tool(trace_capture):
    from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor

    # Instrument() is deliberately idempotent; no second processor is installed.
    OpenAIAgentsInstrumentor().instrument(tracer_provider=trace_capture.provider)
    calls = []

    async def run():
        async def response(request):
            calls.append(request)
            first = len(calls) == 1
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "fixture-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls" if first else "stop",
                            "message": {
                                "role": "assistant",
                                "content": None if first else "private-result",
                                **(
                                    {
                                        "tool_calls": [
                                            {
                                                "id": "native-1",
                                                "type": "function",
                                                "function": {
                                                    "name": "lookup",
                                                    "arguments": '{"query":"private-query"}',
                                                },
                                            }
                                        ]
                                    }
                                    if first
                                    else {}
                                ),
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 4,
                        "total_tokens": 14,
                    },
                },
            )

        @function_tool
        async def lookup(query: str) -> str:
            return "private-tool-output"

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(response)
        ) as transport:
            client = AsyncOpenAI(api_key="test-key-only", http_client=transport)
            model = OpenAIChatCompletionsModel(
                model="fixture-model", openai_client=client
            )
            return await Runner.run(
                Agent(name="agent", model=model, tools=[lookup]),
                "private-input",
                run_config=RunConfig(trace_include_sensitive_data=False),
            )

    result = asyncio.run(run())
    spans = trace_capture.spans()
    llm = kinds(spans, "LLM")
    assert len(llm) == len(calls) == 2
    assert len(kinds(spans, "TOOL")) == 1
    assert (
        sum(s.attributes["llm.token_count.total"] for s in llm)
        == result.context_wrapper.usage.total_tokens
        == 28
    )
    assert all(s.attributes["llm.model_name"] == "fixture-model" for s in llm)
    assert "private-" not in json.dumps([json.loads(s.to_json()) for s in spans])


@pytest.mark.parametrize("cancel", [False, True])
def test_original_failure_or_cancel_survives_and_is_redacted(
    trace_capture, tmp_path, cancel
):
    async def run():
        workspace, _, _ = _workspace(tmp_path)
        harness = SDKContextHarness(workspace)

        async def fail(call):
            if cancel:
                raise asyncio.CancelledError("private-cancel-reason")
            raise RuntimeError("private-exception-text")

        agent = Agent(
            name="failure",
            model=ScriptedModel([ModelStep.respond(fail)], emit_traces=True),
        )
        await harness.run(agent, "private-input")

    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        asyncio.run(run())
    spans = trace_capture.spans()
    assert any(s.status.status_code == StatusCode.ERROR for s in spans)
    assert "private-" not in json.dumps([json.loads(s.to_json()) for s in spans])


def test_exporter_removes_status_events_resources_and_arbitrary_metadata(trace_capture):
    with telemetry.business_span("privacy_probe") as span:
        span.set_attributes(
            {
                "input.value": "secret-prompt",
                "tool.parameters": "secret-schema",
                "dbos.user.name": "secret-owner",
                "status": "secret-unprefixed-business-data",
                "por.run_id": "run-safe",
            }
        )
        span.add_event("secret-event", {"exception.message": "secret-message"})
        span.set_status(Status(StatusCode.ERROR, "secret-status"))
    item = trace_capture.spans()[0]
    assert item.attributes["session.id"] == "run-safe"
    assert "secret-" not in item.to_json()
    raw = ReadableSpan(
        name="safe",
        resource=Resource({"secret-resource": "secret-value"}),
        status=Status(StatusCode.ERROR, "secret-text"),
    )
    assert "secret-" not in telemetry.safe_span(raw).to_json()


def test_attribute_allowlist_preserves_exact_types_and_rejects_similar_keys():
    values = {
        "por.run_id": "safe-run",
        "por.generation": 0,
        "por.model_calls": 2,
        "por.total_tokens": 15,
        "por.error_type": "ValueError",
        "llm.model_name": "fixture/model-1",
        "llm.provider": "fixture",
        "llm.token_count.prompt_details.cache_read": 3,
        "llm.token_count.completion_details.reasoning": 1,
        "por.input_tokens": True,
        "por.output_tokens": float("nan"),
        "por.nodes": float("inf"),
        "por.edges": -1,
        "por.model_turn": "1",
        "porpor.run_id": "private",
        "por.run_id.extra": "private",
        "por.span_layer": "business",
        "por.role": "private text with spaces",
        "input.value": "private",
        "llm.token_count.total.extra": 99,
    }
    assert telemetry._attributes(values) == {
        "por.run_id": "safe-run",
        "por.generation": 0,
        "por.model_calls": 2,
        "por.total_tokens": 15,
        "por.error_type": "ValueError",
        "llm.model_name": "fixture/model-1",
        "llm.provider": "fixture",
        "llm.token_count.prompt_details.cache_read": 3,
        "llm.token_count.completion_details.reasoning": 1,
    }
    assert values["por.span_layer"] == "business"  # caller data is untouched


def test_sanitized_resource_is_immutable_and_cannot_carry_between_spans():
    first = telemetry.safe_span(
        ReadableSpan(
            name="first",
            resource=Resource({"secret-resource": "private-first"}),
            attributes={"por.run_id": "run-a"},
        )
    )
    with pytest.raises(TypeError):
        first.resource.attributes["secret-resource"] = "private-mutation"
    second = telemetry.safe_span(
        ReadableSpan(
            name="second",
            resource=Resource({"secret-resource": "private-second"}),
            attributes={"por.run_id": "run-b"},
        )
    )
    assert (
        dict(first.resource.attributes)
        == dict(second.resource.attributes)
        == {"service.name": "public-opinion-research"}
    )
    assert first.attributes["por.run_id"] == "run-a"
    assert second.attributes["por.run_id"] == "run-b"
    assert "private-" not in first.to_json() + second.to_json()


def test_exporter_discards_tracestate_in_span_parent_and_link_contexts():
    def context(span_id, marker):
        return SpanContext(
            trace_id=123,
            span_id=span_id,
            is_remote=True,
            trace_flags=TraceFlags(1),
            trace_state=TraceState([("vendor", marker)]),
        )

    current = context(1, "private-span-state")
    parent = context(2, "private-parent-state")
    linked = context(3, "private-link-state")
    raw = ReadableSpan(
        name="privacy_probe",
        context=current,
        parent=parent,
        links=[Link(linked, {"authorization": "private-link-attribute"})],
    )
    safe = telemetry.safe_span(raw)
    assert not safe.context.trace_state
    assert not safe.parent.trace_state
    assert not safe.links[0].context.trace_state
    assert not safe.links[0].attributes
    assert (safe.context.trace_id, safe.context.span_id) == (123, 1)
    assert (safe.parent.trace_id, safe.parent.span_id) == (123, 2)
    assert (safe.links[0].context.trace_id, safe.links[0].context.span_id) == (123, 3)
    assert safe.context.is_remote and safe.context.trace_flags.sampled


def test_exporter_labels_spaced_agent_names_with_the_safe_existing_role():
    raw = ReadableSpan(
        name="Public Signal Agent",
        attributes={"openinference.span.kind": "AGENT", "por.role": "public_signal"},
    )
    safe = telemetry.safe_span(raw)
    assert safe.name == "public_signal.agent"
    assert safe.attributes["por.role"] == "public_signal"
    assert "Public Signal Agent" not in safe.to_json()


def test_export_failure_does_not_change_business(trace_capture):
    def fail(spans):
        raise OSError("secret-export-error")

    trace_capture.memory.export = fail
    with telemetry.business_span("offline"):
        result = 42
    assert result == 42
    assert trace_capture.provider.force_flush(timeout_millis=500)
    assert (
        telemetry.SafeExporter(SimpleNamespace(export=fail)).export([])
        == SpanExportResult.FAILURE
    )


def test_shutdown_deadline_covers_a_stuck_sdk(monkeypatch):
    import threading

    release = threading.Event()
    monkeypatch.setattr(telemetry, "_active", True)
    monkeypatch.setattr(
        telemetry, "_client", SimpleNamespace(shutdown=lambda: release.wait(2))
    )
    monkeypatch.setattr(telemetry, "_provider", SimpleNamespace(shutdown=lambda: None))
    monkeypatch.setattr(telemetry, "_settings", SimpleNamespace(shutdown_seconds=0.03))
    start = time.monotonic()
    try:
        telemetry.shutdown_telemetry()
        assert time.monotonic() - start < 0.3
    finally:
        release.set()


@pytest.mark.parametrize("trace_capture", [0], indirect=True)
def test_sampling_zero_has_no_exports_or_behavior_change(trace_capture):
    with telemetry.business_span("unsampled"):
        value = 17
    assert value == 17 and not trace_capture.spans()


def test_initialization_failure_does_not_leak_or_change_business(monkeypatch, caplog):
    import langfuse
    from opentelemetry import trace

    def fail(**kwargs):
        raise RuntimeError("secret-initialization-text")

    monkeypatch.setattr(telemetry, "_attempted", False)
    monkeypatch.setattr(telemetry, "_active", False)
    monkeypatch.setenv("POR_TRACING_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr(trace, "get_tracer_provider", trace.ProxyTracerProvider)
    monkeypatch.setattr(langfuse, "Langfuse", fail)
    assert not telemetry.initialize_telemetry()
    with telemetry.business_span("business") as span:
        assert span is None
    assert "secret-initialization-text" not in caplog.text


def test_responses_native_usage_and_details_are_collected(trace_capture, tmp_path):
    from test_sdk_models import _reply

    requests = []

    async def run():
        async def respond(request):
            requests.append(request)
            return httpx.Response(
                200, json=_reply("responses", len(requests), "fixture-responses")
            )

        @function_tool
        async def lookup(query: str) -> str:
            return "private-lookup-response"

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as transport:
            client = AsyncOpenAI(api_key="test-key-only", http_client=transport)
            model = OpenAIResponsesModel(
                model="fixture-responses", openai_client=client
            )
            workspace, _, _ = _workspace(tmp_path, producer=False)
            return await SDKContextHarness(workspace).run(
                Agent(name="agent", model=model, tools=[lookup]),
                "private-query",
                run_config=RunConfig(trace_include_sensitive_data=False),
            )

    result = asyncio.run(run())
    spans = trace_capture.spans()
    llms = kinds(spans, "LLM")
    assert len(llms) == len(requests) == 2
    assert len(kinds(spans, "TOOL")) == 1
    assert (
        sum(s.attributes["llm.token_count.total"] for s in llms)
        == result.context_wrapper.usage.total_tokens
        == 30
    )
    assert all(
        s.attributes["llm.token_count.prompt_details.cache_read"] == 0 for s in llms
    )
    assert all(s.attributes["llm.model_name"] == "fixture-responses" for s in llms)
    assert "private-" not in json.dumps([json.loads(s.to_json()) for s in spans])


def test_rolling_compaction_exposes_estimates_and_preserves_budget(
    trace_capture, tmp_path, monkeypatch
):
    from reference_workflow import PythonWorkflow
    from test_python_workflow_sdk import install_sdk

    from open_deep_research.budget import budget_capture
    from open_deep_research.research_graph.store import clear_research_graph_store_cache

    state, cfg, runtimes, _, calls, _ = install_sdk(monkeypatch, tmp_path, "rolling")
    try:
        with budget_capture() as charged:
            result = asyncio.run(
                PythonWorkflow().ainvoke(state, cfg, start_at="research")
            )
        spans = trace_capture.spans()
        compacted = [s for s in spans if s.name == "rolling_compaction"]
        assert compacted
        assert all(
            s.attributes["por.estimated_tokens_before"]
            > s.attributes["por.estimated_tokens_after"]
            > 0
            for s in compacted
        )
        assert (
            sum(rt.budget_usage["model_calls"] for rt in runtimes)
            <= result["runtime"]["budget"]["model_calls"]
        )
        assert (
            charged["model_calls"] + state["runtime"]["budget"]["model_calls"]
            == result["runtime"]["budget"]["model_calls"]
        )
        assert calls == {"public_signal": 25, "internal_knowledge": 25}
        assert all("private-" not in s.to_json() for s in spans)
    finally:
        clear_research_graph_store_cache()


def test_real_otlp_timeout_runs_off_the_business_thread(otlp_receiver):
    import threading

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    otlp_receiver.delay = 0.3
    # Bind the real native exporter before the in-memory fixture can patch it.
    exporter = RealOTLPExporter(
        endpoint=otlp_receiver.url + "/api/public/otel/v1/traces", timeout=0.04
    )
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(
        BatchSpanProcessor(telemetry.SafeExporter(exporter), schedule_delay_millis=10)
    )
    tracer = provider.get_tracer(telemetry.__name__)
    business_thread = threading.get_ident()
    start = time.monotonic()
    with tracer.start_as_current_span("bounded_export"):
        completed = threading.get_ident() == business_thread
    elapsed = time.monotonic() - start
    assert completed and elapsed < 0.2
    provider.force_flush(timeout_millis=1000)
    provider.shutdown()
    assert otlp_receiver.spans


def test_api_shutdown_flushes_telemetry_even_when_history_close_fails(monkeypatch):
    from open_deep_research.web import durable, server

    calls = []

    def fail():
        calls.append("history")
        raise RuntimeError("storage-close-failed")

    monkeypatch.setattr(telemetry, "initialize_telemetry", lambda: False)
    monkeypatch.setattr(
        telemetry, "shutdown_telemetry", lambda: calls.append("telemetry")
    )
    monkeypatch.setattr(durable, "close_run_client", lambda: calls.append("run"))
    monkeypatch.setattr(server, "close_history_store", fail)

    async def close():
        async with server.lifespan(server.app):
            pass

    with pytest.raises(RuntimeError, match="storage-close-failed"):
        asyncio.run(close())
    assert calls == ["history", "run", "telemetry"]
