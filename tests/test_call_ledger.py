# 验证模型、工具与外部操作的调用记录各自只有一处权威来源。
"""Call-ledger contracts: one row per real call, and one owner per number."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError
from test_tool_budget import _executor

from open_deep_research import budget
from open_deep_research.configuration import Configuration
from open_deep_research.runtime import tool_executor
from open_deep_research.runtime.sdk_context import ObservedToolResult
from open_deep_research.workflow import execution
from open_deep_research.workflow.execution import stage_context


class _Cursor:
    def __init__(self, *, rowcount=0, rows=()):
        self.rowcount = rowcount
        self._rows = list(rows)

    def one(self):
        return self._rows[0]

    def all(self):
        return self._rows


class _DB:
    """Record every statement and answer the three shapes this ledger uses."""

    def __init__(self, *, exists=None, counts=(0, 0), updated=1):
        self.exists = exists
        self.counts = counts
        self.updated = updated
        self.statements = []

    def scalar(self, statement, parameters=None):
        self.statements.append((str(statement), dict(parameters or {})))
        return self.exists

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.statements.append((sql, dict(parameters or {})))
        if "count(*)" in sql:
            return _Cursor(rows=[self.counts])
        if sql.lstrip().startswith("UPDATE"):
            return _Cursor(rowcount=self.updated)
        return _Cursor(rowcount=1)

    def insert(self, table):
        return next(
            parameters
            for statement, parameters in self.statements
            if f"INSERT INTO por_domain.{table}" in statement
        )


class _Engine:
    def __init__(self, db):
        self.db = db

    def begin(self):
        return _Tx(self.db)


class _Tx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *_exc):
        return False


def _install(monkeypatch, db):
    monkeypatch.setattr(execution, "domain_engine", lambda: _Engine(db))
    monkeypatch.setattr(execution, "require_live_effect", lambda *_args: None)
    return db


def _config(**limits):
    return Configuration(research_model="openai:fixture", **limits)


def test_admission_records_a_call_when_budget_checks_are_off(monkeypatch):
    db = _install(monkeypatch, _DB())

    admitted = execution.admit_tool_call(
        _config(budget_enabled=False), "run", "wf", "executor", "call-1", "web_search", True
    )

    assert admitted is True
    assert db.insert("tool_calls") == {
        "r": "run",
        "id": "call-1",
        "name": "web_search",
        "search": True,
    }


def test_a_rejected_call_is_never_recorded_as_admitted(monkeypatch):
    db = _install(monkeypatch, _DB(counts=(1, 0)))

    admitted = execution.admit_tool_call(
        _config(budget_enabled=True, max_tool_calls=1),
        "run",
        "wf",
        "executor",
        "call-2",
        "load_skill",
        False,
    )

    assert admitted is False
    assert not any("INSERT INTO por_domain.tool_calls" in sql for sql, _ in db.statements)


def test_a_repeated_call_identity_is_not_charged_again(monkeypatch):
    db = _install(monkeypatch, _DB(exists=1))

    admitted = execution.admit_tool_call(
        _config(budget_enabled=True, max_tool_calls=1),
        "run",
        "wf",
        "executor",
        "call-1",
        "web_search",
        True,
    )

    assert admitted is True
    assert not any("count(*)" in sql for sql, _ in db.statements)


def test_settling_a_call_keeps_its_first_outcome(monkeypatch):
    db = _install(monkeypatch, _DB(updated=0, exists=1))

    assert execution.finish_tool_call("run", "call-1", "failed") is True

    statement, parameters = db.statements[0]
    assert "UPDATE por_domain.tool_calls SET status=:status" in statement
    assert parameters == {"r": "run", "id": "call-1", "status": "failed"}


def test_a_missing_call_receipt_is_reported_without_raising(monkeypatch):
    _install(monkeypatch, _DB(updated=0, exists=None))

    # The journal still owns the tool outcome; a missing status row is a gap in
    # the ledger, not an unconfirmed external effect.
    assert execution.finish_tool_call("run", "call-1", "returned") is False


def test_an_operation_records_the_tool_call_it_belongs_to(monkeypatch):
    db = _install(monkeypatch, _DB())

    execution.start_operation("run", "wf", "executor", "op-1", "call-1", "publish", "fp")

    assert db.insert("operations")["tool_call"] == "call-1"


def test_tool_ledger_counts_read_the_admission_table():
    assert budget.tool_ledger(_DB(counts=(7, 3)), "run") == {
        "tool_calls": 7,
        "search_calls": 3,
    }


def test_run_usage_takes_each_number_from_its_own_ledger():
    usage = budget.compose_run_usage(
        {"model_calls": 2, "tool_calls": 1, "search_calls": 1},
        {"model_calls": 3, "input_tokens": 10, "total_tokens": 10},
        {"tool_calls": 4, "search_calls": 2},
        {
            "tool_calls": 4,
            "search_calls": 2,
            "budget_warnings": ["tool_calls is at 5/5."],
            "degradation_reasons": ["Skipped research compression."],
        },
    )

    assert usage["model_calls"] == 5
    assert usage["input_tokens"] == 10
    assert usage["tool_calls"] == 5
    assert usage["search_calls"] == 3
    assert usage["budget_warnings"] == ["tool_calls is at 5/5."]
    assert usage["degradation_reasons"] == ["Skipped research compression."]


def _record(status):
    return ObservedToolResult(
        sdk_run_id="SDK_run_public_signal",
        tool_call_id="call-1",
        tool_name="web_search",
        arguments_json="{}",
        args={},
        model_turn=1,
        status=status,
    )


def _settlement(monkeypatch, observed):
    """Settle one observed call with the ledger write captured, not stored."""
    calls = []
    monkeypatch.setattr(
        tool_executor, "finish_tool_call", lambda *args: calls.append(args)
    )
    executor = _executor(_config(budget_enabled=True, max_tool_calls=1))
    token = stage_context.set(SimpleNamespace(run_id="run-1"))
    try:
        asyncio.run(executor._settle_call(_record(observed)))
    finally:
        stage_context.reset(token)
    return calls


def test_a_succeeded_tool_call_settles_as_returned(monkeypatch):
    # The Harness reports ``succeeded``; the ledger's success terminal is
    # ``returned``. Passing the Harness status through left rows at 'admitted'.
    calls = _settlement(monkeypatch, "succeeded")

    assert [(args[0], args[2]) for args in calls] == [("run-1", "returned")]


@pytest.mark.parametrize("observed", ["failed", "cancelled"])
def test_failed_and_cancelled_calls_keep_their_ledger_status(monkeypatch, observed):
    calls = _settlement(monkeypatch, observed)

    assert [(args[0], args[2]) for args in calls] == [("run-1", observed)]


@pytest.mark.parametrize("observed", ["unobserved", "running"])
def test_an_unsettled_observation_is_never_written_as_success(monkeypatch, observed):
    assert _settlement(monkeypatch, observed) == []


def test_a_settlement_write_failure_never_masks_the_tool(monkeypatch, caplog):
    def fail(*_args):
        raise SQLAlchemyError("storage unavailable")

    monkeypatch.setattr(tool_executor, "finish_tool_call", fail)
    executor = _executor(_config(budget_enabled=True, max_tool_calls=1))
    token = stage_context.set(SimpleNamespace(run_id="run-1"))
    try:
        asyncio.run(executor._settle_call(_record("failed")))
    finally:
        stage_context.reset(token)

    assert "Could not record tool call outcome" in caplog.text
