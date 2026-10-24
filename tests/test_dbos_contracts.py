"""Public native DBOS contracts replacing SQL RunStore/Worker white-box tests."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from open_deep_research.web.run_data import (
    RunConflict,
    RunNotFound,
    dumps,
)
from open_deep_research.workflow.run_facts import facts

pytestmark = pytest.mark.integration


def test_sse_terminal_receipt_cannot_close_over_an_older_cursor(
    native_case, monkeypatch
):
    """Force the relay/read interleaving that previously dropped the final done."""
    from open_deep_research.web.runs import event_stream

    case = native_case
    run = case.create()
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    end = result["seq"]
    before = len(case.models)
    original = case.client.client.get_event
    reads = []

    def raced(workflow_id, key, **kwargs):
        value = original(workflow_id, key, **kwargs)
        if workflow_id == run["run_id"] + ":events" and key == "cursor":
            reads.append(value)
            # read_events and the initial Run projection both precede the
            # last cursor commit; the subsequent done/history reads observe it.
            if len(reads) <= 2:
                return end - 1
        return value

    class Subscription:
        async def is_disconnected(self):
            return False

    monkeypatch.setattr(case.client.client, "get_event", raced)

    async def receive():
        return [
            frame
            async for frame in event_stream(
                case.client, run["run_id"], "alice", end - 1, Subscription()
            )
        ]

    frames = asyncio.run(receive())
    assert len(reads) >= 3
    assert any(
        f"id: {end}\n" in frame and '"type": "done"' in frame for frame in frames
    )
    assert len(case.models) == before == 4


@pytest.mark.parametrize(
    "native_case", ["normal", "complete", "rolling"], indirect=True
)
def test_native_four_roles_real_sdk_graph_budget_and_history(native_case):
    case = native_case
    run = case.create()
    result = case.wait(run)
    assert result["status"] == "completed", result["error"]
    assert "FINAL brand recall report" in result["report"]
    assert len(case.runtimes) == len(case.models) == 4
    assert all(rt.harness.last_settlement.succeeded for rt in case.runtimes)
    assert all(rt.workspace.working_context.active_evidence_ids for rt in case.runtimes)
    assert (
        result["usage"]["model_calls"]
        == 7 + sum(rt.budget_usage["model_calls"] for rt in case.runtimes) + 5
    )
    # Intake uses three model calls, sections/final report use two.
    # Exact normal fixture is also an oracle for all SDK/Graph service charges.
    if case.mode != "rolling":
        assert result["usage"]["total_tokens"] == 311
        assert result["usage"]["model_calls"] == 28
    archived = case.wait(run, lambda r: r["archive_status"] == "saved")
    saved = case.history.get(archived["history_id"], owner="alice")
    assert saved["run_id"] == run["run_id"]
    assert len(saved["role_reports"]) == 4
    assert saved["report"] == result["report"]


def test_concurrent_acceptance_is_idempotent_and_conflicts_are_atomic(native_case):
    case = native_case
    key = uuid4().hex
    with ThreadPoolExecutor(max_workers=5) as pool:
        values = list(pool.map(lambda _: case.create(key=key), range(8)))
    assert len({r["run_id"] for r in values}) == 1
    with pytest.raises(RunConflict, match="another request"):
        case.client.create("alice", key, {"topic": "different"}, case.cfg, case.state)
    case.wait(values[0])


@pytest.mark.parametrize(
    "operation", ["get", "read_events", "cancel", "approve", "retry"]
)
def test_all_controls_remain_owner_scoped(native_case, operation):
    case = native_case
    run = case.create(approval=True)
    args = {
        "get": (),
        "read_events": (0,),
        "cancel": (),
        "approve": ("0" * 64, "approve"),
        "retry": ("r", 1),
    }[operation]
    with pytest.raises(RunNotFound):
        getattr(case.client, operation)(run["run_id"], "bob", *args)
    with pytest.raises(RunNotFound):
        case.create(owner="bob", session=run["research_session_id"])
    case.client.cancel(run["run_id"], "alice")


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_approval_immutable_once_and_no_replanning(native_case, decision):
    case = native_case
    run = case.create(approval=True)
    planned = case.wait(run, lambda r: r["status"] == "awaiting_approval")
    assert not case.runtimes
    before = [u["id"] for u in facts().units(run["run_id"])]
    with pytest.raises(RunConflict):
        case.client.approve(run["run_id"], "alice", "0" * 64, decision)
    case.client.approve(run["run_id"], "alice", planned["plan"]["version"], decision)
    with pytest.raises(RunConflict):
        case.client.approve(
            run["run_id"], "alice", planned["plan"]["version"], decision
        )
    result = case.wait(run)
    assert result["status"] == ("completed" if decision == "approve" else "cancelled")
    after = [u["id"] for u in facts().units(run["run_id"])]
    assert set(before) <= set(after)
    assert len([k for k in after if "write_research_brief" in k]) == 1
    if decision == "reject":
        assert not case.runtimes


def test_sse_cursor_replay_is_read_only_and_redacted(native_case):
    case = native_case
    run = case.create()
    case.wait(run, lambda r: r["archive_status"] == "saved")
    events = case.client.read_events(run["run_id"], "alice", 0, 1000)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    types = {e["type"] for e in events}
    assert {
        "run_status",
        "step_started",
        "step_completed",
        "stream",
        "report",
        "usage",
        "settlement",
        "done",
        "history_saved",
    } <= types
    assert all(e["protocol_version"] == 1 for e in events)
    tools = [e for e in events if e["type"] == "sdk_event" and e.get("event") == "tool"]
    assert len(tools) == 4
    assert [e["status"] for e in tools].count("started") == 2
    assert [e["status"] for e in tools].count("returned") == 2
    assert (
        sum(
            e["type"] == "sdk_event" and e.get("event") == "agent_started"
            for e in events
        )
        == 4
    )
    assert any(
        e["type"] == "status" and e.get("granularity") == "stage" for e in events
    )
    assert events[-1]["type"] == "done" and events[-1]["archive_status"] == "saved"
    before = len(case.models)
    assert case.client.read_events(run["run_id"], "alice", 3, 1000) == events[3:]
    assert len(case.models) == before
    for cursor in (-1, 1000000):
        with pytest.raises(RunConflict):
            case.client.read_events(run["run_id"], "alice", cursor)
    assert "arguments" not in dumps(events)
    assert "Private Agent Memory" not in dumps(events)


@pytest.mark.parametrize(
    "native_case", ["model_error", "double_error", "timeout", "cancel"], indirect=True
)
def test_sdk_failure_and_cancel_preserve_budget_and_settlement(native_case):
    case = native_case
    run = case.create()
    if case.mode == "cancel":
        end = time.monotonic() + 15
        while not case.blocked.is_set() and time.monotonic() < end:
            time.sleep(0.05)
        assert case.blocked.is_set()
        case.client.cancel(run["run_id"], "alice")
    result = case.wait(run)
    assert result["status"] in {"failed", "cancelled", "interrupted"}
    assert not result["report"]
    assert result["usage"]["model_calls"] >= 7 + sum(
        rt.budget_usage["model_calls"] for rt in case.runtimes
    )
    assert all(rt.harness.last_settlement.succeeded for rt in case.runtimes)
    assert all(len(rt.workspace.receipts) == 1 for rt in case.runtimes)
    assert len(case.models) == 2


def test_transactional_capacity_includes_owner_session_and_quarantine(pg_database):
    from test_python_workflow import config, initial

    from open_deep_research.web.durable import RunClient

    client = RunClient()
    repo = facts()
    first = client.create("alice", "a", {"topic": "a"}, config(), initial())[0]
    second = client.create(
        "alice",
        "b",
        {"topic": "b"},
        config(),
        initial(),
        session_id=first["research_session_id"],
    )[0]
    third = client.create("alice", "c", {"topic": "c"}, config(), initial())[0]
    fourth = client.create("alice", "d", {"topic": "d"}, config(), initial())[0]
    other = client.create("bob", "e", {"topic": "e"}, config(), initial())[0]
    assert repo.reserve(first["run_id"])
    assert not repo.reserve(second["run_id"])
    assert repo.reserve(third["run_id"])
    assert not repo.reserve(fourth["run_id"])
    assert repo.reserve(other["run_id"])
    permit, _, _ = repo.acquire(
        first["run_id"], first["run_id"] + ":role", "public_signal_agent"
    )
    with pytest.raises(RunConflict):
        repo.acquire(first["run_id"], first["run_id"] + ":role", "public_signal_agent")
    repo.release(first["run_id"])
    assert not repo.reserve(second["run_id"])
    with pytest.raises(RunConflict):
        repo.check(first["run_id"] + ":role", permit)


def test_tool_operation_identity_never_authorizes_unknown_replay(pg_database):
    from test_python_workflow import config, initial

    from open_deep_research.web.durable import RunClient

    c = RunClient()
    r = c.create("alice", "a", {"topic": "a"}, config(), initial())[0]
    repo = facts()
    unit = r["run_id"] + ":role"
    permit, _, _ = repo.acquire(r["run_id"], unit, "public_signal_agent")
    identity = dict(
        role="public_signal",
        name="write",
        task_id="t",
        research_round=1,
        tool_call_id="native",
        arguments="secret-body",
    )
    operation = repo.operation(unit, permit, identity)
    assert len(operation) == 64
    with pytest.raises(RunConflict):
        repo.operation(unit, permit, identity)
    with repo.engine.connect() as db:
        row = (
            db.execute(
                text("SELECT * FROM por_domain.operations WHERE id=:id"),
                {"id": operation},
            )
            .mappings()
            .one()
        )
        assert "secret-body" not in str(dict(row))
    repo.operation(unit, permit, identity, "returned")
    with pytest.raises(RunConflict):
        repo.operation(unit, permit, identity, "returned")


@pytest.mark.parametrize("entrypoint", ["/api/runs", "/api/research", "cli"])
def test_api_contract_create_auth_cancel_and_sse(
    native_case, monkeypatch, entrypoint, capsys
):
    from open_deep_research.web import runs, server

    case = native_case
    from contextlib import asynccontextmanager

    from agents.testing import (
        ModelStep,
        ScriptedModel,
        assistant_message,
        function_call,
    )
    from test_sdk_runtime import TOKENS

    from open_deep_research.runtime import business_agent

    @asynccontextmanager
    async def api_model(*args, **kwargs):
        def first(call):
            role = call.system_instructions.split("<Role>\n", 1)[1].split(
                "\n</Role>", 1
            )[0]
            if role in {"public_signal", "internal_knowledge"}:
                return [
                    function_call(
                        "web_search" if role == "public_signal" else "rag_search",
                        {"query": role},
                        call_id="api-" + role,
                    )
                ]
            assert "[Evidence]" in str(call.input)
            return [assistant_message("analysis")]

        yield ScriptedModel(
            [ModelStep.respond(first), [assistant_message("complete")]],
            default_usage=TOKENS,
        )

    monkeypatch.setattr(business_agent, "sdk_model", api_model)
    monkeypatch.setattr(runs, "get_run_client", lambda: case.client)
    monkeypatch.setattr(server, "WEB_API_TOKEN", "test-db-os-token")
    # Inference is already installed; accepted choices use the official Web schema.
    monkeypatch.setattr(runs, "accepted_config", lambda body: case.cfg)
    with TestClient(server.app) as http:
        body = dict(
            topic="brand recall",
            model="openai:gpt-4.1",
        )
        path = "/api/runs" if entrypoint == "cli" else entrypoint
        assert http.post(path, json=body).status_code == 401
        headers = {"Authorization": "Bearer test-db-os-token", "Idempotency-Key": "api"}
        if entrypoint == "cli":
            import json

            import httpx

            from open_deep_research.web import cli

            original = httpx.Client
            accepted = []

            def bridge(request):
                result = http.request(
                    request.method,
                    str(request.url),
                    content=request.content,
                    headers=dict(request.headers),
                )
                accepted.append(result.status_code)
                return httpx.Response(result.status_code, json=result.json())

            monkeypatch.setenv("PUBLIC_OPINION_API_TOKEN", "test-db-os-token")
            monkeypatch.setattr(
                cli.httpx,
                "Client",
                lambda **kw: original(transport=httpx.MockTransport(bridge), **kw),
            )
            capsys.readouterr()
            assert (
                cli.main(
                    [
                        "--url",
                        "http://testserver",
                        "create",
                        "brand recall",
                        "--model",
                        "openai:gpt-4.1",
                        "--key",
                        "api",
                    ]
                )
                == 0
            )
            run = json.loads(capsys.readouterr().out)
            assert accepted == [202]
        else:
            response = http.post(entrypoint, json=body, headers=headers)
            assert response.status_code == (
                202 if entrypoint == "/api/runs" else 200
            ), response.text
            if entrypoint == "/api/research":
                assert '"type": "done"' in response.text
                assert '"type": "report"' in response.text
                run = case.client.get(response.headers["x-run-id"], "operator")
            else:
                run = response.json()
        assert http.post("/api/runs", json=body, headers=headers).status_code == 200
        case.wait(dict(run, owner="operator"), lambda r: r["archive_status"] == "saved")
        replay = http.get("/api/runs/" + run["run_id"] + "/events", headers=headers)
        assert "id: 1" in replay.text
        assert "history_saved" in replay.text
        assert (
            http.get("/api/runs/" + run["run_id"], headers=headers).json()[
                "execution_engine"
            ]
            == "dbos"
        )
        bad = dict(body, owner="attacker")
        assert http.post("/api/runs", json=bad, headers=headers).status_code == 400


@pytest.mark.parametrize("pg_database", [{"RUN_APPROVAL_SECONDS": 1}], indirect=True)
def test_approval_timeout_never_runs_roles(native_case):
    run = native_case.create(approval=True)
    result = native_case.wait(run)
    assert result["status"] == "cancelled"
    assert not native_case.runtimes
    assert result["settlement"] == "settled"


@pytest.mark.parametrize("changed", ["MAX_MODEL_CALLS", "sdk_version"])
def test_configuration_and_sdk_drift_fail_before_external_work(
    native_case, monkeypatch, changed
):
    case = native_case
    run = case.create(approval=True)
    plan = case.wait(run, lambda r: r["status"] == "awaiting_approval")["plan"]
    if changed == "MAX_MODEL_CALLS":
        monkeypatch.setenv("MAX_MODEL_CALLS", "101")
    else:
        with facts().engine.begin() as db:
            req = facts().request(run["run_id"], db=db)
            req["data"]["sdk"] = "unsupported-sdk"
            db.execute(
                text("UPDATE por_domain.requests SET value=:v WHERE id=:id"),
                {"id": run["run_id"], "v": dumps(req["data"])},
            )
    case.client.approve(run["run_id"], "alice", plan["version"], "approve")
    result = case.wait(run)
    assert result["status"] == "failed"
    assert not case.runtimes
    assert not result["retry_safe"]
    assert result["usage"]["model_calls"] == 10


def test_archive_compensates_independently_without_rerunning_research(
    native_case, monkeypatch
):
    import threading

    from open_deep_research.web.history import HistoryUnavailable

    case = native_case
    unavailable = threading.Event()
    unavailable.set()
    original = case.history.save

    def save(records):
        if unavailable.is_set():
            raise HistoryUnavailable("Synthetic archive outage")
        return original(records)

    monkeypatch.setattr(case.history, "save", save)
    run = case.create()
    completed = case.wait(run)
    assert completed["status"] == "completed"
    case.wait(
        run,
        lambda r: any(
            e["type"] == "archive_pending"
            for e in case.client.read_events(run["run_id"], "alice", 0, 1000)
        ),
    )
    assert completed["report"]
    before = len(case.models)
    unavailable.clear()
    result = case.wait(run, lambda r: r["archive_status"] == "saved", timeout=50)
    assert result["report"] == completed["report"]
    assert len(case.models) == before == 4
    assert result["usage"] == completed["usage"]


def test_safe_read_retry_is_idempotent_and_reuses_successful_roles(
    native_case, monkeypatch
):
    from open_deep_research.workflow import dbos_workflow as engine

    case = native_case
    original = engine.OPERATIONS["research_review"]
    attempts = []

    async def flaky(state, cfg):
        if not attempts:
            attempts.append("failure")
            from open_deep_research.budget import capture_budget_usage

            capture_budget_usage({"model_calls": 1, "input_tokens": 2})
            raise RuntimeError("Synthetic safe read failure")
        return await original(state, cfg)

    monkeypatch.setitem(engine.OPERATIONS, "research_review", flaky)
    run = case.create()
    failed = case.wait(run)
    assert failed["status"] == "failed"
    assert failed["retry_safe"]
    assert len(case.models) == 2
    failed = case.wait(run, lambda r: r["archive_status"] == "saved")
    from open_deep_research.web.history import HistoryRecord

    prior = HistoryRecord.model_validate(
        case.history.get(failed["history_id"], owner="alice")
    )
    assert prior.run_state_version == 1
    key = uuid4().hex
    case.client.retry(run["run_id"], "alice", key, failed["state_version"])
    case.client.retry(run["run_id"], "alice", key, failed["state_version"])
    result = case.wait(run, lambda r: r["status"] == "completed")
    assert len(case.models) == 4
    assert result["usage"]["model_calls"] == 29
    assert result["generation"] == 1
    saved = case.wait(run, lambda r: r["archive_status"] == "saved")
    assert case.history.get(saved["history_id"], owner="alice")["status"] == "completed"
    # A delayed acknowledgement/commit for the earlier failed outcome cannot win.
    case.history.save([prior])
    latest = case.history.get(saved["history_id"], owner="alice")
    assert latest["status"] == "completed"
    assert latest["run_state_version"] == 2
    assert latest["report"] == result["report"]
    assert latest["failed_stage"] == ""
    assert "_por_archive_lock" not in latest
    forged = prior.model_copy(
        update={"owner": "bob", "run_state_version": 999, "report": "foreign overwrite"}
    )
    case.history.save([forged])
    unchanged = case.history.get(saved["history_id"], owner="alice")
    assert unchanged["owner"] == "alice"
    assert unchanged["report"] == result["report"]
    assert unchanged["run_state_version"] == 2


@pytest.mark.parametrize("pg_database", [{"RUN_QUEUE_CAPACITY": 1}], indirect=True)
def test_queue_capacity_and_policy_mismatch_are_explicit(pg_database):
    from test_python_workflow import config, initial

    from open_deep_research.web.durable import RunClient
    from open_deep_research.workflow.dbos_config import ExecutionSettings
    from open_deep_research.workflow.run_facts import RunFacts

    client = RunClient()
    client.create("alice", "first", {"topic": "a"}, config(), initial())
    with pytest.raises(RunConflict, match="full"):
        client.create("bob", "second", {"topic": "b"}, config(), initial())
    changed = RunFacts(ExecutionSettings(pg_database, queue_capacity=2))
    try:
        with pytest.raises(RunConflict, match="policy"):
            changed.initialize()
    finally:
        changed.close()


def test_domain_artifact_and_native_enqueue_rollback_are_atomic(
    pg_database, monkeypatch
):
    from test_python_workflow import config, initial

    from open_deep_research.web.durable import RunClient

    repo = facts()
    client = RunClient()

    def fail(*args, **kw):
        raise RuntimeError("Synthetic native-message transaction failure")

    with monkeypatch.context() as patch:
        patch.setattr(repo, "publish", fail)
        with pytest.raises(RuntimeError):
            client.create("alice", "a", {"topic": "a"}, config(), initial())
    with repo.engine.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM por_domain.requests")).scalar_one()
            == 0
        )
    assert not repo.client.list_workflows(name="por.run")


@pytest.mark.parametrize(
    "path,method",
    [
        ("", "post"),
        ("/unknown", "get"),
        ("/unknown/events", "get"),
        ("/unknown/cancel", "post"),
        ("/unknown/approval", "post"),
        ("/unknown/retry", "post"),
        ("/unknown/resume", "post"),
    ],
)
def test_every_run_api_control_requires_auth(native_case, monkeypatch, path, method):
    from open_deep_research.web import runs, server

    monkeypatch.setattr(server, "WEB_API_TOKEN", "test-auth-token")
    monkeypatch.setattr(runs, "get_run_client", lambda: native_case.client)
    with TestClient(server.app) as http:
        body = (
            {"plan_version": "0" * 64, "decision": "approve"}
            if path.endswith("approval")
            else {"state_version": 1}
        )
        result = (
            http.post("/api/runs" + path, json=body)
            if method == "post"
            else http.get("/api/runs" + path)
        )
        assert result.status_code == 401


def test_gap_loop_uses_native_role_children_and_original_reducer(
    native_case, monkeypatch
):
    from langchain_core.runnables import RunnableLambda
    from test_python_workflow import gap

    from open_deep_research.state import ResearchReview
    from open_deep_research.workflow import shared

    case = native_case
    original = shared.configurable_model
    reviews = []

    class ReviewServices:
        def with_config(self, cfg):
            return self

        def with_structured_output(self, schema, include_raw=False):
            chain = original.with_structured_output(schema, include_raw)
            if schema is not ResearchReview:
                return chain

            async def reply(messages, config=None):
                value = await chain.ainvoke(messages, config)
                value["parsed"] = ResearchReview(
                    research_complete=bool(reviews),
                    next_tasks=[]
                    if reviews
                    else [gap("date"), gap("approval", "internal_knowledge")],
                )
                reviews.append(value["parsed"])
                return value

            return RunnableLambda(reply)

        async def ainvoke(self, *args, **kw):
            return await original.ainvoke(*args, **kw)

    monkeypatch.setattr(shared, "configurable_model", ReviewServices())
    run = case.create()
    result = case.wait(run)
    assert result["status"] == "completed", result["error"]
    assert len(case.runtimes) == 6
    assert case.calls == {"public_signal": 2, "internal_knowledge": 2}
    final = case.client.client.list_workflows(
        workflow_ids=[run["run_id"]], load_input=False, load_output=True
    )[0].output
    assert final["workflow"]["round"] == 2
    assert {t.task_id for t in final["workflow"]["completed_tasks"]} == {
        "date",
        "approval",
    }
    assert final["workflow"]["pending_tasks"] == []
    assert len(result["role_reports"]) == 6
    assert all(rt.harness.last_settlement.succeeded for rt in case.runtimes)


@pytest.mark.parametrize(
    "pg_database",
    [{"RUN_SDK_SESSION_PATH": ".tmp/dbos-sdk-sessions.sqlite"}],
    indirect=True,
)
def test_sdk_session_is_private_to_role_invocation_not_workflow_recovery(native_case):
    import sqlite3

    case = native_case
    run = case.create()
    case.wait(run)
    with sqlite3.connect(".tmp/dbos-sdk-sessions.sqlite") as db:
        rows = db.execute(
            "SELECT session_id FROM agent_sessions WHERE session_id LIKE ?",
            (run["run_id"] + "%",),
        ).fetchall()
        assert len(rows) == 4
        assert len({r[0] for r in rows}) == 4
        for role in (
            "public_signal",
            "internal_knowledge",
            "risk_assessment",
            "response_strategy",
        ):
            assert sum(role in row[0] for row in rows) == 1
    assert len(case.models) == 4


@pytest.mark.parametrize(
    "payload",
    [
        {"owner": "attacker"},
        {"dbos_database_url": "bad"},
        {"max_model_calls": 99999},
        {"organization_context": "override"},
        {"research_graph_uri": "bolt://foreign"},
        {"topic": ""},
        {"workflow_backend": "langgraph"},
        {"business_agent_runtime": "legacy"},
        {"research_session_id": "../foreign"},
        {"model": "unsupported:model"},
    ],
)
def test_api_rejects_unadmitted_parameters_before_sdk_work(
    native_case, monkeypatch, payload
):
    from open_deep_research.web import runs, server

    monkeypatch.setattr(server, "WEB_API_TOKEN", "test-rejection-token")
    monkeypatch.setattr(runs, "get_run_client", lambda: native_case.client)
    body = (
        dict(
            topic="brand recall",
            workflow_backend="python",
            business_agent_runtime="openai_agents",
            model="openai:gpt-4.1",
        )
        | payload
    )
    with TestClient(server.app) as http:
        response = http.post(
            "/api/runs",
            json=body,
            headers={
                "Authorization": "Bearer test-rejection-token",
                "Idempotency-Key": "reject",
            },
        )
        assert response.status_code == 400, response.text
    assert not native_case.models


def test_cancellation_during_real_receipt_commit_drains_harness(
    native_case, monkeypatch
):
    import threading

    from open_deep_research.workflow.run_facts import RunFacts

    case = native_case
    original = RunFacts.operation
    release = threading.Event()
    returned = set()
    lock = threading.Lock()

    def operation(repo, unit, permit, identity, status="started"):
        if status == "returned":
            with lock:
                returned.add(identity["role"])
            assert release.wait(20), "Receipt gate was not released"
        return original(repo, unit, permit, identity, status)

    monkeypatch.setattr(RunFacts, "operation", operation)
    run = case.create()
    try:
        end = time.monotonic() + 15
        while len(returned) < 2 and time.monotonic() < end:
            time.sleep(0.05)
        assert len(returned) == 2
        case.client.cancel(run["run_id"], "alice")
        cancelling = case.client.get(run["run_id"], "alice")
        assert cancelling["native_status"] == "CANCELLED"
        assert cancelling["settlement"] == "pending"
        assert cancelling["status"] == "cancelling"
    finally:
        release.set()
    result = case.wait(run)
    assert result["status"] == "cancelled"
    assert result["settlement"] == "settled"
    assert len(case.models) == 2
    assert all(rt.harness.last_settlement.succeeded for rt in case.runtimes)
    assert all(len(rt.workspace.receipts) == 1 for rt in case.runtimes)
    assert result["usage"]["model_calls"] == 10 + sum(
        rt.budget_usage["model_calls"] for rt in case.runtimes
    )


@pytest.mark.parametrize("native_case", ["cancel"], indirect=True)
def test_persisted_cancel_intent_survives_lost_web_cancel_rpc(native_case):
    case = native_case
    run = case.create()
    end = time.monotonic() + 15
    while not case.blocked.is_set() and time.monotonic() < end:
        time.sleep(0.05)
    assert case.blocked.is_set()
    # Simulate death after the cancellation intent transaction and before its RPC.
    with facts().engine.begin() as db:
        db.execute(
            text("UPDATE por_domain.requests SET cancel_requested=true WHERE id=:id"),
            {"id": run["run_id"]},
        )
    result = case.wait(run)
    assert result["native_status"] == "CANCELLED"
    assert result["status"] == "cancelled"
    assert result["settlement"] == "settled"
    assert len(case.models) == 2


def test_unknown_memory_commit_is_quarantined_not_retried(native_case, monkeypatch):
    from open_deep_research.workflow import dbos_workflow as engine

    case = native_case

    async def uncertain_memory(state, config):
        with facts().engine.begin() as db:
            db.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS external_memory_effects(id text PRIMARY KEY)"
                )
            )
            db.execute(text("INSERT INTO external_memory_effects VALUES ('one')"))
        raise RuntimeError("Memory committed but acknowledgement is unknown")

    monkeypatch.setitem(engine.OPERATIONS, "compile_final_report", uncertain_memory)
    run = case.create()
    result = case.wait(run)
    assert result["status"] == "interrupted"
    assert result["settlement"] == "unresolved"
    assert not result["retry_safe"]
    assert facts().request(run["run_id"])["occupied"]
    with facts().engine.connect() as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM external_memory_effects")
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize("pg_database", [{"RUN_APPROVAL_SECONDS": 1}], indirect=True)
def test_approval_expiry_gap_before_recv_is_still_enforced(native_case, monkeypatch):
    from open_deep_research.workflow import dbos_workflow as engine

    original = engine.send_event

    async def delayed(run_id, event_type, **data):
        if event_type == "run_status" and data.get("status") == "awaiting_approval":
            # Real native wait has not yet been armed; no framework API is mocked.
            await asyncio.sleep(3)
        return await original(run_id, event_type, **data)

    monkeypatch.setattr(engine, "send_event", delayed)
    run = native_case.create(approval=True)
    result = native_case.wait(run, lambda r: r["native_status"] == "CANCELLED")
    assert result["status"] == "cancelled"
    assert not native_case.runtimes
    with facts().engine.connect() as db:
        assert (
            db.execute(
                text("SELECT decision FROM por_domain.approvals WHERE run_id=:id"),
                {"id": run["run_id"]},
            ).scalar_one()
            == "expired"
        )


def test_committed_role_ack_loss_does_not_repeat_sdk_or_drop_usage(
    native_case, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    from open_deep_research.workflow.run_facts import RunFacts

    original = RunFacts.finish_unit
    lost = []

    def finish(repo, unit, permit, **data):
        result = original(repo, unit, permit, **data)
        if (
            data.get("artifact") is not None
            and unit.endswith(":response_strategy")
            and not lost
        ):
            lost.append(unit)
            raise OperationalError(
                "Synthetic acknowledgement loss", None, Exception("after commit")
            )
        return result

    monkeypatch.setattr(RunFacts, "finish_unit", finish)
    run = native_case.create()
    result = native_case.wait(run)
    assert lost
    assert result["status"] == "completed"
    assert result["usage"]["model_calls"] == 28
    assert result["usage"]["total_tokens"] == 311
    assert len(native_case.models) == 4
    native_case.wait(run, lambda r: r["archive_status"] == "saved")
    events = native_case.client.read_events(run["run_id"], "alice", 0, 1000)
    assert (
        sum(
            e["type"] == "agent_report" and e.get("role") == "response_strategy"
            for e in events
        )
        == 1
    )


def test_tool_control_storage_errors_are_fatal_not_model_observations(
    native_case, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    from open_deep_research.workflow.run_facts import RunFacts

    original = RunFacts.operation

    def fail(repo, unit, permit, identity, status="started"):
        if status == "started":
            raise OperationalError(
                "Synthetic admission outage", None, Exception("unavailable")
            )
        return original(repo, unit, permit, identity, status)

    monkeypatch.setattr(RunFacts, "operation", fail)
    run = native_case.create()
    result = native_case.wait(run)
    assert result["status"] == "failed"
    assert result["error"]["kind"] == "execution_control_error"
    assert not result["report"]
    assert not native_case.calls
    assert len(native_case.models) == 2
    assert all(rt.budget_usage["model_calls"] == 1 for rt in native_case.runtimes)


def test_cancel_of_safe_retry_cannot_release_an_active_unsettled_unit(
    native_case, monkeypatch
):
    import threading

    from open_deep_research.workflow import dbos_workflow as engine

    entered = threading.Event()
    attempts = []

    async def review(state, config):
        if not attempts:
            attempts.append("failed")
            raise RuntimeError("Safe review failure")
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setitem(engine.OPERATIONS, "research_review", review)
    case = native_case
    run = case.create()
    failed = case.wait(
        run, lambda r: r["status"] == "failed" and r["archive_status"] == "saved"
    )
    assert failed["retry_safe"]
    case.client.retry(run["run_id"], "alice", "retry", failed["state_version"])
    end = time.monotonic() + 10
    while not entered.is_set() and time.monotonic() < end:
        time.sleep(0.05)
    assert entered.is_set()
    assert facts().unresolved(run["run_id"])
    assert facts().request(run["run_id"])["occupied"]
    case.client.cancel(run["run_id"], "alice")
    case.client.cancel(run["run_id"], "alice")
    result = case.wait(run)
    assert result["status"] == "cancelled"
    assert result["settlement"] == "settled"
    assert len(case.models) == 2
    assert not result["retry_safe"]


def test_native_workflow_matches_frozen_business_reference(
    native_case, monkeypatch, tmp_path
):
    from reference_workflow import PythonWorkflow
    from test_python_workflow_sdk import install_sdk

    case = native_case
    run = case.create()
    case.wait(run, lambda r: r["archive_status"] == "saved")
    native = case.client.client.list_workflows(
        workflow_ids=[run["run_id"]], load_input=False, load_output=True
    )[0].output
    with monkeypatch.context() as patch:
        state, cfg, *_ = install_sdk(patch, tmp_path / "reference")
        reference = asyncio.run(PythonWorkflow().ainvoke(state, cfg))
    assert native["workflow"] == reference["workflow"]
    assert native["report"] == reference["report"]
    assert native["runtime"]["budget"] == reference["runtime"]["budget"]
    for role in (
        "public_signal",
        "internal_knowledge",
        "risk_assessment",
        "response_strategy",
    ):
        assert native["agents"][role] == reference["agents"][role]


def test_message_identity_generation_stays_outside_workflow_replay(
    native_case, monkeypatch
):
    from dbos import DBOS

    from open_deep_research.workflow import python_state

    original = python_state.uuid4
    observed = []

    def identity():
        in_workflow = DBOS.workflow_id is not None
        in_step = DBOS.step_id is not None
        assert not in_workflow or in_step, (
            "Random message ID generated by Workflow replay"
        )
        observed.append((in_workflow, in_step))
        return original()

    monkeypatch.setattr(python_state, "uuid4", identity)
    case = native_case
    case.state["messages"][0].id = None
    run = case.create()
    result = case.wait(run)
    assert result["status"] == "completed", result["error"]
    assert (False, False) in observed
    assert (True, True) in observed
    final = case.client.client.list_workflows(
        workflow_ids=[run["run_id"]], load_input=False, load_output=True
    )[0].output
    assert all(message.id for message in final["messages"])


def test_operator_approval_cannot_be_bypassed_or_replanned(native_case, monkeypatch):
    case = native_case
    monkeypatch.setenv("ALLOW_PLAN_FEEDBACK", "true")
    run = case.create(approval=False)
    paused = case.wait(run, lambda r: r["status"] == "awaiting_approval")
    assert not case.models
    monkeypatch.setenv("ALLOW_PLAN_FEEDBACK", "false")
    case.client.approve(run["run_id"], "alice", paused["plan"]["version"], "approve")
    result = case.wait(run)
    assert result["status"] == "completed"
    assert result["usage"]["model_calls"] == 28
    assert result["plan"] == paused["plan"]


def test_paused_runs_do_not_starve_another_runs_archive(native_case):
    case = native_case
    runs = [case.create(key="paused-" + str(i), approval=True) for i in range(21)]
    for run in runs:
        case.wait(run, lambda r: r["status"] == "awaiting_approval")
    last = runs[-1]
    plan = case.client.get(last["run_id"], "alice")["plan"]
    case.client.approve(last["run_id"], "alice", plan["version"], "approve")
    saved = case.wait(last, lambda r: r["archive_status"] == "saved")
    assert saved["status"] == "completed"
    assert len(case.models) == 4
    assert all(
        case.client.get(r["run_id"], "alice")["status"] == "awaiting_approval"
        for r in runs[:-1]
    )


def test_distinct_retry_keys_cannot_reaccept_one_pending_outcome(
    native_case, monkeypatch
):
    import threading

    from open_deep_research.workflow import dbos_workflow as engine

    case = native_case
    original = engine.OPERATIONS["research_review"]
    entered, release = threading.Event(), threading.Event()
    attempts = []

    async def review(state, cfg):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Safe read failed")
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.02)
        return await original(state, cfg)

    monkeypatch.setitem(engine.OPERATIONS, "research_review", review)
    run = case.create()
    failed = case.wait(
        run, lambda r: r["status"] == "failed" and r["archive_status"] == "saved"
    )
    case.client.retry(run["run_id"], "alice", "first", failed["state_version"])
    try:
        assert entered.wait(15)
        observed = case.client.get(run["run_id"], "alice")
        with pytest.raises(RunConflict):
            case.client.retry(
                run["run_id"], "alice", "second", observed["state_version"]
            )
    finally:
        release.set()
    result = case.wait(run, lambda r: r["status"] == "completed")
    assert result["generation"] == 1
    assert len(attempts) == 2
    assert len(case.models) == 4
