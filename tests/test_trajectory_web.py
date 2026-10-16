"""One-execution SSE/replay/cancel behavior and tracing/persistence failure isolation."""

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import HumanMessage
from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client, tracing_context
from starlette.requests import Request
from trajectory_fixture import FixtureChat, answer, build_graph

from open_deep_research.trajectory import TrajectorySession, TrajectorySessionRecorder
from open_deep_research.trajectory.persistence import JsonlWriter


def raw_request():
    return Request({"type": "http", "method": "POST", "path": "/api/research", "headers": []})


@pytest.fixture
def web(monkeypatch, tmp_path):
    from open_deep_research.web import server
    monkeypatch.setenv("TRAJECTORY_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("TRAJECTORY_PERSISTENCE_ENABLED", "true")
    monkeypatch.setattr(server, "WEB_API_TOKEN", "")
    monkeypatch.setattr(server, "_trajectory_service", None)
    monkeypatch.setattr(server, "_executions", {})
    monkeypatch.setattr(server, "_RESEARCH_SEMAPHORE", asyncio.Semaphore(2))
    return server


def test_sse_ledger_report_budget_and_done_share_one_real_graph(web, monkeypatch):
    graph, counts, models = build_graph()
    factory_calls = []
    def factory(config):
        factory_calls.append(config)
        return graph
    monkeypatch.setattr(web, "_deep_researcher_factory", factory)
    async def run():
        response = await web.research(web.ResearchRequest(topic="battery"), raw_request())
        packets = [json.loads(chunk.removeprefix("data: ")) async for chunk in response.body_iterator]
        session_id = response.headers["X-Research-Session-Id"]
        history = await web.trajectory_session(session_id, raw_request())
        return packets, history, session_id
    packets, history, session_id = asyncio.run(run())
    assert len(factory_calls) == 1 and counts["report"] == 1
    assert sum(m.calls for m in models.values()) == 6
    assert history["snapshot"]["status"] == "completed"
    assert [p["type"] for p in packets][-3:] == ["report", "usage", "done"]
    assert next(p for p in packets if p["type"] == "usage")["total_tokens"] == 90
    assert history["snapshot"]["budgetUsage"]["total_tokens"] == 90
    rows = web.trajectory_service().storage.read(session_id).events
    correlation = rows[0].data["correlation"]
    assert correlation["thread_id"] == correlation["research_run_id"] == session_id
    assert correlation["logical_run_id"] == factory_calls[0]["metadata"]["logical_run_id"]
    assert next(p for p in reversed(packets) if p["type"] == "trajectory")["snapshot"] == history["snapshot"]


def test_disk_history_pages_replay_after_service_restart_and_auth(web, monkeypatch):
    graph, _, _ = build_graph(rounds=12)
    monkeypatch.setattr(web, "_deep_researcher_factory", lambda _config: graph)
    async def run():
        response = await web.research(web.ResearchRequest(topic="battery"), raw_request())
        async for _ in response.body_iterator:
            pass
        session_id = response.headers["X-Research-Session-Id"]
        # A new service has no memory of the execution. Only disk supplies replay.
        monkeypatch.setattr(web, "_trajectory_service", None)
        async with AsyncClient(transport=ASGITransport(app=web.app), base_url="http://test") as client:
            latest = (await client.get(f"/api/trajectory/sessions/{session_id}/events?limit=10")).json()
            assert latest["has_more"] and not latest["writer_active"]
            older = (await client.get(f"/api/trajectory/sessions/{session_id}/events?before_seq={latest['before_seq']}&limit=10")).json()
            assert older["events"][-1]["seq"] < latest["events"][0]["seq"]
            assert len(older["snapshot"]["eventNodes"]) >= len(latest["snapshot"]["eventNodes"])
            assert older["latest_seq"] == latest["latest_seq"]
            assert (await client.get("/api/trajectory/sessions/missing")).status_code == 404
            assert (await client.get(f"/api/trajectory/sessions/{session_id}/events?limit=501")).status_code == 400
            monkeypatch.setattr(web, "WEB_API_TOKEN", "unit-token")
            assert (await client.get(f"/api/trajectory/sessions/{session_id}")).status_code == 401
            assert (await client.get(f"/api/trajectory/sessions/{session_id}", headers={"Authorization": "Bearer unit-token"})).status_code == 200
    asyncio.run(run())


def test_sse_disconnect_does_not_cancel_or_repeat_execution(web, monkeypatch):
    graph, counts, models = build_graph(delay=0.005)
    monkeypatch.setattr(web, "_deep_researcher_factory", lambda _config: graph)
    async def run():
        response = await web.research(web.ResearchRequest(topic="battery"), raw_request())
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
        session_id = response.headers["X-Research-Session-Id"]
        execution = web._executions[session_id]
        await execution.task
        history = await web.trajectory_session(session_id, raw_request())
        reconnect = await web.trajectory_stream(session_id, raw_request())
        assert "report" in "".join([s async for s in reconnect.body_iterator])
        return history
    history = asyncio.run(run())
    assert history["snapshot"]["status"] == "completed" and counts["report"] == 1
    assert sum(m.calls for m in models.values()) == 6


def test_stop_is_backend_settlement_after_partial_stream(web, monkeypatch):
    graph, _, _ = build_graph(delay=0.03)
    monkeypatch.setattr(web, "_deep_researcher_factory", lambda _config: graph)
    async def run():
        response = await web.research(web.ResearchRequest(topic="battery"), raw_request())
        async for chunk in response.body_iterator:
            packet = json.loads(chunk.removeprefix("data: "))
            if (packet.get("snapshot", {}).get("partial") or {}).get("blocks"):
                break
        session_id = response.headers["X-Research-Session-Id"]
        result = await web.cancel_research(session_id, raw_request())
        assert result["status"] == "cancelled"
        await response.body_iterator.aclose()
        return web.trajectory_service().storage.read(session_id).events
    events = asyncio.run(run())
    assert events[-2].type == "turn/end" and events[-2].data["reason"] == "cancelled"
    assert events[-1].type == "session/end" and events[-1].data["status"] == "cancelled"
    attempts = [e for e in events if e.type == "assistant/attempt"]
    assert attempts and any(e.data["stream"] for e in attempts)
    assert all(e.data["status"] == "cancelled" for e in attempts)


@pytest.mark.parametrize("failure", ["open", "append", "serialization"])
def test_trajectory_failure_degrades_but_never_repeats_business(web, monkeypatch, failure):
    graph, counts, models = build_graph()
    monkeypatch.setattr(web, "_deep_researcher_factory", lambda _config: graph)
    def fail(*_args, **_kwargs):
        raise RuntimeError("persistence or serialization failed")
    if failure == "open":
        monkeypatch.setattr(web.trajectory_service().storage, "create", fail)
    elif failure == "append":
        monkeypatch.setattr(JsonlWriter, "append", fail)
    else:
        import open_deep_research.trajectory.session as module
        original = module.sanitize
        def sanitize(value):
            if isinstance(value, dict) and "attempt_id" in value:
                return original({**value, "unserializable": object()})
            return original(value)
        monkeypatch.setattr(module, "sanitize", sanitize)
    async def run():
        response = await web.research(web.ResearchRequest(topic="battery"), raw_request())
        result = "".join([chunk async for chunk in response.body_iterator])
        return result, web.trajectory_service().sessions[response.headers["X-Research-Session-Id"]]
    body, session = asyncio.run(run())
    assert session.degraded and '"type": "report"' in body and '"type": "done"' in body
    assert counts["report"] == 1 and sum(m.calls for m in models.values()) == 6


def test_history_failure_has_no_workflow_side_effect(web, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(web.trajectory_service(), "history", fail)
    async def run():
        async with AsyncClient(transport=ASGITransport(app=web.app), base_url="http://test") as client:
            response = await client.get("/api/trajectory/sessions/test")
            assert response.status_code == 503
    asyncio.run(run())
    assert not web._executions


def test_official_langsmith_tracer_and_ledger_share_exact_model_tool_invocations(monkeypatch):
    created, updated = [], []
    # The real official LangSmith tracer is enabled; only its HTTP transport is
    # replaced. No fixture trace is uploaded and no custom span is added.
    monkeypatch.setattr(Client, "create_run", lambda self, **kw: created.append(kw))
    monkeypatch.setattr(Client, "update_run", lambda self, **kw: updated.append(kw))
    client = Client(api_url="http://127.0.0.1:9", api_key="unit-test", auto_batch_tracing=False)
    tracer = LangChainTracer(client=client, project_name="ledger-coexistence")
    graph, counts, models = build_graph()
    session = TrajectorySession("coexistence")
    session.start_turn("battery safety", {})
    recorder = TrajectorySessionRecorder(session)
    async def run():
        with tracing_context(enabled=True, client=client):
            async for _ in graph.astream_events({"runtime": {"budget": {}}}, {"callbacks": [tracer, recorder]}, version="v2"):
                pass
    asyncio.run(run())
    recorder.finish("completed")
    model_runs = [r for r in created if r["run_type"] == "llm"]
    tool_runs = [r for r in created if r["run_type"] == "tool"]
    assert len(model_runs) == 6 == sum(m.calls for m in models.values())
    assert len(tool_runs) == 3 == sum(counts[n] for n in ("web_search", "rag_search", "lookup_document"))
    assert {str(r["id"]) for r in model_runs} == {e.run_id for e in session.events if e.type == "assistant/message"}
    assert {str(r["id"]) for r in tool_runs} == {e.run_id for e in session.events if e.type == "tool/call"}
    assert counts["report"] == 1 and updated


def test_sync_worker_callbacks_publish_to_the_original_async_sse_loop():
    session = TrajectorySession("worker")
    session.start_turn("sync RAG worker", {})
    recorder = TrajectorySessionRecorder(session)
    models = [FixtureChat(script=[answer(f"answer {i}")]) for i in range(8)]
    async def run():
        stream = session.bus.listen(session.packet())
        await anext(stream)
        async def collect():
            packets = []
            async for packet in stream:
                packets.append(packet)
            return packets
        consumer = asyncio.create_task(collect())
        await asyncio.gather(*[asyncio.to_thread(model.invoke, [HumanMessage(content="x")],
                                                 {"callbacks": [recorder]}) for model in models])
        recorder.finish("completed")
        session.bus.close()
        return await asyncio.wait_for(consumer, timeout=3)
    packets = asyncio.run(run())
    assert packets[-1]["type"] == "done" and not session.degraded
    assert sum(e.type == "assistant/message" for e in session.events) == 8
    assert [e.seq for e in session.events] == list(range(len(session.events)))
    assert len({e.data["step"] for e in session.events if e.type == "step/start"}) == 8
