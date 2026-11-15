"""Native durable business orchestration. SDK Runner owns every Agent loop."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy

from dbos import DBOS, SetEnqueueOptions, SetWorkflowID, SetWorkflowTimeout
from dbos import error as dbos_error
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.budget import (
    budget_capture,
    diff_budget_usage,
    merge_budget_usage,
)
from open_deep_research.configuration import Configuration
from open_deep_research.observability.telemetry import (
    annotate,
    business_span,
    correlation,
    dbos_scope,
)
from open_deep_research.state import ResearchRoleResult
from open_deep_research.time_utils import get_today_str
from open_deep_research.web.run_data import (
    TERMINAL,
    ExecutionRevoked,
    digest,
    dumps,
    public_payload,
    timestamp,
)
from open_deep_research.workflow import intake, reporting, research
from open_deep_research.workflow.dbos_config import BUSINESS, SETTLEMENT_GRACE_SECONDS
from open_deep_research.workflow.execution import (
    ExecutionControlError,
    error_chain,
    failure_kind,
    owned_io,
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
from open_deep_research.workflow.run_facts import (
    UnitReceipts,
    confirm_cancel,
    current_unit,
    facts,
)
from open_deep_research.workflow.state_utils import agents_for_new_run


async def send_event(run_id, event_type, **data):
    # Called from workflows: the message is a native durable command.
    await DBOS.send_async(
        run_id + ":events", dict(type=event_type, **data), topic="events"
    )


@DBOS.step(name="por.stage_started")
async def publish_stage_started(unit, research_round):
    def publish():
        with unit.repo.engine.begin() as db:
            unit.repo.check(unit.id, unit.permit, db=db, locked=True)
            payload = {
                "step": unit.name,
                "unit_id": unit.id,
                "execution_id": unit.execution_id,
                "research_round": research_round,
            }
            event_key = digest([unit.id, unit.generation, "stage-start"])
            unit.repo.publish(
                db, unit.run_id, "step_started", idempotency_key=event_key, **payload
            )
            unit.repo.publish(
                db,
                unit.run_id,
                "status",
                idempotency_key=event_key + "-status",
                message=f"Running {unit.name}",
                granularity="stage",
            )
        return get_today_str()

    date, cancelled = await owned_io(publish)
    if cancelled is not None:
        raise cancelled
    return date


async def watch_stage_fence(unit, owner, workflow_id):
    """Interrupt non-preemptible I/O on a cancelled DBOS workflow or stale fence.

    The public DBOS client observes status without consuming a journal slot.
    """
    while True:
        await asyncio.sleep(1)
        try:
            if await asyncio.to_thread(
                unit.repo.cancellation_requested,
                unit.run_id,
                unit.id,
                unit.permit,
                workflow_id,
            ):
                owner.cancel()
                return True
        except (SQLAlchemyError, ExecutionRevoked) as exc:
            error = ExecutionControlError(
                "Durable execution permission/storage unavailable"
            )
            error.__cause__ = exc
            unit.remember_control_error(error)
            owner.cancel()
            return False


async def record_stage_failure(unit, error, observed, *, retryable, original):
    kind = failure_kind(error)
    annotate(failure_kind=kind, status="cancelled" if kind == "cancelled" else "failed")
    settlement = getattr(original, "context_settlement", None)
    settled = getattr(original, "context_harness", None) is None or (
        settlement is not None and settlement.succeeded
    )
    settled = settled and not getattr(error, "unknown_outcome", False)
    for cause in error_chain(original):
        usage = getattr(cause, "budget_usage", {})
        observed = merge_budget_usage(observed, diff_budget_usage(usage, observed))
        settled = settled and not getattr(cause, "unknown_outcome", False)
    try:
        settled = settled and not await asyncio.to_thread(
            unit.repo.unconfirmed_operations, unit.id
        )
        billed = await asyncio.to_thread(
            unit.repo.accounted_usage, unit.id, observed, unit.carry
        )
        await unit.write(
            unit.repo.finish_unit,
            usage=billed,
            settled=settled,
            error={
                "kind": kind,
                "error_type": type(error).__name__,
                "step": unit.name,
                "usage_complete": settled,
                "retry_safe": settled and retryable and kind != "cancelled",
                "delivery_ids": sorted(unit.delivery_ids),
            },
        )
    except (ExecutionRevoked, SQLAlchemyError):
        # The Finalizer still sees the pending settlement and its receipts.
        pass


@asynccontextmanager
async def stage_receipts(
    run_id,
    unit_id,
    name,
    state,
    config,
    *,
    retryable=False,
    reserve_report=True,
    expires_at=None,
):
    """Keep the domain's permit, billing and result projection scoped to one unit."""
    repo = facts()
    try:
        # Retry can revisit a leaf after the previous Finalizer released capacity.
        if not (await asyncio.to_thread(repo.request, run_id))["occupied"]:
            while await asyncio.to_thread(repo.reserve, run_id) is None:
                await asyncio.sleep(0.5)
        permit, _, past = await asyncio.to_thread(
            repo.acquire, run_id, unit_id, name, native_replay=True
        )
    except ExecutionRevoked:
        if await confirm_cancel(repo, run_id):
            raise dbos_error.DBOSWorkflowCancelledError(
                "Cancellation requested"
            ) from None
        raise
    unit = UnitReceipts(
        repo=repo,
        run_id=run_id,
        id=unit_id,
        name=name,
        permit=permit,
        config=Configuration.from_runnable_config(config),
        baseline=state.get("runtime", {}).get("budget", {}),
        generation=0,
        carry={},
        reserve_report=reserve_report,
        expires_at=expires_at,
    )
    research_round = state.get("workflow", {}).get("round", 1)
    token = current_unit.set(unit)
    watcher = asyncio.create_task(
        watch_stage_fence(unit, asyncio.current_task(), DBOS.workflow_id)
    )
    try:
        with budget_capture() as observed:
            try:
                unit.generation = await asyncio.to_thread(repo.retry_generation, run_id)
                unit.carry = await asyncio.to_thread(repo.failed_usage, unit_id, past)
                with correlation(
                    run_id=run_id,
                    unit_id=unit_id,
                    step=name,
                    execution_id=unit.execution_id,
                    research_round=research_round,
                    generation=unit.generation,
                ):
                    date = await publish_stage_started(unit, research_round)
                    yield unit, {**config, "research_date": date}
            except dbos_error.DBOSWorkflowConflictIDError:
                raise
            except BaseException as exc:
                cancelled = (
                    watcher.done() and not watcher.cancelled() and watcher.result()
                )
                watcher.cancel()
                failure = exc if cancelled else unit.remember_control_error(exc) or exc
                await record_stage_failure(
                    unit,
                    failure,
                    merge_budget_usage(unit.carry, observed),
                    retryable=retryable,
                    original=exc,
                )
                if cancelled and isinstance(exc, asyncio.CancelledError):
                    raise dbos_error.DBOSWorkflowCancelledError(
                        "Business Workflow cancelled"
                    ) from exc
                if failure is not exc:
                    raise failure from exc
                raise
    finally:
        watcher.cancel()
        current_unit.reset(token)


@DBOS.step(name="por.stage_result")
async def stage_result(unit, artifact):
    """Freeze a business result and its billed usage in the native journal."""
    if unit.control_error is not None:
        raise unit.control_error
    artifact = deepcopy(artifact)
    patch = artifact.get("patch", {})
    role = artifact.get("research_result")
    usage = role["budget_delta"] if role else patch.get("runtime", {}).get("budget", {})
    usage = merge_budget_usage(unit.carry, usage)
    billed = await asyncio.to_thread(
        unit.repo.accounted_usage, unit.id, usage, unit.carry
    )
    if role:
        role["budget_delta"] = billed
    elif billed:
        patch.setdefault("runtime", {})["budget"] = billed
    if "messages" in patch:
        patch["messages"] = copy_state({"messages": patch["messages"]})["messages"]
    artifact["delivery_ids"] = sorted(unit.delivery_ids)
    return artifact


@DBOS.step(name="por.save_stage")
async def save_stage(unit, artifact):
    """Commit the idempotent public projection after DBOS has recorded the result."""
    role = artifact.get("research_result")
    usage = (
        role["budget_delta"]
        if role
        else artifact["patch"].get("runtime", {}).get("budget", {})
    )
    _, cancelled = await unit.write(
        unit.repo.finish_unit, usage=usage, artifact=artifact
    )
    if cancelled is not None:
        raise cancelled


def business_config(request):
    config = deepcopy(request["data"]["config"])
    config["configurable"]["allow_plan_feedback"] = False
    return config


@DBOS.workflow(name="por.role")
@dbos_scope
async def role_workflow(run_id, unit_id, role, initial):
    name = role + "_agent"
    state = role_snapshot(initial, role)
    config = business_config(await load_request(run_id))
    async with stage_receipts(run_id, unit_id, name, state, config) as (unit, cfg):
        async with stage_timeout(unit.config.workflow_step_timeout_seconds):
            result = await getattr(research, name)(state, cfg)
        if isinstance(result, ResearchRoleResult):
            artifact = {"research_result": result.model_dump(mode="json")}
        else:
            artifact = {"patch": result}
            if not result and role not in {
                str(enabled).strip().lower()
                for enabled in unit.config.enabled_business_agents or ()
            }:
                artifact["observation"] = {"skipped": True, "reason": "role_disabled"}
        saved = await stage_result(unit, artifact)
        await save_stage(unit, saved)
        return saved


@DBOS.workflow(name="por.review")
@dbos_scope
async def review_workflow(run_id, initial):
    state = copy_state(initial)
    round_ = state.get("workflow", {}).get("round", 1)
    config = business_config(await load_request(run_id))
    async with stage_receipts(
        run_id,
        f"{run_id}:review:{round_}",
        "research_review",
        state,
        config,
        retryable=True,
    ) as (unit, cfg):
        with business_span("research_review", step=unit.name):
            async with stage_timeout(unit.config.workflow_step_timeout_seconds):
                patch = await research.research_review(state, cfg)
        saved = await stage_result(unit, {"patch": patch})
        apply_patch(state, saved["patch"])
        await save_stage(unit, saved)
        return {**saved, "followup": research.followup_assignments(state, unit.config)}


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
        async with stage_receipts(
            run_id,
            run_id + ":" + name,
            name,
            state,
            config,
            retryable=True,
            reserve_report=name
            not in {"generate_final_report", "write_final_sections"},
        ) as (unit, cfg):
            with business_span("report_generation", step=name):
                async with stage_timeout(unit.config.workflow_step_timeout_seconds):
                    # Section writers update their input objects. Only a
                    # saved patch may update the Workflow's shared State.
                    result = await generate(deepcopy(state), cfg)
            patch = result.update or {} if name == "plan_report_sections" else result
            saved = await stage_result(unit, {"patch": patch})
            await save_stage(unit, saved)
            apply_patch(state, saved["patch"])
    name = "compile_final_report"
    async with stage_receipts(
        run_id,
        run_id + ":" + name,
        name,
        state,
        config,
        reserve_report=False,
    ) as (unit, cfg):
        with business_span("report_generation", step=name):
            async with stage_timeout(unit.config.workflow_step_timeout_seconds):
                patch = await reporting.compile_final_report(
                    state, cfg, fail_on_error=True
                )
        saved = await stage_result(unit, {"patch": patch})
        await save_stage(unit, saved)
        apply_patch(state, saved["patch"])
    return state


@DBOS.step()
async def reserve_step(run_id):
    try:
        return await asyncio.to_thread(facts().reserve, run_id) is not None
    except ExecutionRevoked:
        if await confirm_cancel(facts(), run_id):
            raise dbos_error.DBOSWorkflowCancelledError(
                "Cancellation requested"
            ) from None
        raise


@DBOS.step()
async def release_step(run_id):
    await asyncio.to_thread(facts().release, run_id)


async def reserve(run_id):
    while not await reserve_step(run_id):
        await DBOS.sleep_async(1)


@DBOS.workflow(name="por.plan")
@dbos_scope
async def planning_workflow(run_id, initial, round_=1, *, revision_request=None):
    # Approvals wait in por.run, after the business reservation is released.
    await reserve(run_id)
    state = copy_state(initial)
    try:
        config = business_config(await load_request(run_id))
        if revision_request is not None:
            async with stage_receipts(
                run_id,
                DBOS.workflow_id,
                "revise_brief",
                state,
                config,
                retryable=True,
                expires_at=revision_request["expires_at"],
            ) as (unit, cfg):
                async with stage_timeout(unit.config.workflow_step_timeout_seconds):
                    revised = await intake.revise_brief(
                        state, cfg, feedback=revision_request["feedback"]
                    )
                saved = await stage_result(unit, revised)
                await save_stage(unit, saved)
                apply_patch(state, saved["patch"])
                return {"state": state, "revision_error": saved["revision_error"]}
        if round_ == 1:
            name = "enrich_query_images"
            async with stage_receipts(
                run_id, run_id + ":" + name, name, state, config, retryable=True
            ) as (unit, cfg):
                async with stage_timeout(unit.config.workflow_step_timeout_seconds):
                    patch = await intake.enrich_query_images(state, cfg)
                saved = await stage_result(unit, {"patch": patch})
                await save_stage(unit, saved)
                apply_patch(state, saved["patch"])
        async with stage_receipts(
            run_id,
            f"{run_id}:plan:{round_}",
            "plan_research",
            state,
            config,
            retryable=True,
        ) as (unit, cfg):
            async with stage_timeout(unit.config.workflow_step_timeout_seconds):
                planned = await intake.plan_research(state, cfg)
            saved = await stage_result(unit, planned)
            await save_stage(unit, saved)
            apply_patch(state, saved["patch"])
            return {"state": state, "decision": saved["decision"]}
    finally:
        # A resumed planner may have re-acquired capacity after a lost ACK.
        # Repeat the idempotent release even if the plan was already saved.
        _, cancelled = await owned_io(facts().release, run_id)
        if cancelled is not None:
            raise cancelled


@DBOS.step()
async def cleanup_children(run_id):
    # DBOS terminal status can precede SDK's bounded drain. This is domain settlement.
    for _ in range(int(SETTLEMENT_GRACE_SECONDS * 10)):
        if not await asyncio.to_thread(facts().pending_settlement, run_id):
            return
        await asyncio.sleep(0.1)
    await asyncio.to_thread(facts().quarantine_pending, run_id)


@DBOS.workflow(name="por.research")
@dbos_scope
async def research_workflow(run_id, initial):
    await reserve(run_id)
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
    try:
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
                await cleanup_children(run_id)
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
    finally:
        await release_step(run_id)


@DBOS.step()
async def load_request(run_id):
    req = await asyncio.to_thread(facts().request, run_id)
    facts().validate(req)
    return req


def _interaction_now():
    repo = facts()
    with repo.engine.connect() as db:
        return repo.now(db)


@DBOS.step()
async def approval_deadline():
    return _interaction_now() + facts().settings.approval_seconds


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
            await asyncio.wait_for(facts().client.wait_first_async([handle]), remaining)
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
        await send_event(run_id, "run_status", status="awaiting_clarification")
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
    cursor = 0
    generation = 0
    seen = set()
    delivered_ids = set()
    terminal = None
    history = None
    acknowledged = False
    while True:
        event = await DBOS.recv_async("events", timeout_seconds=86400)
        if event is None:
            continue
        if "generation" in event and event["generation"] < generation:
            continue
        if event.get("generation", generation) > generation:
            generation = event["generation"]
            seen, terminal, history, acknowledged = set(), None, None, False
        cursor += 1
        event = dict(
            event, protocol_version=1, run_id=run_id, attempt_id=run_id, seq=cursor
        )
        event.setdefault("generation", generation)
        event = await public_event(event)
        await DBOS.write_stream_async("events", event)
        await DBOS.set_event_async("cursor", cursor)
        if event.get("event_id"):
            delivered_ids.add(event["event_id"])
        if event["type"] != "run_status" or event.get("status") in TERMINAL:
            seen.add(event["type"])
        if event["type"] == "done":
            terminal = event
        if event["type"] == "run_status":
            await DBOS.set_event_async("phase", event["status"])
        if event["type"] == "history_saved":
            history = event
        # DBOS 3.3 orders native messages by millisecond transaction timestamp.
        # A committed archive packet can overtake tail packets. Receipt readiness
        # is a delivery fact, never permission to execute or replay research.
        required = {"settlement", "usage", "run_status"}
        if terminal:
            required.update(terminal.get("expected_events", []))
        if (
            not terminal
            or not required.issubset(seen)
            or not set(terminal.get("expected_event_ids", [])).issubset(delivered_ids)
        ):
            continue
        if history:
            # Preserve the public archive-done tail, then publish its receipts.
            cursor += 1
            done = await public_event(
                dict(history, type="done", seq=cursor, archive_status="saved")
            )
            await DBOS.write_stream_async("events", done)
            await DBOS.set_event_async("cursor", cursor)
            await DBOS.set_event_async("done", {"generation": generation})
            await DBOS.set_event_async(
                "history", {"id": history["id"], "generation": generation}
            )
            if history["status"] in {"completed", "cancelled"}:
                await DBOS.close_stream_async("events")
                return
            history = None
            acknowledged = True
        elif not acknowledged:
            await DBOS.set_event_async("done", {"generation": generation})
            acknowledged = True


@DBOS.step(retries_allowed=True, max_attempts=10, interval_seconds=0.5, backoff_rate=1)
async def public_event(event):
    with facts().engine.connect() as db:
        return public_payload(dict(event, timestamp=timestamp(facts().now(db))))


@DBOS.step()
async def completion_facts(run_id, cleanup_expired=False):
    from open_deep_research.web.durable import RunClient

    client = RunClient(facts())
    result = await asyncio.to_thread(client.get, run_id, None)
    if (
        result["native_status"]
        in {"SUCCESS", "ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"}
        and result["settlement"] == "pending"
    ):
        if not cleanup_expired:
            return {"cleanup_pending": True}
        await asyncio.to_thread(facts().quarantine_pending, run_id)
        result = await asyncio.to_thread(client.get, run_id, None)
    if result["status"] == "cancelling":
        await asyncio.to_thread(
            facts().client.cancel_workflow, run_id, cancel_children=True
        )
    if result["status"] not in {"completed", "failed", "cancelled", "interrupted"}:
        return None
    if result["settlement"] == "pending":
        return None
    await asyncio.to_thread(facts().release, run_id)
    # Delivery metadata from committed unit facts, not execution authorization.
    units = await asyncio.to_thread(facts().units, run_id)
    result["expected_event_ids"] = sorted(
        {
            event_id
            for unit in units
            for event_id in (unit["artifact"] or unit["error"] or {}).get(
                "delivery_ids", []
            )
        }
    )
    return result


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=1, backoff_rate=2)
async def archive_step(run_id, result):
    from open_deep_research.web.history import (
        HistoryStage,
        get_history_store,
        history_record,
    )

    record = history_record(
        run_id=run_id,
        request=(await asyncio.to_thread(facts().request, run_id))["data"]["request"],
        report=result["report"],
        usage=result["usage"],
        started_at=result["created_at"],
        status=result["status"]
        if result["status"] in {"completed", "failed", "cancelled"}
        else "incomplete",
        source="dbos",
        role_reports=[HistoryStage.model_validate(s) for s in result["role_reports"]],
        error_message="Research requires review." if result["error"] else "",
        failed_stage=result["current_step"],
    )
    record.owner = result["owner"]
    record.research_session_id = result["research_session_id"]
    # Archive order follows the business retry generation, not relay latency.
    record.run_state_version = result["generation"] + 1
    await asyncio.to_thread(get_history_store().save, [record])
    return record.id


@DBOS.step()
async def retry_execution(run_id, command):
    from open_deep_research.web.durable import RunClient

    try:
        return await asyncio.to_thread(RunClient(facts())._rewind_safe, run_id, command)
    except SQLAlchemyError:
        # JSON-normalized exceptions need not retain their Python type on replay.
        # Persist transient control failure as data, without acknowledging a grant.
        return {"retryable": True}


async def apply_retry(run_id, command, applied_generation):
    # A duplicate/late message cannot become a grant for another native ERROR.
    if (
        not isinstance(command, dict)
        or command.get("generation") != applied_generation + 1
    ):
        return applied_generation
    while True:
        receipt = await retry_execution(run_id, command)
        if receipt != {"retryable": True}:
            break
        await DBOS.sleep_async(1)
    if receipt is None:
        return applied_generation
    await DBOS.set_event_async("retry_applied", receipt)
    return receipt["generation"]


@DBOS.workflow(name="por.finalize")
@dbos_scope
async def finalize_workflow(run_id):
    applied_generation = 0
    while True:
        cleanup_wait = 0
        while True:
            try:
                result = await completion_facts(
                    run_id, cleanup_wait >= SETTLEMENT_GRACE_SECONDS
                )
            except Exception:
                await DBOS.sleep_async(5)
                continue
            if result and not result.get("cleanup_pending"):
                if result["generation"] > applied_generation:
                    command = await DBOS.recv_async("retry", timeout_seconds=0)
                    if command:
                        applied_generation = await apply_retry(
                            run_id, command, applied_generation
                        )
                    else:
                        await DBOS.sleep_async(1)
                    # Never publish the previous failure as the new generation
                    # while its transactionally submitted command is pending.
                    continue
                break
            cleanup_wait = cleanup_wait + 1 if result else 0
            await DBOS.sleep_async(1)
        await send_event(
            run_id,
            "settlement",
            generation=result["generation"],
            status=result["settlement"],
            usage=result["usage"],
            error=result["error"],
        )
        if result["report"]:
            await send_event(
                run_id,
                "report",
                content=result["report"],
                generation=result["generation"],
            )
        await send_event(
            run_id,
            "usage",
            generation=result["generation"],
            **{
                k: result["usage"].get(k, 0)
                for k in (
                    "model_calls",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                )
            },
        )
        if result["error"]:
            await send_event(
                run_id,
                "error",
                generation=result["generation"],
                message="Research requires review.",
                kind=result["error"].get("kind", "execution_error"),
                failed_stage=result["current_step"],
            )
        await send_event(
            run_id,
            "run_status",
            status=result["status"],
            generation=result["generation"],
        )
        await send_event(
            run_id,
            "done",
            status=result["status"],
            archive_status="pending",
            generation=result["generation"],
            expected_event_ids=result.get("expected_event_ids", []),
            expected_events=["settlement", "usage", "run_status"]
            + (["report"] if result["report"] else [])
            + (["error"] if result["error"] else []),
        )
        advanced = False
        while True:
            try:
                history_id = await archive_step(run_id, result)
                await send_event(
                    run_id,
                    "history_saved",
                    id=history_id,
                    status=result["status"],
                    generation=result["generation"],
                )
                break
            except Exception:
                await send_event(
                    run_id,
                    "archive_pending",
                    generation=result["generation"],
                    message="Report archive is pending; the Run retains its result.",
                )
                command = await DBOS.recv_async("retry", timeout_seconds=30)
                if command and result["retry_safe"]:
                    updated = await apply_retry(run_id, command, applied_generation)
                    if updated > applied_generation:
                        applied_generation = updated
                        advanced = True
                        break
        if advanced:
            continue
        if not result["retry_safe"]:
            return
        while True:
            command = await DBOS.recv_async("retry", timeout_seconds=86400)
            if command:
                updated = await apply_retry(run_id, command, applied_generation)
                if updated > applied_generation:
                    applied_generation = updated
                    break
