"""Cutover refuses old journals through public DBOS catalog APIs."""

import pytest

from open_deep_research.web.run_data import RunConflict
from open_deep_research.workflow.run_facts import facts

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "version,workflow,args",
    [
        ("dbos-brief-spec-v1", "por.role", ("old", "old:role", "public_signal", {})),
        ("dbosrunner-unified-v1", "por.run", ("old",)),
        ("dbosrunner-clarification-v1", "por.run", ("old",)),
        ("dbosrunner-brief-revision-v1", "por.plan", ("old", {}, 1)),
    ],
)
def test_old_pending_journal_is_inventoried_and_not_deleted(
    pg_database, version, workflow, args
):
    repo = facts()
    assert repo.migration_inventory() == []
    repo.client.enqueue(
        {
            "workflow_name": workflow,
            "workflow_id": "old-journal-test",
            "queue_name": "por-business",
            "app_version": version,
        },
        *args,
    )
    inventory = repo.migration_inventory()
    assert inventory == [
        {
            "workflow_id": "old-journal-test",
            "name": workflow,
            "version": version,
            "status": "ENQUEUED",
        }
    ]
    with pytest.raises(RunConflict, match="original release"):
        repo.require_matching_journals()
    assert (
        repo.client.list_workflows(workflow_ids=["old-journal-test"])[0].status
        == "ENQUEUED"
    )


def test_historical_unit_projection_remains_readable(pg_database):
    from sqlalchemy import text
    from test_python_workflow import config, initial

    from open_deep_research.web.durable import RunClient
    from open_deep_research.web.run_data import dumps

    repo = facts()
    client = RunClient()
    run, _ = client.create("alice", "historical", {"topic": "a"}, config(), initial())
    run_id = run["run_id"]
    client.client.cancel_workflow(run_id, cancel_children=True)
    unit = run_id + ":compile_final_report"
    with repo.engine.begin() as db:
        request = repo.request(run_id, db=db)["data"]
        request["version"] = "dbosrunner-brief-revision-v1"
        db.execute(
            text("UPDATE por_domain.requests SET value=:v WHERE id=:id"),
            {"id": run_id, "v": dumps(request)},
        )
        db.execute(
            text(
                "INSERT INTO por_domain.units(id,run_id,name,permit,settled,artifact) "
                "VALUES (:unit,:run,'compile_final_report','old',true,:artifact)"
            ),
            {
                "unit": unit,
                "run": run_id,
                "artifact": dumps(
                    {
                        "completed": True,
                        "delivery_ids": [],
                        "patch": {"report": {"final": "historical report"}},
                    }
                ),
            },
        )
    snapshot = client.get(run_id, "alice")
    assert snapshot["completed_steps"] == ["compile_final_report"]
    assert snapshot["report"] == "historical report"
    assert snapshot["settlement"] == "settled"
    assert repo.units(run_id)[0]["artifact"]["completed"] is True


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
