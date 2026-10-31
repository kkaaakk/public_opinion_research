"""Actual PostgreSQL/DBOS/SDK/Neo4j interactions; inference only is scripted."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from test_research_brief import BRIEF

from open_deep_research.web.run_data import RunConflict
from open_deep_research.workflow.run_facts import facts

pytestmark = pytest.mark.integration


def test_prior_edit_receipt_cannot_block_current_brief_expiration(
    native_case, monkeypatch
):
    c = native_case
    run = c.create(approval=True)
    old = c.wait(run, lambda r: r["status"] == "awaiting_approval")["plan"]
    c.client.approve(
        run["run_id"],
        "alice",
        old["version"],
        "edit",
        BRIEF + "\nKeep uncertainty explicit.",
    )
    current = c.wait(run, lambda r: r["plan"] and r["plan"]["revision"] == 2)["plan"]
    repo = facts()
    original = repo.now
    with monkeypatch.context() as patch:
        patch.setattr(repo, "now", lambda db: original(db) + 90000)
        assert repo.expire_approval(run["run_id"], current)
        assert repo.expire_approval(run["run_id"], current)
    with repo.engine.connect() as db:
        receipt = db.execute(
            text("SELECT version,decision FROM por_domain.approvals WHERE run_id=:id"),
            {"id": run["run_id"]},
        ).one()
    assert tuple(receipt) == (current["version"], "expired")
    assert not c.models and not c.calls
    c.client.cancel(run["run_id"], "alice")


def test_accepted_report_topology_is_frozen_across_operator_default_changes(
    native_case, monkeypatch
):
    c = native_case
    c.cfg["configurable"]["allow_clarification"] = False
    run = c.create(approval=True)
    pending = c.wait(run, lambda r: r["status"] == "awaiting_approval")
    accepted = facts().request(run["run_id"])["data"]["config"]["configurable"]
    assert accepted["report_mode"] == "synthesis"
    monkeypatch.setenv("REPORT_MODE", "sections")
    c.client.approve(run["run_id"], "alice", pending["plan"]["version"], "approve")
    done = c.wait(
        run, lambda r: r["archive_status"] == "saved" and r["events_complete"]
    )
    assert done["status"] == "completed"
    assert done["plan"]["brief"].startswith("# ")
    assert done["completed_steps"].count("plan_research") == 1
    events = c.client.read_events(run["run_id"], "alice", 0, 1000)
    assert not any(event.get("status") == "awaiting_clarification" for event in events)
    assert "generate_final_report" in done["completed_steps"]
    assert not {"plan_report_sections", "section_writer", "write_final_sections"} & set(
        done["completed_steps"]
    )


@pytest.mark.parametrize("native_case", ["clarify"], indirect=True)
@pytest.mark.parametrize("skip", [False, True])
def test_clarification_same_run_releases_quota_and_resumes(
    native_case, skip, monkeypatch
):
    from dbos import DBOS

    from open_deep_research.workflow import python_state

    original_uuid = python_state.uuid4

    def message_identity():
        assert DBOS.workflow_id is None or DBOS.step_id is not None
        return original_uuid()

    monkeypatch.setattr(python_state, "uuid4", message_identity)
    c = native_case
    run = c.create(approval=True)
    pending = c.wait(run, lambda r: r["status"] == "awaiting_clarification")
    assert pending["native_status"] == "PENDING" and not pending["events_complete"]
    with facts().engine.connect() as db:
        assert (
            db.execute(
                text("SELECT occupied FROM por_domain.requests WHERE id=:id"),
                {"id": run["run_id"]},
            ).scalar_one()
            is False
        )
    assert not c.runtimes and not c.calls
    version = pending["clarification"]["version"]
    for _ in range(2):
        c.client.clarify(
            run["run_id"], "alice", version, {} if skip else {"brand": "Brand A"}, skip
        )
    approved = c.wait(run, lambda r: r["status"] == "awaiting_approval")
    assert approved["clarification"] is None and approved["run_id"] == run["run_id"]
    c.client.approve(run["run_id"], "alice", approved["plan"]["version"], "approve")
    done = c.wait(
        run, lambda r: r["archive_status"] == "saved" and r["events_complete"]
    )
    assert done["status"] == "completed" and len(c.models) == 4
    assert (
        done["plan"]["locked"] and "plan_report_sections" not in done["completed_steps"]
    )
    assert done["completed_steps"].count("plan_research") == 2
    assert done["completed_steps"].count("generate_final_report") == 1
    assert not {"section_writer", "write_final_sections"} & set(done["completed_steps"])
    assert c.calls == {"public_signal": 1, "internal_knowledge": 1}
    for runtime in c.runtimes:
        assert runtime.workspace.scope.run_id == run["run_id"]


def test_edit_cas_old_approval_and_execution_uses_frozen_brief(native_case):
    c = native_case
    run = c.create(approval=True)
    old = c.wait(run, lambda r: r["status"] == "awaiting_approval")["plan"]
    edited = BRIEF.replace("Brand", "User edited Brand")

    def submit(i):
        try:
            c.client.approve(
                run["run_id"],
                "alice",
                old["version"],
                "edit",
                edited + ("\nExtra constraint" if i else ""),
            )
            return True
        except RunConflict:
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(submit, range(2))) == 1
    pending = c.wait(run, lambda r: r["plan"] and r["plan"]["revision"] == 2)
    with pytest.raises(RunConflict):
        c.client.approve(run["run_id"], "alice", old["version"], "approve")
    version = pending["plan"]["version"]
    c.client.approve(run["run_id"], "alice", version, "approve")
    with pytest.raises(RunConflict):
        c.client.approve(run["run_id"], "alice", version, "approve")
    done = c.wait(
        run, lambda r: r["archive_status"] == "saved" and r["events_complete"]
    )
    assert done["plan"]["version"] == version and done["plan"]["locked"]
    assert done["status"] == "completed" and len(c.models) == 4
    # Initial user item is protected by SDK history compaction, and present once.
    for runtime in c.runtimes:
        for request in runtime.model.calls:
            data = "\n".join(
                item.get("content", "")
                for item in request.input
                if isinstance(item, dict) and isinstance(item.get("content"), str)
            )
            assert data.count(pending["plan"]["brief"]) == 1
    with pytest.raises(RunConflict):
        c.client.approve(run["run_id"], "alice", version, "edit", BRIEF)


@pytest.mark.parametrize("native_case", ["clarify"], indirect=True)
def test_cancel_pending_clarification_is_terminal_and_never_runs_agents(native_case):
    c = native_case
    run = c.create()
    pending = c.wait(run, lambda r: r["status"] == "awaiting_clarification")
    c.client.cancel(run["run_id"], "alice")
    done = c.wait(run, lambda r: r["archive_status"] == "saved")
    assert done["status"] == "cancelled" and not c.models and not c.calls
    with pytest.raises(RunConflict):
        c.client.clarify(
            run["run_id"], "alice", pending["clarification"]["version"], {}, True
        )


@pytest.mark.parametrize("pg_database", [{"RUN_APPROVAL_SECONDS": 1}], indirect=True)
@pytest.mark.parametrize("native_case", ["clarify"], indirect=True)
def test_clarification_timeout_cancels_without_success_or_research(native_case):
    c = native_case
    run = c.create()
    done = c.wait(run, lambda r: r["archive_status"] == "saved")
    assert done["status"] == "cancelled" and not c.models and not c.calls
    assert done["report"] == "" and done["events_complete"]


def test_single_synthesis_failure_retries_without_repeating_roles(
    native_case, monkeypatch
):
    from dbos_support import retry_observed_failure

    from open_deep_research.workflow import dbos_workflow as engine

    original = engine.OPERATIONS["generate_final_report"]
    attempts = []

    async def fail_once(state, cfg):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Controlled synthesis failure before request")
        return await original(state, cfg)

    monkeypatch.setitem(engine.OPERATIONS, "generate_final_report", fail_once)
    c = native_case
    run = c.create()
    failed = c.wait(
        run,
        lambda r: r["status"] == "failed"
        and r["archive_status"] == "saved"
        and r["events_complete"],
    )
    assert len(c.models) == 4 and failed["retry_safe"]
    retry_observed_failure(c, run, "synthesis-retry", failed)
    done = c.wait(
        run,
        lambda r: r["status"] == "completed"
        and r["archive_status"] == "saved"
        and r["events_complete"],
    )
    assert (
        len(attempts) == 2
        and len(c.models) == 4
        and c.calls == {"public_signal": 1, "internal_knowledge": 1}
    )
    assert done["plan"]["version"] == failed["plan"]["version"]


def test_advanced_section_mode_remains_explicit(native_case):
    c = native_case
    c.cfg["configurable"]["report_mode"] = "sections"
    run = c.create()
    done = c.wait(run, lambda r: r["archive_status"] == "saved")
    assert done["status"] == "completed"
    assert {"plan_report_sections", "section_writer", "write_final_sections"} <= set(
        done["completed_steps"]
    )
    assert "generate_final_report" not in done["completed_steps"]


def test_waiting_snapshot_reads_phase_before_its_versioned_payload(
    native_case, monkeypatch
):
    c = native_case
    run = c.create(approval=True)
    plan = c.wait(run, lambda r: r["status"] == "awaiting_approval")["plan"]
    original = c.client.client.get_event
    phase_seen = []

    def crossing(workflow_id, key, **kwargs):
        if workflow_id == run["run_id"]:
            if key == "phase":
                phase_seen.append(True)
            elif key == "plan" and not phase_seen:
                return None  # the snapshot crossed the plan publication commit
        return original(workflow_id, key, **kwargs)

    monkeypatch.setattr(c.client.client, "get_event", crossing)
    observed = c.client.get(run["run_id"], "alice")
    assert observed["status"] == "awaiting_approval"
    assert observed["plan"]["version"] == plan["version"]


def test_terminal_delivery_waits_for_committed_sdk_metadata(native_case, monkeypatch):
    from open_deep_research.workflow import dbos_workflow as engine
    from open_deep_research.workflow.run_facts import RunFacts

    original_publish, original_send = RunFacts.publish, engine.send_event
    held = []
    observed = []

    def reordered(repo, db, run_id, kind, **payload):
        if (
            kind == "sdk_event"
            and payload.get("event") == "agent_completed"
            and payload.get("role") == "response_strategy"
        ):
            held.append((run_id, payload))
            return
        return original_publish(repo, db, run_id, kind, **payload)

    async def send(run_id, event_type, **data):
        await original_send(run_id, event_type, **data)
        if event_type == "history_saved" and held:
            import asyncio

            from dbos import DBOS

            await asyncio.sleep(0.2)
            # Archive can arrive first, but is not a receipt for an unseen SDK tail.
            observed.append(
                await DBOS.get_event_async(
                    run_id + ":events", "done", timeout_seconds=0
                )
            )
            _, packet = held.pop()
            await original_send(run_id, "sdk_event", **packet)

    monkeypatch.setattr(RunFacts, "publish", reordered)
    monkeypatch.setattr(engine, "send_event", send)
    c = native_case
    run = c.create()
    done = c.wait(
        run, lambda r: r["archive_status"] == "saved" and r["events_complete"]
    )
    assert observed == [None] and not held and done["status"] == "completed"
    events = c.client.read_events(run["run_id"], "alice", 0, 1000)
    ends = [
        e
        for e in events
        if e["type"] == "sdk_event" and e.get("event") == "agent_completed"
    ]
    assert len(ends) == 4 and len({e["invocation_id"] for e in ends}) == 4
    assert events[-1]["type"] == "done" and len(c.models) == 4
