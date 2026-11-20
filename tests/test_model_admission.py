# 验证跨 Agent 模型准入在未结算账单上等待而非直接失败。
"""Sequential model-bill admission without holding a transaction while waiting."""

import pytest
from dbos import error as dbos_error

from open_deep_research import budget
from open_deep_research.configuration import Configuration
from open_deep_research.workflow import dbos_config, execution


class _DB:
    def __init__(self):
        self.executions = []

    def scalar(self, *_args, **_kwargs):
        return None

    def execute(self, statement, parameters):
        self.executions.append((str(statement), dict(parameters)))


class _Transaction:
    def __init__(self, engine):
        self.engine = engine
        self.db = _DB()

    def __enter__(self):
        self.engine.entered += 1
        return self.db

    def __exit__(self, *_exc):
        self.engine.exited += 1
        self.engine.transactions.append(self.db)
        return False


class _Engine:
    def __init__(self):
        self.entered = 0
        self.exited = 0
        self.transactions = []

    def begin(self):
        return _Transaction(self)


def _scope(*, token_limit=True):
    return type(
        "Scope",
        (),
        {
            "run_id": "run",
            "stage_id": "stage",
            "expires_at": None,
            "config": Configuration(
                budget_enabled=True,
                max_model_calls=30,
                max_input_tokens=1_000 if token_limit else None,
                max_output_tokens=1_000 if token_limit else None,
            ),
            "initial_usage": {},
            "reserve_report": True,
        },
    )()


def _install(monkeypatch):
    engine = _Engine()
    monkeypatch.setattr(dbos_config, "domain_engine", lambda: engine)
    monkeypatch.setattr(execution, "require_live_effect", lambda *_args: None)
    monkeypatch.setattr(budget.time, "sleep", lambda _seconds: None)
    return engine


def test_blocked_model_call_waits_without_holding_a_transaction(monkeypatch):
    engine = _install(monkeypatch)
    bills = iter([({}, 1, False), ({}, 0, True)])
    monkeypatch.setattr(budget, "model_bill", lambda _db, _run: next(bills))
    monkeypatch.setattr(
        budget, "require_physical_model_budget", lambda *_args, **_kwargs: None
    )
    sleeps = []

    def sleep(seconds):
        # The blocked transaction has already been closed before this sleep.
        assert engine.entered == engine.exited
        sleeps.append(seconds)

    monkeypatch.setattr(budget.time, "sleep", sleep)

    budget.start_model_attempt(
        _scope(), "workflow", "executor", "call-1", wait_seconds=5
    )

    assert len(engine.transactions) == 2
    assert engine.entered == engine.exited == 2
    assert sleeps == [budget.MODEL_BILL_POLL_SECONDS]
    inserts = [
        parameters
        for statement, parameters in engine.transactions[-1].executions
        if "INSERT INTO por_domain.model_attempts" in statement
    ]
    assert inserts == [{"r": "run", "id": "call-1", "stage": "stage"}]


def test_model_admission_times_out_on_an_unsettled_bill(monkeypatch):
    engine = _install(monkeypatch)
    monkeypatch.setattr(budget, "model_bill", lambda _db, _run: ({}, 1, False))
    monkeypatch.setattr(
        budget, "require_physical_model_budget", lambda *_args, **_kwargs: None
    )

    with pytest.raises(budget.ModelBudgetExceeded, match="timed out"):
        budget.start_model_attempt(
            _scope(), "workflow", "executor", "call-1", wait_seconds=0
        )

    assert engine.entered == engine.exited == 1
    assert not any(
        "INSERT INTO por_domain.model_attempts" in statement
        for db in engine.transactions
        for statement, _ in db.executions
    )


def test_waiting_model_admission_observes_cancellation(monkeypatch):
    engine = _install(monkeypatch)
    calls = {"count": 0}

    def live(*_args):
        calls["count"] += 1
        if calls["count"] == 2:
            raise dbos_error.DBOSWorkflowCancelledError("cancelled")

    monkeypatch.setattr(execution, "require_live_effect", live)
    monkeypatch.setattr(budget, "model_bill", lambda _db, _run: ({}, 1, False))

    with pytest.raises(dbos_error.DBOSWorkflowCancelledError):
        budget.start_model_attempt(
            _scope(), "workflow", "executor", "call-1", wait_seconds=5
        )

    assert calls["count"] == 2
    assert engine.entered == engine.exited == 2


def test_pending_bill_does_not_block_without_a_token_ceiling(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(budget, "model_bill", lambda _db, _run: ({}, 1, False))

    budget.start_model_attempt(
        _scope(token_limit=False),
        "workflow",
        "executor",
        "call-1",
        wait_seconds=0,
    )


def test_finalized_bill_without_usage_fails_closed_immediately(monkeypatch):
    engine = _install(monkeypatch)
    sleeps = []
    monkeypatch.setattr(budget.time, "sleep", sleeps.append)
    monkeypatch.setattr(budget, "model_bill", lambda _db, _run: ({}, 0, False))

    with pytest.raises(budget.ModelBudgetExceeded, match="unconfirmed model bill"):
        budget.start_model_attempt(
            _scope(), "workflow", "executor", "call-1", wait_seconds=300
        )

    assert sleeps == []
    assert engine.entered == engine.exited == 1


def test_budget_rejection_still_propagates(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(budget, "model_bill", lambda _db, _run: ({}, 0, True))

    def reject(*_args, **_kwargs):
        raise budget.ModelBudgetExceeded("Physical call receipts exhausted")

    monkeypatch.setattr(budget, "require_physical_model_budget", reject)

    with pytest.raises(budget.ModelBudgetExceeded, match="receipts exhausted"):
        budget.start_model_attempt(
            _scope(), "workflow", "executor", "call-1", wait_seconds=0
        )
