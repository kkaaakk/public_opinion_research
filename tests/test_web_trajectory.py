"""Local presentation stream tests; LangSmith and budget remain independent."""

import asyncio
import json

from starlette.requests import Request

from open_deep_research.web.trajectory import TrajectoryProjector, sanitize


def source(event, name, run_id, *, parents=(), data=None, metadata=None):
    return {
        "event": event, "name": name, "run_id": run_id,
        "parent_ids": list(parents), "data": data or {}, "metadata": metadata or {},
    }


def test_event_mapping_nested_tool_model_usage_and_timing(monkeypatch):
    clock = iter([1.0, 1.05, 1.2, 1.3, 1.4, 1.5, 1.6])
    monkeypatch.setattr("open_deep_research.web.trajectory.time.time", lambda: next(clock))
    project = TrajectoryProjector("thread")
    model = project.project(source("on_chat_model_start", "chat", "llm", data={"input": "prompt"}))
    token = project.project(source("on_chat_model_stream", "chat", "llm", data={"chunk": "a"}))
    end = project.project(source("on_chat_model_end", "chat", "llm", data={
        "output": {"usage_metadata": {"input_tokens": 4, "output_tokens": 2}}
    }))
    tool = project.project(source("on_tool_start", "web_search", "tool", parents=["llm"],
        data={"input": {"query": "battery"}}))
    nested = project.project(source("on_tool_start", "lookup", "child", parents=["llm", "tool"],
        data={"input": {"id": 1}}))
    failed = project.project(source("on_tool_error", "lookup", "child", parents=["llm", "tool"],
        data={"error": RuntimeError("lookup failed")}))
    complete = project.project(source("on_tool_end", "web_search", "tool", parents=["llm"],
        data={"output": "results"}))
    assert [item["seq"] for item in (model, token, end, tool, nested, failed, complete)] == list(range(1, 8))
    assert token["ttft_ms"] == 50
    assert end["duration_ms"] == 200
    assert end["usage"]["input_tokens"] == 4
    assert nested["parent_ids"] == ["llm", "tool"]
    assert failed["status"] == "error" and failed["duration_ms"] == 100
    assert complete["duration_ms"] == 300


def test_retry_is_only_emitted_from_a_real_runtime_event():
    project = TrajectoryProjector("thread")
    assert project.project(source("on_retry", "chat", "retry", data={"attempt": 2}))["kind"] == "retry"
    assert project.project(source("on_chain_stream", "graph", "root")) is None


def test_redaction_recurses_and_keeps_useful_arguments():
    value = {"query": "brand safety", "Authorization": "Bearer abc123",
        "nested": {"db_password": "pw", "input": "api_key=abc123 search term",
            "url": "postgresql://user:pw@db/internal"}}
    clean = sanitize(value)
    assert clean["query"] == "brand safety"
    assert clean["Authorization"] == "[REDACTED]"
    assert clean["nested"]["db_password"] == "[REDACTED]"
    assert "abc123" not in json.dumps(clean)
    assert "pw@db" not in json.dumps(clean)


def test_sse_trajectory_report_usage_and_done_coexist(monkeypatch):
    from open_deep_research.web import server

    class FakeGraph:
        async def astream_events(self, *_args, **_kwargs):
            yield source("on_tool_start", "web_search", "tool", data={"input": {"query": "x"}})
            yield source("on_tool_end", "web_search", "tool", data={"output": "result"})
            yield source("on_chain_stream", "research", "root", data={"chunk": {
                "compile_final_report": {"report": {"final": "# Report"},
                    "runtime": {"budget": {"model_calls": 1, "input_tokens": 2,
                        "output_tokens": 3, "total_tokens": 5}}}}})

    monkeypatch.setattr(server, "WEB_API_TOKEN", "")
    monkeypatch.setattr(server, "_deep_researcher_factory", lambda _config: FakeGraph())

    async def run(enabled=True):
        response = await server.research(server.ResearchRequest(topic="battery", trajectory_enabled=enabled),
            Request({"type": "http", "method": "POST", "path": "/api/research", "headers": []}))
        return [json.loads(chunk.removeprefix("data: ")) async for chunk in response.body_iterator]

    events = asyncio.run(run())
    types = [event["type"] for event in events]
    assert types.count("trajectory") >= 4
    assert [event["event"]["kind"] for event in events if event["type"] == "trajectory"][:2] == ["user", "context"]
    assert "report" in types and "usage" in types and types[-1] == "done"
    assert next(event for event in events if event["type"] == "usage")["total_tokens"] == 5
    disabled = asyncio.run(run(False))
    assert all(event["type"] != "trajectory" for event in disabled)
    assert any(event["type"] == "report" for event in disabled)


def test_projection_failure_isolated_from_report(monkeypatch):
    from open_deep_research.web import server

    class FakeGraph:
        async def astream_events(self, *_args, **_kwargs):
            yield source("on_chain_stream", "research", "root", data={"chunk": {
                "compile_final_report": {"report": {"final": "OK"}}}})

    def broken(self, _source):
        raise ValueError("projection error")

    monkeypatch.setattr(server, "WEB_API_TOKEN", "")
    monkeypatch.setattr(server, "_deep_researcher_factory", lambda _config: FakeGraph())
    monkeypatch.setattr(TrajectoryProjector, "project", broken)

    async def run():
        response = await server.research(server.ResearchRequest(topic="battery"),
            Request({"type": "http", "method": "POST", "path": "/api/research", "headers": []}))
        return "".join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(run())
    assert '"type": "report"' in body and '"type": "done"' in body
