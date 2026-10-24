"""Default Web admission preserves RAG selection and the durable SSE protocol."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from open_deep_research.configuration import Configuration
from open_deep_research.runtime import business_agent
from open_deep_research.web import runs, server


@pytest.mark.parametrize("rag_enabled", [False, True])
def test_research_stream_respects_rag_selection(monkeypatch, rag_enabled):
    monkeypatch.delenv("RAG_ENABLED", raising=False)
    monkeypatch.delenv("ENABLED_BUSINESS_AGENTS", raising=False)
    monkeypatch.setattr(server, "WEB_API_TOKEN", "rag-test-token")

    async def unexpected_tools(*args):
        raise AssertionError("disabled internal agent must not initialize tools")

    monkeypatch.setattr(business_agent, "_business_agent_tools", unexpected_tools)
    events = [
        {"seq": 1, "type": "report", "content": "Fixture report"},
        {"seq": 2, "type": "usage", "model_calls": 1},
        {"seq": 3, "type": "done"},
    ]

    class Store:
        def create(self, owner, key, data, config, initial_state, **kwargs):
            cfg = Configuration.from_runnable_config(config)
            assert cfg.rag_enabled is rag_enabled
            assert {"public_signal", "risk_assessment", "response_strategy"}.issubset(
                cfg.enabled_business_agents
            )
            if rag_enabled:
                assert "internal_knowledge" in cfg.enabled_business_agents
            else:
                assert (
                    asyncio.run(
                        business_agent.run_business_agent(
                            role="internal_knowledge",
                            state=initial_state,
                            config=config,
                        )
                    )
                    == {}
                )
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
