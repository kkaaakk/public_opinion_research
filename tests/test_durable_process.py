"""Kill real workers and disconnect TCP streams against the persistent SQL queue."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from test_durable_runs import create

from open_deep_research.web.run_store import RunConflict, RunStore

FIXTURE = Path(__file__).with_name("durable_process_fixture.py")


def start(mode, directory, *args):
    env = {
        **os.environ,
        "PYTHON_DOTENV_DISABLED": "1",
        "RESEARCH_GRAPH_ENABLED": "false",
        "LANGSMITH_TRACING": "false",
        "OPENAI_AGENTS_DISABLE_TRACING": "1",
    }
    for name in list(env):
        if name.endswith(("_API_KEY", "_TOKEN")):
            env.pop(name)
    output = (directory / f"{mode}.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(FIXTURE), mode, str(directory), *map(str, args)],
        env=env,
        stdout=output,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    output.close()
    return process


def wait_for(condition, process, directory, timeout=25):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if condition():
            return
        if process.poll() is not None:
            pytest.fail(
                "Fixture exited: "
                + "\n".join(
                    p.read_text(encoding="utf-8")[-5000:]
                    for p in directory.glob("*.log")
                )
            )
        time.sleep(0.05)
    pytest.fail("Process fixture did not reach its durable boundary")


def effects(directory):
    with sqlite3.connect(directory / "effects.db") as db:
        return db.execute("SELECT count(*) FROM effects").fetchone()[0]


@pytest.mark.parametrize("mode", ["checkpoint", "lost-receipt"])
def test_real_kill_restart_resumes_only_committed_boundary(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("RESEARCH_GRAPH_ENABLED", "false")
    store = RunStore(
        f"sqlite:///{(tmp_path / 'runs.db').as_posix()}", lease_seconds=1.5
    )
    process = None
    restarted = None
    try:
        run = create(store)
        process = start(mode, tmp_path)
        wait_for(lambda: (tmp_path / "ready").exists(), process, tmp_path)
        process.kill()
        process.wait(timeout=10)
        count = effects(tmp_path)
        assert count >= 1
        # No heartbeat can renew after process death. Use actual database time,
        # not a mocked expiration or Python exception in the worker.
        until = time.monotonic() + 5
        while not store.reconcile() and time.monotonic() < until:
            time.sleep(0.1)
        result = store.get(run["run_id"], "alice")
        assert result["status"] == "interrupted"
        if mode == "lost-receipt":
            assert result["quarantined"] == 1
            with pytest.raises(RunConflict):
                store.retry(run["run_id"], "alice", "retry", result["version"])
            assert store.claim("duplicate") is None
        else:
            assert count == 2
            assert result["data"]["cursor"] == "research_review"
            store.retry(run["run_id"], "alice", "retry", result["version"])
            restarted = start("once", tmp_path)
            restarted.wait(timeout=30)
            assert restarted.returncode == 0, (tmp_path / "once.log").read_text(
                encoding="utf-8"
            )[-4000:]
            complete = store.get(run["run_id"], "alice")
            assert complete["status"] == "completed", complete["data"]["error"]
            assert complete["attempt_id"] != result["attempt_id"]
            assert complete["data"]["state"]["report"]["final"]
        assert effects(tmp_path) == count
    finally:
        for child in (process, restarted):
            if child and child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        store.close()


def test_real_tcp_disconnect_refresh_and_reconnect_do_not_cancel_or_duplicate(tmp_path):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    web = start("web", tmp_path, port)
    agent = None
    address = f"http://127.0.0.1:{port}"

    def healthy():
        try:
            return httpx.get(address + "/api/health", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    try:
        wait_for(healthy, web, tmp_path)
        with httpx.Client(
            base_url=address,
            headers={
                "Authorization": "Bearer process-test-token",
                "Idempotency-Key": "tcp-test",
            },
            timeout=15,
        ) as client:
            body = dict(
                topic="brand recall",
                model="openai:gpt-4.1",
                rag_enabled=True,
                workflow_backend="python",
                business_agent_runtime="openai_agents",
            )
            response = client.post("/api/runs", json=body)
            assert response.status_code == 202, response.text
            run_id = response.json()["run_id"]
            # Actually close a socket after the first durable SSE event.
            with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
                line = next(
                    line for line in stream.iter_lines() if line.startswith("id:")
                )
                cursor = int(line[3:])
            agent = start("normal", tmp_path)
            assert client.post("/api/runs", json=body).json()["run_id"] == run_id

            def completed():
                return client.get(f"/api/runs/{run_id}").json()["status"] == "completed"

            wait_for(completed, agent, tmp_path)
            wait_for(
                lambda: (tmp_path / f"archive-{run_id}.json").exists(), agent, tmp_path
            )
            wait_for(
                lambda: client.get(f"/api/runs/{run_id}").json()["archive_status"]
                == "saved",
                agent,
                tmp_path,
            )
            replay = client.get(
                f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(cursor)}
            )
            events = [
                json.loads(line[6:])
                for line in replay.text.splitlines()
                if line.startswith("data: ")
            ]
            assert events and events[0]["seq"] == cursor + 1
            assert len({e["seq"] for e in events}) == len(events)
            assert any(e["type"] == "report" for e in events)
            assert effects(tmp_path) == 2
            assert client.get(f"/api/runs/{run_id}").json()["archive_status"] == "saved"
    finally:
        for child in (web, agent):
            if child and child.poll() is None:
                child.kill()
                child.wait(timeout=10)
