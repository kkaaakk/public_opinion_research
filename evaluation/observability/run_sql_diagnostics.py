"""Native, opt-in SQL/Event diagnostics with synthetic inference only.

This is separate from formal performance measurement. No SQL bind parameters,
exception messages, payloads or credentials are persisted by the diagnostic.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
probe_spec = importlib.util.spec_from_file_location(
    "sql_probe", Path(__file__).with_name("sql_probe.py")
)
probe_module = importlib.util.module_from_spec(probe_spec)
probe_spec.loader.exec_module(probe_module)
SCOPE, SQLProbe, fingerprint = (
    probe_module.SCOPE,
    probe_module.SQLProbe,
    probe_module.fingerprint,
)


def postgres_observer(url, database=None):
    import psycopg

    parsed = urlsplit(url)
    return psycopg.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        user=unquote(parsed.username),
        password=unquote(parsed.password),
        dbname=database or parsed.path.lstrip("/"),
        autocommit=True,
        connect_timeout=3,
        sslmode="disable",
        gssencmode="disable",
        passfile=os.devnull,
        require_auth="scram-sha-256,md5,password",
        options="-c application_name=por_sql_observer -c statement_timeout=3000",
    )


def server_snapshot(observer, database):
    if database is None:
        return None
    bracket_start = time.perf_counter()
    with observer.cursor() as cursor:
        cursor.execute("SELECT oid FROM pg_database WHERE datname=%s", (database,))
        row = cursor.fetchone()
        if row is None:
            return None
        oid = row[0]
        cursor.execute(
            "SELECT userid,dbid,toplevel,queryid,query,stats_since,calls,plans,total_plan_time,total_exec_time,"
            "min_exec_time,max_exec_time,rows,shared_blks_hit,shared_blks_read,"
            "shared_blk_read_time,shared_blk_write_time,wal_records,wal_fpi,wal_bytes "
            "FROM pg_stat_statements WHERE dbid=%s",
            (oid,),
        )
        names = [description.name for description in cursor.description]
        statements = []
        for values in cursor.fetchall():
            value = dict(zip(names, values))
            key, sql = fingerprint(value.pop("query"))
            value.update(fingerprint=key, sql=sql)
            statements.append(value)
        cursor.execute(
            "SELECT xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,"
            "tup_fetched,tup_inserted,tup_updated,tup_deleted,blk_read_time,blk_write_time "
            "FROM pg_stat_database WHERE datid=%s",
            (oid,),
        )
        database_stats = dict(
            zip([d.name for d in cursor.description], cursor.fetchone())
        )
        cursor.execute(
            "SELECT stats_reset FROM pg_stat_database WHERE datid=%s", (oid,)
        )
        database_reset = cursor.fetchone()[0]
        cursor.execute("SELECT dealloc,stats_reset FROM pg_stat_statements_info")
        statement_info = dict(
            zip([d.name for d in cursor.description], cursor.fetchone())
        )
        cursor.execute(
            "SELECT wal_records,wal_fpi,wal_bytes,wal_buffers_full,wal_write,wal_sync,"
            "wal_write_time,wal_sync_time,stats_reset FROM pg_stat_wal"
        )
        wal = dict(zip([d.name for d in cursor.description], cursor.fetchone()))
        wal_reset = str(wal.pop("stats_reset"))
    return {
        "oid": oid,
        "statements": statements,
        "database": database_stats,
        "wal_cluster": wal,
        "wal_stats_reset": wal_reset,
        "database_stats_reset": str(database_reset),
        "statement_info": {
            k: str(v) if k == "stats_reset" else v for k, v in statement_info.items()
        },
        "client_perf_bracket": [bracket_start, time.perf_counter()],
    }


def server_delta(before, after):
    def key(row):
        return tuple(
            str(row[field]) for field in ("userid", "dbid", "toplevel", "queryid")
        )

    previous = {key(row): row for row in before["statements"]}
    following = {key(row): row for row in after["statements"]}
    issues = []
    if len(previous) != len(before["statements"]):
        issues.append("duplicate_full_key")
    if len(following) != len(after["statements"]):
        issues.append("duplicate_endpoint_key")
    if previous.keys() - following.keys():
        issues.append("statement_key_disappeared")
    if before.get("wal_stats_reset") != after.get("wal_stats_reset"):
        issues.append("wal_stats_reset")
    if before["oid"] != after["oid"]:
        issues.append("database_oid_changed")
    if before.get("database_stats_reset") != after.get("database_stats_reset"):
        issues.append("database_stats_reset")
    if before.get("statement_info") != after.get("statement_info"):
        issues.append("statement_reset_or_deallocation")
    statements = []
    counters = (
        "calls",
        "plans",
        "total_plan_time",
        "total_exec_time",
        "rows",
        "shared_blks_hit",
        "shared_blks_read",
        "shared_blk_read_time",
        "shared_blk_write_time",
        "wal_records",
        "wal_fpi",
        "wal_bytes",
    )
    for row in after["statements"]:
        old = previous.get(key(row), {})
        if old and old.get("stats_since") != row.get("stats_since"):
            issues.append("statement_reset")
        delta = {key: row[key] - old.get(key, 0) for key in counters}
        if any(value < 0 for value in delta.values()):
            issues.append("negative_statement_delta")
        if any(delta.values()):
            delta.update(
                userid=row["userid"],
                dbid=row["dbid"],
                toplevel=row["toplevel"],
                queryid=row["queryid"],
                fingerprint=row["fingerprint"],
                sql=row["sql"],
            )
            statements.append(delta)
    database = {
        key: value - before["database"][key] for key, value in after["database"].items()
    }

    wal = {
        key: value - before["wal_cluster"][key]
        for key, value in after["wal_cluster"].items()
    }
    if any(value < 0 for value in database.values()):
        issues.append("negative_database_delta")
    if any(value < 0 for value in wal.values()):
        issues.append("negative_wal_delta")
    return {
        "valid": not issues,
        "issues": sorted(set(issues)),
        "statements": statements if not issues else [],
        "database": database if not issues else None,
        "wal_cluster": wal if not issues else None,
        "limitations": [
            "Server cumulative views have publication lag",
            "Cluster WAL includes background activity; not per-Run",
            "Statement percentiles and in-window min/max cannot be reconstructed from aggregate deltas",
        ],
    }


def explain_reads(url, database, root_id):
    """Read-only representative plans on the exact owned, quiescent UUID DB."""
    if not re.fullmatch(r"por_test_[0-9a-f]{32}", database):
        raise ValueError("EXPLAIN requires this runner's owned test database")
    cases = [
        (
            "workflow_status_pk",
            "SELECT status FROM por_dbos.workflow_status WHERE workflow_uuid=%s",
            (root_id,),
        ),
        (
            "step_journal_pk",
            "SELECT output,serialization FROM por_dbos.operation_outputs WHERE workflow_uuid=%s AND function_id=%s",
            (root_id, 1),
        ),
        (
            "stream_offset",
            'SELECT max("offset") FROM por_dbos.streams WHERE workflow_uuid=%s AND key=%s',
            (root_id + ":events", "events"),
        ),
        (
            "application_request_pk",
            "SELECT id FROM por_domain.requests WHERE id=%s",
            (root_id,),
        ),
    ]

    def sanitize(value):
        if isinstance(value, str):
            return fingerprint(value)[1]
        if isinstance(value, list):
            return [sanitize(v) for v in value]
        if isinstance(value, dict):
            return {k: sanitize(v) for k, v in value.items()}
        return value

    result = {}
    with postgres_observer(url, database) as connection:
        with connection.cursor() as cursor:
            for name, statement, parameters in cases:
                cursor.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, WAL, FORMAT JSON) " + statement,
                    parameters,
                )
                result[name] = sanitize(cursor.fetchone()[0])
    return result


def worker(args):
    spec = importlib.util.spec_from_file_location(
        "benchmark", ROOT / "evaluation/observability/run_benchmark.py"
    )
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    clean = bench.isolated_environment(os.environ)
    os.environ.clear()
    os.environ.update(clean)
    for key in (
        "TEST_DBOS_DATABASE_URL",
        "TEST_DBOS_NEO4J_URI",
        "TEST_DBOS_NEO4J_PASSWORD",
    ):
        if not os.environ.get(key):
            raise ValueError("Explicit isolated setting missing: " + key)
    bench.local(os.environ["TEST_DBOS_DATABASE_URL"])
    bench.local(os.environ["TEST_DBOS_NEO4J_URI"])
    bench.local(args.healthy_url)
    os.environ.update(
        PYTHON_DOTENV_DISABLED="1",
        LANGSMITH_TRACING="false",
        OPENAI_AGENTS_DISABLE_TRACING="1",
    )
    bench.apply_source_root(ROOT)
    sys.path.insert(0, str(ROOT / "tests"))
    from dbos import DBOS
    from dbos_support import NativeCase

    from open_deep_research.web.history import Neo4jHistoryStore
    from open_deep_research.workflow.run_facts import RunFacts

    probe = SQLProbe()
    probe.install()
    observer = postgres_observer(os.environ["TEST_DBOS_DATABASE_URL"])
    guard = threading.Lock()
    before = after = None
    plans = {}
    timeline = []
    waits, resources = [], []
    stop = threading.Event()
    monitor = postgres_observer(os.environ["TEST_DBOS_DATABASE_URL"])

    def sample_waits():
        while not stop.wait(0.5):
            if probe.started is None or probe.db_name is None:
                continue
            sample_start = (time.perf_counter() - probe.started) * 1000
            try:
                with monitor.cursor() as cursor:
                    cursor.execute(
                        "SELECT state,wait_event_type,wait_event,count(*) FROM pg_stat_activity WHERE datname=%s GROUP BY 1,2,3",
                        (probe.db_name,),
                    )
                    activity = cursor.fetchall()
                    cursor.execute(
                        "SELECT locktype,mode,granted,count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid WHERE a.datname=%s GROUP BY 1,2,3",
                        (probe.db_name,),
                    )
                    locks = cursor.fetchall()
                waits.append(
                    {
                        "at_ms": (time.perf_counter() - probe.started) * 1000,
                        "observation_start_ms": sample_start,
                        "activity": activity,
                        "locks": locks,
                    }
                )
            except Exception as error:
                waits.append({"error_type": type(error).__name__})

    def sample_resources():
        names = (
            "por-obs-benchmark-postgres-1",
            "por-obs-benchmark-neo4j-test-1",
            "por-obs-langfuse-langfuse-web-1",
            "por-obs-langfuse-langfuse-worker-1",
            "por-obs-langfuse-clickhouse-1",
        )
        while not stop.is_set():
            if probe.started is not None:
                sample_start = (time.perf_counter() - probe.started) * 1000
                try:
                    result = subprocess.run(
                        [
                            "docker",
                            "stats",
                            "--no-stream",
                            "--format",
                            "{{json .}}",
                            *names,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=8,
                        creationflags=subprocess.CREATE_NO_WINDOW
                        if os.name == "nt"
                        else 0,
                    )
                    rows = [
                        json.loads(line)
                        for line in result.stdout.splitlines()
                        if line.strip()
                    ]
                    sample = {
                        "observation_start_ms": sample_start,
                        "containers": [
                            {
                                k: r[k]
                                for k in (
                                    "Name",
                                    "CPUPerc",
                                    "MemUsage",
                                    "BlockIO",
                                    "NetIO",
                                    "PIDs",
                                )
                            }
                            for r in rows
                        ],
                    }
                    if os.name == "nt":
                        command = "$c=Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter \"Name='_Total'\"; $d=Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk -Filter \"Name='_Total'\"; $n=Get-CimInstance Win32_PerfFormattedData_Tcpip_NetworkInterface; @{cpu_percent=[double]$c.PercentProcessorTime;disk_bytes_sec=[double]$d.DiskBytesPersec;network_bytes_sec=($n|Measure-Object BytesTotalPersec -Sum).Sum}|ConvertTo-Json -Compress"
                        host = subprocess.run(
                            ["powershell", "-NoProfile", "-Command", command],
                            capture_output=True,
                            text=True,
                            timeout=8,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                        sample["host"] = json.loads(host.stdout)
                    sample["at_ms"] = (time.perf_counter() - probe.started) * 1000
                    resources.append(sample)
                except Exception as error:
                    resources.append({"error_type": type(error).__name__})
            stop.wait(5)

    sampler = threading.Thread(target=sample_waits, daemon=True)
    resource_sampler = threading.Thread(target=sample_resources, daemon=True)
    sampler.start()
    resource_sampler.start()
    original_create = NativeCase.create

    def create(case, **kwargs):
        nonlocal before
        owner = kwargs.get("owner", "alice")
        if not owner.startswith("bench--"):
            with guard:
                if probe.started is None:
                    before = server_snapshot(observer, probe.db_name)
                    probe.reset()
        token = SCOPE.set("api-create:" + owner)
        try:
            result = original_create(case, **kwargs)
            probe.run_aliases["api-create:" + owner] = result["run_id"]
            probe.run_aliases["observer:" + result["run_id"]] = result["run_id"]
            return result
        finally:
            SCOPE.reset(token)

    NativeCase.create = create
    original_wait = bench.wait_for_archive

    def wait(case, run, timeout=120):
        token = SCOPE.set("observer:" + run["run_id"])
        try:
            probe.marker("archive_receipt_wait", "start")
            result = original_wait(case, run, timeout)
            probe.marker("complete_projection_visible", "end")
            visible_at = (
                (time.perf_counter() - probe.started) * 1000
                if probe.started is not None
                else None
            )
            if probe.started is not None:
                statuses = case.client.client.list_workflows(
                    workflow_id_prefix=run["run_id"],
                    load_input=False,
                    load_output=False,
                )
                steps = {}
                for status in statuses:
                    entries = case.client.client.list_workflow_steps(
                        status.workflow_id, load_output=False
                    )
                    steps[status.workflow_id] = [
                        {
                            key: value
                            for key, value in entry.items()
                            if key
                            in {
                                "function_id",
                                "function_name",
                                "started_at_epoch_ms",
                                "completed_at_epoch_ms",
                                "child_workflow_id",
                            }
                        }
                        for entry in entries
                    ]
                timeline.append(
                    {
                        "run_id": run["run_id"],
                        "visible_at_ms": visible_at,
                        "native": [
                            {
                                "id": s.workflow_id,
                                "name": s.name,
                                "status": s.status,
                                "created_at": s.created_at,
                                "updated_at": s.updated_at,
                                "started_at_epoch_ms": getattr(
                                    s, "started_at_epoch_ms", None
                                ),
                            }
                            for s in statuses
                        ],
                        "steps": steps,
                    }
                )
            return result
        finally:
            SCOPE.reset(token)

    bench.wait_for_archive = wait
    from dbos import DBOSClient

    from open_deep_research.web.durable import RunClient

    original_get_event = DBOSClient.get_event

    def get_event(client, workflow_id, key, timeout_seconds=60):
        result = original_get_event(client, workflow_id, key, timeout_seconds)
        if (
            SCOPE.get().startswith("observer:")
            and key == "history"
            and timeout_seconds > 0
        ):
            probe.marker("archive_receipt_readable", "end")
        return result

    DBOSClient.get_event = get_event
    original_get = RunClient.get

    def get_run(client, *values, **kwargs):
        probe.marker("full_run_projection", "start")
        try:
            return original_get(client, *values, **kwargs)
        finally:
            probe.marker("full_run_projection", "end")

    RunClient.get = get_run
    for target, method in (
        (Neo4jHistoryStore, "save"),
        (RunFacts, "release"),
        (RunFacts, "publish"),
    ):
        original = getattr(target, method)

        def observed(*values, _original=original, _method=method, **kwargs):
            label = _method
            if _method == "publish" and len(values) > 3:
                kind = values[3]
                if kind in {
                    "sdk_event",
                    "run_status",
                    "done",
                    "usage",
                    "report",
                    "settlement",
                    "history_saved",
                    "error",
                    "archive_pending",
                    "research_followup",
                }:
                    label += ":" + kind
            probe.marker(label, "start")
            try:
                return _original(*values, **kwargs)
            finally:
                probe.marker(label, "end")

        setattr(target, method, observed)

    if args.executor_no_ping:
        from unittest.mock import Mock

        from open_deep_research.web import executor

        constructor = executor.DBOS

        def configured(*values, **kwargs):
            config = kwargs.get("config")
            if not isinstance(config, dict) or not str(
                config.get("executor_id", "")
            ).startswith("benchmark-"):
                raise ValueError("Experiment only applies to benchmark executor")
            config = dict(config)
            config["db_engine_kwargs"] = {
                **config.get("db_engine_kwargs", {}),
                "pool_pre_ping": False,
            }
            kwargs["config"] = config
            return constructor(*values, **kwargs)

        executor.DBOS = Mock(wraps=constructor, side_effect=configured)

    original_destroy = DBOS.destroy

    def destroy(*values, **kwargs):
        nonlocal after, plans
        if probe.started is not None and after is None:
            probe.marker("benchmark_run_window", "end")
            probe.phase = "drain"
        result = original_destroy(*values, **kwargs)
        if probe.started is not None and after is None:
            probe.marker("diagnostic_capture", "end")
            probe.freeze()
            stop.set()
            time.sleep(1.1)
            after = server_snapshot(observer, probe.db_name)
            if timeline:
                plans = explain_reads(
                    os.environ["TEST_DBOS_DATABASE_URL"],
                    probe.db_name,
                    timeline[0]["run_id"],
                )
        return result

    DBOS.destroy = staticmethod(destroy)
    benchmark_output = args.output.with_name(args.output.stem + "-benchmark.json")
    try:
        bench.worker(
            SimpleNamespace(
                case=args.case,
                healthy_url=args.healthy_url,
                output=benchmark_output,
                runs=args.runs,
                warmup=args.warmup,
            )
        )
    finally:
        stop.set()
        sampler.join(5)
        resource_sampler.join(18)
        monitor.close()
        capture_end = probe.frozen_at_ms
        for sample in waits + resources:
            sample["within_capture"] = (
                capture_end is not None
                and sample.get("at_ms", float("inf")) <= capture_end
            )
        result = {
            "method_version": "sql-event-v2.1",
            "case": args.case,
            "executor_no_ping": args.executor_no_ping,
            "client": probe.snapshot(),
            "server_before": before,
            "server_after": after,
            "server_delta": server_delta(before, after) if before and after else None,
            "timeline": timeline,
            "explain_read_plans": plans,
            "wait_samples": waits,
            "resource_samples": resources,
            "benchmark_output": str(benchmark_output),
            "limitations": [
                "Diagnostic probes change timing; not formal performance samples",
                "Pool connect excluding ping still includes new connection/validation/queue costs",
                "Execute excluding cursor includes compilation and probe overhead; deferred fetch/application decode is not isolated",
                "Commit time includes protocol/WAL acknowledgement; WAL IO timers are server-wide and not safely subtracted per transaction",
                "Retry attempts are not inferred from repeated fingerprints; only observed failures/sqlstates are counted",
                "Failure counters describe call boundaries; a propagated exception can occur at multiple nested boundaries",
                "Resource samples with within_capture=false are retained but excluded from the capture-window summary",
            ],
        }
        args.output.write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8"
        )
        observer.close()
        probe.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-local-services", action="store_true")
    parser.add_argument(
        "--case",
        choices=("off", "healthy", "concurrent_off", "concurrent"),
        required=True,
    )
    parser.add_argument("--healthy-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=4)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--executor-no-ping", action="store_true")
    args = parser.parse_args()
    if not args.enable_local_services or min(args.runs, args.warmup) < 1:
        parser.error("Explicit isolated services, positive samples and warmup required")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    worker(args)


if __name__ == "__main__":
    main()
