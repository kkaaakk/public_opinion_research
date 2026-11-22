"""Real PostgreSQL contract for upgrading an existing call ledger."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from open_deep_research.workflow.dbos_config import domain_engine
from open_deep_research.workflow.domain_schema import initialize_domain

pytestmark = pytest.mark.integration

# The previous release's shape: no tool name, no status, a smallint search
# marker, and Operations with no link to the Tool Call that caused them.
DOWNGRADE = (
    "ALTER TABLE por_domain.operations DROP CONSTRAINT operations_tool_call_fkey",
    "ALTER TABLE por_domain.operations DROP COLUMN tool_call_id",
    "ALTER TABLE por_domain.tool_calls DROP CONSTRAINT tool_calls_status_check",
    "ALTER TABLE por_domain.tool_calls DROP COLUMN name",
    "ALTER TABLE por_domain.tool_calls DROP COLUMN status",
    "ALTER TABLE por_domain.tool_calls DROP COLUMN search",
    "ALTER TABLE por_domain.tool_calls "
    "ADD COLUMN search_calls smallint NOT NULL DEFAULT 0",
)


def _columns(table):
    with domain_engine().connect() as db:
        return {
            row[0]
            for row in db.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='por_domain' AND table_name=:t"
                ),
                {"t": table},
            )
        }


def _constraints(table):
    with domain_engine().connect() as db:
        return {
            row[0]
            for row in db.execute(
                text(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE table_schema='por_domain' AND table_name=:t"
                ),
                {"t": table},
            )
        }


def test_installer_upgrades_the_previous_call_ledger(pg_database):
    with domain_engine().begin() as db:
        for statement in DOWNGRADE:
            db.exec_driver_sql(statement)
        db.execute(
            text(
                "INSERT INTO por_domain.requests"
                "(id, owner, request_key, session, value) "
                "VALUES ('legacy-run', 'alice', 'key', 'session', '{}')"
            )
        )
        db.execute(
            text(
                "INSERT INTO por_domain.tool_calls(run_id, id, search_calls) "
                "VALUES ('legacy-run', 'legacy-call', 1)"
            )
        )
        # An Operation admitted before the link existed has no Tool Call to name.
        db.execute(
            text(
                "INSERT INTO por_domain.operations"
                "(run_id, id, name, fingerprint, executor_id, outcome) "
                "VALUES ('legacy-run', 'legacy-op', 'publish', 'fp', 'executor', 'returned')"
            )
        )

    initialize_domain()
    initialize_domain()  # the upgrade must be replayable

    assert {"name", "search", "status"} <= _columns("tool_calls")
    assert "search_calls" not in _columns("tool_calls")
    assert "tool_call_id" in _columns("operations")
    assert "operations_tool_call_fkey" in _constraints("operations")
    with domain_engine().connect() as db:
        assert db.execute(
            text(
                "SELECT search, status FROM por_domain.tool_calls "
                "WHERE run_id='legacy-run' AND id='legacy-call'"
            )
        ).one() == (True, "admitted")
        assert db.execute(
            text(
                "SELECT tool_call_id, outcome FROM por_domain.operations "
                "WHERE run_id='legacy-run' AND id='legacy-op'"
            )
        ).one() == (None, "returned")

    with pytest.raises(IntegrityError):
        with domain_engine().begin() as db:
            db.execute(
                text(
                    "INSERT INTO por_domain.operations"
                    "(run_id, id, tool_call_id, name, fingerprint, executor_id) "
                    "VALUES ('legacy-run', 'orphan', 'unknown-call', 'publish', 'fp', 'e')"
                )
            )
