"""Web admission is a legacy secondary entry point over the frozen config."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from open_deep_research.config_file import load_configuration
from open_deep_research.configuration import Configuration
from open_deep_research.runtime import business_agent
from open_deep_research.web import runs, server


@pytest.mark.parametrize("rag_enabled", [False, True])
def test_research_stream_ignores_legacy_body_settings(monkeypatch, rag_enabled):
    monkeypatch.setattr(server, "WEB_API_TOKEN", "rag-test-token")
    effective = load_configuration()
    events = [
        {"seq": 1, "type": "report", "content": "Fixture report"},
        {"seq": 2, "type": "usage", "model_calls": 1},
        {"seq": 3, "type": "done"},
    ]

    class Store:
        def create(self, owner, key, data, config, initial_state, **kwargs):
            cfg = Configuration.from_runnable_config(config)
            assert cfg.rag_enabled is effective.rag_enabled
            assert cfg.enabled_business_agents == effective.enabled_business_agents
            assert cfg.research_model == effective.research_model
            assert data["rag_enabled"] is effective.rag_enabled
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
            return [e for e in events if e["seq"] > after][:limit]

        def stream_finished(self, run_id, owner, after):
            assert run_id == "run" and owner == "operator"
            return after == events[-1]["seq"]

    monkeypatch.setattr(runs, "_store", Store)
    with TestClient(server.app) as client:
        response = client.post(
            "/api/research",
            json={
                "topic": "Analyze a customer-service incident",
                "rag_enabled": rag_enabled,
            },
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
    assert delivered == events
    assert response.headers["x-run-id"] == "run"


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
