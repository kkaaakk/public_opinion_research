"""Actual PostgreSQL/executor faults; framework execution is never mocked."""

from __future__ import annotations

import env  # isort: skip

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from api import Gateway, make_app
from codec import NativeJSON
from dbos import DBOSClient
from domain import Domain
from fastapi.testclient import TestClient

HERE = Path(__file__).parent
pytestmark = pytest.mark.skipif(
    os.environ.get("DBOS_NATIVE_LIVE") != "1",
    reason="Explicit isolated PostgreSQL/Neo4j POC",
)


def client():
    from sqlalchemy import create_engine

    sql = create_engine(
        env.DB_URL,
        pool_pre_ping=True,
        pool_timeout=2,
        connect_args={"connect_timeout": 2},
    )
    return DBOSClient(
        system_database_engine=sql,
        application_name=env.APP_NAME,
        serializer=NativeJSON(),
        retry_connection_errors=False,
    )


def wait(predicate, timeout=75):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    pytest.fail("Native POC condition timed out")


class Executor:
    def __init__(
        self,
        directory,
        run_id,
        *,
        mode="run",
        route="A",
        scenario="normal",
        fault="",
        fault_role="",
        executor=None,
        approval=False,
        approval_seconds=120,
        probes=False,
    ):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        executor = executor or "exec-" + run_id
        args = [
            sys.executable,
            str(HERE / "executor.py"),
            mode,
            str(directory),
            "--run-id",
            run_id,
            "--route",
            route,
            "--scenario",
            scenario,
            "--executor",
            executor,
            "--approval-seconds",
            str(approval_seconds),
        ]
        if probes:
            args.append("--probes")
        if approval:
            args += ["--approval"]
        child_env = {
            k: v
            for k, v in os.environ.items()
            if not k.endswith(("_API_KEY", "_TOKEN", "_PASSWORD", "_SECRET"))
        }
        child_env.update(
            NATIVE_FAULT=fault,
            NATIVE_FAULT_ROLE=fault_role,
            NATIVE_LISTEN_QUEUES="control,business"
            if executor.startswith("old-")
            else "control,business,recovery",
        )
        self.log = directory / f"{mode}-{time.time_ns()}.log"
        self.output = self.log.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            args,
            env=child_env,
            cwd=env.ROOT,
            stdout=self.output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    def ready(self, name="ready"):
        return wait(
            lambda: (self.directory / name).exists()
            or (self.process.poll() is not None and self.fail())
        )

    def fail(self):
        pytest.fail(self.log.read_text(encoding="utf-8")[-5000:])

    def finish(self):
        try:
            self.process.wait(timeout=130)
            self.output.close()
            assert self.process.returncode == 0, self.log.read_text(encoding="utf-8")[
                -5000:
            ]
        finally:
            self.close()

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)
        self.output.close()


def run_id():
    return "native_" + uuid4().hex


def value(directory):
    return json.loads((directory / "result.json").read_text(encoding="utf-8"))


def assert_success(directory, id):
    result = value(directory)
    assert "error" not in result, result
    assert (directory / "status.txt").read_text() == "SUCCESS"
    assert "FINAL brand recall report" in result["report"]
    facts = Domain(env.DB_URL).facts(id, "harness")
    assert {f["role"] for f in facts} == {
        "public_signal",
        "internal_knowledge",
        "risk_assessment",
        "response_strategy",
    }
    assert all(f["settled"] and f["filter_calls"] and f["evidence"] for f in facts)
    context = Domain(env.DB_URL).facts(id, "report_context")[-1]
    assert context["evidence_nodes"] > 0 and not context["private_memory"]
    return result, facts


@pytest.mark.parametrize("route", ["A", "B"])
@pytest.mark.parametrize("scenario", ["normal", "gap", "rolling"])
def test_four_roles_native_queue_parallel_gap_budget_harness(route, scenario, tmp_path):
    id = run_id()
    child = Executor(tmp_path, id, route=route, scenario=scenario)
    try:
        child.finish()
        result, facts = assert_success(tmp_path, id)
        assert Domain(env.DB_URL).count_effects(id) == (
            4 if scenario == "gap" else 50 if scenario == "rolling" else 2
        )
        if scenario == "gap":
            assert result["round"] == 2
        if scenario == "rolling":
            assert sum(f["compactions"] for f in facts) > 0
        assert result["budget"]["model_calls"] > 0
        if scenario == "normal":
            assert {
                k: result["budget"][k]
                for k in (
                    "model_calls",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                )
            } == {
                "model_calls": 20,
                "input_tokens": 158,
                "output_tokens": 66,
                "total_tokens": 224,
            }
        assert (
            result["budget"]["total_tokens"]
            == result["budget"]["input_tokens"] + result["budget"]["output_tokens"]
        )
    finally:
        child.close()


@pytest.mark.parametrize("point", ["producer_complete", "lost_receipt"])
def test_kill_restart_skips_completed_children_and_rejects_unknown(point, tmp_path):
    id = run_id()
    first = Executor(
        tmp_path,
        id,
        fault=point,
        fault_role="public_signal" if point == "lost_receipt" else "",
    )
    try:
        first.ready()
        assert Domain(env.DB_URL).count_effects(id) == 2
    finally:
        first.close()
    second = Executor(tmp_path, id, mode="recover")
    try:
        second.finish()
        assert Domain(env.DB_URL).count_effects(id) == 2
        assert len(Domain(env.DB_URL).facts(id, "tool_invoker")) == 2
        if point == "producer_complete":
            assert_success(tmp_path, id)
            assert len(Domain(env.DB_URL).facts(id, "role_enter")) == 4
        else:
            assert (tmp_path / "status.txt").read_text() == "ERROR"
            assert "Unknown external effects" in value(tmp_path)["message"]
    finally:
        second.close()


@pytest.mark.parametrize("decision", ["approve", "reject", "expire"])
def test_native_approval_restart_reject_timeout_no_replanning(decision, tmp_path):
    id = run_id()
    c = client()
    first = Executor(
        tmp_path, id, approval=True, approval_seconds=3 if decision == "expire" else 120
    )
    second = None
    try:
        plan = wait(lambda: c.get_event(id, "plan", timeout_seconds=0))
        if decision == "approve":
            first.close()
            second = Executor(tmp_path, id, mode="recover")
        if decision != "expire":
            g = Gateway()
            g.approve(id, "alice", plan["version"], decision)
        (second or first).finish()
        assert len(Domain(env.DB_URL).facts(id, "planning")) == 1
        if decision == "approve":
            assert_success(tmp_path, id)
        else:
            assert (tmp_path / "status.txt").read_text() == "ERROR"
            assert Domain(env.DB_URL).count_effects(id) == 0
    finally:
        first.close()
        if second:
            second.close()
        c.destroy()


@pytest.mark.parametrize("point", ["model_wait", "tool_wait"])
def test_cancel_propagates_to_real_sdk_and_keeps_effect_facts(point, tmp_path):
    id = run_id()
    c = client()
    child = Executor(tmp_path, id, fault=point, fault_role="public_signal")
    try:
        child.ready()
        c.cancel_workflow(id, cancel_children=True)
        child.finish()
        assert (tmp_path / "status.txt").read_text() == "CANCELLED"
        facts = Domain(env.DB_URL).facts(id, "harness")
        assert facts and all(f["settled"] for f in facts)
        assert Domain(env.DB_URL).count_effects(id) == 2
        if point == "tool_wait":
            from sqlalchemy import text

            with Domain(env.DB_URL).engine.connect() as db:
                assert (
                    db.execute(
                        text(
                            "SELECT count(*) FROM native_domain.operations WHERE role_id LIKE :r AND status='uncertain'"
                        ),
                        {"r": id + "%"},
                    ).scalar_one()
                    == 1
                )
    finally:
        child.close()
        c.destroy()


def test_api_owner_idempotency_approval_cas_native_stream_cursor(tmp_path):
    worker = Executor(
        tmp_path / "worker", "worker-placeholder", mode="serve", executor="api-worker"
    )
    c = client()
    g = Gateway(artifact_root=tmp_path / "artifacts")
    app = TestClient(make_app(g))
    headers = {"Authorization": "Bearer native-alice", "Idempotency-Key": uuid4().hex}
    try:
        worker.ready("executor_ready")
        assert (
            app.post(
                "/runs",
                json={"topic": "brand recall"},
                headers={"Idempotency-Key": "noauth"},
            ).status_code
            == 401
        )
        body = {"topic": "brand recall", "approval": True}
        created = app.post("/runs", json=body, headers=headers)
        assert created.status_code == 200, created.text
        id = created.json()["run_id"]
        plan = wait(lambda: c.get_event(id, "plan", timeout_seconds=0))
        assert app.post("/runs", json=body, headers=headers).json()["run_id"] == id
        assert (
            app.post(
                "/runs", json={**body, "topic": "different"}, headers=headers
            ).status_code
            == 409
        )
        assert (
            app.get(
                "/runs/" + id, headers={"Authorization": "Bearer native-bob"}
            ).status_code
            == 404
        )
        assert (
            app.post(
                "/runs/" + id + "/approval",
                json={"version": "0" * 64, "decision": "approve"},
                headers=headers,
            ).status_code
            == 409
        )
        assert (
            app.post(
                "/runs/" + id + "/approval",
                json={"version": plan["version"], "decision": "approve"},
                headers=headers,
            ).status_code
            == 200
        )
        assert (
            app.post(
                "/runs/" + id + "/approval",
                json={"version": plan["version"], "decision": "reject"},
                headers=headers,
            ).status_code
            == 409
        )
        wait(lambda: c.retrieve_workflow(id).get_status().status == "SUCCESS")
        full = app.get("/runs/" + id + "/events", headers=headers)
        assert full.status_code == 200
        ids = [
            int(line[4:]) for line in full.text.splitlines() if line.startswith("id: ")
        ]
        assert ids == list(range(1, len(ids) + 1)) and len(ids) >= 4
        replay = app.get(
            "/runs/" + id + "/events", headers={**headers, "Last-Event-ID": "2"}
        )
        assert [
            int(line[4:])
            for line in replay.text.splitlines()
            if line.startswith("id: ")
        ] == ids[2:]
    finally:
        worker.close()
        c.destroy()
        g.client.destroy()
        g.sql.dispose()


@pytest.mark.parametrize("scenario", ["archive_fail", "normal"])
def test_native_execution_and_domain_report_are_distinct(scenario, tmp_path):
    id = run_id()
    c = client()
    child = Executor(tmp_path, id, scenario=scenario)
    try:
        child.finish()
        report = c.get_event(id, "report", timeout_seconds=0)
        assert report and report["report"]
        assert c.retrieve_workflow(id).get_status().status == (
            "ERROR" if scenario == "archive_fail" else "SUCCESS"
        )
    finally:
        child.close()
        c.destroy()


def enqueue_probe(
    c, id, directory, owner="alice", name="native_ownership_probe", **fields
):
    request = {"run_id": id, "owner": owner, "directory": str(directory), **fields}
    return c.enqueue(
        {
            "workflow_name": name,
            "queue_name": "business",
            "workflow_id": id,
            "app_version": env.VERSION,
            "authenticated_user": owner,
            "queue_partition_key": owner,
        },
        request,
    )


def test_live_zombie_executor_is_fenced_after_public_recovery(tmp_path):
    id = run_id()
    c = client()
    old = Executor(
        tmp_path / "old", id, mode="serve", executor="old-" + id, fault="zombie_wait"
    )
    new = None
    try:
        old.ready("executor_ready")
        enqueue_probe(c, id, tmp_path / "old")
        old.ready()
        assert old.process.poll() is None
        new = Executor(tmp_path / "new", id, mode="serve", executor="new-" + id)
        new.ready("executor_ready")
        c.cancel_workflow(id)
        c.resume_workflow(id, queue_name="recovery")
        wait(lambda: c.retrieve_workflow(id).get_status().status == "SUCCESS")
        assert c.retrieve_workflow(id).get_result()["executor"] == "new-" + id
        assert old.process.poll() is None
        (tmp_path / "old" / "release").write_text("release stale body")
        wait(lambda: Domain(env.DB_URL).facts(id, "stale_denied"))
        assert Domain(env.DB_URL).count_effects(id) == 1
    finally:
        old.close()
        if new:
            new.close()
        c.destroy()


def test_real_postgres_outage_never_returns_false_success(tmp_path):
    id = run_id()
    c = client()
    child = Executor(
        tmp_path, id, mode="serve", executor="outage-" + id, fault="db_wait"
    )
    stopped = False
    try:
        child.ready("executor_ready")
        enqueue_probe(c, id, tmp_path, gate="db_wait")
        child.ready()
        g = Gateway()
        api = TestClient(make_app(g))
        subprocess.run(
            ["docker", "stop", "-t", "1", "por-dbos-native-postgres"],
            check=True,
            capture_output=True,
        )
        stopped = True
        (tmp_path / "release").write_text("database unavailable")
        assert (
            api.get(
                "/runs/" + id, headers={"Authorization": "Bearer native-alice"}
            ).status_code
            == 503
        )
        assert not (tmp_path / "result.json").exists()
        subprocess.run(
            ["docker", "start", "por-dbos-native-postgres"],
            check=True,
            capture_output=True,
        )
        stopped = False
        wait(
            lambda: subprocess.run(
                [
                    "docker",
                    "exec",
                    "por-dbos-native-postgres",
                    "pg_isready",
                    "-U",
                    "postgres",
                ],
                capture_output=True,
            ).returncode
            == 0
        )
        wait(lambda: c.retrieve_workflow(id).get_status().status == "ERROR")
        assert Domain(env.DB_URL).count_effects(id) == 0
        g.client.destroy()
        g.sql.dispose()
    finally:
        if stopped:
            subprocess.run(
                ["docker", "start", "por-dbos-native-postgres"],
                check=True,
                capture_output=True,
            )
        child.close()
        c.destroy()


def test_native_queue_global_and_owner_partition_across_executors(tmp_path):
    c = client()
    a = Executor(
        tmp_path / "a",
        "queue-workers",
        mode="serve",
        executor="queue-a-" + uuid4().hex,
        fault="queue_wait",
    )
    b = Executor(
        tmp_path / "b",
        "queue-workers",
        mode="serve",
        executor="queue-b-" + uuid4().hex,
        fault="queue_wait",
    )
    ids = [run_id() for _ in range(3)]
    dirs = [tmp_path / str(n) for n in range(3)]
    try:
        a.ready("executor_ready")
        b.ready("executor_ready")
        for id, directory, owner in zip(ids, dirs, ["alice", "alice", "bob"]):
            directory.mkdir()
            enqueue_probe(c, id, directory, owner, name="native_queue_probe")
        wait(
            lambda: sum(bool(Domain(env.DB_URL).facts(id, "queue_enter")) for id in ids)
            == 2
        )
        statuses = c.list_workflows(workflow_ids=ids, load_input=False)
        pending = [s for s in statuses if s.status == "PENDING"]
        assert len(pending) == 2 and {s.queue_partition_key for s in pending} == {
            "alice",
            "bob",
        }
        for directory in dirs:
            (directory / "release").write_text("continue")
        wait(
            lambda: all(
                c.retrieve_workflow(id).get_status().status == "SUCCESS" for id in ids
            )
        )
    finally:
        a.close()
        b.close()
        c.destroy()


def test_public_transactional_enqueue_rolls_back_without_delivery(tmp_path):
    from sqlalchemy import create_engine

    c = client()
    sql = create_engine(env.DB_URL)
    id = run_id()
    try:
        with pytest.raises(RuntimeError, match="rollback fixture"):
            with sql.begin() as db:
                c.enqueue_in_transaction(
                    db,
                    {
                        "workflow_name": "native_queue_probe",
                        "queue_name": "business",
                        "workflow_id": id,
                        "app_version": env.VERSION,
                        "authenticated_user": "alice",
                    },
                    {"run_id": id, "owner": "alice", "directory": str(tmp_path)},
                )
                assert c.list_workflows(workflow_ids=[id]) == []
                raise RuntimeError("rollback fixture")
        assert c.list_workflows(workflow_ids=[id]) == []
    finally:
        c.destroy()
        sql.dispose()


def test_opaque_role_cannot_replay_unconfirmed_model_budget(tmp_path):
    from codec import DomainFailure

    d = Domain(env.DB_URL)
    key = run_id() + ":public_signal:1"
    permit, artifact = d.acquire(key)
    assert artifact is None
    with pytest.raises(DomainFailure, match="unconfirmed role"):
        d.acquire(key)
    with pytest.raises(DomainFailure, match="Stale execution"):
        d.check(key, permit)


def test_native_safe_read_retries_and_model_error_cancels_sibling(tmp_path):
    for scenario in ("retry", "producer_error"):
        directory = tmp_path / scenario
        id = run_id()
        child = Executor(directory, id, scenario=scenario)
        try:
            child.finish()
            if scenario == "retry":
                assert_success(directory, id)
                assert len(Domain(env.DB_URL).facts(id, "planning")) == 2
            else:
                assert (directory / "status.txt").read_text() == "ERROR"
                facts = Domain(env.DB_URL).facts(id, "harness")
                assert len(facts) == 2 and all(f["settled"] for f in facts)
                assert {f["role"] for f in facts} == {
                    "public_signal",
                    "internal_knowledge",
                }
        finally:
            child.close()


def test_dbosrunner_native_model_and_tool_cache_rebuilds_hooks_once(tmp_path):
    id = run_id()
    c = client()
    first = Executor(
        tmp_path / "old",
        id,
        mode="serve",
        executor="replay-" + id,
        probes=True,
        fault="native_model_boundary",
    )
    second = None
    request = {"run_id": id, "owner": "alice", "directory": str(tmp_path / "old")}
    try:
        first.ready("executor_ready")
        c.enqueue(
            {
                "workflow_name": "native_sdk_replay",
                "queue_name": "control",
                "workflow_id": id,
                "app_version": env.VERSION,
                "authenticated_user": "alice",
            },
            request,
        )
        first.ready()
        first.close()
        second = Executor(
            tmp_path / "new", id, mode="serve", executor="replay-" + id, probes=True
        )
        second.ready("executor_ready")
        wait(
            lambda: c.retrieve_workflow(id).get_status().status in {"SUCCESS", "ERROR"}
        )
        out = c.retrieve_workflow(id).get_result()
        assert out["settled"] and out["budget"]["model_calls"] == 2
        assert out["budget"]["total_tokens"] == 28
        assert len(Domain(env.DB_URL).facts(id, "actual_provider")) == 2
        assert len(Domain(env.DB_URL).facts(id, "actual_tool")) == 1
        assert Domain(env.DB_URL).count_effects(id) == 1
    finally:
        first.close()
        if second:
            second.close()
        c.destroy()
