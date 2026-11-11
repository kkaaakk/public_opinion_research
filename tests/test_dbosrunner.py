"""Official Runner compatibility and physical checkpoint/restart evidence."""

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from dbos import DBOSClient

from open_deep_research.web.run_data import RunJSON


@pytest.mark.integration
@pytest.mark.parametrize("window", ["model-committed", "tool-committed", "budget"])
def test_official_runner_process_reuses_model_and_tool(pg_database, tmp_path, window):
    from test_dbos_process import kill_owned

    def launch():
        output = (tmp_path / "poc.log").open("a", encoding="utf-8")
        proc = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("dbosrunner_fixture.py")),
                str(tmp_path),
                window,
            ],
            env={**os.environ, "DBOS_DATABASE_URL": pg_database},
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        output.close()
        return proc

    def until(predicate, proc):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if predicate():
                return
            assert proc.poll() is None, (tmp_path / "poc.log").read_text(
                encoding="utf-8"
            )
            time.sleep(0.05)
        pytest.fail((tmp_path / "poc.log").read_text(encoding="utf-8"))

    client = DBOSClient(
        system_database_url=pg_database,
        application_name="runner-poc",
        dbos_system_schema="por_dbos",
        serializer=RunJSON(),
    )
    proc = launch()
    try:
        # enqueue via public client; the child owns actual execution.
        client.enqueue(
            {
                "workflow_name": "poc.producer",
                "queue_name": "_dbos_internal_queue",
                "workflow_id": "poc-producer",
                "app_version": "poc-v1",
            }
        )
        until(lambda: (tmp_path / "ready").exists(), proc)
        steps = client.list_workflow_steps("poc-producer")
        assert any(
            ("por.agent_context" if window == "budget" else "_model_call_step")
            in s["function_name"]
            for s in steps
        )
        kill_owned(proc)
        (tmp_path / "released").touch()
        proc = launch()
        until(
            lambda: client.list_workflows(workflow_ids=["poc-producer"])[0].status
            == "SUCCESS",
            proc,
        )
        expected = (
            {"model_calls": 2, "settled": True} if window == "budget" else "complete"
        )
        assert client.retrieve_workflow("poc-producer").get_result() == expected
        with sqlite3.connect(tmp_path / "calls.db") as db:
            assert dict(
                db.execute("SELECT kind,count(*) FROM calls GROUP BY kind")
            ) == (
                {"context-model": 2}
                if window == "budget"
                else {"model-first": 1, "model-final": 1, "tool": 1}
            )
    finally:
        kill_owned(proc)
        client.destroy()


@pytest.mark.integration
@pytest.mark.parametrize(
    "window", ["runner-model", "runner-tool", "runner-batch", "batch-error-cache"]
)
def test_business_runner_process_restores_harness_and_fences_unknown_batch(
    pg_database, tmp_path, window
):
    from test_dbos_process import configuration, effect_count, kill_owned, start, wait
    from test_python_workflow import initial

    from open_deep_research.web.durable import RunClient
    from open_deep_research.workflow.run_facts import facts

    client = RunClient()
    proc = start(tmp_path, "runner-recovery", window)
    try:
        wait(lambda: (tmp_path / "executor-ready").exists(), proc, tmp_path)
        run = client.create(
            "alice",
            window,
            {"topic": "brand recall"},
            configuration(tmp_path),
            initial(),
        )[0]
        wait(lambda: (tmp_path / window).exists(), proc, tmp_path)
        run_id = run["run_id"]
        public_id = run_id + ":public_signal:1"
        internal_id = run_id + ":internal_knowledge:1"

        def children():
            return {
                w.workflow_id: w
                for w in client.client.list_workflows(
                    name="por.role",
                    parent_workflow_id=run_id + ":research",
                    load_input=False,
                    load_output=True,
                )
            }

        # Kill with one producer still running and its sibling result committed.
        wait(
            lambda: internal_id in (roles := children())
            and roles[internal_id].status == "SUCCESS",
            proc,
            tmp_path,
        )
        before = children()
        assert set(before) == {public_id, internal_id}
        assert before[public_id].status == "PENDING"
        committed = before[internal_id].output
        assert committed["research_result"]["role"] == "internal_knowledge"
        assert any(
            u["id"] == internal_id and u["artifact"] and u["settled"] is True
            for u in facts().units(run_id)
        )

        steps = facts().client.list_workflow_steps(public_id)
        assert any("_model_call_step" in s["function_name"] for s in steps)
        if window == "runner-tool":
            assert any(
                s["function_name"] == "por.agent_tool" and s["output"] for s in steps
            )
        lc_before = (
            effect_count(tmp_path, "lc_models")
            if window in {"runner-batch", "batch-error-cache"}
            else None
        )
        kill_owned(proc)
        (tmp_path / (window + ".release")).touch()
        (tmp_path / "executor-ready").unlink()
        recovery_started = time.perf_counter()
        proc = start(tmp_path, "runner-recovery", window)
        if window == "runner-model":
            wait(lambda: (tmp_path / "runner-second").exists(), proc, tmp_path)
            kill_owned(proc)
            (tmp_path / "runner-second.release").touch()
            (tmp_path / "executor-ready").unlink()
            recovery_started = time.perf_counter()
            proc = start(tmp_path, "runner-recovery", window)
        wait(
            lambda: client.get(run["run_id"], "alice")["status"]
            in {"completed", "failed", "interrupted"},
            proc,
            tmp_path,
            timeout=90,
        )
        result = client.get(run["run_id"], "alice")
        if os.environ.get("RUNNER_RECOVERY_OUTPUT"):
            import json

            with Path(os.environ["RUNNER_RECOVERY_OUTPUT"]).open(
                "a", encoding="utf-8"
            ) as output:
                output.write(
                    json.dumps(
                        dict(
                            window=window,
                            seconds=time.perf_counter() - recovery_started,
                            status=result["status"],
                        )
                    )
                    + "\n"
                )
        if window in {"runner-batch", "batch-error-cache"}:
            assert result["status"] == "interrupted"
            assert not result["retry_safe"]
            assert facts().request(run["run_id"])["occupied"]
            assert effect_count(tmp_path, "lc_models") == lc_before
        else:
            assert result["status"] == "completed", result["error"]
            assert result["usage"]["model_calls"] == 26
            assert result["usage"]["tool_calls"] == 2
            assert effect_count(tmp_path, "models") == 6
            assert result["report"]
            assert len(result["role_reports"]) == 4
            assert {
                report["stage_id"]
                for report in result["role_reports"]
                if report["role"] in {"public_signal", "internal_knowledge"}
            } == {public_id, internal_id}
            after = children()
            assert after[public_id].status == after[internal_id].status == "SUCCESS"
            assert after[internal_id].output == committed
            assert after[internal_id].completed_at == before[internal_id].completed_at
        assert effect_count(tmp_path) == effect_count(tmp_path, "effects") == 2
    finally:
        kill_owned(proc)


@pytest.mark.integration
@pytest.mark.parametrize("scenario", ["plan-error", "runner-error"])
def test_committed_failed_model_attempt_is_replayed_without_rebilling(
    pg_database, tmp_path, scenario
):
    from test_dbos_process import configuration, effect_count, kill_owned, start, wait
    from test_python_workflow import initial

    from open_deep_research.web.durable import RunClient
    from open_deep_research.workflow.run_facts import facts

    client = RunClient()
    proc = start(tmp_path, "failed-response", scenario)
    try:
        wait(lambda: (tmp_path / "executor-ready").exists(), proc, tmp_path)
        cfg = configuration(tmp_path)
        if scenario == "plan-error":
            cfg["configurable"]["max_structured_output_retries"] = 3
        run = client.create(
            "alice",
            scenario,
            {"topic": "brand recall"},
            cfg,
            initial(),
        )[0]
        wait(lambda: (tmp_path / scenario).exists(), proc, tmp_path)
        if scenario == "runner-error":
            wait(
                lambda: any(
                    u["name"] == "internal_knowledge_agent" and u["settled"] is True
                    for u in facts().units(run["run_id"])
                ),
                proc,
                tmp_path,
            )
        kill_owned(proc)
        (tmp_path / (scenario + ".release")).touch()
        (tmp_path / "executor-ready").unlink()
        proc = start(tmp_path, "failed-response", scenario)
        wait(
            lambda: client.get(run["run_id"], "alice")["status"]
            in {"completed", "failed", "interrupted"},
            proc,
            tmp_path,
            timeout=90,
        )
        result = client.get(run["run_id"], "alice")
        if scenario == "plan-error":
            assert result["status"] == "completed", result["error"]
            assert result["usage"]["model_calls"] == 27
            assert effect_count(tmp_path, "service_models") == 2
            assert effect_count(tmp_path, "models") == 6
        else:
            assert result["status"] == "failed", result["error"]
            unit = next(
                u
                for u in facts().units(run["run_id"])
                if u["name"] == "public_signal_agent"
            )
            assert unit["usage"]["model_calls"] == 1
            assert effect_count(tmp_path, "models") == 3
            assert result["settlement"] == "settled"
    finally:
        kill_owned(proc)


@pytest.mark.integration
def test_finalizer_crash_after_leaf_rewind_keeps_original_grant_live(
    pg_database, tmp_path
):
    from types import SimpleNamespace

    from dbos_support import retry_observed_failure
    from test_dbos_process import configuration, effect_count, kill_owned, start, wait
    from test_python_workflow import initial

    from open_deep_research.web.durable import RunClient
    from open_deep_research.workflow.run_facts import facts

    client = RunClient()
    proc = start(tmp_path, "nested-finalizer", "retry-nested")
    try:
        wait(lambda: (tmp_path / "executor-ready").exists(), proc, tmp_path)
        run = client.create(
            "alice",
            "nested-grant",
            {"topic": "brand recall"},
            configuration(tmp_path),
            initial(),
        )[0]
        wait(
            lambda: (r := client.get(run["run_id"], "alice"))["status"] == "failed"
            and r["events_complete"]
            and r["archive_status"] == "saved",
            proc,
            tmp_path,
        )
        observed = client.get(run["run_id"], "alice")
        retry_observed_failure(
            SimpleNamespace(client=client), run, "same-grant", observed
        )
        wait(
            lambda: (tmp_path / "retry-nested").exists()
            and (tmp_path / "nested-leaf").exists(),
            proc,
            tmp_path,
        )
        assert any(
            u["name"] == "research_review" and u["settled"] is None
            for u in facts().units(run["run_id"])
        )
        kill_owned(proc)
        (tmp_path / "retry-nested.release").touch()
        (tmp_path / "nested-leaf.release").touch()
        (tmp_path / "executor-ready").unlink()
        proc = start(tmp_path, "nested-finalizer", "retry-nested")
        wait(
            lambda: (r := client.get(run["run_id"], "alice"))["status"] == "completed"
            and r["events_complete"]
            and r["archive_status"] == "saved",
            proc,
            tmp_path,
            timeout=90,
        )
        result = client.get(run["run_id"], "alice")
        assert result["generation"] == 1 and not result["retry_requested"]
        assert result["usage"]["model_calls"] == 27
        assert effect_count(tmp_path) == effect_count(tmp_path, "effects") == 2
        receipt = client.client.get_event(
            run["run_id"] + ":finalize", "retry_applied", timeout_seconds=0
        )
        assert receipt == {"generation": 1, "outcome": "applied"}
    finally:
        kill_owned(proc)
