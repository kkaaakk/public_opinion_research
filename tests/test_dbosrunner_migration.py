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
        ("dbosrunner-stage-settlement-v1", "por.run", ("old",)),
        ("dbosrunner-stage-settlement-v1", "por.plan", ("old", {}, 1)),
    ],
)
def test_old_pending_journal_is_inventoried_and_not_deleted(
    pg_database, version, workflow, args
):
    from test_python_workflow import config, initial

    from open_deep_research.web.durable import RunClient
    from open_deep_research.web.executor import launch

    repo = facts()
    assert repo.migration_inventory() == []
    workflow_id = (
        "old-journal-test:brief-revision:1"
        if workflow == "por.plan"
        else "old-journal-test"
    )
    options = dict(
        workflow_name=workflow,
        workflow_id=workflow_id,
        queue_name="por-business",
        app_version=version,
    )
    revision = (
        {"revision_request": {"feedback": "Add scope.", "expires_at": 1}}
        if workflow == "por.plan"
        else {}
    )
    repo.client.enqueue(options, *args, **revision)
    inventory = repo.migration_inventory()
    assert inventory == [
        {
            "workflow_id": workflow_id,
            "name": workflow,
            "version": version,
            "status": "ENQUEUED",
        }
    ]
    with pytest.raises(RunConflict, match="original release"):
        repo.require_matching_journals()
    with pytest.raises(RunConflict, match="original release"):
        launch("new-version-executor")
    with pytest.raises(RunConflict, match="original release"):
        RunClient().create("alice", "new-run", {"topic": "a"}, config(), initial())
    assert (
        repo.client.list_workflows(workflow_ids=[workflow_id])[0].status == "ENQUEUED"
    )


def test_brief_receipt_migration_preserves_legacy_plan_and_approval(native_case):
    from datetime import datetime

    from sqlalchemy import text

    from open_deep_research.web.run_data import digest, dumps

    case = native_case
    run = case.create(approval=True)
    run_id = run["run_id"]
    original = case.wait(run, lambda r: r["status"] == "awaiting_approval")["plan"]
    case.client.cancel(run_id, "alice")
    case.wait(run, lambda r: r["archive_status"] == "saved")
    repo = facts()
    plan = dict(
        original,
        expires_at=datetime.fromisoformat(original["expires_at"]).timestamp(),
        ai_revision={"attempt": 2, "used": 1, "error": "model_error"},
    )
    old_slot = digest(dict(plan_version=plan["version"], revision_attempt=1))
    old_decision = "revise:" + digest(
        dict(decision="revise", feedback="Add scope.", revision_attempt=1)
    )
    with repo.engine.begin() as db:
        request = repo.request(run_id, db=db)["data"]
        request["version"] = "dbosrunner-stage-settlement-v1"
        db.execute(
            text("UPDATE por_domain.requests SET value=:value WHERE id=:id"),
            {"id": run_id, "value": dumps(request)},
        )
        db.execute(
            text(
                "UPDATE por_dbos.workflow_events SET value=:value WHERE workflow_uuid=:id AND key='plan'"
            ),
            {"id": run_id, "value": dumps(plan)},
        )
        db.execute(
            text("INSERT INTO por_domain.approvals VALUES (:id,:version,:decision)"),
            {"id": run_id, "version": old_slot, "decision": old_decision},
        )
        db.execute(text("DROP TABLE por_domain.brief_revision_receipts"))

    # --initialize-only uses this additive, idempotent application migration.
    repo.initialize()
    repo.initialize()
    with repo.engine.connect() as db:
        assert tuple(repo.approval_receipt(db, run_id)) == (old_slot, old_decision)
        assert (
            db.execute(
                text("SELECT count(*) FROM por_domain.brief_revision_receipts")
            ).scalar_one()
            == 0
        )
    snapshot = case.client.get(run_id, "alice")
    assert snapshot["workflow_version"] == "dbosrunner-stage-settlement-v1"
    assert snapshot["plan"]["brief"] == original["brief"]
    assert snapshot["plan"]["version"] == original["version"]
    assert snapshot["plan"]["ai_revision"] == {
        "used": 1,
        "error": "model_error",
        "limit": 3,
        "pending": False,
    }
    with pytest.raises(RunConflict, match="original executor"):
        case.client.approve(
            run_id,
            "alice",
            original["version"],
            "revise",
            feedback="Add scope.",
            key="new-key",
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
