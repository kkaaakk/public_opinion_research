"""Focused tests for the optional Agent Observer v0.2 integration."""

import asyncio
import contextvars
import json
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RetryPolicy, interrupt
from typing_extensions import TypedDict

import open_deep_research.deep_researcher as deep_researcher_module
import open_deep_research.observability.agent_observer as observer_module

agent_observer_sdk = pytest.importorskip("agent_observer.sdk")
langgraph_adapter = pytest.importorskip("integrations.langgraph")
SDKAgentObserver = agent_observer_sdk.AgentObserver
get_current_observed_run = agent_observer_sdk.get_current_observed_run
get_current_observed_span = agent_observer_sdk.get_current_observed_span
register_graph_topology = langgraph_adapter.register_graph_topology
run_context = langgraph_adapter.run_context
from open_deep_research.observability import (  # noqa: E402
    ObservedGraph,
    ObserverRunLifecycle,
    observe_graph_node,
    observe_model_ainvoke,
    record_tool_call,
    record_tool_result,
)
from open_deep_research.state import Section  # noqa: E402


class _EchoGraph:
    """Small Runnable-shaped fixture used to exercise graph lifecycle handling."""

    def invoke(self, value, config=None, **kwargs):
        return value

    async def ainvoke(self, value, config=None, **kwargs):
        return value

    def stream(self, value, config=None, **kwargs):
        yield value

    async def astream(self, value, config=None, **kwargs):
        yield value

    def get_graph(self, *args, **kwargs):
        return self


class _NativeFixtureState(TypedDict, total=False):
    value: int


def _native_fixture_graph(
    lifecycle: ObserverRunLifecycle,
    *,
    failing: bool = False,
    retrying: bool = False,
):
    """Build a tiny native Pregel graph around the real lifecycle wrapper."""

    @observe_graph_node(name="native_agent", kind="agent")
    async def native_agent(state, config):
        span = get_current_observed_span()
        if span is not None:
            span.model_request(request_id="native-request", input_tokens=1)
            record_tool_call("native_tool", tool_call_id="native-call", args={"value": state.get("value", 0)})
            record_tool_result(
                "native_tool",
                tool_call_id="native-call",
                success=True,
                duration_ms=1,
                result={"value": state.get("value", 0)},
            )
            span.model_response(request_id="native-request", output_tokens=1, duration_ms=1)
        return {"value": state.get("value", 0) + 1}

    @observe_graph_node(name="native_writer", kind="writer")
    async def native_writer(state, config):
        if failing:
            raise RuntimeError("native fixture failed")
        if retrying and not getattr(native_writer, "has_failed", False):
            native_writer.has_failed = True
            raise ConnectionError("native retry fixture failed once")
        return {"value": state.get("value", 0) + 1}

    builder = StateGraph(_NativeFixtureState)
    builder.add_node("native_agent", lifecycle.wrap_node("native_agent", native_agent))
    builder.add_node(
        "native_writer",
        lifecycle.wrap_node(
            "native_writer",
            native_writer,
            finish=True,
            retry_max_attempts=2 if retrying else None,
        ),
        retry_policy=(
            RetryPolicy(
                initial_interval=0,
                max_attempts=2,
                jitter=False,
                retry_on=lambda _error: True,
            )
            if retrying
            else None
        ),
    )
    builder.add_edge(START, "native_agent")
    builder.add_edge("native_agent", "native_writer")
    builder.add_edge("native_writer", END)
    return builder.compile()


def _interrupt_fixture_graph(lifecycle: ObserverRunLifecycle):
    """Build a checkpointed native graph that pauses once and then resumes."""

    @observe_graph_node(name="approval_node", kind="agent")
    async def approval_node(state, config):
        answer = interrupt("approval required")
        return {"value": answer}

    builder = StateGraph(_NativeFixtureState)
    builder.add_node(
        "approval_node",
        lifecycle.wrap_node("approval_node", approval_node, finish=True),
    )
    builder.add_edge(START, "approval_node")
    return builder.compile(checkpointer=InMemorySaver())


def _recording_observer_class(captured_events):
    class RecordingObserver:
        def __init__(self, **kwargs):
            self.inner = SDKAgentObserver(enabled=False, project=kwargs.get("project", "test"))

            def record(run_id, sequence, event_type, *, span_id=None, **payload):
                captured_events.append(
                    {
                        "run_id": run_id,
                        "sequence": sequence,
                        "type": event_type,
                        "span_id": span_id,
                        **payload,
                    }
                )

            self.inner._record = record

        def start_run(self, **kwargs):
            return self.inner.start_run(**kwargs)

        def close(self, timeout=1.0):
            self.inner.close(timeout)

    return RecordingObserver


def _recording_run():
    observer = SDKAgentObserver(enabled=False, project="public-opinion-test")
    events: list[dict[str, Any]] = []

    def record(run_id, sequence, event_type, *, span_id=None, **payload):
        events.append(
            {
                "run_id": run_id,
                "sequence": sequence,
                "type": event_type,
                "span_id": span_id,
                **payload,
            }
        )

    observer._record = record
    run_holder = {}
    contextvars.copy_context().run(
        lambda: run_holder.setdefault(
            "run",
            observer.start_run(prompt="fixture research"),
        )
    )
    run = run_holder["run"]
    return observer, run, events


def test_observer_disabled_is_noop(monkeypatch):
    """Disabled configuration must not instantiate or contact the sidecar."""

    class BombObserver:
        def __init__(self, **kwargs):  # pragma: no cover - assertion guard
            raise AssertionError("disabled Observer must not be constructed")

    monkeypatch.setattr(observer_module, "AgentObserver", BombObserver)
    state = {"messages": [{"role": "user", "content": "brand risk"}]}
    result = ObservedGraph(_EchoGraph()).invoke(
        state,
        {"configurable": {"agent_observer_enabled": False}},
    )

    assert result is state


def test_observer_offline_does_not_break_graph():
    """An enabled but unreachable sidecar remains fail-open for the graph."""

    state = {"messages": [{"role": "user", "content": "brand risk"}]}
    result = ObservedGraph(_EchoGraph()).invoke(
        state,
        {
            "configurable": {
                "agent_observer_enabled": True,
                "agent_observer_endpoint": "http://127.0.0.1:9",
                "agent_observer_timeout": 0.01,
            }
        },
    )

    assert result is state


def test_public_opinion_run_has_single_observer_run(monkeypatch):
    """A graph invocation produces one outer Run, not one Run per Agent."""

    captured_events: list[dict[str, Any]] = []

    class RecordingObserver:
        def __init__(self, **kwargs):
            self.inner = SDKAgentObserver(enabled=False, project=kwargs.get("project", "test"))

            def record(run_id, sequence, event_type, *, span_id=None, **payload):
                captured_events.append(
                    {
                        "run_id": run_id,
                        "sequence": sequence,
                        "type": event_type,
                        "span_id": span_id,
                        **payload,
                    }
                )

            self.inner._record = record

        def start_run(self, **kwargs):
            return self.inner.start_run(**kwargs)

        def close(self, timeout=1.0):
            self.inner.close(timeout)

    monkeypatch.setattr(observer_module, "AgentObserver", RecordingObserver)
    value = {
        "messages": [{"role": "user", "content": "brand risk"}],
        "role_reports": {"public_signal": "private report that must not be copied"},
        "final_report": "generated report",
    }

    result = asyncio.run(
        ObservedGraph(_EchoGraph()).ainvoke(
            value,
            {"configurable": {"agent_observer_enabled": True}},
        )
    )

    assert result is value
    assert [event["type"] for event in captured_events].count("run_started") == 1
    assert [event["type"] for event in captured_events].count("run_finished") == 1
    assert "private report that must not be copied" not in json.dumps(captured_events)


def test_parallel_agents_have_distinct_spans_and_real_upstreams(monkeypatch):
    """Parallel public-signal/internal-knowledge work keeps isolated context."""

    observer, run, events = _recording_run()

    async def fake_agent(state, config, role):
        span = get_current_observed_span()
        assert span is not None
        await asyncio.sleep(0)
        span.model_request(request_id=f"request-{role}", input_tokens=1)
        span.tool_call("fixture_tool", tool_call_id=f"tool-{role}")
        span.tool_result(
            "fixture_tool",
            tool_call_id=f"tool-{role}",
            success=True,
            duration_ms=1,
            raw_bytes=8,
            context_bytes=8,
        )
        span.model_response(request_id=f"request-{role}", output_tokens=1, duration_ms=1)
        return {"role_reports": {role: f"{role} report"}, "agent_memories": {}}

    monkeypatch.setattr(deep_researcher_module, "_run_public_opinion_agent", fake_agent)
    register_graph_topology(
        {
            "edges": [
                ["public_signal_agent", "risk_assessment_agent"],
                ["internal_knowledge_agent", "risk_assessment_agent"],
                ["risk_assessment_agent", "response_strategy_agent"],
            ]
        },
        run=run,
    )

    async def exercise():
        with run_context(run):
            public_result, internal_result = await asyncio.gather(
                deep_researcher_module.public_signal_agent({}, {}),
                deep_researcher_module.internal_knowledge_agent({}, {}),
            )
            reports = {
                **public_result["role_reports"],
                **internal_result["role_reports"],
            }
            await deep_researcher_module.risk_assessment_agent(
                {"role_reports": reports},
                {},
            )
            await deep_researcher_module.response_strategy_agent(
                {"role_reports": reports},
                {},
            )

    asyncio.run(exercise())

    started = {
        event["name"]: event
        for event in events
        if event["type"] == "span_started"
    }
    assert {"public_signal_agent", "internal_knowledge_agent", "risk_assessment_agent", "response_strategy_agent"} <= set(started)
    assert set(started["risk_assessment_agent"]["upstream_span_ids"]) == {
        started["public_signal_agent"]["span_id"],
        started["internal_knowledge_agent"]["span_id"],
    }
    assert started["response_strategy_agent"]["upstream_span_ids"] == [
        started["risk_assessment_agent"]["span_id"]
    ]

    for role, node_name in (
        ("public_signal", "public_signal_agent"),
        ("internal_knowledge", "internal_knowledge_agent"),
    ):
        span_id = started[node_name]["span_id"]
        role_events = [event for event in events if event["span_id"] == span_id]
        assert any(event["type"] == "model_request" for event in role_events)
        assert any(event["type"] == "tool_call" and event["tool_call_id"] == f"tool-{role}" for event in role_events)
        assert all(event["span_id"] == span_id for event in role_events)

    observer.close()


def test_writer_nodes_have_writer_spans():
    """Section, final-section, and compilation nodes are independently visible."""

    observer, run, events = _recording_run()
    completed = Section(
        name="Evidence",
        description="Evidence",
        research=True,
        content="already written",
        status="done",
    )

    async def exercise():
        with run_context(run):
            await deep_researcher_module.section_writer({"sections": []}, {})
            await deep_researcher_module.write_final_sections({"sections": []}, {})
            await deep_researcher_module.compile_final_report(
                {
                    "sections": [completed],
                    "completed_sections": [completed],
                    "messages": [],
                    "notes": [],
                    "budget_usage": {},
                },
                {"configurable": {"rag_memory_write_enabled": False}},
            )

    asyncio.run(exercise())
    writer_spans = {
        event["name"]: event
        for event in events
        if event["type"] == "span_started" and event["kind"] == "writer"
    }
    assert {"section_writer", "write_final_sections", "compile_final_report"} <= set(writer_spans)
    observer.close()


def test_model_and_tool_events_are_current_span_scoped():
    """Model usage and sanitized tool facts attach to the active Agent Span."""

    observer, run, events = _recording_run()

    class FakeModel:
        async def ainvoke(self, payload):
            return AIMessage(
                content="fixture",
                usage_metadata={"input_tokens": 13, "output_tokens": 5, "total_tokens": 18},
                response_metadata={"finish_reason": "stop"},
            )

    async def exercise():
        with run_context(run):
            async with run.span("agent", kind="agent") as span:
                await observe_model_ainvoke(FakeModel(), [], observer_model="fixture:model")
                record_tool_call("fixture_tool", tool_call_id="call-1", args={"query": "sensitive"})
                record_tool_result(
                    "fixture_tool",
                    tool_call_id="call-1",
                    success=True,
                    duration_ms=2,
                    result="SECRET_ROLE_REPORT_TAIL",
                )
                assert span.span_id

    asyncio.run(exercise())
    span_id = next(event["span_id"] for event in events if event["type"] == "span_started")
    scoped = [event for event in events if event["span_id"] == span_id]
    response = next(event for event in scoped if event["type"] == "model_response")
    assert response["input_tokens"] == 13
    assert response["output_tokens"] == 5
    assert any(event["type"] == "tool_call" for event in scoped)
    tool_result = next(event for event in scoped if event["type"] == "tool_result")
    assert tool_result["raw_bytes"] > 0
    assert "SECRET_ROLE_REPORT_TAIL" not in json.dumps(events)
    observer.close()


def test_observer_does_not_mutate_langgraph_state():
    """Observer lifecycle data stays outside the graph state payload."""

    state = {
        "messages": [{"role": "user", "content": "brand risk"}],
        "role_reports": {"public_signal": "full report"},
    }
    original = dict(state)
    result = asyncio.run(
        ObservedGraph(_EchoGraph()).ainvoke(
            state,
            {"configurable": {"agent_observer_enabled": False}},
        )
    )

    assert state == original
    assert result == original


def test_rag_query_rewrite_model_is_observed():
    """The synchronous RAG rewrite path uses the same model boundary."""

    from open_deep_research.rag.query_rewriter import rewrite_query_with_model

    observer, run, events = _recording_run()

    class FakeModel:
        def invoke(self, payload):
            return AIMessage(
                content="Rewritten query: battery warranty complaint",
                usage_metadata={"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
            )

    with run_context(run):
        with run.span("rag_tool", kind="agent"):
            rewritten = rewrite_query_with_model(
                "battery complaint",
                model_name="openai:fixture",
                max_tokens=32,
                api_key=None,
                model_factory=lambda **_kwargs: FakeModel(),
            )

    assert rewritten == "battery warranty complaint"
    assert any(
        event["type"] == "model_request" and event.get("component") == "rag_query_rewrite"
        for event in events
    )
    observer.close()


def test_vision_model_is_observed(monkeypatch):
    """The multimodal loader reports a runtime Vision request when in a Run."""

    from open_deep_research.rag.loaders import knowledge

    observer, run, events = _recording_run()

    class FakeVisionModel:
        def invoke(self, payload):
            return AIMessage(
                content="a product diagram",
                usage_metadata={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
            )

    monkeypatch.setattr(knowledge, "init_chat_model", lambda **_kwargs: FakeVisionModel())
    with run_context(run):
        with run.span("rag_vision", kind="agent"):
            result = knowledge._invoke_vision_model(
                image_bytes=b"image",
                mime_type="image/png",
                model="openai:vision-fixture",
                prompt="describe",
                max_tokens=32,
            )

    assert result == "a product diagram"
    assert any(event.get("component") == "rag_vision" for event in events if event["type"] == "model_request")
    observer.close()


def test_utils_structured_model_is_observed_and_missing_usage_stays_na():
    """Structured wrappers are visible even when no raw provider usage exists."""

    from open_deep_research.utils import summarize_webpage

    observer, run, events = _recording_run()

    class FakeStructuredModel:
        async def ainvoke(self, payload):
            return SimpleNamespace(summary="short", key_excerpts=["evidence"])

    async def exercise():
        with run_context(run):
            async with run.span("web_search", kind="agent"):
                result = await summarize_webpage(
                    FakeStructuredModel(),
                    "page text",
                    observer_model="openai:summary-fixture",
                )
        return result

    assert "<summary>" in asyncio.run(exercise())
    response = next(
        event
        for event in events
        if event["type"] == "model_response" and event.get("component") == "webpage_summarization"
    )
    assert response["structured_output"] is True
    assert response["input_tokens"] is None
    assert response["output_tokens"] is None
    observer.close()


def test_model_call_not_double_counted():
    """Nested helper use records the provider boundary once."""

    observer, run, events = _recording_run()

    class InnerModel:
        async def ainvoke(self, payload):
            return AIMessage(content="ok")

    class OuterModel:
        async def ainvoke(self, payload):
            return await observe_model_ainvoke(InnerModel(), payload, observer_model="fixture:inner")

    async def exercise():
        with run_context(run):
            async with run.span("agent", kind="agent"):
                await observe_model_ainvoke(OuterModel(), [], observer_model="fixture:outer")

    asyncio.run(exercise())
    assert [event["type"] for event in events].count("model_request") == 1
    assert [event["type"] for event in events].count("model_response") == 1
    observer.close()


def test_failed_model_call_recorded():
    """A model exception emits a failed response fact before being re-raised."""

    observer, run, events = _recording_run()

    class FailingModel:
        async def ainvoke(self, payload):
            raise RuntimeError("model fixture failed")

    async def exercise():
        with run_context(run):
            with pytest.raises(RuntimeError, match="model fixture failed"):
                async with run.span("agent", kind="agent"):
                    await observe_model_ainvoke(FailingModel(), [], observer_model="fixture:model")

    asyncio.run(exercise())
    response = next(event for event in events if event["type"] == "model_response")
    assert response["success"] is False
    assert response["stop_reason"] == "error"
    observer.close()


def test_direct_tool_execution_is_observed_and_not_double_counted():
    """Nested tool adapters emit one privacy-preserving call/result pair."""

    observer, run, events = _recording_run()

    class InnerTool:
        name = "rag_search"
        metadata = {"tool_domain": "rag"}

        async def ainvoke(self, args, config=None):
            return {"context": "SENSITIVE"}

    inner = InnerTool()

    class OuterTool:
        name = "mcp__rag_search"
        metadata = {"tool_domain": "external_mcp"}

        async def ainvoke(self, args, config=None):
            return await observer_module.observe_tool_ainvoke(
                inner,
                args,
                config,
                tool_call_id="inner-call",
            )

    async def exercise():
        with run_context(run):
            async with run.span("agent", kind="agent"):
                result = await observer_module.observe_tool_ainvoke(
                    OuterTool(),
                    {"query": "secret query"},
                    {},
                    tool_call_id="outer-call",
                )
        return result

    assert asyncio.run(exercise()) == {"context": "SENSITIVE"}
    assert [event["type"] for event in events].count("tool_call") == 1
    assert [event["type"] for event in events].count("tool_result") == 1
    assert "SENSITIVE" not in json.dumps(events)
    observer.close()


def test_execute_tool_safely_is_runtime_tool_boundary():
    """The Public Opinion ReAct executor emits exactly one tool call/result."""

    observer, run, events = _recording_run()

    class FakeTool:
        name = "mcp__fixture_search"
        metadata = {"tool_domain": "external_mcp"}

        async def ainvoke(self, args, config=None):
            return "bounded fixture result"

    async def exercise():
        with run_context(run):
            async with run.span("agent", kind="agent"):
                return await deep_researcher_module.execute_tool_safely(
                    FakeTool(),
                    {"query": "fixture"},
                    {},
                    tool_call_id="fixture-call",
                )

    observation, _budget, success = asyncio.run(exercise())
    assert observation == "bounded fixture result"
    assert success is True
    assert [event["type"] for event in events].count("tool_call") == 1
    assert [event["type"] for event in events].count("tool_result") == 1
    observer.close()


def test_failed_tool_recorded():
    """Tool failures remain visible while the original exception propagates."""

    observer, run, events = _recording_run()

    class FailingTool:
        name = "mcp_fixture"

        async def ainvoke(self, args, config=None):
            raise RuntimeError("tool fixture failed")

    async def exercise():
        with run_context(run):
            async with run.span("agent", kind="agent"):
                with pytest.raises(RuntimeError, match="tool fixture failed"):
                    await observer_module.observe_tool_ainvoke(FailingTool(), {}, {})

    asyncio.run(exercise())
    result = next(event for event in events if event["type"] == "tool_result")
    assert result["success"] is False
    assert result["error_type"] == "RuntimeError"
    observer.close()


def test_tool_schema_preserved_after_mcp_wrapping():
    """MCP namespace wrapping keeps the original schema and behavior fields."""

    from open_deep_research.mcp.tool_wrapper import wrap_mcp_tools

    def execute(query: str) -> str:
        return query

    original = StructuredTool.from_function(
        execute,
        name="search",
        description="search fixture",
        return_direct=True,
    )
    wrapped = wrap_mcp_tools("rag", [original], {"search"})[0]
    assert wrapped.name == "rag__search"
    assert wrapped.description.startswith("[rag]")
    assert wrapped.args_schema is original.args_schema
    assert wrapped.return_direct is True


def test_node_retry_creates_distinct_attempt_spans(monkeypatch):
    """LangGraph retry attempts use new Span IDs and close both attempts."""

    captured_events: list[dict[str, Any]] = []
    monkeypatch.setattr(observer_module, "AgentObserver", _recording_observer_class(captured_events))
    graph = _native_fixture_graph(ObserverRunLifecycle(), retrying=True)
    result = asyncio.run(
        graph.ainvoke(
            {"value": 0},
            {"configurable": {"thread_id": "retry-thread", "agent_observer_enabled": True}},
        )
    )
    assert result["value"] == 2
    writer_starts = [
        event
        for event in captured_events
        if event["type"] == "span_started" and event["name"] == "native_writer"
    ]
    assert len(writer_starts) == 2
    assert {event["metadata"]["attempt"] for event in writer_starts} == {1, 2}
    assert len({event["span_id"] for event in writer_starts}) == 2
    assert sum(event["type"] == "span_failed" and event["span_id"] == writer_starts[0]["span_id"] for event in captured_events) == 1
    assert sum(event["type"] == "span_finished" and event["span_id"] == writer_starts[1]["span_id"] for event in captured_events) == 1
    assert [event["type"] for event in captured_events].count("run_finished") == 1
    assert not any(event["type"] == "run_failed" for event in captured_events)


def test_interrupt_resume_uses_linked_segments_without_context_leak(monkeypatch):
    """Interrupt is terminal for one segment; resume starts a linked segment."""

    captured_events: list[dict[str, Any]] = []
    monkeypatch.setattr(observer_module, "AgentObserver", _recording_observer_class(captured_events))
    lifecycle = ObserverRunLifecycle()
    graph = _interrupt_fixture_graph(lifecycle)
    config = {"configurable": {"thread_id": "interrupt-thread", "agent_observer_enabled": True}}

    first = asyncio.run(graph.ainvoke({"value": "start"}, config))
    assert "__interrupt__" in first
    first_run_id = next(event["run_id"] for event in captured_events if event["type"] == "run_started")
    first_run_events = [event for event in captured_events if event["run_id"] == first_run_id]
    assert any(event["type"] == "run_interrupted" for event in first_run_events)
    assert not any(event["type"] in {"run_failed", "run_finished"} for event in first_run_events)
    assert any(event["type"] == "span_interrupted" for event in first_run_events)

    resumed = asyncio.run(graph.ainvoke(Command(resume="approved"), config))
    assert resumed["value"] == "approved"
    run_starts = [event for event in captured_events if event["type"] == "run_started"]
    assert len(run_starts) == 2
    second_run_id = run_starts[1]["run_id"]
    assert second_run_id != first_run_id
    assert run_starts[0]["metadata"]["logical_run_id"] == run_starts[1]["metadata"]["logical_run_id"]
    assert run_starts[1]["metadata"]["invocation_kind"] == "resume"
    second_run_events = [event for event in captured_events if event["run_id"] == second_run_id]
    assert any(event["type"] == "run_finished" for event in second_run_events)
    assert not any(event["type"] == "run_failed" for event in second_run_events)
    assert get_current_observed_run() is None
    assert get_current_observed_span() is None

    fresh = asyncio.run(graph.ainvoke({"value": "fresh"}, config))
    assert "__interrupt__" in fresh
    run_starts = [event for event in captured_events if event["type"] == "run_started"]
    assert len(run_starts) == 3
    assert run_starts[2]["metadata"]["logical_run_id"] != run_starts[0]["metadata"]["logical_run_id"]
    assert run_starts[2]["metadata"]["invocation_kind"] == "start"


def test_concurrent_native_invocations_do_not_cross_contaminate(monkeypatch):
    """One compiled graph can serve two thread-scoped Runs concurrently."""

    captured_events: list[dict[str, Any]] = []
    monkeypatch.setattr(observer_module, "AgentObserver", _recording_observer_class(captured_events))
    graph = _native_fixture_graph(ObserverRunLifecycle())

    async def invoke(thread_id: str):
        return await graph.ainvoke(
            {"value": 0},
            {"configurable": {"thread_id": thread_id, "agent_observer_enabled": True}},
        )

    async def run_both():
        return await asyncio.gather(invoke("concurrent-a"), invoke("concurrent-b"))

    results = asyncio.run(run_both())
    assert [result["value"] for result in results] == [2, 2]
    by_run = {}
    for event in captured_events:
        by_run.setdefault(event["run_id"], []).append(event)
    assert len(by_run) == 2
    for run_events in by_run.values():
        assert [event["type"] for event in run_events].count("run_finished") == 1
        assert {event.get("metadata", {}).get("thread_id") for event in run_events if event["type"] == "span_started"}
        assert all(event["run_id"] == run_events[0]["run_id"] for event in run_events)


def test_native_graph_invocation_creates_one_observer_run(monkeypatch):
    """A native Pregel graph keeps one Run boundary across its nodes."""

    captured_events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        observer_module,
        "AgentObserver",
        _recording_observer_class(captured_events),
    )
    lifecycle = ObserverRunLifecycle()
    graph = _native_fixture_graph(lifecycle)

    result = asyncio.run(
        graph.ainvoke(
            {"value": 0},
            {
                "configurable": {
                    "thread_id": "native-one",
                    "agent_observer_enabled": True,
                }
            },
        )
    )

    assert result["value"] == 2
    assert [event["type"] for event in captured_events].count("run_started") == 1
    assert [event["type"] for event in captured_events].count("run_finished") == 1
    run_id = next(event["run_id"] for event in captured_events if event["type"] == "run_started")
    run_events = [event for event in captured_events if event["run_id"] == run_id]
    assert {event["name"] for event in run_events if event["type"] == "span_started"} == {
        "native_agent",
        "native_writer",
    }
    assert any(event["type"] == "model_request" for event in run_events)
    assert any(event["type"] == "tool_call" for event in run_events)
    assert get_current_observed_run() is None


def test_two_native_graph_invocations_use_distinct_runs(monkeypatch):
    """Factory-style native graphs do not cross-contaminate concurrent Runs."""

    captured_events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        observer_module,
        "AgentObserver",
        _recording_observer_class(captured_events),
    )

    async def invoke(thread_id: str):
        graph = _native_fixture_graph(ObserverRunLifecycle())
        return await graph.ainvoke(
            {"value": 0},
            {
                "configurable": {
                    "thread_id": thread_id,
                    "agent_observer_enabled": True,
                }
            },
        )

    async def run_both():
        return await asyncio.gather(invoke("native-a"), invoke("native-b"))

    results = asyncio.run(run_both())
    assert [result["value"] for result in results] == [2, 2]

    run_ids = {
        event["run_id"]
        for event in captured_events
        if event["type"] == "run_started"
    }
    assert len(run_ids) == 2
    for run_id in run_ids:
        run_events = [event for event in captured_events if event["run_id"] == run_id]
        assert [event["type"] for event in run_events].count("run_finished") == 1
        assert all(event["run_id"] == run_id for event in run_events)


def test_native_graph_failure_marks_run_failed_and_cleans_context(monkeypatch):
    """A native graph exception fails and closes its Run without leaking context."""

    captured_events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        observer_module,
        "AgentObserver",
        _recording_observer_class(captured_events),
    )
    lifecycle = ObserverRunLifecycle()
    graph = _native_fixture_graph(lifecycle, failing=True)

    with pytest.raises(RuntimeError, match="native fixture failed"):
        asyncio.run(
            graph.ainvoke(
                {"value": 0},
                {
                    "configurable": {
                        "thread_id": "native-failure",
                        "agent_observer_enabled": True,
                    }
                },
            )
        )

    assert any(event["type"] == "run_failed" for event in captured_events)
    assert not any(event["type"] == "run_finished" for event in captured_events)
    assert get_current_observed_run() is None


def test_native_graph_disabled_and_offline_are_fail_open():
    """Native graphs still run with Observer disabled or its server offline."""

    disabled_graph = _native_fixture_graph(ObserverRunLifecycle())
    disabled_result = asyncio.run(
        disabled_graph.ainvoke(
            {"value": 0},
            {"configurable": {"agent_observer_enabled": False}},
        )
    )
    assert disabled_result["value"] == 2

    offline_graph = _native_fixture_graph(ObserverRunLifecycle())
    offline_result = asyncio.run(
        offline_graph.ainvoke(
            {"value": 0},
            {
                "configurable": {
                    "agent_observer_enabled": True,
                    "agent_observer_endpoint": "http://127.0.0.1:9",
                    "agent_observer_timeout": 0.01,
                }
            },
        )
    )
    assert offline_result["value"] == 2
