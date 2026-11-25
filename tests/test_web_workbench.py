"""Offline Web workbench contracts over the existing Run and RAG seams."""

import asyncio
import base64
from contextlib import nullcontext
from io import BytesIO
from random import Random
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text

from open_deep_research.config_file import load_configuration
from open_deep_research.rag.types import AnswerReadyContext, RAGChunk, RetrievalResult
from open_deep_research.web import knowledge, runs, server
from open_deep_research.web.run_data import RunConflict

TOKEN = {"Authorization": "Bearer workbench-test-token"}
HEADERS = TOKEN | {"Idempotency-Key": "workbench-test-request"}


@pytest.fixture
def http(monkeypatch):
    from open_deep_research.web import executor

    monkeypatch.setattr(server, "WEB_API_TOKEN", "workbench-test-token")
    monkeypatch.setattr(executor, "_configured_tracing", lambda: False)
    return TestClient(server.app)


def png(side):
    buffer = BytesIO()
    Image.frombytes("RGB", (side, side), Random(9).randbytes(3 * side**2)).save(
        buffer, format="PNG"
    )
    return buffer.getvalue()


def request_image(content, media_type="image/png"):
    return {
        "name": "attachment.png",
        "media_type": media_type,
        "data": base64.b64encode(content).decode(),
    }


def test_web_auth_and_spa_route_isolation(http, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "UI_DIR", tmp_path)
    (tmp_path / "index.html").write_text("<h1>UI</h1>", encoding="utf8")
    assert http.get("/api/health").json() == {"status": "ok"}
    for path in (
        "/api/settings",
        "/api/system/status",
        "/api/knowledge",
        "/api/knowledge/sources",
        "/api/sessions",
        "/api/sessions/" + "a" * 32,
        "/api/runs/submission",
        "/api/runs/test/images/0",
    ):
        assert http.get(path).status_code == 401
    for path in (
        "/",
        "/sessions/" + "a" * 32,
        "/history/test",
        "/runs/test/execution",
        "/knowledge",
    ):
        assert http.get(path).text == "<h1>UI</h1>"
    for path in (
        "/api/no-such-route",
        "/assets/not-found.js",
        "/classic",
        "/studio",
        "/static/app.js",
    ):
        response = http.get(path)
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")


def test_images_have_scoped_body_limit_and_are_verified_before_admission(
    http, monkeypatch
):
    class Store:
        def create(self, owner, key, data, config, state, **kwargs):
            received.append((owner, key, data, config, state, kwargs))
            return {"run_id": "new", "status": "queued"}, True

    config = load_configuration().model_copy(
        update={
            "rag_query_image_enabled": True,
            "rag_query_image_max_images": 1,
            "rag_query_image_max_bytes": 240_000,
        }
    )
    monkeypatch.setattr(runs, "web_configuration", lambda: config)
    received = []
    monkeypatch.setattr(runs, "_store", Store)

    bytes_ = png(256)
    image = request_image(bytes_)
    assert len(image["data"]) > server.MAX_REQUEST_BYTES
    response = http.post(
        "/api/runs",
        headers=HEADERS,
        json={
            "topic": "brand",
            "org_context": "organization",
            "images": [image],
            "rag_enabled": False,
        },
    )
    assert response.status_code == 202
    owner, key, data, frozen, state, kwargs = received[0]
    assert (owner, key) == ("operator", HEADERS["Idempotency-Key"])
    assert kwargs["images"] == [(0, "image/png", bytes_)]
    assert data["images"][0]["size_bytes"] == len(bytes_)
    assert "data" not in data["images"][0]
    assert frozen["configurable"]["organization_context"] == "organization"
    assert frozen["configurable"]["retrieval_mode"] == "web_only"
    assert state["messages"][0].additional_kwargs["web_research_input"] is True

    for images, status in (
        ([request_image(b"plain text")], 415),
        ([request_image(png(8), "image/jpeg")], 415),
        ([request_image(png(8))] * 2, 413),
        ([request_image(b"x" * 260_000)], 413),
    ):
        assert (
            http.post(
                "/api/runs", headers=HEADERS, json={"topic": "brand", "images": images}
            ).status_code
            == status
        )
    assert (
        http.post("/api/runs", json={"topic": "brand", "images": [image]}).status_code
        == 401
    )
    assert (
        http.post(
            "/api/runs/new/approval", headers=HEADERS, json={"brief_md": image["data"]}
        ).status_code
        == 413
    )
    assert len(received) == 1


def test_version_bound_controls_preserve_original_idempotency_identity(
    http, monkeypatch
):
    actions = []

    class Store:
        def approve(self, run_id, owner, version, decision, body, **kwargs):
            actions.append((run_id, owner, version, decision, body, kwargs))
            return {"run_id": run_id}

        def clarify(self, *command):
            actions.append(command)
            raise RunConflict("Interaction version changed")

        def retry(self, *command):
            actions.append(command)
            return {"run_id": command[0]}

    version = "a" * 64
    monkeypatch.setattr(runs, "_store", Store)
    body = {"version": version, "decision": "revise", "feedback": "adjust scope"}
    assert (
        http.post("/api/runs/test/approval", headers=TOKEN, json=body).status_code
        == 400
    )
    assert (
        http.post("/api/runs/test/approval", headers=HEADERS, json=body).status_code
        == 200
    )
    assert (
        http.post(
            "/api/runs/test/clarification",
            headers=HEADERS,
            json={"version": version, "skip": True},
        ).status_code
        == 409
    )
    assert (
        http.post(
            "/api/runs/test/retry", headers=HEADERS, json={"state_version": 7}
        ).status_code
        == 200
    )
    assert actions == [
        (
            "test",
            "operator",
            version,
            "revise",
            None,
            {"feedback": "adjust scope", "key": HEADERS["Idempotency-Key"]},
        ),
        ("test", "operator", version, {}, True),
        ("test", "operator", HEADERS["Idempotency-Key"], 7),
    ]


def test_knowledge_api_lists_real_sources_and_reuses_the_configured_calls(
    http, monkeypatch, tmp_path
):
    (tmp_path / "release.md").write_text("# Release plan", encoding="utf8")
    (tmp_path / "unsupported.zip").write_text("ignored", encoding="utf8")
    config = load_configuration().model_copy(
        update={"rag_knowledge_base_paths": [str(tmp_path)]}
    )
    monkeypatch.setattr(knowledge, "web_configuration", lambda: config)
    monkeypatch.setattr(knowledge, "_jobs", knowledge.OrderedDict())
    monkeypatch.setattr(knowledge, "idle_index", nullcontext)
    builds = []
    indexer = SimpleNamespace(ensure_ready=lambda force: builds.append(force))
    monkeypatch.setattr(
        knowledge,
        "get_or_create_rag_pipeline",
        lambda cfg: SimpleNamespace(indexer=indexer),
    )
    monkeypatch.setattr(
        knowledge,
        "search_idle",
        lambda query, cfg: AnswerReadyContext(
            query=query,
            context="release",
            matched_chunks=[
                RetrievalResult(
                    chunk=RAGChunk(
                        chunk_id="one",
                        content="Release plan",
                        source=str(tmp_path / "release.md"),
                        metadata={"page": 2, "private": "hidden"},
                    ),
                    score=0.42,
                )
            ],
        ),
    )
    view = http.get("/api/knowledge", headers=TOKEN).json()
    assert view["configured"] and view["index_state"] == "unknown"
    response = http.get("/api/knowledge/sources?q=RELEASE", headers=TOKEN)
    assert response.status_code == 200 and response.json()["total"] == 1
    assert response.json()["items"][0]["name"] == "release.md"
    accepted = http.post(
        "/api/knowledge/index", headers=HEADERS, json={"force": True}
    ).json()
    assert accepted["status"] == "running"
    replay = http.post(
        "/api/knowledge/index", headers=HEADERS, json={"force": True}
    ).json()
    assert replay["id"] == accepted["id"] and replay["status"] == "completed"
    assert builds == [True]
    result = http.post(
        "/api/knowledge/search", headers=TOKEN, json={"query": " release "}
    ).json()
    assert result["query"] == "release"
    assert result["matches"][0]["source"] == "release.md"
    assert result["matches"][0]["metadata"] == {"page": 2}
    assert (
        http.post(
            "/api/knowledge/search", headers=TOKEN, json={"query": " "}
        ).status_code
        == 400
    )


@pytest.mark.integration
def test_real_postgres_image_receipt_session_and_index_fence(
    http, pg_database, monkeypatch
):
    from open_deep_research.workflow.dbos_workflow import uploaded_query_message

    store = runs.get_run_client()
    with store.engine.begin() as db:
        store.check_index_maintenance(db)
        assert (
            http.post(
                "/api/runs", headers=HEADERS, json={"topic": "blocked"}
            ).status_code
            == 409
        )
        assert db.scalar(text("SELECT count(*) FROM por_domain.requests")) == 0

    original = png(8)
    body = {"topic": "Research image", "images": [request_image(original)]}
    response = http.post("/api/runs", headers=HEADERS, json=body)
    assert response.status_code == 202, response.text
    run = response.json()
    run_id, session_id = run["run_id"], run["research_session_id"]
    assert http.post("/api/runs", headers=HEADERS, json=body).json()["run_id"] == run_id
    assert http.get("/api/runs/submission", headers=HEADERS).json()["run_id"] == run_id
    execution = http.get("/api/runs/" + run_id + "/execution", headers=TOKEN)
    assert execution.status_code == 200 and execution.json()["calls_available"]
    system = http.get("/api/system/status", headers=TOKEN).json()
    assert system["web"] == "ok" and system["database"] == "reachable"
    assert system["catalog"] == "readable" and system["executor"] == "unknown"
    assert (
        http.get("/api/runs/" + run_id + "/images/0", headers=TOKEN).content == original
    )
    assert (
        http.get("/api/runs/" + run_id + "/images/1", headers=TOKEN).status_code == 404
    )
    message = asyncio.run(uploaded_query_message(run_id))
    assert message.content[0]["image_url"]["url"].endswith(
        request_image(original)["data"]
    )
    assert (
        http.get("/api/sessions", headers=TOKEN).json()["items"][0][
            "research_session_id"
        ]
        == session_id
    )
    detail = http.get("/api/sessions/" + session_id, headers=TOKEN).json()
    assert detail["can_continue"] is False and detail["items"][0]["run_id"] == run_id
    assert (
        http.get("/api/history?q=Research&status=completed", headers=TOKEN).json()[
            "items"
        ]
        == []
    )
    with store.engine.begin() as db, pytest.raises(RunConflict, match="idle research"):
        store.check_index_maintenance(db)

    native = store.native(run_id)
    with monkeypatch.context() as fixtures:
        fixtures.setattr(store.client, "list_workflows", lambda **kwargs: [native])
        fixtures.setattr(store, "_delivered_status", lambda *_args: "completed")
        matching = http.get("/api/history?q=RESEARCH&status=completed", headers=TOKEN)
        assert matching.status_code == 200
        assert matching.json()["items"][0]["run_id"] == run_id
        assert http.get("/api/history?q=absent", headers=TOKEN).json()["items"] == []
        assert (
            http.get("/api/history?status=failed", headers=TOKEN).json()["items"] == []
        )


@pytest.mark.integration
def test_image_context_survives_starting_the_native_plan_and_reopening_history(
    http, native_case, monkeypatch
):
    from open_deep_research.configuration import Configuration
    from open_deep_research.rag import query_images

    config = Configuration.from_runnable_config(native_case.cfg).model_copy(
        update={
            "rag_query_image_enabled": True,
            "allow_plan_feedback": True,
        }
    )
    monkeypatch.setattr(runs, "web_configuration", lambda: config)
    image = png(8)
    calls = []

    def describe(image_bytes, **_kwargs):
        calls.append(image_bytes)
        return "Recognized a recall notice in the screenshot."

    monkeypatch.setattr(query_images, "extract_image_bytes_vision_text", describe)
    response = http.post(
        "/api/runs",
        headers=HEADERS,
        json={
            "topic": "Investigate the attached brand recall notice.",
            "images": [request_image(image)],
        },
    )
    assert response.status_code == 202
    run = response.json() | {"owner": "operator"}
    pending = native_case.wait(
        run, lambda snapshot: snapshot["status"] == "awaiting_approval"
    )
    assert calls == [image]
    assert "Recognized a recall notice" in native_case.plan_prompts[0]
    assert (
        http.get("/api/runs/" + run["run_id"], headers=TOKEN).json()["image_context"]
        == pending["image_context"]
    )
    assert (
        http.post(
            "/api/runs/" + run["run_id"] + "/approval",
            headers=TOKEN,
            json={
                "version": pending["plan"]["version"],
                "decision": "reject",
            },
        ).status_code
        == 200
    )
    native_case.wait(run, lambda snapshot: snapshot["events_complete"])
    archived = http.get("/api/history/" + run["run_id"], headers=TOKEN).json()
    assert archived["status"] == "cancelled"
    assert archived["image_context"] == pending["image_context"]
    assert archived["images"][0]["name"] == "attachment.png"
    assert calls == [image]
