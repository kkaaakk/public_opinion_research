"""Studio egress is read-only, owner scoped and contains only admitted metadata."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from open_deep_research.web import observations, runs, server
from open_deep_research.web.run_data import RunNotFound, sdk_observation

RUN = "a" * 32


@pytest.fixture
def request_lookup(monkeypatch):
    calls = []

    def read_request(run_id, owner):
        calls.append((run_id, owner))
        if run_id != RUN or owner != "operator":
            raise RunNotFound()
        return {"data": {"config": {}}}

    monkeypatch.setattr(observations, "read_request", read_request)
    return calls


def test_sdk_observation_is_bounded_and_excludes_arbitrary_content():
    result = sdk_observation(
        {
            "role": "public_signal",
            "model_turn": 1,
            "duration_ms": 2.3,
            "prompt": "private",
            "arguments": {"api_key": "private"},
            "output": "private",
            "error": "private",
            "usage": {"total_tokens": 4, "secret": 12},
            "context_chars": float("inf"),
            "model_calls": True,
            "name": "n" * 500,
        }
    )
    assert result == {
        "role": "public_signal",
        "model_turn": 1,
        "duration_ms": 2.3,
        "usage": {"total_tokens": 4},
        "name": "n" * 160,
    }


def test_native_view_uses_public_read_apis_and_omits_payloads(request_lookup):
    calls = []

    class Client:
        def list_workflows(self, **kwargs):
            calls.append(kwargs)
            assert kwargs["load_input"] is kwargs["load_output"] is False
            return [
                SimpleNamespace(
                    workflow_id=RUN,
                    name="por.run",
                    status="ERROR",
                    input="private",
                    output="private",
                    error="private",
                )
            ]

        def list_workflow_steps(self, workflow_id, **kwargs):
            assert workflow_id == RUN and kwargs["load_output"] is False
            return [
                {
                    "function_id": 1,
                    "function_name": "load_request",
                    "error": "private",
                    "output": "private",
                }
            ]

    store = SimpleNamespace(client=Client())
    view = observations.execution_view(store, RUN, "operator", "", 0, 20)
    assert view["workflows"][0]["status"] == "ERROR"
    assert "private" not in json.dumps(view)
    assert view["steps"][0]["function_id"] == 1
    assert view["steps"][0]["function_name"] == "load_request"
    with pytest.raises(RunNotFound):
        observations.execution_view(store, RUN, "other", "", 0, 20)
    with pytest.raises(RunNotFound):
        observations.execution_view(store, RUN, "operator", "b" * 32, 0, 20)
    with pytest.raises(RunNotFound):
        observations.execution_view(store, "b" * 32, "operator", "", 0, 20)
    assert len(calls) == 2
    assert request_lookup == [
        (RUN, "operator"),
        (RUN, "other"),
        (RUN, "operator"),
        ("b" * 32, "operator"),
    ]
@pytest.mark.parametrize("path", ["event-page", "execution"])
def test_all_studio_reads_require_the_existing_token(monkeypatch, path):
    monkeypatch.setattr(server, "WEB_API_TOKEN", "test-studio-token")
    monkeypatch.setattr(runs, "_store", lambda: pytest.fail("must authenticate first"))
    response = TestClient(server.app).get(f"/api/runs/{RUN}/{path}")
    assert response.status_code == 401


def test_event_page_is_bounded_continuous_and_never_claims_work(monkeypatch):
    class Store:
        def read_events(self, run_id, owner, after, limit):
            assert run_id == RUN and owner == "operator"
            return [
                {"seq": i, "type": "status"}
                for i in range(after + 1, min(6, after + limit + 1))
            ]

    monkeypatch.setattr(server, "WEB_API_TOKEN", "test-studio-token")
    monkeypatch.setattr(runs, "_store", lambda: Store())
    client = TestClient(server.app)
    headers = {"Authorization": "Bearer test-studio-token"}
    first = client.get(f"/api/runs/{RUN}/event-page?after=0&limit=2", headers=headers)
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    assert first.json() == {
        "items": [{"seq": 1, "type": "status"}, {"seq": 2, "type": "status"}],
        "next_cursor": 2,
        "has_more": True,
    }
    second = client.get(
        f"/api/runs/{RUN}/event-page?after=2&limit=3", headers=headers
    ).json()
    assert second["next_cursor"] == 5 and second["has_more"] is False
    assert (
        client.get(f"/api/runs/{RUN}/event-page?limit=501", headers=headers).status_code
        == 400
    )
