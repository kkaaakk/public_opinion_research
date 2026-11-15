"""Native durable business orchestration. SDK Runner owns every Agent loop."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy

from dbos import DBOS, SetEnqueueOptions, SetWorkflowID, SetWorkflowTimeout
from dbos import error as dbos_error
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.configuration import Configuration
from open_deep_research.observability.telemetry import (
    annotate,
    business_span,
    correlation,
    dbos_scope,
)
from open_deep_research.state import ResearchReview, ResearchRoleResult
from open_deep_research.time_utils import get_today_str
from open_deep_research.web.run_data import (
    RunConflict,
    db_time,
    digest,
    dumps,
    loads,
    public_payload,
    publish_event,
    read_request,
    retry_generation,
    role_deliverables,
    stage_budget,
    timestamp,
)
from open_deep_research.workflow import intake, reporting, research
from open_deep_research.workflow.dbos_config import (
    BUSINESS,
    CONTROL,
    SETTLEMENT_GRACE_SECONDS,
    VERSION,
    domain_engine,
    native_client,
    settings,
)
from open_deep_research.workflow.execution import (
    StageContext,
    error_chain,
    failure_kind,
    is_cancellation,
    owned_io,
    stage_context,
    stage_timeout,
)
from open_deep_research.workflow.python_state import (
    RESEARCH_ROLES,
    apply_patch,
    assemble_research_results,
    copy_state,
    read_research_result,
    role_snapshot,
)
from open_deep_research.workflow.state_utils import agents_for_new_run


async def send_event(run_id, event_type, **data):
    # A replayed send retains its original message and generation.
    if "generation" not in data:
        data["generation"] = await asyncio.to_thread(run_generation, run_id)
    await DBOS.send_async(
        run_id + ":events", dict(type=event_type, **data), topic="events"
    )


def run_generation(run_id):
    with domain_engine().connect() as db:
        return retry_generation(db, run_id)


def stage_event(db, scope, kind, **payload):
    publish_event(
        db,
        scope.run_id,
        kind,
        step=scope.name,
        unit_id=scope.stage_id,
        execution_id=scope.execution_id,
        generation=scope.generation,
        idempotency_key=digest((scope.execution_id, kind, payload.get("role"))),
        **payload,
    )


@DBOS.step(name="por.stage_started")
async def announce_stage(scope, research_round):
    return await asyncio.to_thread(_announce_stage, scope, research_round)


def _announce_stage(scope, research_round):
    with domain_engine().begin() as db:
        stage_event(db, scope, "step_started", research_round=research_round)
        stage_event(
            db, scope, "status", message=f"Running {scope.name}", granularity="stage"
        )
    return get_today_str()


def publish_failure(scope, error):
    with domain_engine().begin() as db:
        stage_event(db, scope, "step_failed", error_kind=error["kind"], error=error)


@DBOS.step(name="por.stage_failed")
async def record_failure(scope, error):
    # Use the Step's function ID so rewind removes the failure with this Step.
    await DBOS.set_event_async("failure", error)
    await asyncio.to_thread(publish_failure, scope, error)


@asynccontextmanager
async def observe_stage(
    run_id,
    name,
    state,
    config,
    *,
    stage_id=None,
    retry=False,
    reserve_report=True,
    expires_at=None,
):
    """Publish phase observations and apply the existing business deadline."""
    round_ = state.get("workflow", {}).get("round", 1)

    scope = StageContext(
        run_id,
        stage_id or f"{DBOS.workflow_id}:{name}",
        name,
        await asyncio.to_thread(run_generation, run_id),
        Configuration.from_runnable_config(config),
        config["initial_budget"],
        reserve_report,
        expires_at,
    )
    token = stage_context.set(scope)
    try:
        with correlation(
            run_id=run_id,
            unit_id=scope.stage_id,
            step=name,
            execution_id=scope.execution_id,
            research_round=round_,
            generation=scope.generation,
        ):
            try:
                date = await announce_stage(scope, round_)
                async with stage_timeout(scope.config.workflow_step_timeout_seconds):
                    yield {**config, "research_date": date}
            except dbos_error.DBOSWorkflowConflictIDError:
                raise
            except BaseException as exc:
                kind = failure_kind(exc)
                annotate(
                    failure_kind=kind,
                    status="cancelled" if kind == "cancelled" else "failed",
                )
                unknown = any(
                    getattr(cause, "unknown_outcome", False)
                    for cause in error_chain(exc)
                )
                error = dict(
                    kind=kind,
                    error_type=type(exc).__name__,
                    step=name,
                    retry_safe=retry and not unknown and kind != "cancelled",
                    unknown_outcome=unknown,
                )
                if is_cancellation(exc):
                    # Native cancellation precludes allocating another DBOS Step.
                    await owned_io(publish_failure, scope, error)
                else:
                    await record_failure(scope, error)
                raise
    finally:
        stage_context.reset(token)


@DBOS.step(name="por.stage_result")
async def freeze_messages(result):
    """Assign missing message IDs once before downstream Workflows receive them."""
    patch = result["patch"]
    patch["messages"] = copy_state({"messages": patch["messages"]})["messages"]
    return result


async def stable_result(result):
    return await freeze_messages(result) if result["patch"].get("messages") else result


async def publish_stage(result):
    scope = stage_context.get()
    if scope.name.endswith("_agent") or scope.name == "research_review":
        # Role and Review completion already have their own native result.
        await publish_stage_step(result)
        return
    # A report can be ready even if a later publication or Memory step fails.
    if report := result.get("patch", {}).get("report", {}).get("final"):
        await DBOS.set_event_async("report", report)
    await publish_stage_step(result)
    steps = await DBOS.get_event_async(DBOS.workflow_id, "completed_stages", 0) or {}
    await DBOS.set_event_async(
        "completed_stages", steps | {scope.name: stage_budget(result)}
    )


@DBOS.step(
    name="por.publish_stage", retries_allowed=True, max_attempts=3, interval_seconds=0.5
)
async def publish_stage_step(result):
    """Send only public business output and stage progress, in one transaction."""
    await asyncio.to_thread(_publish_stage, stage_context.get(), result)


def _publish_stage(scope, result):
    patch = result.get("patch", {})
    with domain_engine().begin() as db:
        skipped = result.get("observation", {}).get("skipped")
        if skipped:
            stage_event(db, scope, "step_skipped", reason="role_disabled")
        else:
            stage_event(db, scope, "step_completed", **stage_budget(result))
        for role, report in role_deliverables(result).items():
            stage_event(db, scope, "agent_report", role=role, content=report)
            stage_event(
                db,
                scope,
                "stream",
                role=role,
                node=role + "_agent",
                content=report,
                granularity="stage",
            )
        review = patch.get("workflow", {}).get("review")
        if review is not None:
            stage_event(
                db,
                scope,
                "research_review",
                review=ResearchReview.model_validate(review).model_dump(mode="json"),
            )
        if report := patch.get("report", {}).get("final"):
            stage_event(db, scope, "report", content=report)


def business_config(request):
    config = deepcopy(request["data"]["config"])
    config["configurable"]["allow_plan_feedback"] = False
    config["initial_budget"] = (
        request["data"]["initial"].get("runtime", {}).get("budget", {})
    )
    return config


@DBOS.workflow(name="por.role")
@dbos_scope
async def role_workflow(run_id, unit_id, role, initial):
    name = role + "_agent"
    state = role_snapshot(initial, role)
    config = business_config(await load_request(run_id))
    async with observe_stage(run_id, name, state, config, stage_id=unit_id) as cfg:
        result = await getattr(research, name)(state, cfg)
        if isinstance(result, ResearchRoleResult):
            artifact = {"research_result": result.model_dump(mode="json")}
        else:
            artifact = {"patch": result}
            if not result and role not in {
                str(enabled).strip().lower()
                for enabled in Configuration.from_runnable_config(
                    cfg
                ).enabled_business_agents
                or ()
            }:
                artifact["observation"] = {"skipped": True, "reason": "role_disabled"}
        await publish_stage(artifact)
        return artifact


@DBOS.workflow(name="por.review")
@dbos_scope
async def review_workflow(run_id, initial):
    state = copy_state(initial)
    config = business_config(await load_request(run_id))
    async with observe_stage(
        run_id, "research_review", state, config, retry=True
    ) as cfg:
        with business_span("research_review"):
            patch = await research.research_review(state, cfg)
        saved = await stable_result({"patch": patch})
        apply_patch(state, saved["patch"])
        await publish_stage(saved)
        return {
            **saved,
            "followup": research.followup_assignments(
                state, Configuration.from_runnable_config(cfg)
            ),
        }


@DBOS.workflow(name="por.report")
@dbos_scope
async def report_workflow(run_id, initial):
    state = copy_state(initial)
    config = business_config(await load_request(run_id))
    sections = config["configurable"].get("report_mode", "synthesis") == "sections"
    report_steps = (
        [
            ("plan_report_sections", intake.section_plan_step),
            ("section_writer", reporting.section_writer),
            ("write_final_sections", reporting.write_final_sections),
        ]
        if sections
        else [("generate_final_report", reporting.generate_final_report)]
    )
    for name, generate in report_steps:
        async with observe_stage(
            run_id,
            name,
            state,
            config,
            retry=True,
            reserve_report=name
            not in {"generate_final_report", "write_final_sections"},
        ) as cfg:
            with business_span("report_generation", step=name):
                result = await generate(deepcopy(state), cfg)
            patch = result.update or {} if name == "plan_report_sections" else result
            saved = await stable_result({"patch": patch})
            await publish_stage(saved)
            apply_patch(state, saved["patch"])
    async with observe_stage(
        run_id, "compile_final_report", state, config, retry=True, reserve_report=False
    ) as cfg:
        with business_span("report_generation", step="compile_final_report"):
            patch = await reporting.compile_final_report(state, cfg, fail_on_error=True)
        saved = await stable_result({"patch": patch})
        await publish_stage(saved)
        report = patch.get("report", {}).get("final") or state["report"].get("final")
        if Configuration.from_runnable_config(cfg).rag_memory_write_enabled:
            await reporting.persist_report_memory(state, cfg, report, {})
        apply_patch(state, saved["patch"])
    return state


@DBOS.workflow(name="por.plan")
@dbos_scope
async def planning_workflow(run_id, initial, round_=1, *, revision_request=None):
    state = copy_state(initial)
    config = business_config(await load_request(run_id))
    if revision_request is not None:
        async with observe_stage(
            run_id,
            "revise_brief",
            state,
            config,
            retry=True,
            expires_at=revision_request["expires_at"],
        ) as cfg:
            revised = await intake.revise_brief(
                state, cfg, feedback=revision_request["feedback"]
            )
            revised = await stable_result(revised)
            await publish_stage(revised)
            apply_patch(state, revised["patch"])
            return {"state": state, "revision_error": revised["revision_error"]}
    if round_ == 1:
        async with observe_stage(
            run_id,
            "enrich_query_images",
            state,
            config,
            stage_id=run_id + ":enrich_query_images",
            retry=True,
        ) as cfg:
            patch = await intake.enrich_query_images(state, cfg)
            saved = await stable_result({"patch": patch})
            await publish_stage(saved)
            apply_patch(state, saved["patch"])
    async with observe_stage(
        run_id,
        "plan_research",
        state,
        config,
        stage_id=f"{run_id}:plan:{round_}",
        retry=True,
    ) as cfg:
        planned = await stable_result(await intake.plan_research(state, cfg))
        await publish_stage(planned)
        apply_patch(state, planned["patch"])
        return {"state": state, "decision": planned["decision"]}


@DBOS.workflow(name="por.research")
@dbos_scope
async def research_workflow(run_id, initial):
    state = copy_state(initial)
    apply_patch(
        state,
        {
            "workflow": {
                "type": "override",
                "value": {
                    **state.get("workflow", {}),
                    "round": 1,
                    "review": None,
                    "pending_tasks": [],
                    "completed_tasks": [],
                },
            },
            "agents": {"type": "override", "value": agents_for_new_run(state)},
            "research": {
                "type": "override",
                "value": {"run_id": run_id, "working_contexts": {}},
            },
        },
    )
    packets = {role: state for role in RESEARCH_ROLES}
    while packets:
        handles = {}
        for role in RESEARCH_ROLES:
            if role not in packets:
                continue
            packet = packets[role]
            unit = f"{run_id}:{role}:{packet['workflow']['round']}"
            with SetWorkflowID(unit):
                handles[role] = await DBOS.start_workflow_async(
                    role_workflow, run_id, unit, role, packet
                )
        remaining = dict(handles)
        results = {}
        try:
            while remaining:
                completed = await DBOS.wait_first_async(list(remaining.values()))
                role = next(
                    k
                    for k, h in remaining.items()
                    if h.workflow_id == completed.workflow_id
                )
                results[role] = read_research_result(
                    await completed.get_result(), role, packets[role]
                )
                del remaining[role]
        except BaseException:
            for handle in remaining.values():
                await DBOS.cancel_workflow_async(
                    handle.workflow_id, cancel_children=True
                )
            raise
        assemble_research_results(state, results, packets)
        round_ = state["workflow"]["round"]
        with SetWorkflowID(f"{run_id}:review:{round_}"):
            reviewed = await review_workflow(run_id, state)
        apply_patch(state, reviewed["patch"])
        packets = reviewed["followup"]
        if packets:
            await send_event(
                run_id,
                "research_followup",
                round=round_ + 1,
                roles=list(packets),
            )
    for role in ("risk_assessment", "response_strategy"):
        unit = run_id + ":" + role
        with SetWorkflowID(unit):
            handle = await DBOS.start_workflow_async(
                role_workflow, run_id, unit, role, state
            )
        apply_patch(state, (await handle.get_result())["patch"])
    with SetWorkflowID(run_id + ":report"):
        return await report_workflow(run_id, state)


@DBOS.step()
async def load_request(run_id):
    from open_deep_research.web.durable import validate_request

    req = await asyncio.to_thread(read_request, run_id)
    validate_request(req)
    return req


def _interaction_now():
    with domain_engine().connect() as db:
        return db_time(db)


@DBOS.step()
async def approval_deadline():
    return await asyncio.to_thread(_interaction_now) + settings().approval_seconds


@DBOS.step()
async def interaction_clock():
    return await asyncio.to_thread(_interaction_now)


async def receive_interaction(topic, owner, interaction):
    deadline = interaction["expires_at"]
    while True:
        # recv replays its recorded timeout. Use the live clock if the Executor
        # stopped before arming this wait, including after discarding a message.
        remaining = deadline - await asyncio.to_thread(_interaction_now)
        command = await DBOS.recv_async(topic, timeout_seconds=max(0, remaining))
        if command is None or await interaction_clock() >= deadline:
            return None
        if (
            isinstance(command, dict)
            and command.get("owner") == owner
            and command.get("version") == interaction["version"]
        ):
            return command


async def wait_for_revision(handle, expires_at):
    async def finished_in_time():
        # A queued workflow's own timeout starts on dequeue. This bounded read
        # also covers queue time, using the approval's original deadline.
        remaining = expires_at - await asyncio.to_thread(_interaction_now)
        if remaining <= 0:
            return False
        try:
            await asyncio.wait_for(
                native_client().wait_first_async([handle]), remaining
            )
        except asyncio.TimeoutError:
            return False
        return True

    if not await DBOS.run_step_async(
        {"name": "por.wait_for_revision", "preemptible": True}, finished_in_time
    ):
        await DBOS.cancel_workflow_async(handle.workflow_id, cancel_children=True)
        return None
    return await handle.get_result()


async def end_approval(run_id, reason):
    await DBOS.set_event_async("approval_end", reason)
    await send_event(run_id, "approval_ended", decision=reason)
    return {"approval": reason}


@DBOS.workflow(name="por.run")
@dbos_scope
async def run_workflow(run_id):
    from langchain_core.messages import HumanMessage

    from open_deep_research.workflow.brief import (
        MAX_BRIEF_REVISIONS,
        MAX_CLARIFICATION_ROUNDS,
        brief_metadata,
        validate_brief,
    )

    req = await load_request(run_id)
    state = req["data"]["initial"]
    for round_ in range(1, MAX_CLARIFICATION_ROUNDS + 2):
        await DBOS.set_event_async("phase", "planning")
        await send_event(run_id, "run_status", status="running")
        with (
            SetWorkflowID(run_id + f":planning:{round_}"),
            SetEnqueueOptions(queue_partition_key=req["owner"]),
            SetWorkflowTimeout(req["data"]["run_seconds"]),
        ):
            handle = await DBOS.enqueue_workflow_async(
                BUSINESS, planning_workflow, run_id, state, round_
            )
        planned = await handle.get_result()
        state = planned["state"]
        decision = planned["decision"]
        if decision["action"] == "ready":
            break
        if round_ > MAX_CLARIFICATION_ROUNDS:
            raise ValueError("Planner requested clarification at the round limit")
        challenge = dict(
            questions=decision["questions"],
            round=round_,
            max_rounds=MAX_CLARIFICATION_ROUNDS,
            expires_at=await approval_deadline(),
        )
        challenge["version"] = digest(dict(run_id=run_id, **challenge))
        await DBOS.set_event_async("clarification", challenge)
        await DBOS.set_event_async("phase", "awaiting_clarification")
        await send_event(
            run_id,
            "run_status",
            status="awaiting_clarification",
            clarification_version=challenge["version"],
        )
        reply = await receive_interaction("clarification", req["owner"], challenge)
        if reply is None:
            await DBOS.set_event_async("approval_end", "expired")
            await send_event(run_id, "clarification_ended", decision="expired")
            return {"clarification": "expired"}
        # Structured control was validated by admission. It is task data, never system instructions.
        apply_patch(
            state,
            {
                "messages": [
                    HumanMessage(
                        id="clarification-" + challenge["version"],
                        additional_kwargs={"clarification_skipped": reply["skip"]},
                        content=(
                            "Clarification questions: "
                            + dumps(challenge["questions"])
                            + "\nUser response: "
                            + (
                                "Skipped. Continue with existing information; do not invent unspecified details."
                                if reply["skip"]
                                else dumps(reply["answers"])
                            )
                        ),
                    )
                ],
            },
        )

    brief = validate_brief(state["workflow"]["brief"])
    plan = dict(
        brief=brief,
        sections=[],
        expires_at=await approval_deadline(),
        **brief_metadata(brief, 1, run_id),
    )
    needs_approval = req["data"]["config"]["configurable"]["allow_plan_feedback"]
    if needs_approval:
        plan["ai_revision"] = {"used": 0, "error": None}
    while True:
        await DBOS.set_event_async("plan", plan)
        if not needs_approval:
            break
        await DBOS.set_event_async("phase", "awaiting_approval")
        await send_event(
            run_id,
            "run_status",
            status="awaiting_approval",
            plan_version=plan["version"],
        )
        command = await receive_interaction("approval", req["owner"], plan)
        if command is None:
            return await end_approval(run_id, "expired")
        decision = command.get("decision")
        if decision == "reject":
            return await end_approval(run_id, "reject")
        if decision == "edit":
            brief = validate_brief(command["brief_md"])
            plan = dict(
                plan,
                brief=brief,
                ai_revision=dict(plan["ai_revision"], error=None),
                **brief_metadata(brief, plan["revision"] + 1, run_id),
            )
            continue
        if decision == "revise":
            revision = plan["ai_revision"]
            key_hash = command.get("revision_key_hash")
            if not key_hash or revision["used"] >= MAX_BRIEF_REVISIONS:
                continue
            await DBOS.set_event_async("phase", "revising_brief")
            await send_event(run_id, "run_status", status="running")
            apply_patch(state, {"workflow": {"brief": plan["brief"]}})
            with (
                SetWorkflowID(f"{run_id}:brief-revision:{key_hash}"),
                SetEnqueueOptions(queue_partition_key=req["owner"]),
                SetWorkflowTimeout(req["data"]["run_seconds"]),
            ):
                handle = await DBOS.enqueue_workflow_async(
                    BUSINESS,
                    planning_workflow,
                    run_id,
                    state,
                    revision_request={
                        "feedback": command["feedback"],
                        "expires_at": plan["expires_at"],
                    },
                )
            revised = await wait_for_revision(handle, plan["expires_at"])
            if revised is None or await interaction_clock() >= plan["expires_at"]:
                return await end_approval(run_id, "expired")
            state = revised["state"]
            error = revised["revision_error"]
            plan = dict(plan, ai_revision=dict(revision, error=error))
            if error is None:
                brief = validate_brief(state["workflow"]["brief"])
                plan.update(
                    brief=brief, **brief_metadata(brief, plan["revision"] + 1, run_id)
                )
                plan["ai_revision"]["used"] += 1
            continue
        if decision == "approve":
            break
    apply_patch(
        state,
        {
            "workflow": {
                "brief": plan["brief"],
                "brief_version": plan["version"],
                "brief_hash": plan["content_hash"],
                "brief_revision": plan["revision"],
            }
        },
    )
    await DBOS.set_event_async("brief_locked", plan["version"])
    await DBOS.set_event_async("phase", "research")
    await send_event(run_id, "run_status", status="running")
    with (
        SetWorkflowID(run_id + ":research"),
        SetEnqueueOptions(queue_partition_key=req["owner"]),
        SetWorkflowTimeout(req["data"]["run_seconds"]),
    ):
        handle = await DBOS.enqueue_workflow_async(
            BUSINESS, research_workflow, run_id, state
        )
    return await handle.get_result()


@DBOS.workflow(name="por.events")
@dbos_scope
async def events_workflow(run_id):
    """Merge native mailboxes into one append-only Stream for all readers."""
    delivery = {"offset": 0, "generation": 0}
    terminal = archive = None
    roles_finished = {}

    async def append(event):
        # DBOS assigns the native offset. This single writer counts its
        # checkpointed appends so GET can expose a delivered-prefix watermark.
        await DBOS.write_stream_async("events", await public_event(event))
        delivery["offset"] += 1

    while True:
        draining = terminal is not None or archive is not None
        waiting_for_cleanup = (
            terminal is not None
            and terminal["drain_roles"]
            and not all(roles_finished.values())
        )
        event = await DBOS.recv_async(
            "events",
            timeout_seconds=(
                SETTLEMENT_GRACE_SECONDS
                if waiting_for_cleanup
                else 0
                if draining
                else 86400
            ),
        )
        if event is None:
            # recv orders by millisecond, with no tie breaker. A terminal
            # notification is a fence: consume the committed mailbox prefix
            # before writing any terminal frames, including when archive won
            # the timestamp tie. Cancelled roles may still be emitting cleanup.
            if not draining:
                continue
            for batch in (terminal, archive):
                if batch is None:
                    continue
                observed_at = batch.get("observed_at") or await event_timestamp()
                for frame in batch["frames"]:
                    await append(
                        dict(
                            frame,
                            observed_at=observed_at,
                            generation=delivery["generation"],
                        )
                    )
                delivery.update(batch["receipt"])
            await DBOS.set_event_async("delivery", delivery)
            if archive and archive["close"]:
                await DBOS.close_stream_async("events")
                return
            terminal = archive = None
            continue

        generation = event["generation"]
        if generation < delivery["generation"]:
            continue
        if generation > delivery["generation"]:
            delivery = {"offset": delivery["offset"], "generation": generation}
            terminal = archive = None
            roles_finished.clear()
        kind = event["type"]
        if kind == "_terminal":
            terminal = event
            continue
        if kind == "_archive_saved":
            archive = event
            continue
        if event.get("step", "").endswith("_agent") and kind.startswith("step_"):
            # Match cleanup to the stage, including a same-millisecond start
            # received after its finish. No tracking of individual SDK events.
            stage = event["execution_id"]
            if kind == "step_started":
                roles_finished.setdefault(stage, False)
            else:
                roles_finished[stage] = True
        if kind == "run_status" and (
            version := event.get("plan_version") or event.get("clarification_version")
        ):
            delivery["pause"] = {"phase": event["status"], "version": version}
        await append(event)
        await DBOS.set_event_async("delivery", delivery)


@DBOS.step(retries_allowed=True, max_attempts=10, interval_seconds=0.5, backoff_rate=1)
async def event_timestamp():
    return timestamp(await asyncio.to_thread(_interaction_now))


async def public_event(event):
    return public_payload(
        dict(event, timestamp=event.get("observed_at") or await event_timestamp())
    )


@DBOS.workflow(name="por.cancel")
async def cancel_workflow(run_id):
    # Accepted transactionally with the request intent. DBOS recovers this
    # command if the API or Executor exits before cancellation reaches the Run.
    await DBOS.cancel_workflow_async(run_id, cancel_children=True)


@DBOS.step(retries_allowed=True, max_attempts=5, interval_seconds=1)
async def completion_facts(run_id):
    from open_deep_research.web.durable import get_run_client

    def read():
        return get_run_client().outcome(run_id)

    return await asyncio.to_thread(read)


async def wait_for_terminal(run_id):
    handle = await DBOS.retrieve_workflow_async(run_id, existing_workflow=False)
    await DBOS.wait_first_async([handle])


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=1, backoff_rate=2)
async def archive_step(run_id, result):
    from open_deep_research.web.history import (
        HistoryStage,
        get_history_store,
        history_record,
    )

    record = history_record(
        run_id=run_id,
        request=result["archive_request"],
        report=public_payload(result["report"]),
        usage=result["usage"],
        started_at=result["created_at"],
        completed_at=result["terminated_at"],
        status=result["status"]
        if result["status"] in {"completed", "failed", "cancelled"}
        else "incomplete",
        source="dbos",
        role_reports=[
            HistoryStage.model_validate(public_payload(stage))
            for stage in result["role_reports"]
        ],
        error_message="Research requires review." if result["error"] else "",
        failed_stage=result["current_step"],
    )
    record.owner = result["owner"]
    record.research_session_id = result["research_session_id"]
    # Archive order follows the business retry generation, not relay latency.
    record.run_state_version = result["generation"] + 1
    await asyncio.to_thread(get_history_store().save, [record])
    return record.id


def failure_target(workflow):
    """Bind a retry to a specific native execution and failed suffix."""
    steps = native_client().list_workflow_steps(workflow.workflow_id)
    stage = next(
        (
            step
            for step in reversed(steps)
            if step["function_name"] == "por.stage_started"
        ),
        None,
    )
    suffix = [
        step
        for step in steps
        if stage is None or step["function_id"] > stage["function_id"]
    ]
    result_steps = {"por.stage_result", "por.publish_stage"}
    publication = next(
        (
            step["function_id"]
            for step in suffix
            if step["function_name"] in result_steps | {"por.report_memory"}
            and step.get("error")
        ),
        None,
    )
    frozen = next(
        (
            step["function_id"]
            for step in reversed(suffix)
            if step["function_name"] in result_steps and step.get("error") is None
        ),
        None,
    )
    last = steps[-1]["function_id"] if steps else 0
    # Pure business validation can fail after a successful model call. Cut
    # at that call while retaining valid earlier phases and their receipts.
    calls = [
        step["function_id"]
        for step in suffix
        if step["function_name"] in {"por.llm", "por.llm_sync"}
    ]
    errors = [step["function_id"] for step in suffix if step.get("error") is not None]
    failed = bool(steps and steps[-1]["function_name"] == "por.stage_failed")
    failed_start = stage["function_id"] if stage and stage.get("error") else None
    start = failed_start or publication
    if start is None:
        start = (
            frozen + 1
            if frozen
            else max(calls or errors, default=last if failed else last + 1)
        )
    return dict(
        workflow_id=workflow.workflow_id,
        queue=CONTROL if workflow.name == "por.run" else BUSINESS,
        start_step=start,
        failure=digest(
            [
                workflow.app_version,
                workflow.updated_at,
                workflow.completed_at,
                [
                    (
                        step["function_id"],
                        step["function_name"],
                        step.get("child_workflow_id"),
                        step.get("started_at_epoch_ms"),
                        step.get("completed_at_epoch_ms"),
                    )
                    for step in steps
                ],
            ]
        ),
    )


def retry_targets(run_id):
    client = native_client()
    workflows = client.list_workflows(
        workflow_id_prefix=run_id,
        status="ERROR",
        app_version=VERSION,
        load_input=False,
        load_output=False,
    )
    root = next((wf for wf in workflows if wf.workflow_id == run_id), None)
    if root is None:
        raise RunConflict("The observed native failure changed")
    candidates = [wf for wf in workflows if wf.workflow_id.startswith(run_id + ":")]
    leaves = sorted(
        (
            wf
            for wf in candidates
            if wf.name in {"por.plan", "por.review", "por.report"}
        ),
        key=lambda wf: wf.completed_at or 0,
        reverse=True,
    )
    if not leaves:
        raise RunConflict("No safe failed native Step to rewind")
    leaf = leaves[0]
    failure = client.get_event(leaf.workflow_id, "failure", 0) or {}
    if not failure.get("retry_safe"):
        raise RunConflict("The failed Step requires reconciliation")
    parents = [failure_target(wf) for wf in candidates if wf.name == "por.research"]
    return [failure_target(leaf), *parents, failure_target(root)]


@DBOS.step()
async def retry_execution(run_id, command, index):
    """Apply only the native failure version captured by HTTP admission."""
    from open_deep_research.web.durable import external_effects, validate_request

    def rewind():
        with domain_engine().begin() as db:
            req = read_request(run_id, db=db, lock=True)
            row = db.execute(
                text("""
                SELECT version,generation,outcome FROM por_domain.retries
                WHERE run_id=:id AND key=:key
                """),
                {"id": run_id, "key": command.get("key")},
            ).first()
            if not row or not row.outcome:
                return None
            grant = loads(row.outcome)
            if (
                command
                != dict(
                    key=command.get("key"),
                    generation=row.generation,
                    version=row.version,
                    outcome=digest(grant),
                )
                or grant["version"] != VERSION
                or grant["source_generation"] + 1 != row.generation
                or retry_generation(db, run_id) != row.generation
            ):
                return None
            validate_request(req)
            if req["cancel_requested"]:
                return {"outcome": "cancelled"}
            client = native_client()
            targets = grant["targets"]
            root = targets[-1]
            statuses = {
                wf.workflow_id: wf
                for wf in client.list_workflows(
                    workflow_ids=[t["workflow_id"] for t in targets],
                    load_input=False,
                    load_output=False,
                )
            }

            def same_failure(target):
                wf = statuses.get(target["workflow_id"])
                return wf and wf.status == "ERROR" and failure_target(wf) == target

            if not same_failure(root):
                return {"outcome": "superseded"}
            target = targets[index]
            unchanged = same_failure(target)
            if index == 0 and unchanged:
                from open_deep_research.budget import model_bill

                if any(external_effects(db, run_id)) or model_bill(db, run_id)[1]:
                    return {"outcome": "blocked"}
            if unchanged:
                options = {
                    "start_step": target["start_step"],
                    "queue_name": target["queue"],
                }
                if target["queue"] == BUSINESS:
                    options["queue_partition_key"] = req["owner"]
                client.rewind_workflow(target["workflow_id"], **options)
            return {"target": target["workflow_id"], "last": target == root}

    try:
        return await asyncio.to_thread(rewind)
    except SQLAlchemyError:
        return {"retryable": True}


async def apply_retry(run_id, command, applied_generation):
    if (
        not isinstance(command, dict)
        or command.get("generation") != applied_generation + 1
    ):
        return applied_generation
    index = 0
    while True:
        receipt = await retry_execution(run_id, command, index)
        if receipt is None:
            return applied_generation
        if receipt.get("retryable"):
            await DBOS.sleep_async(1)
            continue
        if receipt.get("outcome") or receipt["last"]:
            break
        # Use the same native business queue for retries. A parent must not
        # occupy the last queue slot while waiting for its rewound child.
        await wait_for_terminal(receipt["target"])
        index += 1
    generation = command["generation"]
    await DBOS.set_event_async(
        "retry_applied",
        dict(generation=generation, outcome=receipt.get("outcome", "applied")),
    )
    return generation


def terminal_frames(result):
    status, usage = result["status"], result["usage"]
    yield dict(
        type="settlement",
        status=result["settlement"],
        usage=usage,
        error=result["error"],
    )
    yield dict(
        type="usage",
        **{
            key: usage.get(key, 0)
            for key in ("model_calls", "input_tokens", "output_tokens", "total_tokens")
        },
    )
    if error := result["error"]:
        yield dict(
            type="error",
            message="Research requires review.",
            kind=error["kind"],
            failed_stage=result["current_step"],
        )
    yield dict(type="run_status", status=status)
    yield dict(type="done", status=status, archive_status="pending")


@DBOS.workflow(name="por.finalize")
@dbos_scope
async def finalize_workflow(run_id):
    generation = 0
    while True:
        await wait_for_terminal(run_id)
        while True:
            try:
                result = await completion_facts(run_id)
            except Exception:
                await DBOS.sleep_async(5)
                continue
            if result["settlement"] != "pending":
                break
            await DBOS.sleep_async(1)

        await send_event(
            run_id,
            "_terminal",
            generation=generation,
            frames=list(terminal_frames(result)),
            receipt={"terminal": True, "pause": None},
            drain_roles=result["native_status"] != "SUCCESS",
        )
        archived = notified = False
        safe = result["retry_safe"]
        while True:
            if not archived:
                try:
                    history_id = await archive_step(
                        run_id, dict(result, generation=generation)
                    )
                except Exception:
                    if not notified:
                        await send_event(
                            run_id,
                            "archive_pending",
                            generation=generation,
                            message="Report archive is pending; the Run retains its result.",
                        )
                        notified = True
                else:
                    status = result["status"]
                    await send_event(
                        run_id,
                        "_archive_saved",
                        generation=generation,
                        close=not safe,
                        receipt={"history_id": history_id},
                        frames=[
                            dict(type="history_saved", id=history_id, status=status),
                            dict(type="done", status=status, archive_status="saved"),
                        ],
                    )
                    archived = True
                    if not safe:
                        return

            command = await DBOS.recv_async(
                "retry", timeout_seconds=86400 if archived else 30
            )
            if safe:
                advanced = await apply_retry(run_id, command, generation)
                if advanced > generation:
                    generation = advanced
                    break
