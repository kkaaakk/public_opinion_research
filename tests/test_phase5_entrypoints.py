"""Default admission, migration rejection and CLI contracts without inference."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from open_deep_research.configuration import Configuration
from open_deep_research.web import cli, runs, server


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"workflow_backend": "python", "business_agent_runtime": "openai_agents"},
        {"workflow_backend": "dbos"},
    ],
)
def test_selectors_are_input_aliases_and_never_persisted_switches(values):
    cfg = Configuration(**values)
    request = server.ResearchRequest(topic="brand", **values)
    for data in (cfg.model_dump(), request.model_dump()):
        assert "workflow_backend" not in data
        assert "business_agent_runtime" not in data


@pytest.mark.parametrize(
    "name,value",
    [
        ("workflow_backend", "langgraph"),
        ("workflow_backend", "unknown"),
        ("business_agent_runtime", "legacy"),
    ],
)
def test_retired_engine_rejected_in_config_request_and_environment(
    monkeypatch, name, value
):
    with pytest.raises(ValueError, match=name):
        Configuration(**{name: value})
    with pytest.raises(ValueError, match=name):
        server.ResearchRequest(topic="brand", **{name: value})
    monkeypatch.setenv(name.upper(), value)
    with pytest.raises(ValueError, match=name):
        Configuration.from_runnable_config({"metadata": {"accepted_run": True}})


@pytest.mark.parametrize("path", ["/api/runs", "/api/research"])
def test_default_admission_uses_durable_client(monkeypatch, path):
    received = []
    monkeypatch.setattr(server, "WEB_API_TOKEN", "phase5-test-token")

    class Store:
        def create(self, owner, key, data, config, state, **kw):
            received.append((owner, key, data, config, state))
            return {"run_id": "r", "status": "queued"}, True

    monkeypatch.setattr(runs, "_store", Store)
    monkeypatch.setattr(runs, "subscription", lambda *a: {"run_id": "r"})
    with TestClient(server.app) as client:
        response = client.post(
            path,
            json={"topic": "brand"},
            headers={
                "Authorization": "Bearer phase5-test-token",
                "Idempotency-Key": "same-request",
            },
        )
    assert response.status_code == (202 if path == "/api/runs" else 200)
    assert len(received) == 1
    owner, key, data, config, state = received[0]
    assert (owner, key) == ("operator", "same-request")
    assert data["topic"] == "brand"
    assert state["messages"][0].content == "brand"
    assert "workflow_backend" not in config["configurable"]
    assert "business_agent_runtime" not in config["configurable"]


@pytest.mark.parametrize("path", ["/api/runs", "/api/research"])
def test_default_admission_requires_operator_and_request_identity(monkeypatch, path):
    monkeypatch.setattr(server, "WEB_API_TOKEN", "phase5-test-token")
    monkeypatch.setattr(
        runs,
        "_store",
        lambda: (_ for _ in ()).throw(AssertionError("must reject before storage")),
    )
    with TestClient(server.app) as client:
        assert client.post(path, json={"topic": "brand"}).status_code == 401
        response = client.post(
            path,
            json={"topic": "brand"},
            headers={"Authorization": "Bearer phase5-test-token"},
        )
        assert response.status_code == 400


def test_cli_sends_default_run_and_stable_key(monkeypatch, capsys):
    received = []
    original = httpx.Client

    def respond(request):
        received.append(request)
        return httpx.Response(202, json={"run_id": "same-run"})

    monkeypatch.setenv("PUBLIC_OPINION_API_TOKEN", "cli-test-token")
    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )
    for _ in range(2):
        assert cli.main(["create", "brand", "--key", "stable-key"]) == 0
    assert [r.url.path for r in received] == ["/api/runs", "/api/runs"]
    assert received[0].content == received[1].content
    assert received[0].headers["Idempotency-Key"] == "stable-key"
    assert "workflow_backend" not in json.loads(received[0].content)
    assert "cli-test-token" not in capsys.readouterr().out


@pytest.mark.parametrize("terminal", [None, "completed"])
def test_stress_client_uses_native_identity_and_requires_terminal_event(
    monkeypatch, terminal
):
    from contextlib import contextmanager
    from types import SimpleNamespace

    import agent_stress

    sent = []
    frames = [
        {"type": "step_started", "step": "compile_final_report"},
        {"type": "report", "content": "fixture report"},
        {"type": "usage", "total_tokens": 12, "model_calls": 1},
    ]
    if terminal:
        frames.append({"type": "done", "status": terminal})

    @contextmanager
    def stream(*args, **kwargs):
        sent.append(kwargs)
        yield SimpleNamespace(
            status_code=200,
            iter_lines=lambda: ["data: " + json.dumps(e) for e in frames],
        )

    monkeypatch.setenv("PUBLIC_OPINION_API_TOKEN", "stress-test-token")
    monkeypatch.setattr(agent_stress.httpx, "stream", stream)
    result = agent_stress.AgentStressTester().run_one("public_opinion_risk", "fast")
    assert result.success is (terminal == "completed")
    assert result.nodes_visited == ["compile_final_report"]
    assert result.total_tokens == 12
    assert "scenario" not in sent[0]["json"]
    assert sent[0]["headers"]["Authorization"] == "Bearer stress-test-token"
    assert len(sent[0]["headers"]["Idempotency-Key"]) == 32


def test_mcp_oauth_exchange_does_not_depend_on_engine_store_or_share_tokens(
    monkeypatch,
):
    import asyncio

    from open_deep_research.mcp import tools

    calls = []

    async def exchange(token, url):
        calls.append((token, url))
        return {"access_token": "exchanged-" + token}

    monkeypatch.setattr(tools, "get_mcp_access_token", exchange)

    async def run():
        for owner in ("alice", "bob", "alice"):
            result = await tools.fetch_tokens(
                {
                    "metadata": {"owner": owner},
                    "configurable": {
                        "x-supabase-access-token": owner,
                        "mcp_config": {"url": "https://mcp.example.test"},
                    },
                }
            )
            assert result == {"access_token": "exchanged-" + owner}

    asyncio.run(run())
    assert calls == [
        (owner, "https://mcp.example.test") for owner in ("alice", "bob", "alice")
    ]


def test_authenticated_mcp_is_not_connected_when_exchange_fails(monkeypatch):
    import asyncio

    from open_deep_research.mcp import tools

    async def rejected(*args):
        return None

    monkeypatch.setattr(tools, "get_mcp_access_token", rejected)
    config = {
        "configurable": {
            "mcp_config": {
                "url": "https://mcp.example.test",
                "tools": ["lookup"],
                "auth_required": True,
            },
            "x-supabase-access-token": "denied-fixture-token",
        }
    }
    result = asyncio.run(
        tools._build_legacy_config(Configuration.from_runnable_config(config), config)
    )
    assert result is None


@pytest.mark.parametrize("path", ["/api/runs", "/api/research"])
def test_retired_operator_environment_rejects_before_storage(monkeypatch, path):
    monkeypatch.setattr(server, "WEB_API_TOKEN", "migration-test-token")
    monkeypatch.setenv("BUSINESS_AGENT_RUNTIME", "legacy")
    monkeypatch.setattr(
        runs,
        "_store",
        lambda: (_ for _ in ()).throw(
            AssertionError("old settings must fail before storage")
        ),
    )
    with TestClient(server.app) as client:
        response = client.post(
            path,
            json={"topic": "brand"},
            headers={
                "Authorization": "Bearer migration-test-token",
                "Idempotency-Key": "migration-test",
            },
        )
    assert response.status_code == 503
    assert (
        response.json()["detail"]
        == "Research configuration is unavailable; migrate retired runtime settings to DBOS/SDK."
    )
