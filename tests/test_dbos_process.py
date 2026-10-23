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


@pytest.mark.parametrize("scenario", ["checkpoint", "lost-receipt"])
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
        wait(lambda: (directory / scenario).exists(), process, directory)
        kill_owned(process)
        before = effect_count(directory)
        assert before >= 1
        if scenario == "checkpoint":
            assert before == 2
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
        if scenario == "checkpoint":
            assert result["status"] == "completed", result["error"]
            assert result["report"]
            assert result["usage"]["model_calls"] == 28
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
        assert result["plan"] == plan
        assert result["usage"]["model_calls"] == 28
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
