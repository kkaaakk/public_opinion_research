"""Same real-service workload on main and the candidate; synthetic inference."""

import asyncio
import json
import os
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import text

from open_deep_research.workflow.dbos_config import domain_engine, native_client

pytestmark = pytest.mark.integration


def test_native_runner_profile(native_case, monkeypatch):
    c = native_case
    from sdk_model_support import install_inference

    bounded_calls = 0
    model_lock = threading.Lock()

    async def respond(request):
        nonlocal bounded_calls
        with model_lock:
            bounded_calls += 1
        return None

    install_inference(monkeypatch, respond)
    delay = float(os.environ.get("PROFILE_PROVIDER_DELAY", "0"))
    if delay:
        from agents.testing import ScriptedModel

        original = ScriptedModel.get_response

        async def delayed(self, *args, **kwargs):
            await asyncio.sleep(delay)
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(ScriptedModel, "get_response", delayed)

    with domain_engine().connect() as db:
        database = db.execute(text("SELECT current_database()")).scalar_one()
    if os.environ.get("PROFILE_SQL_STATS") == "1":
        with domain_engine().begin() as db:
            db.execute(text("CREATE EXTENSION IF NOT EXISTS pg_stat_statements"))

    def statistics_row():
        with domain_engine().connect() as db:
            db.execute(text("SELECT pg_stat_clear_snapshot()"))
            values = dict(
                db.execute(
                    text(
                        "SELECT xact_commit,xact_rollback,tup_inserted,tup_updated,tup_deleted,blks_read,blks_hit,blk_read_time,blk_write_time FROM pg_stat_database WHERE datname=current_database()"
                    )
                )
                .mappings()
                .one()
            )
            if os.environ.get("PROFILE_SQL_STATS") == "1":
                values.update(
                    db.execute(
                        text(
                            "SELECT coalesce(sum(calls) FILTER (WHERE query ~* '(INSERT INTO|UPDATE |DELETE FROM)'),0) AS sql_write_statements,coalesce(sum(total_exec_time),0) AS sql_exec_ms,coalesce(sum(wal_bytes),0) AS wal_bytes FROM pg_stat_statements WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database())"
                        )
                    )
                    .mappings()
                    .one()
                )
                values = {key: float(value) for key, value in values.items()}
            return values

    def one(_):
        start = time.perf_counter()
        run = c.create()
        done = c.wait(
            run,
            lambda r: r["events_complete"],
            timeout=120,
        )
        assert done["status"] == "completed", done["error"]
        return {
            "seconds": time.perf_counter() - start,
            "usage": done["usage"],
            "run_id": run["run_id"],
            "workflow_version": done["workflow_version"],
        }

    results = []
    for concurrency in (1, 2):
        before = statistics_row()
        model_count, tool_count, bounded_before = (
            sum(len(m.calls) for m in c.models),
            sum(c.calls.values()),
            bounded_calls,
        )
        since = str(int(time.time()))
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            samples = list(pool.map(one, range(6)))
        elapsed = time.perf_counter() - start
        time.sleep(1.1)  # PostgreSQL publishes cumulative stats asynchronously.
        after = statistics_row()
        for sample in samples:
            workflows = native_client().list_workflows(
                workflow_id_prefix=sample.pop("run_id"),
                load_input=False,
                load_output=False,
            )
            sample["journal_rows"] = sum(
                len(
                    native_client().list_workflow_steps(
                        w.workflow_id, load_output=False
                    )
                )
                for w in workflows
            )
        times = [s["seconds"] for s in samples]
        row = dict(
            concurrency=concurrency,
            n=len(samples),
            wall_seconds=elapsed,
            throughput=len(samples) / elapsed,
            p50=statistics.median(times),
            p95=statistics.quantiles(times, n=20, method="inclusive")[18],
            actual_sdk_calls=sum(len(m.calls) for m in c.models) - model_count,
            actual_bounded_model_calls=bounded_calls - bounded_before,
            actual_tool_calls=sum(c.calls.values()) - tool_count,
            pg={key: after[key] - before[key] for key in before},
            samples=samples,
        )
        container = os.environ.get("PROFILE_POSTGRES_CONTAINER")
        if container:
            logs = subprocess.check_output(
                ["docker", "logs", "--since", since, container],
                stderr=subprocess.STDOUT,
                text=True,
            )
            row["sql_write_statements"] = sum(
                database in line
                and any(
                    word in line for word in (": INSERT ", ": UPDATE ", ": DELETE ")
                )
                for line in logs.splitlines()
            )
        results.append(row)
    output = os.environ.get("RUNNER_PROFILE_OUTPUT")
    if output:
        Path(output).write_text(
            json.dumps(
                dict(
                    database=database,
                    inference="controlled; no paid models",
                    provider_delay_seconds=delay,
                    results=results,
                ),
                indent=2,
            ),
            encoding="utf-8",
        )
