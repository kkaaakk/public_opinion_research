"""Real PostgreSQL contracts for atomic Run-level tool admission."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.configuration import Configuration
from open_deep_research.workflow import execution
from open_deep_research.workflow.dbos_config import domain_engine

pytestmark = pytest.mark.integration


def _configuration(**limits):
    return Configuration(
        research_model="openai:fixture", budget_enabled=True, **limits
    )


def _create_request(run_id):
    with domain_engine().begin() as db:
        db.execute(
            text(
                "INSERT INTO por_domain.requests"
                "(id, owner, request_key, session, value) "
                "VALUES (:id, 'alice', 'key', 'session', '{}')"
            ),
            {"id": run_id},
        )


def _admit(config, run_id, call_id, *, search=False):
    return execution.admit_tool_call(config, run_id, "role", "executor", call_id, search)


def _compete(config, run_id, call_ids, *, search=False):
    barrier = Barrier(len(call_ids), timeout=10)

    def attempt(call_id):
        barrier.wait()
        return _admit(config, run_id, call_id, search=search)

    with ThreadPoolExecutor(max_workers=len(call_ids)) as pool:
        return list(pool.map(attempt, call_ids))


def _counts(run_id):
    with domain_engine().connect() as db:
        return db.execute(
            text(
                "SELECT count(*), coalesce(sum(search_calls), 0) "
                "FROM por_domain.tool_calls WHERE run_id=:r"
            ),
            {"r": run_id},
        ).one()


@pytest.fixture
def admit(monkeypatch):
    monkeypatch.setattr(execution, "require_live_effect", lambda *args: None)
    return _admit


def test_two_producers_share_the_last_tool_allowance(pg_database, admit):
    run_id = "run-tool-limit"
    _create_request(run_id)
    config = _configuration(max_tool_calls=1)

    admitted = _compete(config, run_id, ["producer-a", "producer-b"])

    assert sorted(admitted) == [False, True]
    assert tuple(_counts(run_id)) == (1, 0)


def test_two_producers_share_the_last_search_allowance(pg_database, admit):
    run_id = "run-search-limit"
    _create_request(run_id)
    config = _configuration(max_search_calls=1)

    admitted = _compete(
        config, run_id, ["producer-a", "producer-b"], search=True
    )

    assert sorted(admitted) == [False, True]
    assert tuple(_counts(run_id)) == (1, 1)


def test_repeated_call_identity_is_not_charged_twice(pg_database, admit):
    run_id = "run-replay"
    _create_request(run_id)
    config = _configuration(max_tool_calls=1)

    assert _admit(config, run_id, "same-call") is True
    assert _admit(config, run_id, "same-call") is True
    assert _admit(config, run_id, "next-call") is False
    assert tuple(_counts(run_id)) == (1, 0)


def test_distinct_call_ids_follow_the_real_allowance(pg_database, admit):
    run_id = "run-multiple"
    _create_request(run_id)
    config = _configuration(max_tool_calls=2)

    assert _admit(config, run_id, "call-a") is True
    assert _admit(config, run_id, "call-b") is True
    assert _admit(config, run_id, "call-c") is False
    assert tuple(_counts(run_id)) == (2, 0)


def test_non_search_calls_do_not_consume_the_search_allowance(pg_database, admit):
    run_id = "run-search-mix"
    _create_request(run_id)
    config = _configuration(max_search_calls=1)

    assert _admit(config, run_id, "lookup") is True
    assert _admit(config, run_id, "search-a", search=True) is True
    assert _admit(config, run_id, "search-b", search=True) is False
    assert tuple(_counts(run_id)) == (2, 1)


def test_admission_failure_leaves_no_phantom_row(pg_database, monkeypatch):
    run_id = "run-storage-down"
    _create_request(run_id)
    config = _configuration(max_tool_calls=1)

    def unavailable(*args):
        raise SQLAlchemyError("storage unavailable")

    monkeypatch.setattr(execution, "require_live_effect", unavailable)
    with pytest.raises(SQLAlchemyError, match="storage unavailable"):
        _admit(config, run_id, "call-a")

    assert tuple(_counts(run_id)) == (0, 0)
