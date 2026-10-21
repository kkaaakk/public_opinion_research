"""Exercise the real SQL queue, SDK/Workflow boundaries and durable API offline."""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from test_python_workflow import config, initial, install_steps
from test_python_workflow_sdk import install_sdk

from open_deep_research.research_graph.store import clear_research_graph_store_cache
from open_deep_research.web import runs, server, worker
from open_deep_research.web.run_store import (
    TERMINAL,
    LeaseLost,
    RunConflict,
    RunNotFound,
    RunStore,
    dumps,
    loads,
    public_run,
)
from open_deep_research.workflow.execution import execution_observer
from open_deep_research.workflow.python_workflow import WORKFLOW_VERSION, PythonWorkflow


@pytest.fixture
def store(tmp_path):
    result = RunStore(
        f"sqlite:///{(tmp_path / 'runs.db').as_posix()}", lease_seconds=1.5
    )
    yield result
    result.close()
    clear_research_graph_store_cache()


def create(
    store, owner="alice", key="create", *, state=None, cfg=None, session_id=None
):
    return store.create(
        owner,
        key,
        {"topic": "brand recall"},
        cfg or config(),
        state or initial(),
        session_id=session_id,
    )[0]


def expire(store, run_id):
    with store.engine.begin() as db:
        db.execute(
            update(store.runs)
            .where(store.runs.c.run_id == run_id)
            .values(lease_until=0)
        )


def get(store, run):
    return store.get(run["run_id"], run["owner"])


def test_concurrent_acceptance_is_idempotent_and_conflict_is_atomic(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: create(store), range(12)))
    assert len({r["run_id"] for r in values}) == 1
    run = values[0]
    assert store.read_events(run["run_id"], "alice", 0)[0]["seq"] == 1
    with pytest.raises(RunConflict, match="another request"):
        store.create("alice", "create", {"topic": "different"}, config(), initial())
    assert create(store, "bob")["run_id"] != run["run_id"]
    assert create(store, key="CREATE")["run_id"] != run["run_id"]


def test_cross_worker_global_owner_and_session_limits(store, tmp_path):
    a = create(store)
    create(store, key="same-session", session_id=a["session_id"])
    create(store, key="second")
    create(store, key="third")
    create(store, "bob")
    create(store, "charlie")
    other = RunStore(str(store.engine.url), lease_seconds=1.5)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(
                pool.map(lambda n: (store if n % 2 else other).claim(f"w{n}"), range(8))
            )
        active = [r for r in claims if r]
        assert len(active) == 4
        assert sum(r["owner"] == "alice" for r in active) == 2
        assert len({r["session_id"] for r in active}) == 4
        assert store.claim("duplicate-delivery") is None
    finally:
        other.close()


@pytest.mark.parametrize(
    "operation", ["get", "read_events", "cancel", "approve", "retry"]
)
def test_all_controls_are_owner_scoped(store, operation):
    run = create(store)
    args = {
        "get": (),
        "read_events": (0,),
        "cancel": (),
        "approve": ("0" * 64, "approve"),
        "retry": ("retry", 1),
    }[operation]
    with pytest.raises(RunNotFound):
        getattr(store, operation)(run["run_id"], "bob", *args)
    with pytest.raises(RunNotFound):
        create(store, "bob", session_id=run["session_id"])


def test_terminal_commit_cancel_race_has_one_terminal_and_contiguous_events(store):
    run = create(store)
    claim = store.claim("worker")
    state = claim["data"]["state"]
    state.setdefault("report", {})["final"] = "report"
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(store.cancel, run["run_id"], "alice")
        b = pool.submit(
            store.finish, run["run_id"], claim["attempt_id"], "completed", state
        )
        a.result()
        b.result()
    result = get(store, run)
    assert result["status"] in {"completed", "cancelled"}
    events = store.read_events(run["run_id"], "alice", 0)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert (
        len(
            [e for e in events if e["type"] == "run_status" and e["status"] in TERMINAL]
        )
        == 1
    )
    with pytest.raises(LeaseLost):
        store.finish(run["run_id"], claim["attempt_id"], "completed", state)


def test_event_failure_rolls_back_run_state_and_outbox(store, monkeypatch):
    run = create(store)
    original = deepcopy(get(store, run))

    def broken(*args, **kwargs):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(store, "_event", broken)
    with pytest.raises(RuntimeError):
        store.cancel(run["run_id"], "alice")
    assert get(store, run) == original


@pytest.mark.parametrize("inflight", [False, True])
def test_expired_lease_is_fenced_and_only_quiescent_checkpoint_can_resume(
    store, inflight
):
    run = create(store)
    claim = store.claim("old")
    if inflight:
        store.progress(
            run["run_id"], claim["attempt_id"], "step_started", step="research_agents"
        )
        identity = dict(
            role="public_signal", name="write", tool_call_id="call", arguments="secret"
        )
        store.operation(run["run_id"], claim["attempt_id"], identity)
    expire(store, run["run_id"])
    assert store.reconcile() == 1
    result = get(store, run)
    assert result["status"] == "interrupted"
    assert bool(result["quarantined"]) == inflight
    for method, args in [("heartbeat", ()), ("progress", ("model_started",))]:
        with pytest.raises(LeaseLost):
            getattr(store, method)(run["run_id"], claim["attempt_id"], *args)
    if inflight:
        with pytest.raises(RunConflict):
            store.retry(run["run_id"], "alice", "retry", result["version"])
        assert store.claim("new") is None
    else:
        resumed = store.retry(run["run_id"], "alice", "retry", result["version"])
        assert (
            store.retry(run["run_id"], "alice", "retry", result["version"])["run_id"]
            == run["run_id"]
        )
        new = store.claim("new")
        assert new["attempt_id"] != claim["attempt_id"]
        assert resumed["status"] == "queued"


def test_tool_intent_and_receipt_do_not_authorize_replay_or_expose_arguments(store):
    run = create(store)
    claim = store.claim("worker")
    identity = dict(
        role="public_signal", name="write", tool_call_id="call", arguments="SECRET"
    )
    op = store.operation(run["run_id"], claim["attempt_id"], identity)
    store.operation(run["run_id"], claim["attempt_id"], identity, status="returned")
    with pytest.raises(RunConflict, match="replay"):
        store.operation(run["run_id"], claim["attempt_id"], identity)
    assert len(op) == 64
    with store.engine.connect() as db:
        rows = db.execute(select(store.operations)).mappings().all()
    assert len(rows) == 1
    assert "SECRET" not in dumps(rows[0]["data"])
    assert "SECRET" not in dumps(store.read_events(run["run_id"], "alice", 0))


@pytest.mark.parametrize(
    "mode", ["normal", "double_error", "model_error", "timeout", "cancel"]
)
def test_real_sdk_runner_python_workflow_and_sql_worker(
    monkeypatch, tmp_path, store, mode
):
    async def run_test():
        state, cfg, runtimes, models, calls, blocked = install_sdk(
            monkeypatch, tmp_path / "transcripts", mode
        )
        cfg["configurable"]["allow_clarification"] = False
        # Token accounting assertions use the same nonzero Phase 3 baseline.
        run = create(store, state=state, cfg=cfg)
        claim = store.claim("worker")
        task = asyncio.create_task(worker.RunWorker(store).execute(claim))
        if mode == "cancel":
            await asyncio.wait_for(blocked.wait(), 5)
            store.cancel(run["run_id"], "alice")
        await asyncio.wait_for(task, 15)
        result = get(store, run)
        assert result["status"] == (
            "completed"
            if mode == "normal"
            else "cancelled"
            if mode == "cancel"
            else "failed"
        )
        assert result["data"]["settlement"] == "settled"
        assert calls == {"public_signal": 1, "internal_knowledge": 1}
        assert all(rt.harness.last_settlement.succeeded for rt in runtimes)
        assert result["data"]["usage"]["model_calls"] > 7
        events = store.read_events(run["run_id"], "alice", 0, 1000)
        assert all(e["protocol_version"] == 1 for e in events)
        assert any(e["type"] == "sdk_event" for e in events)
        assert all(e.get("event") not in {"prompt", "tool_arguments"} for e in events)
        if mode == "normal":
            assert len(result["data"]["stages"]) == 4
            assert len(models) == 4
        else:
            with pytest.raises(RunConflict):
                store.retry(run["run_id"], "alice", "unsafe-replay", result["version"])
        assert len(calls) == 2

    asyncio.run(run_test())


def test_approval_plan_is_immutable_consumed_once_and_never_recomputed(
    monkeypatch, tmp_path, store
):
    async def run_test():
        state, cfg, runtimes, models, calls, _ = install_sdk(
            monkeypatch, tmp_path / "transcripts"
        )
        cfg["configurable"]["allow_plan_feedback"] = True
        run = create(store, state=state, cfg=cfg)
        await worker.RunWorker(store).execute(store.claim("planner"))
        paused = get(store, run)
        assert paused["status"] == "awaiting_approval"
        assert not models and not calls
        assert paused["data"]["usage"]["model_calls"] == 10
        version = paused["data"]["plan"]["version"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [
                pool.submit(store.approve, run["run_id"], "alice", version, "approve")
                for _ in range(2)
            ]
            errors = []
            for job in jobs:
                try:
                    job.result()
                except RunConflict as exc:
                    errors.append(exc)
        assert len(errors) == 1
        # Replace planning with a hard failure so any replay is visible.
        from open_deep_research.workflow import intake

        async def forbidden(*args, **kwargs):
            raise AssertionError("Planner was re-executed")

        monkeypatch.setattr(intake, "research_brief_step", forbidden)
        monkeypatch.setattr(intake, "section_plan_step", forbidden)
        await worker.RunWorker(store, session_path=str(tmp_path / "sdk.db")).execute(
            store.claim("researcher")
        )
        result = get(store, run)
        assert result["status"] == "completed"
        assert result["data"]["plan"] == paused["data"]["plan"]
        assert len(models) == 4
        assert result["data"]["usage"]["model_calls"] == 28
        assert calls == {"public_signal": 1, "internal_knowledge": 1}
        with sqlite3.connect(tmp_path / "sdk.db") as database:
            ids = [
                row[0]
                for row in database.execute("SELECT session_id FROM agent_sessions")
            ]
        assert len(ids) == 4
        assert all(value.startswith(f"alice:{run['run_id']}:") for value in ids)

    asyncio.run(run_test())


@pytest.mark.parametrize("decision", ["reject", "expired"])
def test_approval_rejection_expiry_and_start_failure_are_durable(store, decision):
    run = create(store)
    claim = store.claim("planner")
    state = claim["data"]["state"]
    store.progress(run["run_id"], claim["attempt_id"], "approval_required", state=state)
    paused = get(store, run)
    version = paused["data"]["plan"]["version"]
    if decision == "expired":
        with store._write() as db:
            current = store._get(db, run["run_id"])
            current["data"]["plan"]["expires_at"] = 0
            store._save(db, current)
        store.reconcile()
    else:
        store.approve(run["run_id"], "alice", version, decision)
    assert get(store, run)["status"] == "cancelled"
    assert store.claim("worker") is None
    with pytest.raises(RunConflict):
        store.approve(run["run_id"], "alice", version, "approve")


def test_archive_compensation_retains_failed_stages_and_does_not_repeat_work(
    store, monkeypatch
):
    run = create(store)
    claim = store.claim("worker")
    state = claim["data"]["state"]
    state["agents"]["public_signal"]["report"] = "retained stage"
    store.finish(
        run["run_id"], claim["attempt_id"], "failed", state, error={"kind": "fixture"}
    )
    archive = Mock()
    archive.save.side_effect = [RuntimeError("SECRET_DSN"), 1]
    monkeypatch.setattr(worker, "get_history_store", lambda: archive)
    instance = worker.RunWorker(store)
    asyncio.run(instance.archive())
    result = get(store, run)
    assert result["data"]["archive_status"] == "pending"
    assert result["data"]["stages"][0]["content"] == "retained stage"
    assert "SECRET_DSN" not in dumps(public_run(result))
    with store._write() as db:
        current = store._get(db, run["run_id"])
        current["data"]["archive_after"] = 0
        store._save(db, current)
    asyncio.run(instance.archive())
    assert get(store, run)["data"]["archive_status"] == "saved"
    assert archive.save.call_count == 2
    record = archive.save.call_args.args[0][0]
    assert record.owner == "alice"
    assert record.role_reports[0].content == "retained stage"
    assert store.claim("worker") is None


def test_checkpoint_roundtrip_and_resume_skips_completed_producers(monkeypatch, store):
    async def run_test():
        events = install_steps(monkeypatch)
        snapshots = []

        async def observer(kind, **facts):
            if kind == "checkpoint":
                snapshots.append(loads(dumps(facts)))

        token = execution_observer.set(observer)
        try:
            original = await PythonWorkflow().ainvoke(
                initial(), config(), start_at="research"
            )
        finally:
            execution_observer.reset(token)
        saved = next(s for s in snapshots if s["cursor"] == "research_review")
        events.clear()
        resumed = await PythonWorkflow().ainvoke(
            saved["state"],
            config(),
            resume={"cursor": saved["cursor"], "version": WORKFLOW_VERSION},
        )
        assert resumed["report"] == original["report"]
        assert all(e[0] not in {"public_signal", "internal_knowledge"} for e in events)
        assert resumed["runtime"]["budget"] == original["runtime"]["budget"]

    asyncio.run(run_test())


@pytest.fixture
def api(store, monkeypatch):
    monkeypatch.setattr(runs, "get_run_store", lambda: store)
    monkeypatch.setattr(server, "WEB_API_TOKEN", "test-token")
    with TestClient(server.app) as client:
        client.headers.update(
            {"Authorization": "Bearer test-token", "Idempotency-Key": "create"}
        )
        yield client


BODY = dict(
    topic="brand recall",
    workflow_backend="python",
    business_agent_runtime="openai_agents",
)


def test_api_create_refresh_and_sse_reconnect_are_subscription_only(api, store):
    response = api.post("/api/runs", json=BODY)
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    repeated = api.post("/api/runs", json=BODY)
    assert repeated.status_code == 200 and repeated.json()["run_id"] == run_id
    assert api.post("/api/runs", json={**BODY, "topic": "different"}).status_code == 409
    assert api.post(f"/api/runs/{run_id}/cancel").json()["status"] == "cancelled"
    first = api.get(f"/api/runs/{run_id}/events")
    assert first.status_code == 200
    assert "id: 1" in first.text
    second = api.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "1"})
    assert "id: 1\n" not in second.text
    terminal_cursor = api.get(f"/api/runs/{run_id}").json()["event_cursor"]
    third = api.get(
        f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(terminal_cursor)}
    )
    assert not third.text
    assert store.claim("subscriber") is None
    assert api.get(f"/api/runs/{run_id}/events?after=9999").status_code == 409
    compatible = api.post("/api/research", json=BODY)
    assert compatible.status_code == 200
    assert compatible.headers["X-Run-ID"] == run_id
    assert store.claim("legacy-subscriber") is None


@pytest.mark.parametrize(
    "payload",
    [
        {"owner": "bob"},
        {"configurable": {"apiKeys": {"key": "SECRET"}}},
        {"workflow_backend": "langgraph"},
        {"business_agent_runtime": "legacy"},
        {"model": "anthropic:claude-sonnet-4-20250514"},
        {"search_api": "openai"},
        {"research_session_id": "../../path"},
    ],
)
def test_api_rejects_untrusted_configuration(api, payload):
    response = api.post("/api/runs", json={**BODY, **payload})
    assert response.status_code == 400
    assert "SECRET" not in response.text


@pytest.mark.parametrize(
    "path,method",
    [
        ("", "post"),
        ("/missing", "get"),
        ("/missing/events", "get"),
        ("/missing/cancel", "post"),
        ("/missing/retry", "post"),
        ("/missing/approval", "post"),
    ],
)
def test_api_authentication_for_every_control(api, path, method):
    body = (
        BODY
        if not path
        else {"state_version": 1}
        if path.endswith("retry")
        else {"plan_version": "0" * 64, "decision": "approve"}
        if path.endswith("approval")
        else {}
    )
    response = getattr(api, method)(
        "/api/runs" + path,
        **({"json": body} if method == "post" else {}),
        headers={"Authorization": "Bearer bad"},
    )
    assert response.status_code == 401


def test_api_foreign_owner_cannot_read_stream_cancel_approve_or_retry(
    api, store, monkeypatch
):
    run = create(store, owner="bob")
    base = f"/api/runs/{run['run_id']}"
    for path in [base, base + "/events"]:
        assert api.get(path).status_code == 404
    for suffix, body in [
        ("cancel", {}),
        ("approval", {"plan_version": "0" * 64, "decision": "approve"}),
        ("retry", {"state_version": 1}),
    ]:
        assert api.post(base + "/" + suffix, json=body).status_code == 404
    assert get(store, run)["status"] == "queued"


def test_actual_body_byte_limit_without_trusting_content_length(api):
    chunks = iter([b'{"topic":"', b"x" * server.MAX_REQUEST_BYTES, b'"}'])
    response = api.post(
        "/api/runs", content=chunks, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    response = api.post(
        "/api/runs",
        content=b"x" * (server.MAX_REQUEST_BYTES + 1),
        headers={"Content-Length": "1"},
    )
    assert response.status_code == 413


def test_durable_api_requires_operator_auth_even_when_legacy_is_anonymous(
    api, monkeypatch
):
    monkeypatch.setattr(server, "WEB_API_TOKEN", "")
    assert api.post("/api/runs", json=BODY).status_code == 503


def test_sql_policy_mismatch_and_queue_capacity_are_explicit(tmp_path):
    url = f"sqlite:///{(tmp_path / 'queue.db').as_posix()}"
    store = RunStore(url, queue_capacity=1)
    try:
        create(store)
        with pytest.raises(RunConflict, match="full"):
            create(store, key="overflow")
        with pytest.raises(RunConflict, match="policy"):
            RunStore(url, queue_capacity=2)
    finally:
        store.close()


def test_in_memory_database_is_rejected_for_durable_execution():
    with pytest.raises(ValueError, match="file-backed"):
        RunStore("sqlite:///:memory:")


def test_configuration_drift_never_executes_the_accepted_work(monkeypatch, store):
    run = create(store)
    claim = store.claim("worker")
    monkeypatch.setenv("RESEARCH_MODEL", "changed:model")
    asyncio.run(worker.RunWorker(store).execute(claim))
    result = get(store, run)
    assert result["status"] == "failed"
    assert result["data"]["error"]["kind"] == "configuration_changed"
    assert "research_model" in result["data"]["error"]["fields"]
    assert not result["data"]["completed_steps"]


def test_approval_delivery_survives_worker_death_before_first_research_step(store):
    run = create(store)
    claim = store.claim("planner")
    store.progress(
        run["run_id"],
        claim["attempt_id"],
        "approval_required",
        state=claim["data"]["state"],
    )
    paused = get(store, run)
    store.approve(run["run_id"], "alice", paused["data"]["plan"]["version"], "approve")
    accepted = store.claim("crashed-after-claim")
    expire(store, run["run_id"])
    store.reconcile()
    interrupted = get(store, run)
    store.retry(run["run_id"], "alice", "resume", interrupted["version"])
    replacement = store.claim("replacement")
    assert replacement["attempt_id"] != accepted["attempt_id"]
    assert replacement["data"]["cursor"] == "research_init"
    assert replacement["data"]["plan"] == paused["data"]["plan"]


def test_safe_read_retry_keeps_usage_and_reuses_saved_plan_cursor(store):
    run = create(store)
    claim = store.claim("worker")
    store.progress(
        run["run_id"],
        claim["attempt_id"],
        "checkpoint",
        step="clarify_with_user",
        cursor="write_research_brief",
        state=claim["data"]["state"],
        version=WORKFLOW_VERSION,
    )
    store.progress(
        run["run_id"], claim["attempt_id"], "step_started", step="write_research_brief"
    )
    state = deepcopy(claim["data"]["state"])
    state["runtime"]["budget"]["model_calls"] += 1
    store.finish(run["run_id"], claim["attempt_id"], "failed", state)
    result = get(store, run)
    assert public_run(result)["retry_safe"]
    store.retry(run["run_id"], "alice", "read-retry", result["version"])
    repeated = store.retry(run["run_id"], "alice", "read-retry", result["version"])
    assert repeated["status"] == "queued"
    with pytest.raises(RunConflict, match="another state"):
        store.retry(run["run_id"], "alice", "read-retry", result["version"] + 1)
    retried = store.claim("retry")
    assert retried["data"]["usage"]["model_calls"] == 8
    assert retried["data"]["cursor"] == "write_research_brief"


def test_unresolved_effect_holds_capacity_until_manual_reconciliation(store):
    run = create(store)
    claim = store.claim("worker")
    store.finish(
        run["run_id"],
        claim["attempt_id"],
        "cancelled",
        claim["data"]["state"],
        settled=False,
    )
    result = get(store, run)
    assert result["status"] == "interrupted" and result["quarantined"]
    with pytest.raises(RunConflict):
        store.release_quarantine(
            run["run_id"],
            "alice",
            result["version"],
            worker_stopped=False,
            note="unverified",
        )
    store.release_quarantine(
        run["run_id"],
        "alice",
        result["version"],
        worker_stopped=True,
        note="Process is stopped; synthetic effect checked in its authoritative store",
    )
    result = get(store, run)
    assert not result["quarantined"] and not result["data"]["retry_safe"]
    with pytest.raises(RunConflict):
        store.retry(run["run_id"], "alice", "replay", result["version"])


def test_cancelled_quiescent_attempt_is_not_resumable_after_lease_expiry(store):
    run = create(store)
    store.claim("worker")
    store.cancel(run["run_id"], "alice")
    expire(store, run["run_id"])
    store.reconcile()
    result = get(store, run)
    assert result["status"] == "cancelled" and not result["data"].get("retry_safe")


def test_public_events_allowlist_sdk_facts_and_redact_report_credentials(
    store, monkeypatch
):
    monkeypatch.setenv("FIXTURE_API_KEY", "fixture-secret-value")
    run = create(store)
    claim = store.claim("worker")
    store.progress(
        run["run_id"],
        claim["attempt_id"],
        "model_started",
        role="public_signal",
        prompt="INTERNAL_PROMPT",
        arguments="PRIVATE_ARGUMENT",
    )
    state = claim["data"]["state"]
    state["report"]["final"] = "Report fixture-secret-value password=hidden-value"
    store.finish(run["run_id"], claim["attempt_id"], "completed", state)
    published = dumps(public_run(get(store, run))) + dumps(
        store.read_events(run["run_id"], "alice", 0)
    )
    for private in (
        "INTERNAL_PROMPT",
        "PRIVATE_ARGUMENT",
        "fixture-secret-value",
        "hidden-value",
    ):
        assert private not in published
    assert "[redacted]" in published


def test_database_control_failure_drains_sdk_then_requires_restart_reconciliation(
    monkeypatch, tmp_path, store
):
    async def run_test():
        state, cfg, runtimes, _, calls, blocked = install_sdk(
            monkeypatch, tmp_path / "transcripts", "cancel"
        )
        run = create(store, state=state, cfg=cfg)
        claim = store.claim("worker")

        def unavailable(*args):
            raise RuntimeError("database offline")

        monkeypatch.setattr(store, "heartbeat", unavailable)
        task = asyncio.create_task(worker.RunWorker(store).execute(claim))
        await asyncio.wait_for(blocked.wait(), 5)
        await asyncio.wait_for(task, 10)
        assert all(rt.harness.last_settlement.succeeded for rt in runtimes)
        assert calls == {"public_signal": 1, "internal_knowledge": 1}
        expire(store, run["run_id"])
        store.reconcile()
        result = get(store, run)
        assert result["status"] == "interrupted"
        assert result["data"]["error"]["usage_complete"] is False
        assert not public_run(result)["retry_safe"]

    asyncio.run(run_test())
