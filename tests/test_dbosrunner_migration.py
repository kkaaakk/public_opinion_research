"""Cutover refuses old journals through public DBOS catalog APIs."""

import pytest

from open_deep_research.web.run_data import RunConflict
from open_deep_research.workflow.run_facts import facts

pytestmark = pytest.mark.integration


def test_old_pending_journal_is_inventoried_and_not_deleted(pg_database):
    repo = facts()
    assert repo.migration_inventory() == []
    repo.client.enqueue(
        {
            "workflow_name": "por.role",
            "workflow_id": "old-journal-test",
            "queue_name": "por-business",
            "app_version": "dbos-brief-spec-v1",
        },
        "old",
        "old:role",
        "public_signal",
        {},
    )
    inventory = repo.migration_inventory()
    assert inventory == [
        {
            "workflow_id": "old-journal-test",
            "name": "por.role",
            "version": "dbos-brief-spec-v1",
            "status": "ENQUEUED",
        }
    ]
    with pytest.raises(RunConflict, match="original release"):
        repo.require_matching_journals()
    assert (
        repo.client.list_workflows(workflow_ids=["old-journal-test"])[0].status
        == "ENQUEUED"
    )


@pytest.mark.parametrize("boundary", ["reserve", "acquire"])
def test_cancel_intent_before_execution_admission_is_delivered_without_rpc(
    native_case, monkeypatch, boundary
):
    import threading

    from sqlalchemy import text

    from open_deep_research.workflow.run_facts import RunFacts

    entered, release = threading.Event(), threading.Event()
    original = getattr(RunFacts, boundary)

    def gate(self, *args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(20)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(RunFacts, boundary, gate)
    case = native_case
    run = case.create()
    try:
        case.wait(run, lambda _: entered.is_set())
        with facts().engine.begin() as db:
            db.execute(
                text(
                    "UPDATE por_domain.requests SET cancel_requested=true WHERE id=:id"
                ),
                {"id": run["run_id"]},
            )
    finally:
        release.set()
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    assert result["native_status"] == "CANCELLED" and result["status"] == "cancelled"
    assert result["settlement"] == "settled"
    assert not case.models and not case.calls
    assert not facts().request(run["run_id"])["occupied"]


def test_unknown_operation_from_prior_permit_blocks_new_work_and_cleanup(pg_database):
    from sqlalchemy import text
    from test_python_workflow import config, initial

    from open_deep_research.web.durable import RunClient
    from open_deep_research.web.run_data import ExecutionRevoked

    repo = facts()
    run = RunClient().create(
        "alice", "unconfirmed-context", {"topic": "a"}, config(), initial()
    )[0]
    unit = run["run_id"] + ":role"
    first, _, _ = repo.acquire(
        run["run_id"], unit, "public_signal_agent", native_replay=True
    )
    identity = dict(
        role="service",
        name="context_batch",
        task_id="task",
        research_round=1,
        tool_call_id="context-1",
        arguments="",
    )
    op = repo.operation(unit, first, identity)
    second, _, _ = repo.acquire(
        run["run_id"], unit, "public_signal_agent", native_replay=True
    )
    assert first != second
    cleanup = dict(identity, name="context_settlement", tool_call_id="settlement-2")
    with pytest.raises(RunConflict, match="earlier executor"):
        repo.operation(unit, second, cleanup)
    with pytest.raises(ExecutionRevoked):
        repo.operation(unit, first, identity, "returned")
    with repo.engine.begin() as db:
        db.execute(
            text("UPDATE por_domain.operations SET admission_permit=NULL WHERE id=:id"),
            {"id": op},
        )
    with pytest.raises(RunConflict, match="earlier executor"):
        repo.operation(unit, second, cleanup)
