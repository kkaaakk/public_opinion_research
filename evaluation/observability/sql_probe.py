"""Offline-test SQL measurements; no exporter, scheduler or production hook."""

from __future__ import annotations

import hashlib
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from contextvars import ContextVar
from functools import lru_cache

SCOPE = ContextVar("sql_diagnostic_scope", default="background")
_EXECUTION = ContextVar("sql_diagnostic_execution", default=None)
_ACQUIRE = ContextVar("sql_diagnostic_acquire", default=None)
_NUMBER = re.compile(
    r"(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9][0-9_]*)(?:[eE][+-]?[0-9_]+)?)"
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def redact_sql(statement):
    """Scan PG17 standard-conforming strings/comments before any normalization.

    Unknown/unterminated quoting is opaque. Quoted identifiers are hashed rather
    than copied, so a user-defined alias cannot become a content escape hatch.
    This scanner never interprets or formats the DBAPI bind parameters.
    """
    result = []
    size, index = len(statement), 0
    while index < size:
        if statement.startswith("--", index):
            ends = [statement.find(c, index + 2) for c in ("\r", "\n")]
            end = min((value for value in ends if value >= 0), default=size)
            index = min(end + 1, size)
            result.append(" ")
            continue
        if statement.startswith("/*", index):
            depth, index = 1, index + 2
            while index < size and depth:
                if statement.startswith("/*", index):
                    depth, index = depth + 1, index + 2
                elif statement.startswith("*/", index):
                    depth, index = depth - 1, index + 2
                else:
                    index += 1
            if depth:
                return None
            result.append(" ")
            continue
        char = statement[index]
        if ord(char) >= 128:
            # PG identifier bytes are broader than Python isalpha/isalnum.
            # Unknown unquoted identifier syntax must not change quote boundaries.
            return None
        if statement[index : index + 2].lower() == "u&" and statement[
            index + 2 : index + 3
        ] in {"'", '"'}:
            # UESCAPE changes how backslashes are interpreted. Do not approximate it.
            return None
        identifier = _IDENTIFIER.match(statement, index)
        if identifier and statement[index : index + 2].lower() != "e'":
            result.append(identifier.group())
            index = identifier.end()
            continue
        if char == "$":
            if index + 1 < size and statement[index + 1].isdigit():
                index += 2
                while index < size and statement[index].isdigit():
                    index += 1
                result.append("?")
                continue
            delimiter_end = statement.find("$", index + 1)
            if delimiter_end >= 0:
                tag = statement[index + 1 : delimiter_end]
                valid_first = (
                    not tag or tag[0].isalpha() or tag[0] == "_" or ord(tag[0]) >= 128
                )
                valid_rest = all(c.isalnum() or c == "_" or ord(c) >= 128 for c in tag)
                if not valid_first or not valid_rest:
                    return None
                marker = statement[index : delimiter_end + 1]
                end = statement.find(marker, index + len(marker))
                if end < 0:
                    return None
                index = end + len(marker)
                result.append("?")
                continue
            return None
        quote = index if char == "'" else None
        escaped = False
        boundary = index == 0 or not (
            statement[index - 1].isalnum() or statement[index - 1] in "_$"
        )
        if boundary and (
            char.isdigit()
            or char == "."
            and index + 1 < size
            and statement[index + 1].isdigit()
        ):
            number = _NUMBER.match(statement, index)
            if number:
                index = number.end()
                result.append("?")
                continue
        if boundary and char in "Ee" and statement[index : index + 2].lower() == "e'":
            quote, escaped = index + 1, True
        if quote is not None:
            index = quote + 1
            closed = False
            while index < size:
                if escaped and statement[index] == "\\":
                    index += 2
                elif statement[index] == "\\":
                    return None
                elif statement[index] == "'":
                    if statement.startswith("''", index):
                        index += 2
                    else:
                        index, closed = index + 1, True
                        break
                else:
                    index += 1
            if not closed:
                return None
            result.append("?")
            continue
        if char == '"':
            index += 1
            identifier = []
            closed = False
            while index < size:
                if statement.startswith('""', index):
                    identifier.append('"')
                    index += 2
                elif statement[index] == '"':
                    index, closed = index + 1, True
                    break
                else:
                    identifier.append(statement[index])
                    index += 1
            if not closed:
                return None
            digest = hashlib.sha256("".join(identifier).encode()).hexdigest()[:12]
            result.append("quoted_identifier_" + digest)
            continue
        result.append(char)
        index += 1
    return "".join(result)


@lru_cache(maxsize=1024)
def fingerprint(statement):
    """Keep normalized SQL structure, never bind values or SQL literal content."""
    sql = redact_sql(statement)
    if sql is None:
        sql = "unparseable_sql_redacted"
        return hashlib.sha256(sql.encode()).hexdigest()[:20], sql
    sql = re.sub(r"por_test_[0-9a-f]{32}", "por_test_ID", sql)
    sql = re.sub(r"%\([^)]+\)s|%s|\$\d+", "?", sql)
    sql = " ".join(sql.split()).lower()
    return hashlib.sha256(sql.encode()).hexdigest()[:20], sql


def distribution(values):
    if not values:
        return None
    values = sorted(values)

    def q(p):
        value = (len(values) - 1) * p
        low = int(value)
        high = min(low + 1, len(values) - 1)
        return values[low] + (values[high] - values[low]) * (value - low)

    return {
        "n": len(values),
        "total_ms": sum(values),
        "p50": q(0.5),
        "p95": q(0.95),
        "p99": q(0.99),
        "max": values[-1],
    }


def category(sql, callsite):
    if "por_domain" in sql:
        if "retries" in sql or "approvals" in sql:
            return "application_control"
        return "application_facts"
    if "workflow_events" in sql or "notifications" in sql or "pg_notify" in sql:
        return "dbos_event"
    if "streams" in sql or "stream_entries" in sql:
        return "dbos_stream"
    if any(x in callsite for x in ("queue", "dequeue", "partition", "pending")):
        return "dbos_queue_scheduler"
    if "operation_outputs" in sql or "transaction_outputs" in sql:
        return "dbos_step_journal"
    if "workflow_status" in sql:
        return "dbos_workflow_status"
    if sql.startswith("set ") or "set_config" in sql:
        return "connection_configuration"
    if "run_facts.py:now" in callsite:
        return "application_timestamp"
    return "other_unclassified"


class SQLProbe:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = None
        self.db_name = None
        self.queries = {}
        self.phases = []
        self.transactions = []
        self.statement_start = {}
        self.patches = []
        self.timings = defaultdict(list)
        self.pool_databases = {}
        self.instrumented_dialects = set()
        self.phase = "startup"
        self.frozen = False
        self.frozen_at_ms = None
        self.errors = Counter()
        self.run_aliases = {}
        self.listeners = []
        self.installed = False

    @staticmethod
    def identity():
        from dbos import DBOS

        try:
            workflow = DBOS.workflow_id
            return (
                (workflow.split(":", 1)[0], workflow, DBOS.step_id)
                if workflow
                else (SCOPE.get(), None, None)
            )
        except Exception:
            return SCOPE.get(), None, None

    @staticmethod
    def callsite():
        frame = sys._getframe(2)
        while frame is not None:
            filename = frame.f_code.co_filename.replace("\\", "/")
            if "/dbos/" in filename or "/open_deep_research/" in filename:
                return (
                    filename.rsplit("/", 2)[-2]
                    + "/"
                    + filename.rsplit("/", 1)[-1]
                    + ":"
                    + frame.f_code.co_name
                )
            frame = frame.f_back
        return "unknown"

    def reset(self):
        with self.lock:
            self.queries.clear()
            self.transactions.clear()
            self.phases.clear()
            self.timings.clear()
            self.statement_start.clear()
            self.errors.clear()
            self.run_aliases.clear()
            self.frozen = False
            self.frozen_at_ms = None
            self.started = time.perf_counter()
            self.started_epoch_ms = time.time() * 1000
            self.phase = "measurement"

    def freeze(self):
        with self.lock:
            if self.started is not None and not self.frozen:
                self.frozen_at_ms = (time.perf_counter() - self.started) * 1000
            self.frozen = True

    def failure(self, operation, error):
        code = getattr(error, "sqlstate", None)
        safe = (
            code
            if isinstance(code, str) and re.fullmatch(r"[A-Z0-9]{5}", code)
            else "unknown"
        )
        with self.lock:
            if self.started is not None and not self.frozen:
                self.errors[(operation, type(error).__name__, safe)] += 1

    def before(self, conn, cursor, statement, parameters, context, executemany):
        database = conn.engine.url.database or ""
        if re.fullmatch(r"por_test_[0-9a-f]{32}", database):
            self.db_name = database
        if self.started is None or self.frozen or database != self.db_name:
            return
        key, sql = fingerprint(statement)
        origin = self.callsite()
        identity = self.identity()
        self.statement_start[id(context)] = (
            key,
            sql,
            origin,
            identity,
            executemany,
            self.phase,
            time.perf_counter(),
        )

    def after(self, conn, cursor, statement, parameters, context, executemany):
        self.finish(cursor, context)

    def error(self, exception_context):
        error = exception_context.original_exception
        self.finish(
            None,
            exception_context.execution_context,
            type(error).__name__,
            getattr(error, "sqlstate", None),
        )

    def finish(self, cursor, context, error_type=None, sqlstate=None):
        entry = self.statement_start.pop(id(context), None)
        if entry is None:
            return
        key, sql, origin, identity, many, phase, start = entry
        if self.frozen:
            return
        elapsed = (time.perf_counter() - start) * 1000
        current = _EXECUTION.get()
        if current is not None:
            current["cursor_ms"] += elapsed
        with self.lock:
            if self.frozen:
                return
            row = self.queries.setdefault(
                key,
                {
                    "sql": sql,
                    "categories": Counter(),
                    "callsites": Counter(),
                    "by_run": Counter(),
                    "by_workflow": Counter(),
                    "ms": [],
                    "samples": [],
                    "rowcount_known": 0,
                    "rows": 0,
                    "executemany": 0,
                    "failures": 0,
                    "error_types": Counter(),
                    "sqlstates": Counter(),
                    "phases": Counter(),
                },
            )
            row["ms"].append(elapsed)
            row["phases"][phase] += 1
            source_category = category(sql, origin)
            row["categories"][source_category] += 1
            row["samples"].append(
                (elapsed, source_category, identity[0], identity[1], phase)
            )
            row["callsites"][origin] += 1
            row["by_run"][identity[0]] += 1
            row["by_workflow"][identity[1] or "unassigned"] += 1
            row["executemany"] += int(many)
            if error_type:
                row["failures"] += 1
                row["error_types"][error_type] += 1
                if isinstance(sqlstate, str) and re.fullmatch(r"[A-Z0-9]{5}", sqlstate):
                    row["sqlstates"][sqlstate] += 1
            elif cursor.rowcount >= 0:
                row["rows"] += cursor.rowcount
                row["rowcount_known"] += 1

    def timing(self, name, elapsed, run=None):
        with self.lock:
            if self.started is not None and not self.frozen:
                self.timings[name].append(
                    (elapsed, run or self.identity()[0], self.phase)
                )

    def marker(self, name, kind):
        with self.lock:
            if self.started is not None and not self.frozen:
                run, workflow, step = self.identity()
                self.phases.append(
                    {
                        "name": name,
                        "kind": kind,
                        "at_ms": (time.perf_counter() - self.started) * 1000,
                        "run": run,
                        "workflow": workflow,
                        "step": step,
                    }
                )

    def install(self):
        from sqlalchemy import event
        from sqlalchemy.engine import Connection, Engine
        from sqlalchemy.pool import Pool

        if self.installed:
            raise RuntimeError("Diagnostic probe is already installed")
        self.installed = True

        def listen(name, function):
            event.listen(Engine, name, function)
            self.listeners.append((Engine, name, function))

        listen("before_cursor_execute", self.before)
        listen("after_cursor_execute", self.after)
        listen("handle_error", self.error)

        def commit(conn):
            with self.lock:
                if (
                    self.started is not None
                    and not self.frozen
                    and conn.engine.url.database == self.db_name
                ):
                    self.transactions.append(("commit_attempt", self.identity()[0]))

        def rollback(conn):
            with self.lock:
                if (
                    self.started is not None
                    and not self.frozen
                    and conn.engine.url.database == self.db_name
                ):
                    self.transactions.append(("rollback_attempt", self.identity()[0]))

        listen("commit", commit)
        listen("rollback", rollback)

        def engine_connect(conn):
            database = conn.engine.url.database
            self.pool_databases[id(conn.engine.pool)] = database
            dialect = conn.engine.dialect
            if id(dialect) in self.instrumented_dialects:
                return
            self.instrumented_dialects.add(id(dialect))
            for name in ("do_ping", "do_commit", "do_rollback"):
                original = getattr(dialect, name)

                def measured(*args, _original=original, _name=name, **kwargs):
                    start = time.perf_counter()
                    try:
                        return _original(*args, **kwargs)
                    except BaseException as error:
                        if database == self.db_name:
                            self.failure(_name, error)
                        raise
                    finally:
                        elapsed = (time.perf_counter() - start) * 1000
                        if database == self.db_name:
                            self.timing(_name, elapsed)
                            acquire = _ACQUIRE.get()
                            if _name == "do_ping" and acquire is not None:
                                acquire["ping_ms"] += elapsed

                setattr(dialect, name, measured)
                self.patches.append((dialect, name, original))

        listen("engine_connect", engine_connect)
        original_connect = Pool.connect

        def connect(pool, *args, **kwargs):
            active = {"ping_ms": 0}
            token = _ACQUIRE.set(active)
            start = time.perf_counter()
            try:
                return original_connect(pool, *args, **kwargs)
            except BaseException as error:
                if self.pool_databases.get(id(pool)) == self.db_name:
                    self.failure("pool_connect", error)
                raise
            finally:
                elapsed = (time.perf_counter() - start) * 1000
                if self.pool_databases.get(id(pool)) == self.db_name:
                    self.timing("pool_connect_inclusive", elapsed)
                    self.timing(
                        "pool_connect_excluding_ping",
                        max(0, elapsed - active["ping_ms"]),
                    )
                _ACQUIRE.reset(token)

        Pool.connect = connect
        self.patches.append((Pool, "connect", original_connect))
        original_execute = Connection.execute

        def execute(conn, *args, **kwargs):
            active = {"cursor_ms": 0}
            token = _EXECUTION.set(active)
            start = time.perf_counter()
            try:
                return original_execute(conn, *args, **kwargs)
            except BaseException as error:
                if conn.engine.url.database == self.db_name:
                    self.failure("connection_execute", error)
                raise
            finally:
                elapsed = (time.perf_counter() - start) * 1000
                if conn.engine.url.database == self.db_name:
                    self.timing("connection_execute_inclusive", elapsed)
                    self.timing(
                        "execute_excluding_cursor",
                        max(0, elapsed - active["cursor_ms"]),
                    )
                _EXECUTION.reset(token)

        Connection.execute = execute
        self.patches.append((Connection, "execute", original_execute))

    def close(self):
        from sqlalchemy import event

        for target, name, function in reversed(self.listeners):
            event.remove(target, name, function)
        for target, name, original in reversed(self.patches):
            setattr(target, name, original)
        self.listeners.clear()
        self.patches.clear()
        self.installed = False

    def snapshot(self):
        result = []
        for key, value in self.queries.items():
            row = {k: v for k, v in value.items() if k != "ms"}
            row.update(fingerprint=key, timing_ms=distribution(value["ms"]))
            result.append(row)
        return {
            "database": self.db_name,
            "sql": sorted(
                result, key=lambda r: r["timing_ms"]["total_ms"], reverse=True
            ),
            "transactions": dict(Counter(k for k, _ in self.transactions)),
            "transaction_by_run": [
                {"kind": k, "run": r, "count": n}
                for (k, r), n in Counter(self.transactions).items()
            ],
            "phases": self.phases,
            "timings": {
                key: distribution([row[0] for row in values])
                for key, values in self.timings.items()
            },
            "timing_samples": dict(self.timings),
            "timings_by_phase": {
                phase: {
                    key: distribution([r[0] for r in values if r[2] == phase])
                    for key, values in self.timings.items()
                }
                for phase in ("measurement", "drain")
            },
            "phase": self.phase,
            "frozen": self.frozen,
            "frozen_at_ms": self.frozen_at_ms,
            "sql_sample_fields": ["ms", "category", "run", "workflow", "phase"],
            "started_epoch_ms": getattr(self, "started_epoch_ms", None),
            "uncursored_operation_failures": [
                {"operation": o, "error_type": e, "sqlstate": s, "count": n}
                for (o, e, s), n in self.errors.items()
            ],
            "run_aliases": self.run_aliases,
        }
