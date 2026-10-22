"""Actual process faults, engine journals and SDK/Harness contracts.

Run explicitly with ENGINE_POC_LIVE=1 and the disposable local services from
README.md. No paid model or remote MCP/API call is permitted.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import common
import pytest

HERE = Path(__file__).resolve().parent
pytestmark = pytest.mark.skipif(
    os.environ.get("ENGINE_POC_LIVE") != "1",
    reason="explicit local engine/Neo4j POC, see README",
)


class Child:
    def __init__(
        self,
        engine,
        directory,
        mode="start",
        *,
        scenario="normal",
        block="",
        guarded=True,
        approval=False,
    ):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.engine = engine
        self.run_id = directory.name
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.endswith(("_API_KEY", "_TOKEN", "_PASSWORD", "_SECRET"))
        }
        env.update(
            RESEARCH_GRAPH_URI="bolt://127.0.0.1:17687",
            RESEARCH_GRAPH_USERNAME="neo4j",
            RESEARCH_GRAPH_PASSWORD="evaluation-only-password",
            RAG_MEMORY_WRITE_ENABLED="false",
            BUSINESS_AGENT_RUNTIME="openai_agents",
            WORKFLOW_BACKEND="python",
            PYTHON_DOTENV_DISABLED="1",
            OPENAI_AGENTS_DISABLE_TRACING="1",
            LANGSMITH_TRACING="false",
            LANGCHAIN_TRACING_V2="false",
            ALLOW_PLAN_FEEDBACK="false",
            POC_BLOCK_AT=block,
        )
        self.log = directory / f"{mode}-{time.time_ns()}.log"
        self.output = self.log.open("w", encoding="utf-8")
        command = [
            sys.executable,
            str(HERE / f"{engine}_poc.py"),
            mode,
            str(directory),
            "--run-id",
            self.run_id,
        ]
        if mode in {"start", "recover"}:
            command += ["--scenario", scenario, "--graph", "neo4j"]
            if not guarded:
                command.append("--unguarded")
            if approval:
                command.append("--approval")
        self.process = subprocess.Popen(
            command,
            env=env,
            cwd=common.ROOT,
            stdout=self.output,
            stderr=subprocess.STDOUT,
        )

    def finish(self, *, success=True):
        try:
            rc = self.process.wait(timeout=100)
            self.output.close()
            if success:
                assert rc == 0, self.log.read_text(encoding="utf-8")[-6000:]
            return rc
        finally:
            self.close()

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)
        self.output.close()

    def wait_file(self, name):
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline:
            path = self.directory / name
            if path.exists() and path.stat().st_size:
                return path
            assert self.process.poll() is None, self.log.read_text(encoding="utf-8")[
                -6000:
            ]
            time.sleep(0.1)
        pytest.fail(
            f"POC timed out waiting for {name}: {self.log.read_text(encoding='utf-8')[-3000:]}"
        )


def result(directory):
    return json.loads((directory / "result.json").read_text(encoding="utf-8"))


def complete(directory):
    value = result(directory)
    assert value["cursor"] == "done", value.get("error")
    state = common.loads(value["state"])
    assert "FINAL brand recall report" in state["report"]["final"]
    roles = common.audit(directory, "runtime")
    assert {row["role"] for row in roles} == set(common.ROLES)
    assert all(
        row["settled"] and row["filter_calls"] > 0 and row["evidence"] for row in roles
    )
    assert all(row["receipts"] > 0 for row in roles if row["role"] in common.ROLES[:2])
    return state


def new_dir(tmp_path, engine, suffix):
    return tmp_path / f"engine_eval_{engine}_{suffix}_{uuid4().hex[:12]}"


@pytest.mark.parametrize("engine", ["dbos", "temporal"])
@pytest.mark.parametrize("scenario", ["normal", "gap", "rolling", "clarify"])
def test_real_sdk_harness_parallel_review_order_budget(engine, scenario, tmp_path):
    directory = new_dir(tmp_path, engine, scenario)
    child = Child(engine, directory, scenario=scenario)
    try:
        child.finish()
        if scenario == "clarify":
            assert result(directory)["cursor"] == "done"
            assert (
                common.loads(result(directory)["state"])["messages"][-1].content
                == "Which brand?"
            )
            assert not common.audit(directory, "runtime")
            assert not common.effects(directory)
            return
        state = complete(directory)
        if scenario == "normal":
            assert {
                k: state["runtime"]["budget"][k]
                for k in (
                    "model_calls",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                )
            } == {
                "model_calls": 28,
                "input_tokens": 235,
                "output_tokens": 76,
                "total_tokens": 311,
            }
            assert len(common.effects(directory)) == 2
        if scenario == "gap":
            assert state["workflow"]["round"] == 2
            assert len(state["workflow"]["completed_tasks"]) == 2
            assert len(common.effects(directory)) == 4
        if scenario == "rolling":
            assert (
                sum(row["summaries"] for row in common.audit(directory, "runtime")) > 0
            )
    finally:
        child.close()


@pytest.mark.parametrize("engine", ["dbos", "temporal"])
def test_process_kill_after_engine_committed_producers(engine, tmp_path):
    directory = new_dir(tmp_path, engine, "checkpoint")
    child = Child(engine, directory, block="checkpoint")
    try:
        assert child.wait_file("ready").read_text() == "checkpoint"
        assert len(common.effects(directory)) == 2
    finally:
        child.close()
    recovered = Child(engine, directory, "recover")
    try:
        recovered.finish()
        complete(directory)
        assert len(common.effects(directory)) == 2
        assert (
            sum(
                row["cursor"] == "research_agents"
                for row in common.audit(directory, "segment_enter")
            )
            == 1
        )
    finally:
        recovered.close()


@pytest.mark.parametrize("engine", ["dbos", "temporal"])
@pytest.mark.parametrize("guarded", [True, False])
def test_successful_external_effect_lost_receipt_is_not_exactly_once(
    engine, guarded, tmp_path
):
    directory = new_dir(tmp_path, engine, f"lost_{guarded}")
    child = Child(engine, directory, block="lost-receipt", guarded=guarded)
    try:
        child.wait_file("ready")
        assert len(common.effects(directory)) == 2
    finally:
        child.close()
    recovered = Child(engine, directory, "recover", guarded=guarded)
    try:
        recovered.finish()
        if guarded:
            assert result(directory)["cursor"] == "interrupted"
            assert len(common.effects(directory)) == 2
            assert len(common.audit(directory, "quarantined")) == 1
            with common.ledger(directory) as db:
                assert (
                    db.execute(
                        "SELECT count(*) FROM operations WHERE status='started'"
                    ).fetchone()[0]
                    == 2
                )
        else:
            complete(directory)
            assert len(common.effects(directory)) == 4  # real negative control
    finally:
        recovered.close()


async def temporal_pending(run_id):
    from temporalio.client import Client

    client = await Client.connect("127.0.0.1:17233")
    handle = client.get_workflow_handle(run_id)
    for _ in range(100):
        version = await handle.query("pending_plan")
        if version:
            return version
        await asyncio.sleep(0.1)
    raise AssertionError("no durable Temporal plan")


@pytest.mark.parametrize("engine", ["dbos", "temporal"])
def test_durable_approval_survives_process_without_replanning(engine, tmp_path):
    directory = new_dir(tmp_path, engine, "approval")
    child = Child(engine, directory, approval=True)
    try:
        child.wait_file("plan.json")
        if engine == "dbos":
            from dbos import DBOSClient

            client = DBOSClient(
                system_database_url=f"sqlite:///{(directory / 'dbos.sqlite').as_posix()}",
                application_name="por-engine-poc",
            )
            assert client.get_event(directory.name, "plan", timeout_seconds=10)
        else:
            assert asyncio.run(temporal_pending(directory.name))
    finally:
        child.close()
    recovered = Child(engine, directory, "recover", approval=True)
    control = None
    try:
        control = Child(engine, directory, "approve")
        control.finish()
        recovered.finish()
        complete(directory)
        assert (
            sum(
                row["from"] == "plan_report_sections"
                for row in common.audit(directory, "checkpoint")
            )
            == 1
        )
        assert len(common.effects(directory)) == 2
    finally:
        recovered.close()
        if control:
            control.close()


@pytest.mark.parametrize("engine", ["dbos", "temporal"])
def test_engine_cancel_drains_actual_sdk_tools_and_harness(engine, tmp_path):
    directory = new_dir(tmp_path, engine, "cancel")
    child = Child(engine, directory, scenario="cancel")
    control = None
    try:
        assert child.wait_file("ready").read_text() == "sdk-blocked"
        control = Child(engine, directory, "cancel")
        control.finish()
        child.finish(success=False)
        settlements = common.audit(directory, "cancelled")
        assert settlements and settlements[-1]["settled"]
        assert settlements[-1]["budget"]["model_calls"] > 10
        assert len(common.effects(directory)) == 2
        with common.ledger(directory) as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM operations WHERE status='returned'"
                ).fetchone()[0]
                == 2
            )
    finally:
        child.close()
        if control:
            control.close()


def test_dbos_native_runner_really_checks_project_harness(tmp_path):
    directory = new_dir(tmp_path, "dbos", "native")
    child = Child("dbos", directory, "probe")
    try:
        child.finish()
        output = result(directory)
        assert output["bare"] == "offline DBOSRunner response"
        assert not output["harness"]["compatible"]
        assert any(
            "separate SDKContextHarness" in line
            for line in output["harness"]["failure"]
        )
    finally:
        child.close()
