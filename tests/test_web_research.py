"""Web admission accepts business input and applies the per-Run RAG switch."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from open_deep_research.config_file import frozen_config, load_configuration
from open_deep_research.configuration import Configuration, RetrievalMode
from open_deep_research.runtime import business_agent
from open_deep_research.tools.registry import get_retrieval_tools
from open_deep_research.web import runs, server


class RecordingStore:
    """Capture the frozen admission without a database."""

    def __init__(self):
        self.created = []
        events = [
            {"seq": 1, "type": "report", "content": "Fixture report"},
            {"seq": 2, "type": "usage", "model_calls": 1},
            {"seq": 3, "type": "done"},
        ]
        self.events = events

    def create(self, owner, key, data, config, initial_state, **kwargs):
        self.created.append((owner, key, data, config, initial_state, kwargs))
        return self.get("run", owner), True

    def get(self, run_id, owner):
        return dict(
            run_id=run_id,
            status="completed",
            settlement="complete",
            events_complete=True,
            seq=3,
        )

    def read_events(self, run_id, owner, after, limit=100):
        return [e for e in self.events if e["seq"] > after][:limit]

    def stream_finished(self, run_id, owner, after):
        assert run_id == "run" and owner == "operator"
        return after == self.events[-1]["seq"]


def _post_run(monkeypatch, body, *, key="rag-test"):
    store = RecordingStore()
    monkeypatch.setattr(server, "WEB_API_TOKEN", "rag-test-token")
    monkeypatch.setattr(runs, "_store", lambda: store)
    with TestClient(server.app) as client:
        response = client.post(
            "/api/runs",
            json=body,
            headers={
                "Authorization": "Bearer rag-test-token",
                "Idempotency-Key": key,
            },
        )
    return store, response


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"topic": "Analyze a customer-service incident"}, False),
        ({"topic": "Analyze a customer-service incident", "rag_enabled": False}, False),
        ({"topic": "Analyze a customer-service incident", "rag_enabled": True}, True),
    ],
)
def test_topic_only_request_creates_a_run_with_the_requested_rag_switch(
    monkeypatch, body, expected
):
    """A topic-only body is valid and RAG defaults to off for this Run."""
    store, response = _post_run(monkeypatch, body)
    assert response.status_code == 202
    assert len(store.created) == 1
    _owner, _key, data, config, state, _kwargs = store.created[0]
    assert state["messages"][0].content == body["topic"]
    assert data["rag_enabled"] is expected
    frozen = Configuration.from_runnable_config(config)
    assert frozen.rag_enabled is expected
    assert frozen.retrieval_mode is (
        RetrievalMode.HYBRID if expected else RetrievalMode.WEB_ONLY
    )
    # 仅 RAG 可用的 internal_knowledge 角色必须随开关一起退出本次 Run。
    assert ("internal_knowledge" in frozen.enabled_business_agents) is expected
    assert set(frozen.enabled_business_agents) == {
        "public_signal",
        "risk_assessment",
        "response_strategy",
    } | ({"internal_knowledge"} if expected else set())
    # 服务端 config.yaml 打开 RAG 时，Web 默认仍必须关闭本次 Run 的 RAG。
    assert load_configuration().rag_enabled is True


def test_explicit_false_overrides_an_enabled_server_configuration(monkeypatch):
    """The Run switch wins over config.yaml instead of being ignored."""
    server_config = load_configuration()
    assert server_config.rag_enabled is True
    store, _response = _post_run(monkeypatch, {"topic": "brand", "rag_enabled": False})
    frozen = Configuration.from_runnable_config(store.created[0][3])
    assert frozen.rag_enabled is False
    assert frozen.retrieval_mode is RetrievalMode.WEB_ONLY
    # 共享的全局配置对象不被修改。
    assert load_configuration().rag_enabled is True


@pytest.mark.parametrize("value", ["true", "false", 1])
def test_rag_switch_rejects_non_boolean_values(monkeypatch, value):
    """Only a real boolean may select the RAG switch."""
    store, response = _post_run(monkeypatch, {"topic": "brand", "rag_enabled": value})
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid research request."
    assert store.created == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "openai:gpt-4.1"),
        ("mode", "deep"),
        ("search_api", "openai"),
        ("require_approval", True),
        ("allow_clarification", False),
    ],
)
def test_removed_technical_settings_are_rejected_instead_of_ignored(
    monkeypatch, field, value
):
    """Removed user-level settings must not be silently accepted."""
    store, response = _post_run(monkeypatch, {"topic": "brand", field: value})
    assert response.status_code == 400
    assert response.json()["detail"] == "Unsupported research parameter."
    assert store.created == []


@pytest.mark.parametrize("rag_enabled", [False, True])
def test_retrieval_tools_follow_the_run_rag_switch(monkeypatch, rag_enabled):
    """RAG changes tool assembly without removing normal web search."""
    from open_deep_research.config_file import run_configuration

    configuration = run_configuration(load_configuration(), rag_enabled=rag_enabled)
    config = {"configurable": frozen_config(configuration)["configurable"]}
    names = {tool.name for tool in asyncio.run(get_retrieval_tools(config))}
    assert names == ({"web_search", "rag_search"} if rag_enabled else {"web_search"})


def test_research_stream_reuses_the_durable_create(monkeypatch):
    """The legacy /api/research entry point keeps its streaming reply."""
    store = RecordingStore()
    monkeypatch.setattr(server, "WEB_API_TOKEN", "rag-test-token")
    monkeypatch.setattr(runs, "_store", lambda: store)
    with TestClient(server.app) as client:
        response = client.post(
            "/api/research",
            json={"topic": "Analyze a customer-service incident", "rag_enabled": True},
            headers={
                "Authorization": "Bearer rag-test-token",
                "Idempotency-Key": "rag-test",
            },
        )
    assert response.status_code == 200
    delivered = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert delivered == store.events
    assert response.headers["x-run-id"] == "run"
    assert store.created[0][2]["rag_enabled"] is True


def test_disabled_role_still_returns_no_patch(monkeypatch):
    from langchain_core.messages import HumanMessage

    async def unexpected_tools(*args):
        raise AssertionError("disabled internal agent must not initialize tools")

    monkeypatch.setattr(business_agent, "_business_agent_tools", unexpected_tools)
    state = {"messages": [HumanMessage(content="brand")]}
    config = {
        "configurable": {
            "enabled_business_agents": [
                "public_signal",
                "risk_assessment",
                "response_strategy",
            ]
        }
    }
    assert (
        asyncio.run(
            business_agent.run_business_agent(
                role="internal_knowledge",
                state=state,
                config=config,
            )
        )
        == {}
    )
