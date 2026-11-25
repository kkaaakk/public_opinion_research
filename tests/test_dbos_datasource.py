"""Real PostgreSQL contracts for the two business Datasource transactions.

The unit contracts fake the transaction boundary. These tests own the real one:
DBOS commits the business rows and the transaction checkpoint in the same SQL
transaction, replays a committed outcome instead of writing again, and leaves no
partial stage behind when the body fails.
"""

import asyncio
from uuid import uuid4

import pytest
from dbos import DBOS, DBOSClient, SetWorkflowID
from sqlalchemy import text

from open_deep_research.configuration import Configuration
from open_deep_research.web.durable import close_run_client
from open_deep_research.web.run_data import RunJSON
from open_deep_research.workflow import dbos_workflow, transactions
from open_deep_research.workflow.dbos_config import domain_engine, native_client
from open_deep_research.workflow.execution import StageContext
from open_deep_research.workflow.transactions import commit_stage

pytestmark = pytest.mark.integration

ATTEMPTS = []


def stage_scope(run_id, name="plan_research", generation=0):
    """Build the production stage identity inside a Workflow, never as input."""
    return StageContext(
        run_id,
        f"{run_id}:{name}",
        name,
        generation,
        Configuration(),
        {},
        True,
        None,
        1,
    )


def business_events(run_id):
    """Read the committed stage messages the transaction published."""
    with domain_engine().connect() as db:
        rows = db.execute(
            text(
                "SELECT message FROM por_dbos.notifications "
                "WHERE destination_uuid=:target ORDER BY created_at_epoch_ms, message_uuid"
            ),
            {"target": run_id + ":events"},
        ).scalars()
        return [RunJSON().deserialize(message) for message in rows]


def checkpoints(workflow_id):
    with domain_engine().connect() as db:
        return (
            db.execute(
                text(
                    "SELECT step_id FROM por_dbos.datasource_outputs "
                    "WHERE workflow_id=:id ORDER BY step_id"
                ),
                {"id": workflow_id},
            )
            .scalars()
            .all()
        )


@pytest.fixture
def native_database(pg_database):
    """Bind every cached engine and client to this test's isolated database."""
    close_run_client()
    ATTEMPTS.clear()
    yield pg_database
    close_run_client()


def launch(url):
    DBOS(
        config={
            "name": "por-test-datasource",
            "system_database_url": url,
            "dbos_system_schema": "por_dbos",
            "application_version": "datasource-test",
            "serializer": RunJSON(),
            "log_level": "WARN",
        }
    )
    transactions.prepare()
    DBOS.launch()


def count_attempts(monkeypatch):
    """Count real transaction attempts without changing what they commit."""
    original = transactions._commit_stage

    def counted(scope, status, result, error):
        ATTEMPTS.append(status)
        return original(scope, status, result, error)

    monkeypatch.setattr(transactions, "_commit_stage", counted)


@DBOS.workflow(name="por.test.mailbox")
async def mailbox(run_id):
    """Stand in for por.events: the mailbox target must exist to accept a send."""
    return run_id


def test_stage_transaction_commits_events_and_checkpoint_together(native_database):
    run_id = "ds-" + uuid4().hex[:8]
    workflow_id = run_id + ":research"
    monkeypatch = pytest.MonkeyPatch()
    count_attempts(monkeypatch)
    launch(native_database)

    @DBOS.workflow(name="por.test.stage_commit")
    async def stage():
        scope = stage_scope(run_id)
        # The production call site hands the sync transaction to a worker
        # thread, so its DBOS context must survive that hop.
        await asyncio.to_thread(commit_stage, scope, "running")
        return await asyncio.to_thread(
            commit_stage,
            scope,
            "completed",
            result={"patch": {"report": {"final": "# report"}}},
        )

    async def drive():
        with SetWorkflowID(run_id + ":events"):
            await mailbox(run_id)
        with SetWorkflowID(workflow_id):
            return await stage()

    try:
        assert asyncio.run(drive()) == {"stage": "plan_research", "status": "completed"}
        events = business_events(run_id)
        assert sorted(event["type"] for event in events) == [
            "report",
            "status",
            "step_completed",
            "step_started",
        ]
        assert {event["content"] for event in events if event["type"] == "report"} == {
            "# report"
        }
        assert ATTEMPTS == ["running", "completed"]
        steps = native_client().list_workflow_steps(workflow_id)
        # One unified transaction stays readable through its recorded transition.
        assert [dbos_workflow.commit_status(step) for step in steps] == [
            "running",
            "completed",
        ]
        # A finished Workflow keeps no transaction checkpoints: they exist to
        # make an in-flight commit replayable, not to outlive the Run.
        assert checkpoints(workflow_id) == []
    finally:
        DBOS.destroy()
        monkeypatch.undo()


def test_failed_stage_transaction_leaves_no_partial_write(native_database):
    run_id = "ds-" + uuid4().hex[:8]
    launch(native_database)

    @DBOS.workflow(name="por.test.stage_failure")
    async def stage():
        return await asyncio.to_thread(commit_stage, stage_scope(run_id), "unknown")

    async def drive():
        with SetWorkflowID(run_id + ":events"):
            await mailbox(run_id)
        with SetWorkflowID(run_id + ":research"):
            return await stage()

    try:
        with pytest.raises(ValueError, match="Unknown stage status"):
            asyncio.run(drive())
        assert business_events(run_id) == []
        assert checkpoints(run_id + ":research") == []
    finally:
        DBOS.destroy()


def test_retry_reruns_the_transaction_and_keeps_single_business_writes(native_database):
    run_id = "ds-" + uuid4().hex[:8]
    workflow_id = run_id + ":research"
    failure = {"armed": True}
    monkeypatch = pytest.MonkeyPatch()
    count_attempts(monkeypatch)
    launch(native_database)

    @DBOS.workflow(name="por.test.stage_replay")
    async def stage():
        scope = stage_scope(run_id)
        await asyncio.to_thread(commit_stage, scope, "running")
        if failure["armed"]:
            raise RuntimeError("plain python failure after the stage started")
        return await asyncio.to_thread(commit_stage, scope, "completed", result={})

    async def drive():
        with SetWorkflowID(run_id + ":events"):
            await mailbox(run_id)
        with SetWorkflowID(workflow_id):
            return await stage()

    def rewind(start_step):
        """Rewind from the outside, then let the restarted worker recover it."""
        client = DBOSClient(
            system_database_url=native_database,
            dbos_system_schema="por_dbos",
            serializer=RunJSON(),
        )
        try:
            handle = client.rewind_workflow(workflow_id, start_step=start_step)
        finally:
            client.destroy()
        return handle

    try:
        with pytest.raises(RuntimeError, match="plain python failure"):
            asyncio.run(drive())
        # DBOS clears a Workflow's transaction checkpoints on every terminal
        # state, so a retry re-runs the transaction body rather than replaying it.
        assert ATTEMPTS == ["running"] and checkpoints(workflow_id) == []
        first = native_client().list_workflow_steps(workflow_id)[0]["function_id"]
        DBOS.destroy()
        rewind(first)
        launch(native_database)
        with pytest.raises(RuntimeError):
            native_client().retrieve_workflow(workflow_id).get_result()
        assert ATTEMPTS == ["running", "running"]
        # The business idempotency key, not the checkpoint, kept the mailbox at
        # one copy of each event.
        assert sorted(event["type"] for event in business_events(run_id)) == [
            "status",
            "step_started",
        ]

        failure["armed"] = False
        DBOS.destroy()
        rewind(first)
        launch(native_database)
        assert native_client().retrieve_workflow(workflow_id).get_result() == {
            "stage": "plan_research",
            "status": "completed",
        }
        # Every retry re-runs the transaction, because a terminal Workflow never
        # keeps a checkpoint; three executions still leave three business events.
        assert ATTEMPTS == ["running", "running", "running", "completed"]
        assert sorted(event["type"] for event in business_events(run_id)) == [
            "status",
            "step_completed",
            "step_started",
        ]
    finally:
        DBOS.destroy()
        monkeypatch.undo()


def test_retry_target_reads_the_stage_transition_it_recorded(native_database):
    run_id = "ds-" + uuid4().hex[:8]
    workflow_id = run_id + ":research"
    launch(native_database)

    @DBOS.workflow(name="por.test.stage_target")
    async def stage():
        scope = stage_scope(run_id)
        await asyncio.to_thread(commit_stage, scope, "running")
        raise RuntimeError("plain python failure after the stage started")

    async def drive():
        with SetWorkflowID(run_id + ":events"):
            await mailbox(run_id)
        with SetWorkflowID(workflow_id):
            return await stage()

    try:
        with pytest.raises(RuntimeError, match="plain python failure"):
            asyncio.run(drive())
        workflow = native_client().list_workflows(
            workflow_ids=[workflow_id], load_input=False, load_output=False
        )[0]
        assert workflow.status == "ERROR"
        steps = native_client().list_workflow_steps(workflow_id)
        target = dbos_workflow.failure_target(workflow)
        # The transaction committed, so a retry resumes after it instead of
        # repeating the stage transition.
        assert target["start_step"] == max(step["function_id"] for step in steps) + 1
    finally:
        DBOS.destroy()
