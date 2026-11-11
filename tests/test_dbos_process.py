"""Kill/restart genuine Executors against PostgreSQL and shared Neo4j evidence."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from dbos_support import NEO4J, NEO4J_PASSWORD  # noqa:F401
from test_python_workflow import config, initial

from open_deep_research.web.durable import RunClient
from open_deep_research.workflow.run_facts import facts

pytestmark = pytest.mark.integration


def test_actual_neo4j_outage_keeps_report_and_compensates_without_sdk_replay(
    native_case, monkeypatch
):
    import json

    from fastapi.testclient import TestClient
    from neo4j import GraphDatabase

    from open_deep_research.web import server
    from open_deep_research.web.history import Neo4jHistoryStore

    container = os.environ.get(
        "TEST_DBOS_NEO4J_CONTAINER", "por-dbos-phase4-neo4j-test-1"
    )
    inspected = json.loads(
        subprocess.check_output(["docker", "inspect", container], text=True)
    )[0]
    assert inspected["Config"]["Labels"].get("por-integration") == "dbos-phase4"
    assert not any(m["Type"] == "bind" for m in inspected["Mounts"])
    owned_id = inspected["Id"]
    case = native_case
    original_history = case.history
    bounded = Neo4jHistoryStore(
        GraphDatabase.driver(
            NEO4J,
            auth=("neo4j", NEO4J_PASSWORD),
            connection_timeout=1,
            connection_acquisition_timeout=2,
            max_transaction_retry_time=0.1,
        )
    )
    case.history = bounded
    monkeypatch.setattr(server, "get_history_store", lambda: bounded)
    monkeypatch.setattr(server, "WEB_API_TOKEN", "neo4j-outage-test-token")
    stopped = False
    try:
        subprocess.run(
            ["docker", "stop", "--time", "1", owned_id], check=True, capture_output=True
        )
        stopped = True
        run = case.create(owner="operator")
        completed = case.wait(run)
        assert completed["status"] == "completed"
        assert completed["report"]
        case.wait(
            run,
            lambda r: any(
                e["type"] == "archive_pending"
                for e in case.client.read_events(run["run_id"], "operator", 0, 1000)
            ),
        )
        assert case.client.get(run["run_id"], "operator")["archive_status"] == "pending"
        with TestClient(server.app) as http:
            unavailable = http.get(
                "/api/history",
                headers={"Authorization": "Bearer neo4j-outage-test-token"},
            )
            assert unavailable.status_code == 503
            assert "local-dbos-test-only" not in unavailable.text
        before = len(case.models)
        assert before == 4
        subprocess.run(["docker", "start", owned_id], check=True, capture_output=True)
        stopped = False
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                bounded.driver.verify_connectivity()
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise AssertionError("Isolated Neo4j did not restart")
        saved = case.wait(run, lambda r: r["archive_status"] == "saved", timeout=60)
        record = bounded.get(saved["history_id"], owner="operator")
        assert record["report"] == saved["report"] == completed["report"]
        assert saved["usage"] == completed["usage"]
        assert record["usage"] == {
            k: saved["usage"][k]
            for k in ("model_calls", "input_tokens", "output_tokens", "total_tokens")
        }
        assert len(case.models) == before
        assert any(
            e["type"] == "history_saved"
            for e in case.client.read_events(run["run_id"], "operator", 0, 1000)
        )
    finally:
        if stopped:
            subprocess.run(
                ["docker", "start", owned_id], check=True, capture_output=True
            )
        case.history = original_history
        bounded.close()


def start(directory, executor, scenario="normal", *, queues=None):
    output = (directory / (executor + ".log")).open("w", encoding="utf-8")
    env = {
        **os.environ,
        "PYTHON_DOTENV_DISABLED": "1",
        "OPENAI_AGENTS_DISABLE_TRACING": "1",
    }
    if queues is not None:
        env["TEST_EXECUTOR_QUEUES"] = queues
    for key in list(env):
        if key.endswith(("_API_KEY", "_TOKEN", "_SECRET")):
            env.pop(key)
    proc = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("dbos_process_fixture.py")),
            str(directory),
            "--executor",
            executor,
            "--scenario",
            scenario,
        ],
        env=env,
        stdout=output,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    output.close()
    return proc


def kill_owned(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        # The venv redirector owns a child interpreter; kill only this created tree.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    else:
        process.kill()
    process.wait(timeout=10)


def wait(predicate, process, directory, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        if process.poll() is not None:
            raise AssertionError(
                "\n".join(
                    p.read_text(encoding="utf-8")[-7000:]
                    for p in directory.glob("*.log")
                )
            )
        time.sleep(0.1)
    raise AssertionError(
        "Process did not reach native boundary: "
        + "\n".join(
            p.read_text(encoding="utf-8")[-4000:] for p in directory.glob("*.log")
        )
    )


def effect_count(directory, table="invocations"):
    if not (directory / "external.db").exists():
        return 0
    with sqlite3.connect(directory / "external.db") as db:
        return db.execute("SELECT count(*) FROM " + table).fetchone()[0]


def plan_prompts(directory):
    with sqlite3.connect(directory / "planning.db") as db:
        return [
            row[0] for row in db.execute("SELECT prompt FROM plan_requests ORDER BY id")
        ]


def configuration(directory):
    return config(
        research_graph_enabled=True,
        research_graph_backend="neo4j",
        research_graph_uri=NEO4J,
        research_graph_username="neo4j",
        research_graph_password=NEO4J_PASSWORD,
        research_graph_transcript_dir=str(directory / "t"),
        recent_raw_steps=0,
        allow_clarification=True,
        research_model="fixture:review",
        max_structured_output_retries=1,
        budget_enabled=True,
        max_model_calls=100,
    )


@pytest.mark.parametrize(
    "scenario", ["checkpoint", "lost-receipt", "legacy-checkpoint", "legacy-artifact"]
)
def test_process_kill_journal_recovery_never_replays_unknown_tool(
    pg_database, tmp_path, scenario
):
    client = RunClient()
    executor = "fault-" + uuid4().hex
    directory = tmp_path
    process = start(directory, executor, scenario)
    restart = None
    try:
        wait(lambda: (directory / "executor-ready").exists(), process, directory)
        run = client.create(
            "alice",
            "kill",
            {"topic": "brand recall"},
            configuration(directory),
            initial(),
        )[0]
        # Both producer effects must be in the lost-ack window before the kill.
        # A sibling's first call after recovery would otherwise change the count.
        markers = (
            ("lost-receipt-public_signal", "lost-receipt-internal_knowledge")
            if scenario == "lost-receipt"
            else (scenario,)
        )
        wait(
            lambda: all((directory / name).exists() for name in markers),
            process,
            directory,
        )
        if scenario.startswith("legacy-"):
            wait(
                lambda: sum(
                    bool(u["artifact"])
                    for u in facts().units(run["run_id"])
                    if u["name"] in {"public_signal_agent", "internal_knowledge_agent"}
                )
                == 2,
                process,
                directory,
            )
            producers = [
                u
                for u in facts().units(run["run_id"])
                if u["name"] in {"public_signal_agent", "internal_knowledge_agent"}
            ]
            assert all(
                "patch" in u["artifact"] and "research_result" not in u["artifact"]
                for u in producers
            )
            committed_usage = {u["id"]: u["usage"] for u in producers}
            assert effect_count(directory, "models") == 4
        kill_owned(process)
        before = effect_count(directory)
        assert before == 2
        assert effect_count(directory, "effects") == before
        (directory / (scenario + ".release")).write_text("continue")
        (directory / "executor-ready").unlink()
        restart = start(directory, executor, scenario)
        wait(
            lambda: client.get(run["run_id"], "alice")["status"]
            in {"completed", "failed", "interrupted"},
            restart,
            directory,
        )
        result = client.get(run["run_id"], "alice")
        if scenario != "lost-receipt":
            assert result["status"] == "completed", result["error"]
            assert result["report"]
            assert result["usage"]["model_calls"] == 26
            if scenario.startswith("legacy-"):
                assert effect_count(directory, "models") == 6
                after = {u["id"]: u["usage"] for u in facts().units(run["run_id"])}
                assert all(
                    after[key] == usage for key, usage in committed_usage.items()
                )
                stages = {r["role"] for r in result["role_reports"]}
                assert {"public_signal", "internal_knowledge"} <= stages
        else:
            assert result["status"] == "interrupted"
            assert result["settlement"] == "unresolved"
            assert not result["retry_safe"]
            assert facts().unresolved(run["run_id"])
            assert facts().request(run["run_id"])["occupied"]
            from open_deep_research.web.run_data import RunConflict

            with pytest.raises(RunConflict):
                client.reconcile(
                    run["run_id"],
                    "alice",
                    result["state_version"],
                    worker_stopped=False,
                    note="not yet stopped",
                )
            kill_owned(restart)
            restart = None
            observed = client.get(run["run_id"], "alice")
            client.reconcile(
                run["run_id"],
                "alice",
                observed["state_version"],
                worker_stopped=True,
                note="Both owned Executor process trees stopped; synthetic external receipt checked",
            )
            assert not facts().request(run["run_id"])["occupied"]
            assert not client.get(run["run_id"], "alice")["retry_safe"]
            with pytest.raises(RunConflict):
                client.retry(
                    run["run_id"], "alice", "unsafe-retry", observed["state_version"]
                )
        assert effect_count(directory) == before
        assert effect_count(directory, "effects") == before
    finally:
        for child in (process, restart):
            if child and child.poll() is None:
                kill_owned(child)


def test_approval_process_restart_uses_committed_plan(pg_database, tmp_path):
    client = RunClient()
    directory = tmp_path
    executor = "approval-" + uuid4().hex
    cfg = configuration(directory)
    cfg["configurable"]["allow_plan_feedback"] = True
    process = start(directory, executor)
    restart = None
    try:
        wait(lambda: (directory / "executor-ready").exists(), process, directory)
        run = client.create(
            "alice", "approval", {"topic": "brand recall"}, cfg, initial()
        )[0]
        wait(
            lambda: client.get(run["run_id"], "alice")["status"] == "awaiting_approval",
            process,
            directory,
        )
        plan = client.get(run["run_id"], "alice")["plan"]
        units_before = [u["id"] for u in facts().units(run["run_id"])]
        kill_owned(process)
        client.approve(run["run_id"], "alice", plan["version"], "approve")
        (directory / "executor-ready").unlink()
        restart = start(directory, executor)
        wait(
            lambda: client.get(run["run_id"], "alice")["status"] == "completed",
            restart,
            directory,
        )
        result = client.get(run["run_id"], "alice")
        assert result["plan"] == dict(plan, locked=True)
        assert result["usage"]["model_calls"] == 26
        assert set(units_before) <= {u["id"] for u in facts().units(run["run_id"])}
        assert effect_count(directory) == 2
    finally:
        for child in (process, restart):
            if child and child.poll() is None:
                kill_owned(child)


def test_live_old_executor_is_fenced_after_public_recovery(pg_database, tmp_path):
    from open_deep_research.workflow.dbos_config import CONTROL, RECOVERY, VERSION

    client = RunClient()
    directory = tmp_path
    old_id = "old-" + uuid4().hex
    new_id = "new-" + uuid4().hex
    old = start(directory, old_id, queues="por-control,por-business,por-archive")
    new = None
    try:
        wait(lambda: (directory / "executor-ready").exists(), old, directory)
        cfg = configuration(directory)
        cfg["configurable"]["allow_plan_feedback"] = True
        run = client.create(
            "alice", "old-alive", {"topic": "brand recall"}, cfg, initial()
        )[0]
        wait(
            lambda: client.get(run["run_id"], "alice")["status"] == "awaiting_approval",
            old,
            directory,
        )
        probe = run["run_id"] + ":ownership-probe"
        client.client.enqueue(
            {
                "workflow_name": "por.test.ownership",
                "queue_name": CONTROL,
                "workflow_id": probe,
                "app_version": VERSION,
            },
            {"run_id": run["run_id"], "directory": str(directory)},
        )
        wait(lambda: (directory / "old-active").exists(), old, directory)
        assert (directory / "old-active").read_text() == old_id
        assert old.poll() is None
        client.client.cancel_workflow(probe, cancel_children=True)
        client.client.resume_workflow(probe, queue_name=RECOVERY)
        new = start(directory, new_id, queues=RECOVERY)
        wait(lambda: client.native(probe).status == "ERROR", new, directory)
        assert client.native(probe).executor_id == new_id
        assert old.poll() is None
        (directory / "ownership.release").write_text("continue")
        wait(lambda: (directory / "old-denied").exists(), old, directory)
        assert facts().unresolved(run["run_id"])
        assert effect_count(directory) == 0
    finally:
        for process in (old, new):
            if process:
                kill_owned(process)


@pytest.mark.parametrize(
    "pg_database", [{"RUN_MAX_ACTIVE": 2, "RUN_OWNER_ACTIVE": 1}], indirect=True
)
def test_two_executors_enforce_global_owner_session_reservations(pg_database, tmp_path):
    from sqlalchemy import text

    from open_deep_research.workflow.dbos_config import BUSINESS, VERSION

    client = RunClient()
    directory = tmp_path
    first = start(directory, "queue-a-" + uuid4().hex)
    second = None
    try:
        wait(lambda: (directory / "executor-ready").exists(), first, directory)
        cfg = configuration(directory)
        cfg["configurable"]["allow_plan_feedback"] = True
        a = client.create("alice", "a", {"topic": "a"}, cfg, initial())[0]
        same = client.create(
            "alice",
            "same",
            {"topic": "same"},
            cfg,
            initial(),
            session_id=a["research_session_id"],
        )[0]
        independent = client.create(
            "alice", "independent", {"topic": "independent"}, cfg, initial()
        )[0]
        b = client.create("bob", "b", {"topic": "b"}, cfg, initial())[0]
        c = client.create("charlie", "c", {"topic": "c"}, cfg, initial())[0]
        all_runs = [a, same, independent, b, c]
        wait(
            lambda: all(
                client.get(r["run_id"], r["owner"])["status"] == "awaiting_approval"
                for r in all_runs
            ),
            first,
            directory,
        )
        second = start(directory, "queue-b-" + uuid4().hex)
        for run in all_runs:
            client.client.enqueue(
                {
                    "workflow_name": "por.test.queue",
                    "queue_name": BUSINESS,
                    "workflow_id": run["run_id"] + ":probe",
                    "app_version": VERSION,
                    "queue_partition_key": run["owner"],
                },
                {"run_id": run["run_id"], "directory": str(directory)},
            )
        wait(lambda: len(list(directory.glob("*.entered"))) == 2, first, directory)
        time.sleep(1)
        with facts().engine.connect() as db:
            occupied = list(
                db.execute(
                    text(
                        "SELECT owner,session FROM por_domain.requests WHERE occupied=true"
                    )
                ).mappings()
            )
        assert len(occupied) == 2
        assert len({r["owner"] for r in occupied}) == 2
        assert len({r["session"] for r in occupied}) == 2
        assert len(list(directory.glob("*.entered"))) == 2
        # Release an admitted unit and verify another queued delivery progresses.
        entered = next(directory.glob("*.entered"))
        (directory / (entered.stem + ".release")).write_text("continue")
        wait(lambda: len(list(directory.glob("*.entered"))) >= 3, first, directory)
    finally:
        for process in (first, second):
            if process:
                kill_owned(process)


def test_actual_postgresql_outage_fails_closed(pg_database, tmp_path, monkeypatch):
    import json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from open_deep_research.web import runs, server

    container = os.environ.get(
        "TEST_DBOS_POSTGRES_CONTAINER", "por-dbos-phase4-postgres-1"
    )
    inspected = json.loads(
        subprocess.check_output(["docker", "inspect", container], text=True)
    )[0]
    assert inspected["Config"]["Labels"].get("por-integration") == "dbos-phase4"
    owned_id = inspected["Id"]
    assert not any(m["Type"] == "bind" for m in inspected["Mounts"])
    client = RunClient()
    directory = tmp_path
    executor = "outage-" + uuid4().hex
    process = start(directory, executor, "model-wait")
    stopped = False
    app = FastAPI()
    app.include_router(runs.router)
    monkeypatch.setattr(runs, "get_run_client", lambda: client)
    monkeypatch.setattr(server, "WEB_API_TOKEN", "outage-test-token")
    try:
        wait(lambda: (directory / "executor-ready").exists(), process, directory)
        run = client.create(
            "operator",
            "outage",
            {"topic": "brand recall"},
            configuration(directory),
            initial(),
        )[0]
        wait(lambda: (directory / "model-wait").exists(), process, directory)
        wait(
            lambda: any(
                u["name"] == "internal_knowledge_agent" and u["artifact"]
                for u in facts().units(run["run_id"])
            ),
            process,
            directory,
        )
        before = effect_count(directory)
        assert before == 1
        subprocess.run(
            ["docker", "stop", "--time", "1", owned_id], check=True, capture_output=True
        )
        stopped = True
        with TestClient(app) as http:
            response = http.get(
                "/api/runs/" + run["run_id"],
                headers={"Authorization": "Bearer outage-test-token"},
            )
            assert response.status_code == 503
            assert "completed" not in response.text
        (directory / "model-wait.release").write_text("continue")
        # Establish a real failed SDK control read, not merely a connection
        # that waits for PostgreSQL to return and then legitimately succeeds.
        wait(
            lambda: (directory / "control-failed").exists(),
            process,
            directory,
            timeout=15,
        )
        assert effect_count(directory) == before
        subprocess.run(["docker", "start", owned_id], check=True, capture_output=True)
        stopped = False

        def restored():
            try:
                state = client.get(run["run_id"], "operator")
            except Exception:
                return False
            if state["status"] == "completed":
                pytest.fail(
                    "Storage admission failure was masked as SDK tool success: "
                    + str(state["error"])
                )
            return state["status"] in {"failed", "interrupted"}

        wait(restored, process, directory, timeout=90)
        result = client.get(run["run_id"], "operator")
        assert not result["report"]
        assert result["status"] != "completed"
        assert effect_count(directory) == before
    finally:
        if stopped:
            subprocess.run(
                ["docker", "start", owned_id], check=True, capture_output=True
            )
        kill_owned(process)


def test_gap_replay_keeps_child_order_and_committed_rounds(pg_database, tmp_path):
    client = RunClient()
    executor = "gap-" + uuid4().hex
    directory = tmp_path
    process = start(directory, executor, "gap-checkpoint")
    restart = None
    try:
        wait(lambda: (directory / "executor-ready").exists(), process, directory)
        run = client.create(
            "alice",
            "gap",
            {"topic": "brand recall"},
            configuration(directory),
            initial(),
        )[0]
        wait(lambda: (directory / "gap-checkpoint").exists(), process, directory)
        wait(
            lambda: any(
                u["id"].endswith(":internal_knowledge:2") and u["artifact"]
                for u in facts().units(run["run_id"])
            ),
            process,
            directory,
        )
        committed = {
            u["id"]: u["usage"] for u in facts().units(run["run_id"]) if u["artifact"]
        }
        assert effect_count(directory) == 3
        kill_owned(process)
        (directory / "gap-checkpoint.release").write_text("continue")
        (directory / "executor-ready").unlink()
        restart = start(directory, executor, "gap-checkpoint")
        wait(
            lambda: client.get(run["run_id"], "alice")["status"]
            in {"completed", "failed", "interrupted"},
            restart,
            directory,
        )
        result = client.get(run["run_id"], "alice")
        assert result["status"] == "completed", result["error"]
        final = client.client.list_workflows(
            workflow_ids=[run["run_id"]], load_input=False, load_output=True
        )[0].output
        assert final["workflow"]["round"] == 2
        assert {t.task_id for t in final["workflow"]["completed_tasks"]} == {
            "date",
            "approval",
        }
        assert effect_count(directory) == effect_count(directory, "effects") == 4
        after = {u["id"]: u["usage"] for u in facts().units(run["run_id"])}
        assert all(after[key] == value for key, value in committed.items())
    finally:
        for child in (process, restart):
            if child:
                kill_owned(child)


@pytest.mark.parametrize(
    "scenario", ["revision-queued", "revision-ack", "revision-unconfirmed"]
)
def test_brief_revision_executor_recovery(pg_database, tmp_path, scenario):
    from open_deep_research.web.run_data import RunConflict

    client = RunClient()
    executor = "brief-revision-" + uuid4().hex
    process = start(tmp_path, executor, scenario)
    restart = None
    try:
        wait(lambda: (tmp_path / "executor-ready").exists(), process, tmp_path)
        cfg = configuration(tmp_path)
        cfg["configurable"]["allow_plan_feedback"] = True
        run_id = client.create(
            "alice", "revision-kill", {"topic": "brand recall"}, cfg, initial()
        )[0]["run_id"]

        def snapshot():
            return client.get(run_id, "alice")

        wait(lambda: snapshot()["status"] == "awaiting_approval", process, tmp_path)
        old = snapshot()["plan"]
        feedback = "Compare documented competitor product quality."
        child_id = run_id + ":brief-revision:1"

        def submit():
            return client.approve(
                run_id,
                "alice",
                old["version"],
                "revise",
                feedback=feedback,
                revision_attempt=0,
            )

        def journal():
            return [
                step
                for step in client.client.list_workflow_steps(child_id)
                if step["function_name"] == "por.llm"
            ]

        if scenario == "revision-queued":
            kill_owned(process)
            assert submit()["plan"]["ai_revision"]["pending"]
            assert submit()["plan"]["ai_revision"]["pending"]
            assert not (tmp_path / "revision-calls").exists()
        else:
            submit()
            wait(lambda: (tmp_path / scenario).exists(), process, tmp_path)
            assert snapshot()["plan"]["ai_revision"]["pending"]
            assert facts().request(run_id)["occupied"]
            kill_owned(process)
            assert (tmp_path / "revision-calls").read_text().splitlines() == ["call"]

        committed = journal()
        if scenario == "revision-ack":
            assert len(committed) == 1 and committed[0]["output"]
        else:
            assert not committed

        (tmp_path / (scenario + ".release")).write_text("resume")
        (tmp_path / "executor-ready").unlink()
        restart = start(tmp_path, executor, scenario)
        if scenario == "revision-unconfirmed":
            wait(lambda: snapshot()["archive_status"] == "saved", restart, tmp_path)
            done = snapshot()
            assert (
                done["status"] == "interrupted" and done["settlement"] == "unresolved"
            )
            assert done["plan"]["version"] == old["version"]
            assert done["plan"]["ai_revision"]["used"] == 0 and not done["retry_safe"]
            assert facts().request(run_id)["occupied"]
            with pytest.raises(RunConflict):
                client.approve(run_id, "alice", old["version"], "approve")
        else:
            wait(
                lambda: snapshot()["status"] == "awaiting_approval"
                and snapshot()["plan"]["ai_revision"]["used"] == 1,
                restart,
                tmp_path,
            )
            plan = snapshot()["plan"]
            assert plan["revision"] == old["revision"] + 1 and feedback in plan["brief"]
            assert plan["expires_at"] == old["expires_at"]
            assert not facts().request(run_id)["occupied"]
            if committed:
                assert journal() == committed
            assert submit()["plan"]["version"] == plan["version"]
            client.approve(run_id, "alice", plan["version"], "approve")
            wait(lambda: snapshot()["archive_status"] == "saved", restart, tmp_path)
            assert snapshot()["status"] == "completed" and snapshot()["plan"]["locked"]
        assert (tmp_path / "revision-calls").read_text().splitlines() == ["call"]
    finally:
        kill_owned(process)
        if restart:
            kill_owned(restart)


@pytest.mark.parametrize("scenario", ["planning-release", "clarification-deadline"])
@pytest.mark.parametrize("pg_database", [{"RUN_APPROVAL_SECONDS": 6}], indirect=True)
def test_planning_recovers_before_waiting_for_clarification(
    pg_database, tmp_path, scenario
):
    client = RunClient()
    executor = "pre-clarification-" + uuid4().hex
    process = start(tmp_path, executor, scenario)
    restart = None
    try:
        wait(lambda: (tmp_path / "executor-ready").exists(), process, tmp_path)
        run_id = client.create(
            "alice",
            "pre-clarification",
            {"topic": "ambiguous brand"},
            configuration(tmp_path),
            initial(),
        )[0]["run_id"]

        def pending(number):
            challenge = client.get(run_id, "alice")["clarification"]
            return challenge if challenge and challenge["round"] == number else None

        if scenario == "planning-release":
            wait(lambda: pending(1), process, tmp_path)
            client.clarify(run_id, "alice", pending(1)["version"], {"brand": "Brand A"})
        wait(lambda: (tmp_path / scenario).exists(), process, tmp_path)
        kill_owned(process)
        assert not facts().request(run_id)["occupied"]
        if scenario == "clarification-deadline":
            challenge = client.client.get_event(
                run_id, "clarification", timeout_seconds=0
            )
            time.sleep(max(0, challenge["expires_at"] - time.time()) + 0.1)
            assert client.get(run_id, "alice")["clarification"] is None

        (tmp_path / (scenario + ".release")).write_text("resume")
        (tmp_path / "executor-ready").unlink()
        restart = start(tmp_path, executor, scenario)
        if scenario == "planning-release":
            wait(lambda: pending(2), restart, tmp_path)
            assert not facts().request(run_id)["occupied"]
            assert len(plan_prompts(tmp_path)) == 2
            client.clarify(run_id, "alice", pending(2)["version"], {}, skip=True)

        wait(
            lambda: client.get(run_id, "alice")["archive_status"] == "saved",
            restart,
            tmp_path,
        )
        done = client.get(run_id, "alice")
        if scenario == "planning-release":
            assert done["status"] == "completed"
            assert len(plan_prompts(tmp_path)) == 3
        else:
            assert done["status"] == "cancelled" and done["plan"] is None
            assert len(plan_prompts(tmp_path)) == 1
            assert effect_count(tmp_path) == 0
    finally:
        kill_owned(process)
        if restart:
            kill_owned(restart)


@pytest.mark.parametrize("crash_round", [1, 2, 3])
def test_clarification_wait_survives_executor_kill_and_same_run_answer(
    pg_database, tmp_path, monkeypatch, crash_round
):
    """Replay a real recv and model journal with the original Executor identity."""
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient
    from test_python_workflow_sdk import CLARIFICATION_ANSWERS

    from open_deep_research.web import server

    owner = "operator"
    token = "test-clarification-http-only"
    monkeypatch.setattr(server, "WEB_API_TOKEN", token)
    scenario = "clarification" if crash_round == 1 else "multi-clarification"
    total_rounds = 1 if crash_round == 1 else 3
    client = RunClient()
    executor = "brief-wait-" + uuid4().hex
    process = start(tmp_path, executor, scenario)
    restart = None
    try:
        wait(lambda: (tmp_path / "executor-ready").exists(), process, tmp_path)
        run_id = client.create(
            owner,
            "clarification-kill",
            {"topic": "ambiguous brand"},
            configuration(tmp_path),
            initial(),
        )[0]["run_id"]
        versions = []

        def pending_at(number, executor_process):
            wait(
                lambda: (client.get(run_id, owner)["clarification"] or {}).get("round")
                == number,
                executor_process,
                tmp_path,
            )
            pending = client.get(run_id, owner)
            assert pending["status"] == "awaiting_clarification"
            assert not facts().request(run_id)["occupied"]
            return pending["clarification"]

        def steps(workflow_id, name):
            return [
                step
                for step in client.client.list_workflow_steps(workflow_id)
                if step["function_name"] == name
            ]

        for number in range(1, crash_round + 1):
            versions.append(pending_at(number, process)["version"])
            if number < crash_round:
                client.clarify(
                    run_id, owner, versions[-1], CLARIFICATION_ANSWERS[number - 1]
                )
        wait(
            lambda: len(steps(run_id, "DBOS.sleep")) == crash_round,
            process,
            tmp_path,
        )
        units = [u["id"] for u in facts().units(run_id)]
        committed = {
            f"{run_id}:planning:{number}": steps(
                f"{run_id}:planning:{number}", "por.llm"
            )
            for number in range(1, crash_round + 1)
        }
        assert all(
            len(journal) == 1 and journal[0]["output"] for journal in committed.values()
        )

        kill_owned(process)
        assert effect_count(tmp_path) == 0
        assert len(plan_prompts(tmp_path)) == crash_round
        assert client.get(run_id, owner)["clarification"]["version"] == versions[-1]
        if crash_round == 2:
            # Both identical HTTP requests arrive before the Executor can recv.
            # The second must acknowledge the first transaction without sending again.
            with TestClient(
                server.app, headers={"Authorization": f"Bearer {token}"}
            ) as http:
                path = f"/api/runs/{run_id}"
                body = {"version": versions[-1], "answers": CLARIFICATION_ANSWERS[1]}
                with ThreadPoolExecutor(2) as pool:
                    responses = list(
                        pool.map(
                            lambda _: http.post(path + "/clarification", json=body),
                            range(2),
                        )
                    )
                assert [response.status_code for response in responses] == [200, 200]
                for response in responses:
                    assert response.json()["run_id"] == run_id
                    assert response.json()["clarification"] is None
                assert http.get(path).json()["clarification"] is None
        (tmp_path / "executor-ready").unlink()
        restart = start(tmp_path, executor, scenario)
        wait(lambda: (tmp_path / "executor-ready").exists(), restart, tmp_path)
        if crash_round != 2:
            restored = pending_at(crash_round, restart)
            assert restored["version"] == versions[-1]
            assert [u["id"] for u in facts().units(run_id)] == units
            assert len(plan_prompts(tmp_path)) == crash_round
            client.clarify(
                run_id, owner, versions[-1], CLARIFICATION_ANSWERS[crash_round - 1]
            )
        for number in range(crash_round + 1, total_rounds + 1):
            versions.append(pending_at(number, restart)["version"])
            client.clarify(
                run_id, owner, versions[-1], CLARIFICATION_ANSWERS[number - 1]
            )

        wait(
            lambda: client.get(run_id, owner)["archive_status"] == "saved",
            restart,
            tmp_path,
        )
        result = client.get(run_id, owner)
        assert result["status"] == "completed" and result["events_complete"]
        assert result["plan"]["locked"] and effect_count(tmp_path) == 2
        assert result["completed_steps"].count("plan_research") == total_rounds + 1
        assert [s["output"]["version"] for s in steps(run_id, "DBOS.recv")] == versions
        for workflow_id, journal in committed.items():
            assert steps(workflow_id, "por.llm") == journal
        prompts = plan_prompts(tmp_path)
        assert len(prompts) == total_rounds + 1
        for number, prompt in enumerate(prompts):
            assert prompt.count("\nUser response: ") == number
            for answers in CLARIFICATION_ANSWERS[:number]:
                assert next(iter(answers.values())) in prompt
    finally:
        kill_owned(process)
        if restart:
            kill_owned(restart)
