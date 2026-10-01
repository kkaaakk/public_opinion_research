"""Storage, privacy, projection and real callback lifecycle invariants."""

import asyncio
import json
import os
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from trajectory_fixture import FixtureChat, answer, build_graph

from open_deep_research.trajectory import TrajectorySession, TrajectorySessionRecorder
from open_deep_research.trajectory.events import SessionEvent
from open_deep_research.trajectory.ownership import WriterOwnedError
from open_deep_research.trajectory.persistence import JsonlPersistence
from open_deep_research.trajectory.projection import replay
from open_deep_research.trajectory.sanitization import sanitize


def begin(tmp_path=None, session_id="test"):
    storage = JsonlPersistence(tmp_path) if tmp_path else None
    session = TrajectorySession(session_id, storage=storage)
    session.start_turn("battery safety", {})
    return session, TrajectorySessionRecorder(session), storage


def select(session, kind):
    return [e for e in session.events if e.type == kind]


def test_append_only_monotonic_detached_redacted_utf8_and_serialization(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "a-secret-environment-value")
    session, recorder, storage = begin(tmp_path)
    original = storage.path("test").read_bytes()
    session.append("request/context", {"turn": 1, "credentials": "opaque", "nested": {
        "query": "电池安全", "Authorization": "Bearer aaa", "text": "api_key=abc123",
        "dsn": "mysql://me:password@db", "value": "a-secret-environment-value"}})
    copy = session.events[-1]
    copy.data["nested"]["query"] = "changed"
    assert session.events[-1].data["nested"]["query"] == "电池安全"
    recorder.finish("completed")
    content = storage.path("test").read_text(encoding="utf-8")
    assert storage.path("test").read_bytes().startswith(original)
    assert all(secret not in content for secret in ("abc123", "password@", "a-secret-environment-value", "Bearer aaa"))
    rows = storage.read("test").events
    assert [e.seq for e in rows] == list(range(len(rows)))
    assert [e.to_dict() for e in rows] == [e.to_dict() for e in session.events]
    assert sanitize(sanitize("api_key=abc123 Authorization=Bearer abc123")) == sanitize("api_key=abc123 Authorization=Bearer abc123")
    assert SessionEvent.from_dict(rows[0].to_dict()).to_dict() == rows[0].to_dict()


def test_duplicate_gap_write_ownership_and_live_read(tmp_path):
    storage = JsonlPersistence(tmp_path)
    writer = storage.create("test")
    event = SessionEvent("test", 0, 10, "session/start", {})
    writer.append([event])
    with pytest.raises(ValueError, match="contiguous"):
        writer.append([event])
    with pytest.raises(ValueError, match="contiguous"):
        writer.append([SessionEvent("test", 2, 11, "session/end", {})])
    with pytest.raises(WriterOwnedError):
        storage.create("test")
    assert storage.active("test")
    assert len(storage.read("test").events) == 1
    writer.close()
    assert not storage.active("test")


@pytest.mark.parametrize("tail", [b'{"seq":', b'{"version":1}'])
def test_torn_tail_returns_prefix_and_refuses_append(tmp_path, tail):
    session, recorder, storage = begin(tmp_path)
    recorder.finish("completed")
    count = len(session.events)
    with storage.path("test").open("ab") as handle:
        handle.write(tail)
    result = storage.read("test")
    assert len(result.events) == count and result.warning == "torn_tail"
    with pytest.raises(ValueError, match="torn"):
        storage.create("test")


@pytest.mark.parametrize("corruption", [b'not-json\n', b'{"version":500}\n'])
def test_complete_corrupt_record_is_never_silently_swallowed(tmp_path, corruption):
    session, recorder, storage = begin(tmp_path)
    recorder.finish("completed")
    content = storage.path("test").read_bytes().splitlines(keepends=True)
    storage.path("test").write_bytes(content[0] + corruption + b"".join(content[2:]))
    with pytest.raises(ValueError, match="Corrupt"):
        storage.read("test")


def test_retention_skips_live_writer_and_disables_at_zero(tmp_path):
    storage = JsonlPersistence(tmp_path, retention_days=1)
    writer = storage.create("test")
    writer.append([SessionEvent("test", 0, 10, "session/start", {})])
    os.utime(storage.path("test"), (1, 1))
    storage.prune()
    assert storage.path("test").exists()
    writer.close()
    storage.retention_days = 0
    storage.prune()
    assert storage.path("test").exists()
    storage.retention_days = 1
    storage.prune()
    assert not storage.path("test").exists()


def test_final_request_one_turn_one_step_missing_usage_and_ttft():
    session, recorder, _ = begin()
    model = FixtureChat(script=[AIMessage(content="answer")])
    asyncio.run(model.ainvoke([SystemMessage(content="system"), HumanMessage(content="battery")], {"callbacks": [recorder]}))
    recorder.finish("completed")
    assert len(select(session, "turn/start")) == len(select(session, "step/start")) == 1
    message = select(session, "assistant/message")[0]
    assert "usage" not in message.data and message.data["first_token_at"] is None
    assert message.data["stream"] == []


def test_official_parallel_nested_graph_steps_schemas_and_identical_replay(tmp_path):
    session, recorder, storage = begin(tmp_path)
    graph, counts, models = build_graph()
    async def run():
        async for _ in graph.astream_events({"runtime": {"budget": {}}}, {"callbacks": [recorder]}, version="v2"):
            pass
    asyncio.run(run())
    recorder.finish("completed")
    assert counts["public_signal"] == counts["internal_knowledge"] == counts["risk_assessment"] == counts["response_strategy"] == 1
    assert sum(m.calls for m in models.values()) == 6
    assert len(select(session, "turn/start")) == 1
    assert len(select(session, "step/start")) == 6
    assert len(select(session, "assistant/message")) == 6
    assert counts["web_search"] == counts["rag_search"] == counts["lookup_document"] == 1
    assert len(select(session, "tool/call")) == 3
    root_tools = [e for e in select(session, "tool/call") if not e.data["parent_call_id"]]
    assert all(e.data["step"] is not None and e.data["parent_resolution"] == "call_id" for e in root_tools)
    nested = next(e for e in select(session, "tool/call") if e.data["name"] == "lookup_document")
    rag = next(e for e in root_tools if e.data["name"] == "rag_search")
    assert nested.data["parent_call_id"] == rag.data["call_id"] and nested.data["step"] == rag.data["step"]
    assert session.projection.snapshot(active=False) == replay(storage.read("test").events)
    snapshot = replay(session.events)
    assert len(snapshot["callSchemas"]) >= 2
    assert all(e.run_id and e.parent_run_id for e in select(session, "assistant/message"))
    assert all(e.data["first_token_at"] is not None and e.data["stream"] for e in select(session, "assistant/message"))
    assert any(e.agent_name == "risk_assessment_agent" for e in select(session, "step/start"))
    assert not session.degraded


def test_multiple_parallel_tools_settle_before_step_end():
    session, recorder, _ = begin()
    @tool
    async def lookup(query: str):
        """Read a query."""
        await asyncio.sleep(0)
        return query
    model = FixtureChat(script=[answer("search", [
        {"id": "a", "name": "lookup", "args": {"query": "one"}},
        {"id": "b", "name": "lookup", "args": {"query": "two"}}]), answer("done")])
    async def body(_input, config):
        history = [HumanMessage(content="brand")]
        first = await model.ainvoke(history, config)
        results = await asyncio.gather(*[lookup.ainvoke({"type": "tool_call", **c}, config) for c in first.tool_calls])
        await model.ainvoke([*history, first, *results], config)
    asyncio.run(RunnableLambda(body).ainvoke({}, {"callbacks": [recorder]}))
    recorder.finish("completed")
    tools = select(session, "tool/call")
    assert [e.data["step"] for e in tools] == [1, 1]
    end = next(e for e in select(session, "step/end") if e.data["step"] == 1)
    assert all(e.seq < end.seq for e in select(session, "tool/result"))
    assert [e.data["step"] for e in select(session, "step/start")] == [1, 2]


def test_unresolved_parent_and_ambiguous_declarations_never_use_latest_model():
    session, recorder, _ = begin()
    life = recorder.lifecycle
    life.model_start("m1", None, [], {"config": {}}, {})
    life.model_end("m1", answer("", [{"id": "a", "name": "search", "args": {"q": "x"}}]))
    life.model_start("m2", None, [], {"config": {}}, {})
    life.model_end("m2", answer("", [{"id": "b", "name": "search", "args": {"q": "x"}}]))
    life.tool_start("tool", None, "search", {"q": "x"}, {})
    event = select(session, "tool/call")[0]
    assert event.data["step"] is None and event.data["parent_resolution"] == "unresolved"
    life.tool_end("tool", "ok")
    recorder.finish("completed")


@pytest.mark.parametrize("partial", [False, True])
def test_failed_attempt_before_or_after_stream_is_retained(partial):
    session, recorder, _ = begin()
    model = FixtureChat(script=[answer("partial text") if partial else RuntimeError("request failed")], partial_error=partial)
    async def run():
        with pytest.raises(RuntimeError):
            async for _ in model.astream([HumanMessage(content="x")], {"callbacks": [recorder]}):
                pass
    asyncio.run(run())
    recorder.finish("workflow_error")
    attempts = select(session, "assistant/attempt")
    assert len(attempts) == 1 and not select(session, "assistant/message")
    assert bool(attempts[0].data["stream"]) == partial
    assert (attempts[0].data["first_token_at"] is not None) == partial
    assert attempts[0].data["error"]["kind"] == "model_error"


def test_runnable_retry_native_tag_preserves_failed_attempt_and_no_extra_turn():
    session, recorder, _ = begin()
    model = FixtureChat(script=[RuntimeError("first attempt"), answer("recovered")])
    retried = model.with_retry(stop_after_attempt=2, wait_exponential_jitter=False)
    asyncio.run(retried.ainvoke([HumanMessage(content="x")], {"callbacks": [recorder]}))
    recorder.finish("completed")
    assert len(select(session, "assistant/attempt")) == len(select(session, "assistant/message")) == 1
    assert len(select(session, "turn/start")) == 1
    assert len(select(session, "retry/started")) == 1
    assert select(session, "retry/started")[0].data["retry_ordinal"] == 2
    assert model.calls == 2


def test_cancel_after_partial_stream_has_durable_attempt_and_cancelled_tool():
    session, recorder, _ = begin()
    life = recorder.lifecycle
    life.model_start("model", None, [], {"config": {}}, {})
    life.model_chunk("model", {"content": "partial"})
    life.tool_start("tool", None, "read", {}, {})
    recorder.finish("cancelled")
    assert select(session, "assistant/attempt")[0].data["status"] == "cancelled"
    assert select(session, "tool/result")[0].data["status"] == "cancelled"
    assert select(session, "turn/end")[0].data["reason"] == "cancelled"
    assert select(session, "session/end")[0].data["status"] == "cancelled"
    assert replay(session.events)["runningCalls"] == []


def test_tool_error_large_result_and_size_marker():
    session, recorder, _ = begin()
    @tool
    async def fails():
        """Fail while reading a resource."""
        raise RuntimeError("lookup failed")
    @tool
    async def large():
        """Return a large read result."""
        return "a" * 100_000
    async def run():
        with pytest.raises(RuntimeError):
            await fails.ainvoke({}, {"callbacks": [recorder]})
        await large.ainvoke({}, {"callbacks": [recorder]})
    asyncio.run(run())
    recorder.finish("completed")
    results = select(session, "tool/result")
    assert results[0].data["is_error"] and results[0].data["error"]["kind"] == "tool_error"
    assert results[1].data["result"]["truncated"]
    assert results[1].data["result"]["original_size"] >= 100_000


def test_10k_chunks_one_durable_settlement_cross_chunk_secret_redaction(tmp_path):
    session, recorder, storage = begin(tmp_path)
    life = recorder.lifecycle
    life.model_start("model", None, [], {"config": {}}, {})
    life.model_chunk("model", {"content": "api_"})
    life.model_chunk("model", {"content": "key=chunk-split-secret "})
    for _ in range(10_000):
        life.model_chunk("model", {"content": "x"})
    life.model_end("model", AIMessage(content="ok"))
    recorder.finish("completed")
    rows = storage.read("test").events
    assert len(rows) < 20
    assert len(select(session, "assistant/message")[0].data["stream"]) == 10_002
    assert "chunk-split-secret" not in storage.path("test").read_text(encoding="utf-8")


def test_stream_limit_is_explicit_and_system_changes_clear_deterministically():
    session, _, _ = begin()
    recorder = TrajectorySessionRecorder(session, stream_limit=20)
    life = recorder.lifecycle
    for i, system in enumerate(("prompt", "prompt", "", "new")):
        life.model_start(str(i), "owner", [SystemMessage(content=system)], {"config": {}}, {})
        life.model_chunk(str(i), {"content": "x" * 30})
        life.model_end(str(i), AIMessage(content="done"))
    recorder.finish("completed")
    systems = select(session, "system/message")
    assert [e.data["content"] for e in systems] == [["prompt"], [""], ["new"]]
    assert all(e.data["stream_truncated"] for e in select(session, "assistant/message"))


def test_interrupted_replay_balances_in_memory_and_does_not_claim_success(tmp_path):
    session, recorder, storage = begin(tmp_path)
    life = recorder.lifecycle
    life.model_start("model", None, [], {"config": {}}, {})
    life.tool_start("tool", None, "lookup", {}, {})
    original = storage.path("test").read_bytes()
    live = replay(storage.read("test").events, active=True)
    cold = replay(storage.read("test").events, active=False)
    assert live["status"] == "running" and live["runningCalls"]
    assert cold["status"] == "interrupted" and cold["runningCalls"] == []
    assert cold["requests"][0]["status"] == "error"
    assert any(n.get("isError") for n in cold["eventNodes"] if n["kind"] == "tool-result")
    assert storage.path("test").read_bytes() == original
    session.writer.close()


def test_snapshot_is_detached_and_future_turn_keeps_session_identity():
    session, recorder, _ = begin()
    life = recorder.lifecycle
    life.model_start("model", None, [], {"config": {}}, {})
    snapshot = session.packet()["snapshot"]
    life.model_end("model", AIMessage(content="done"))
    assert snapshot["requests"][0]["status"] == "running"
    assert snapshot["eventLocations"][-1][1]["step"]["status"] == "open"
    recorder.finish("completed")
    # The storage session supports later turns; the Web currently creates a
    # fresh research conversation per POST rather than resuming business state.
    session.finished = False
    session.start_turn("followup", {})
    assert select(session, "turn/start")[-1].data["turn"] == 2
    assert all(e.session_id == "test" for e in session.events)


def test_fixed_event_log_replays_the_same_frontend_contract():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "trajectory.json").read_text(encoding="utf8"))
    assert replay([SessionEvent.from_dict(e) for e in fixture["events"]]) == fixture["snapshot"]


def test_empty_content_blocks_do_not_fabricate_ttft_and_reasoning_does():
    session, recorder, _ = begin()
    life = recorder.lifecycle
    life.model_start("model", None, [], {"config": {}}, {})
    life.model_chunk("model", {"content": [{"type": "text", "text": ""}]})
    assert life.models["model"].first_token_at is None
    life.model_chunk("model", {"content": "", "reasoning_content": "consider evidence"})
    life.model_end("model", AIMessage(content="done"))
    recorder.finish("completed")
    assert select(session, "assistant/message")[0].data["first_token_at"] is not None
    assert any(b["kind"] == "reasoning" for n in replay(session.events)["eventNodes"] if n["kind"] == "assistant" for b in n["blocks"])


def test_native_provider_tool_ids_can_repeat_in_distinct_requests():
    session, recorder, _ = begin()
    @tool
    async def lookup(query: str):
        """Read a resource."""
        return query
    model = FixtureChat(script=[answer("first", [{"id": "same-id", "name": "lookup", "args": {"query": "one"}}]),
        answer("second", [{"id": "same-id", "name": "lookup", "args": {"query": "two"}}])])
    async def run(_input, config):
        for _ in range(2):
            response = await model.ainvoke([HumanMessage(content="x")], config)
            await lookup.ainvoke({"type": "tool_call", **response.tool_calls[0]}, config)
    asyncio.run(RunnableLambda(run).ainvoke({}, {"callbacks": [recorder]}))
    recorder.finish("completed")
    tools = select(session, "tool/call")
    assert [e.data["step"] for e in tools] == [1, 2]
    assert len({e.data["call_id"] for e in tools}) == 2
    assert [e.data["tool_call_id"] for e in tools] == ["same-id", "same-id"]


def test_real_micro_and_rolling_compaction_events_no_noop_events():
    from langchain_core.messages import ToolMessage

    from open_deep_research.research_graph.compaction import (
        micro_compact_messages,
        rolling_compact,
    )
    from open_deep_research.trajectory.semantics import CURRENT_RECORDER
    session, recorder, _ = begin()
    messages = [HumanMessage(content="assignment"),
        answer("old", [{"id": "old", "name": "lookup", "args": {}}]),
        ToolMessage(content="long old observation", tool_call_id="old"),
        answer("new", [{"id": "new", "name": "lookup", "args": {}}]),
        ToolMessage(content="new observation", tool_call_id="new")]
    token = CURRENT_RECORDER.set(recorder)
    try:
        micro_compact_messages(messages, {}, recent_raw_steps=1)
        assert not select(session, "compaction/start")
        result = micro_compact_messages(messages, {"old": "[persisted receipt]"}, recent_raw_steps=1)
        assert result.messages[2].content == "[persisted receipt]"
        model = FixtureChat(script=[answer("", [{"id": "summary", "name": "RollingCompactOutput",
            "args": {"rolling_summary": "Verified old observations"}}])])
        # Use an async function so the official inherited callback configuration
        # reaches the real summarization model.
        async def body(_input, config):
            return await rolling_compact(messages, previous_summary="", protected_context="", model=model,
                                         model_name="fixture-chat", max_retries=1, recent_raw_steps=1)
        summary = asyncio.run(RunnableLambda(body).ainvoke({}, {"callbacks": [recorder]}))
        assert summary.rolling_summary == "Verified old observations"
    finally:
        CURRENT_RECORDER.reset(token)
    recorder.finish("completed")
    assert [e.data["kind"] for e in select(session, "compaction/start")] == ["micro", "rolling"]
    assert len(select(session, "compaction/summary")) == len(select(session, "compaction/end")) == 2
    assert len(select(session, "assistant/message")) == 1
    assert select(session, "assistant/message")[0].data["tool_calls"][0]["executable"] is False
    assert not select(session, "step/end")[0].data["unresolved_calls"]
    assert replay(session.events)["runningCalls"] == []
