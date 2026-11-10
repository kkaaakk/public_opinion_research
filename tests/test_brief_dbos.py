"""Actual PostgreSQL/DBOS/SDK/Neo4j interactions; inference only is scripted."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import text
from test_python_workflow_sdk import CLARIFICATION_ANSWERS, CLARIFICATION_QUESTIONS
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


@pytest.mark.parametrize("native_case", ["clarify"], indirect=True)
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
    assert (
        len(c.plan_prompts) == 1
        and "Clarification allowed: False." in c.plan_prompts[0]
    )
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


def waiting_for_round(case, run, number):
    pending = case.wait(
        run,
        lambda r: (r["clarification"] or {}).get("round") == number,
    )
    assert pending["status"] == "awaiting_clarification"
    assert pending["clarification"]["max_rounds"] == 3
    assert not facts().request(run["run_id"])["occupied"]
    return pending["clarification"]


@pytest.mark.parametrize(
    "native_case,rounds,skip",
    [
        ("normal", 0, False),
        ("clarify-2", 2, False),
        ("clarify-3", 3, False),
        ("clarify-3", 2, True),
    ],
    indirect=["native_case"],
    ids=["clear", "two-rounds", "three-rounds", "later-skip"],
)
def test_clarification_rounds_preserve_context_and_versioned_approval(
    native_case, rounds, skip
):
    c = native_case
    image_context = "Observed packaging from user image."
    c.state["messages"].append(
        HumanMessage(
            content=image_context,
            additional_kwargs={"rag_query_image_context": True},
        )
    )
    run = c.create(approval=True)
    run_id = run["run_id"]
    previous = None
    for number in range(1, rounds + 1):
        pending = waiting_for_round(c, run, number)
        if previous:
            old_version, old_answers = previous
            assert pending["version"] != old_version
            with pytest.raises(RunConflict, match="version changed"):
                c.client.clarify(run_id, "alice", old_version, old_answers)
        skip_this_round = skip and number == rounds
        answers = {} if skip_this_round else CLARIFICATION_ANSWERS[number - 1]
        c.client.clarify(run_id, "alice", pending["version"], answers, skip_this_round)
        previous = pending["version"], answers

    planned = c.wait(run, lambda r: r["status"] == "awaiting_approval")
    assert planned["run_id"] == run_id and planned["clarification"] is None
    assert not c.models and not c.calls
    assert len(c.plan_prompts) == rounds + 1
    for number, prompt in enumerate(c.plan_prompts):
        assert "brand recall" in prompt and image_context in prompt
        assert prompt.count("\nUser response: ") == number
        for index in range(number):
            assert CLARIFICATION_QUESTIONS[index].text in prompt
            if skip and index == rounds - 1:
                assert "Skipped. Continue with existing information" in prompt
            else:
                assert next(iter(CLARIFICATION_ANSWERS[index].values())) in prompt
    allowed = not skip and rounds < 3
    assert f"Clarification allowed: {allowed}." in c.plan_prompts[-1]
    assert planned["completed_steps"].count("enrich_query_images") == 1
    assert planned["completed_steps"].count("plan_research") == rounds + 1

    old_plan = planned["plan"]
    edited = old_plan["brief"] + "\n\nKeep unresolved facts explicit."
    c.client.approve(run_id, "alice", old_plan["version"], "edit", edited)
    new_plan = c.wait(run, lambda r: r["plan"]["revision"] == 2)["plan"]
    assert new_plan["brief"] == edited and new_plan["version"] != old_plan["version"]
    assert new_plan["expires_at"] == old_plan["expires_at"]
    with pytest.raises(RunConflict):
        c.client.approve(run_id, "alice", old_plan["version"], "approve")
    c.client.approve(run_id, "alice", new_plan["version"], "approve")
    done = c.wait(
        run, lambda r: r["archive_status"] == "saved" and r["events_complete"]
    )
    assert done["status"] == "completed" and done["plan"]["locked"]
    assert done["plan"]["brief"] == edited and len(c.plan_prompts) == rounds + 1


@pytest.mark.parametrize("native_case", ["clarify-over-limit"], indirect=True)
def test_fourth_plan_asking_again_fails_without_publishing_a_fourth_challenge(
    native_case,
):
    c = native_case
    run = c.create()
    for number, answers in enumerate(CLARIFICATION_ANSWERS, 1):
        pending = waiting_for_round(c, run, number)
        c.client.clarify(run["run_id"], "alice", pending["version"], answers)
    done = c.wait(
        run, lambda r: r["archive_status"] == "saved" and r["events_complete"]
    )
    assert done["status"] == "failed" and done["native_status"] == "ERROR"
    assert done["clarification"] is None and done["plan"] is None
    assert (
        len(c.plan_prompts) == 4
        and "Clarification allowed: False." in c.plan_prompts[-1]
    )
    assert (
        c.client.client.get_event(run["run_id"], "clarification", timeout_seconds=0)[
            "round"
        ]
        == 3
    )
    assert not c.models and not c.calls


@pytest.mark.parametrize("native_case", ["clarify-2"], indirect=True)
def test_concurrent_runs_keep_clarification_answers_scoped_to_their_run(native_case):
    c = native_case
    c.state["messages"][0].content = "First concurrent request"
    first = c.create(approval=True)
    c.state["messages"][0].content = "Second concurrent request"
    second = c.create(approval=True)
    first_challenge = waiting_for_round(c, first, 1)
    second_challenge = waiting_for_round(c, second, 1)
    with pytest.raises(RunConflict):
        c.client.clarify(
            first["run_id"],
            "alice",
            second_challenge["version"],
            {"brand": "Wrong run"},
        )
    for run, challenge, brand in [
        (second, second_challenge, "Second unique brand"),
        (first, first_challenge, "First unique brand"),
    ]:
        c.client.clarify(run["run_id"], "alice", challenge["version"], {"brand": brand})
    for run in (first, second):
        pending = waiting_for_round(c, run, 2)
        c.client.clarify(run["run_id"], "alice", pending["version"], {}, skip=True)
        c.wait(run, lambda r: r["status"] == "awaiting_approval")
    assert len(c.plan_prompts) == 6
    for prompt in c.plan_prompts:
        first_prompt = "First concurrent request" in prompt
        expected = "First unique brand" if first_prompt else "Second unique brand"
        other = "Second unique brand" if first_prompt else "First unique brand"
        assert other not in prompt
        if "\nUser response: " in prompt:
            assert expected in prompt


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


@pytest.mark.parametrize("pg_database", [{"RUN_APPROVAL_SECONDS": 3}], indirect=True)
@pytest.mark.parametrize("native_case", ["clarify-3"], indirect=True)
@pytest.mark.parametrize("number", [1, 2, 3])
def test_clarification_timeout_cancels_without_success_or_research(native_case, number):
    c = native_case
    run = c.create()
    for prior, answers in enumerate(CLARIFICATION_ANSWERS[: number - 1], 1):
        pending = waiting_for_round(c, run, prior)
        c.client.clarify(run["run_id"], "alice", pending["version"], answers)
    pending = waiting_for_round(c, run, number)
    done = c.wait(run, lambda r: r["archive_status"] == "saved")
    assert done["status"] == "cancelled" and not c.models and not c.calls
    assert done["report"] == "" and done["events_complete"]
    assert done["clarification"] is None and len(c.plan_prompts) == number
    with pytest.raises(RunConflict):
        c.client.clarify(run["run_id"], "alice", pending["version"], {}, True)


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
