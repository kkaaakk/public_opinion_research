"""Parameter-free diagnostic SQL fingerprints and unambiguous measurements."""

import importlib.util
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "sql_probe",
    Path(__file__).resolve().parents[1] / "evaluation/observability/sql_probe.py",
)
probe_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe_module)


@pytest.mark.parametrize(
    "literal",
    [
        "'PRIVATE_PROMPT'",
        "E'PRIVATE\\'PASSWORD'",
        "$$PRIVATE_MEMORY$$",
        "$body$PRIVATE_RESPONSE$body$",
        "'quoted ''PRIVATE_TOOL'''",
    ],
)
def test_fingerprint_removes_literal_content(literal):
    key, sql = probe_module.fingerprint(
        "SELECT " + literal + " /* PRIVATE_COMMENT */ -- PRIVATE_LINE\n"
    )
    assert "private" not in sql
    assert len(key) == 20


def test_bind_syntax_and_volatile_database_ids_are_normalized():
    assert (
        probe_module.fingerprint("SELECT * FROM t WHERE id=%(id)s")[0]
        == probe_module.fingerprint("SELECT * FROM t WHERE id=$1")[0]
    )
    assert (
        probe_module.fingerprint("CREATE DATABASE por_test_" + "a" * 32)[0]
        == probe_module.fingerprint("CREATE DATABASE por_test_" + "b" * 32)[0]
    )


def test_distribution_keeps_unknown_distinct_from_zero():
    assert probe_module.distribution([]) is None
    value = probe_module.distribution([1, 2, 3, 4])
    assert value["n"] == 4 and value["total_ms"] == 10
    assert value["p50"] == 2.5 and value["p95"] == pytest.approx(3.85)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT /* comment ' */ 'PRIVATE_SENTINEL'",
        "SELECT $$x'$$, 'PRIVATE_SENTINEL'",
        "SELECT '\\', 'PRIVATE_SENTINEL'",
        "SELECT /* outer /* nested ' */ comment */ 'PRIVATE_SENTINEL'",
        "SELECT E'escaped\\'PRIVATE_SENTINEL'",
        "SELECT $tag$'PRIVATE_SENTINEL$tag$",
        'SELECT "PRIVATE_SENTINEL" FROM por_dbos.workflow_status',
        "SELECT 'PRIVATE_SENTINEL",
        "SELECT $tag$PRIVATE_SENTINEL",
        "SELECT /* PRIVATE_SENTINEL",
    ],
)
def test_postgres_lexical_combinations_fail_closed(sql):
    assert "private" not in probe_module.fingerprint(sql)[1]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT $é$PRIVATE SENTINEL$é$",
        "SELECT $中$PRIVATE: value$中$",
        "SELECT $😀$PRIVATE VALUE$😀$",
        "SELECT 123456789e12",
        "SELECT 0x1DEADBEEF",
        "SELECT 123_456_789",
        "SELECT .123456e-89",
        "SELECT 0b10101010",
        "SELECT 0o1234567",
    ],
)
def test_unicode_tags_and_all_pg17_numeric_forms_are_redacted(sql):
    normalized = probe_module.fingerprint(sql)[1]
    assert "private" not in normalized
    assert not any(
        marker in normalized for marker in ("123456", "deadbeef", "10101010", "456_789")
    )


def test_custom_unicode_escape_cannot_expose_adjacent_literals():
    sql = r"""SELECT U&'slash\' UESCAPE '!', 'PRIVATE_SENTINEL', E'quote\''"""
    assert "private" not in probe_module.fingerprint(sql)[1]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT foo$tag$ || $tag$PRIVATE_SENTINEL$tag$ AS foo$tag$ FROM t",
        r"""SELECT 😀E'slash\', 'PRIVATE_SENTINEL', E'quote\''""",
    ],
)
def test_identifier_boundaries_cannot_change_string_delimiters(sql):
    assert "private" not in probe_module.fingerprint(sql)[1]


def test_carriage_return_ends_a_postgres_line_comment():
    sql = "SELECT 1 -- comment\r, 'x\nPRIVATE_SENTINEL', E'quote\\''"
    assert "private" not in probe_module.fingerprint(sql)[1]


def test_probe_never_reads_or_renders_bind_parameters():
    class SecretParameters:
        def __repr__(self):
            raise AssertionError("Sensitive parameters rendered")

        def __iter__(self):
            raise AssertionError("Sensitive parameters read")

    probe = probe_module.SQLProbe()
    probe.identity = lambda: ("run-a", "run-a:events", 12)
    probe.callsite = lambda: "dbos/_sys_db.py:write_stream_from_workflow"
    probe.reset()
    conn = SimpleNamespace(
        engine=SimpleNamespace(url=SimpleNamespace(database="por_test_" + "a" * 32))
    )
    context, cursor = object(), SimpleNamespace(rowcount=3)
    params = SecretParameters()
    probe.before(
        conn,
        cursor,
        "SELECT * FROM por_dbos.streams WHERE value=%(value)s",
        params,
        context,
        False,
    )
    probe.after(conn, cursor, None, params, context, False)
    row = probe.snapshot()["sql"][0]
    assert row["timing_ms"]["n"] == 1 and row["rows"] == 3
    assert row["by_run"] == {"run-a": 1}
    assert row["categories"] == {"dbos_stream": 1}
    assert len(row["samples"]) == 1
    assert row["samples"][0][1:] == (
        "dbos_stream",
        "run-a",
        "run-a:events",
        "measurement",
    )
    assert row["samples"][0][0] == row["timing_ms"]["total_ms"]


def test_failure_records_type_and_sqlstate_but_not_exception_message():
    probe = probe_module.SQLProbe()
    probe.identity = lambda: ("run-a", "run-a", 1)
    probe.callsite = lambda: "workflow/run_facts.py:request"
    probe.reset()
    conn = SimpleNamespace(
        engine=SimpleNamespace(url=SimpleNamespace(database="por_test_" + "a" * 32))
    )
    context = object()
    probe.before(conn, None, "SELECT * FROM por_domain.requests", None, context, False)
    error = RuntimeError("PRIVATE_PASSWORD")
    error.sqlstate = "40001"
    probe.error(SimpleNamespace(original_exception=error, execution_context=context))
    row = probe.snapshot()["sql"][0]
    assert row["failures"] == 1 and row["sqlstates"] == {"40001": 1}
    assert "PRIVATE" not in str(row)
    assert row["rowcount_known"] == 0


def test_frozen_probe_rejects_late_cleanup_data():
    probe = probe_module.SQLProbe()
    probe.identity = lambda: ("run-a", "run-a", 1)
    probe.callsite = lambda: "workflow/run_facts.py:request"
    probe.reset()
    probe.freeze()
    conn = SimpleNamespace(
        engine=SimpleNamespace(url=SimpleNamespace(database="por_test_" + "a" * 32))
    )
    context = object()
    probe.before(conn, None, "SELECT 1", None, context, False)
    probe.after(conn, SimpleNamespace(rowcount=1), None, None, context, False)
    probe.timing("late", 1)
    probe.marker("cleanup", "end")
    result = probe.snapshot()
    assert not result["sql"] and not result["timings"] and not result["phases"]
    assert result["frozen"] is True


@pytest.mark.parametrize("operation", ["timing", "failure", "cursor"])
def test_inflight_writer_cannot_append_after_freeze(operation):
    probe = probe_module.SQLProbe()
    probe.identity = lambda: ("run-a", "run-a", 1)
    probe.callsite = lambda: "workflow/run_facts.py:request"
    probe.reset()
    conn = SimpleNamespace(
        engine=SimpleNamespace(url=SimpleNamespace(database="por_test_" + "a" * 32))
    )
    context = object()
    probe.before(conn, None, "SELECT 1", None, context, False)
    reached, resume = threading.Event(), threading.Event()
    underlying = threading.Lock()

    class GateLock:
        def __enter__(self):
            if threading.current_thread() is worker:
                reached.set()
                assert resume.wait(3)
            return underlying.__enter__()

        def __exit__(self, *args):
            return underlying.__exit__(*args)

    probe.lock = GateLock()

    def write():
        if operation == "timing":
            probe.timing("late", 1)
        elif operation == "failure":
            probe.failure("late", RuntimeError("PRIVATE_MESSAGE"))
        else:
            probe.after(conn, SimpleNamespace(rowcount=1), None, None, context, False)

    worker = threading.Thread(target=write)
    worker.start()
    try:
        assert reached.wait(3), "Writer must reach the boundary before freeze"
        probe.freeze()
    finally:
        resume.set()
        worker.join(3)
    assert not worker.is_alive()
    result = probe.snapshot()
    assert not result["sql"] and not result["timings"]
    assert not result["uncursored_operation_failures"]


def test_probe_install_is_not_nested_and_public_hooks_restore():
    from sqlalchemy.engine import Connection
    from sqlalchemy.pool import Pool

    execute, connect = Connection.execute, Pool.connect
    probe = probe_module.SQLProbe()
    try:
        probe.install()
        with pytest.raises(RuntimeError, match="already installed"):
            probe.install()
    finally:
        probe.close()
    assert Connection.execute is execute and Pool.connect is connect
