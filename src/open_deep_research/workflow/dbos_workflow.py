"""Native durable business orchestration. SDK Runner owns every Agent loop."""

from __future__ import annotations

import asyncio
import time
from contextlib import ExitStack
from copy import deepcopy
from functools import partial
from uuid import uuid4

from agents import SQLiteSession
from dbos import DBOS, SetEnqueueOptions, SetWorkflowID, SetWorkflowTimeout
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.budget import (
    ModelBudgetExceeded,
    can_spend_model_call,
    is_over_budget,
    merge_budget_usage,
)
from open_deep_research.configuration import Configuration
from open_deep_research.llm.errors import ModelInvocationError
from open_deep_research.observability.telemetry import (
    annotate,
    correlation,
    dbos_scope,
    telemetry_enabled,
)
from open_deep_research.state import ResearchRoleResult
from open_deep_research.web.run_data import (
    TERMINAL,
    ExecutionRevoked,
    RunConflict,
    digest,
    dumps,
    public_payload,
    sdk_observation,
    timestamp,
)
from open_deep_research.workflow import intake, reporting, research
from open_deep_research.workflow.business_steps import SAFE_STEPS, _step, failure_kind
from open_deep_research.workflow.dbos_config import (
    BUSINESS,
    FACT_WRITE_GRACE_SECONDS,
    SETTLEMENT_GRACE_SECONDS,
)
from open_deep_research.workflow.durability import (
    checkpoint,
    durable_generation,
    durable_unit,
    event_delivery,
    physical_calls,
)
from open_deep_research.workflow.execution import (
    ExecutionControlError,
    execution_observer,
    owned_io,
)
from open_deep_research.workflow.python_state import (
    RESEARCH_ROLES,
    apply_patch,
    assemble_research_results,
    copy_state,
    read_research_result,
    role_snapshot,
)
from open_deep_research.workflow.run_facts import facts
from open_deep_research.workflow.state_utils import agents_for_new_run

OPERATIONS = {
    "enrich_query_images": intake.enrich_query_images,
    "plan_research": intake.plan_research,
    "revise_brief": intake.revise_brief,
    "generate_final_report": reporting.generate_final_report,
    "plan_report_sections": intake.section_plan_step,
    "research_review": research.research_review,
    "section_writer": reporting.section_writer,
    "write_final_sections": reporting.write_final_sections,
    "compile_final_report": partial(reporting.compile_final_report, fail_on_error=True),
}


async def send_event(run_id, event_type, **data):
    # Called from workflows: the message is a native durable command.
    await DBOS.send_async(
        run_id + ":events", dict(type=event_type, **data), topic="events"
    )


async def confirm_cancel(repo, run_id):
    """Deliver an already authorized cancellation before a phase can fail fast."""
    if (await asyncio.to_thread(repo.request, run_id))["cancel_requested"]:
        await owned_io(repo.client.cancel_workflow, run_id, cancel_children=True)
        return True
    return False


@dbos_scope
async def business_step(run_id, unit_id, name, state, *, revision_request=None):
    """Rebuild a business stage in Workflow context; only its I/O uses Steps.

    The historical name remains for callers. This is deliberately NOT a Step:
    DBOSRunner and bounded LangChain calls need independent native checkpoints.
    """
    repo = facts()
    try:
        while (req := await asyncio.to_thread(repo.reserve, run_id)) is None:
            await asyncio.sleep(0.5)
        permit, _, past = await asyncio.to_thread(
            repo.acquire,
            run_id,
            unit_id,
            name,
            safe=name in SAFE_STEPS,
            native_replay=True,
        )
    except ExecutionRevoked:
        if await confirm_cancel(repo, run_id):
            raise asyncio.CancelledError()
        raise
    # Execution results are read exclusively from the DBOS journal.
    cfg = deepcopy(req["data"]["config"])
    cfg["configurable"]["allow_plan_feedback"] = False
    sessions = []
    delivery_ids = set()
    control_failure = None

    async def observe_business(kind, **payload):
        if kind == "cancel_requested":
            return (await asyncio.to_thread(repo.request, run_id))["cancel_requested"]
        if kind == "execution_poll":
            if await asyncio.to_thread(repo.check, unit_id, permit, allow_cancel=True):
                await owned_io(
                    repo.client.cancel_workflow, run_id, cancel_children=True
                )
                raise asyncio.CancelledError()
            return None
        if kind == "effect_guard":
            return repo.effect_guard(unit_id, permit)
        if kind in {
            "tool_started",
            "tool_finished",
            "boundary_started",
            "boundary_finished",
        }:
            if kind == "boundary_started" and payload["identity"]["name"] in {
                "llm",
                "llm_sync",
                "sdk_model",
            }:
                actual, _ = await asyncio.to_thread(repo.physical_usage, unit_id)
                total = merge_budget_usage(
                    state.get("runtime", {}).get("budget", {}), actual
                )
                policy = Configuration.from_runnable_config(cfg)
                reserve_report = name not in {
                    "generate_final_report",
                    "write_final_sections",
                    "compile_final_report",
                }
                if is_over_budget(policy, total) or not can_spend_model_call(
                    policy, total, reserve_final_report_call=reserve_report
                ):
                    raise ModelBudgetExceeded(
                        "Physical call receipts exhausted the research budget"
                    )
                if (
                    revision_request is not None
                    and await asyncio.to_thread(_interaction_now)
                    >= revision_request["expires_at"]
                ):
                    # This guard runs inside the native model Step, so cached
                    # results replay without authorizing an expired provider call.
                    raise ModelInvocationError("BriefApprovalExpired")

            async def write_receipt():
                deadline = time.monotonic() + FACT_WRITE_GRACE_SECONDS
                while True:
                    try:
                        return await asyncio.to_thread(
                            repo.operation,
                            unit_id,
                            permit,
                            payload["identity"],
                            status="started"
                            if kind.endswith("_started")
                            else payload["status"],
                            usage=payload.get("usage"),
                        )
                    except SQLAlchemyError:
                        if kind.endswith("_started") or time.monotonic() >= deadline:
                            raise
                        await asyncio.sleep(0.5)

            # Own only the idempotent control write, never replay the external
            # provider/tool. Cancellation cannot discard its known usage while
            # a short storage outage or committed-but-lost ACK is being settled.
            pending_receipt = asyncio.create_task(write_receipt())
            cancelled = None
            while not pending_receipt.done():
                try:
                    await asyncio.shield(pending_receipt)
                except asyncio.CancelledError as exc:
                    cancelled = cancelled or exc
            result = pending_receipt.result()
            annotate(
                operation_id=result,
                effect_status="started"
                if kind.endswith("_started")
                else payload["status"],
            )
            if kind.endswith("_started") and cancelled:
                await owned_io(
                    repo.operation,
                    unit_id,
                    permit,
                    payload["identity"],
                    status="not_executed",
                )
                raise cancelled
            return result
        await asyncio.to_thread(
            repo.check,
            unit_id,
            permit,
            allow_cancel=kind
            in {
                "model_completed",
                "model_failed",
                "agent_completed",
                "agent_failed",
                "harness",
                "tool_result",
            },
        )
        # Never persist raw prompt/tool arguments/output/exception text publicly.
        allowed = sdk_observation(payload)

        def publish():
            event_id = uuid4().hex
            with repo.engine.begin() as db:
                repo.publish(
                    db,
                    run_id,
                    "sdk_event",
                    event_id=event_id,
                    event="session_started" if kind == "sdk_session" else kind,
                    step=name,
                    unit_id=unit_id,
                    execution_id=digest({"unit": unit_id, "permit": permit}),
                    **allowed,
                )
            return event_id

        async def publish_step():
            result, cancel = await owned_io(publish)
            if cancel:
                raise cancel
            return result

        cleanup = kind in {
            "model_completed",
            "model_failed",
            "agent_completed",
            "agent_failed",
            "harness",
            "tool_result",
        }
        cancelled_request = (
            cleanup
            and (await asyncio.to_thread(repo.request, run_id))["cancel_requested"]
        )
        # Native cancelled workflows cannot append commands. Their still-owned
        # cleanup writes business delivery facts directly, as the former Step did.
        event_id = (
            await publish_step()
            if cancelled_request
            else await checkpoint("por.sdk_observation", publish_step)
        )
        delivery_ids.add(event_id)
        if kind == "sdk_session":
            if repo.settings.session_path:
                session, cancelled = await owned_io(
                    SQLiteSession,
                    unit_id + ":" + payload["invocation_id"],
                    repo.settings.session_path,
                )
                sessions.append(session)
                if cancelled:
                    raise cancelled
                return session
            return None
        return None

    async def observer(kind, **payload):
        nonlocal control_failure
        if kind == "execution_failed":
            pending = [payload.get("error")]
            seen = set()
            while pending:
                error = pending.pop()
                if error is None or id(error) in seen:
                    continue
                seen.add(id(error))
                if isinstance(error, ExecutionControlError):
                    if control_failure is None:
                        control_failure = error
                    elif getattr(error, "unknown_outcome", False):
                        control_failure.unknown_outcome = True
                pending.extend(getattr(error, "exceptions", ()))
                pending.extend(
                    [
                        getattr(error, "__cause__", None),
                        getattr(error, "__context__", None),
                    ]
                )
            return control_failure
        if (
            control_failure is not None
            and (
                kind in {"model_started", "tool_started", "boundary_started"}
                or (
                    kind == "effect_guard"
                    and getattr(control_failure, "unknown_outcome", False)
                )
            )
            and (
                getattr(control_failure, "unknown_outcome", False)
                or not payload.get("identity", {}).get("settlement", False)
            )
        ):
            raise control_failure
        try:
            return await observe_business(kind, **payload)
        except (RunConflict, SQLAlchemyError) as exc:
            if (
                isinstance(exc, ExecutionRevoked)
                and (await asyncio.to_thread(repo.request, run_id))["cancel_requested"]
            ):
                await asyncio.to_thread(repo.check, unit_id, permit, allow_cancel=True)
                await owned_io(
                    repo.client.cancel_workflow, run_id, cancel_children=True
                )
                raise asyncio.CancelledError() from exc
            error = ExecutionControlError(
                "Durable execution permission/storage unavailable"
            )
            error.unknown_outcome = isinstance(exc, RunConflict) and not isinstance(
                exc, ExecutionRevoked
            )
            if control_failure is not None and error.unknown_outcome:
                control_failure.unknown_outcome = True
            control_failure = control_failure or error
            raise error from exc

    async def persist(**data):
        # Retry idempotent fact writes only, never the SDK invocation or tool.
        deadline = time.monotonic() + FACT_WRITE_GRACE_SECONDS
        while True:
            try:
                _, cancelled = await owned_io(repo.finish_unit, unit_id, permit, **data)
                return cancelled
            except SQLAlchemyError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.5)

    artifact = None
    observed_usage = past
    operation_started = False
    operation_returned = False
    identity = ExitStack()
    identity.enter_context(
        correlation(
            run_id=run_id,
            unit_id=unit_id,
            step=name,
            execution_id=digest({"unit": unit_id, "permit": permit}),
            research_round=state.get("workflow", {}).get("round", 1),
        )
    )
    token = execution_observer.set(observer)
    durable_token = durable_unit.set(unit_id)
    calls_token = physical_calls.set({})
    delivery_token = event_delivery.set(delivery_ids)
    owner_task = asyncio.current_task()

    async def watch_cancellation():
        while True:
            await asyncio.sleep(1)
            try:
                await observer("execution_poll")
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    return
                owner_task.cancel()
                return
            except Exception:
                owner_task.cancel()
                return

    watcher = asyncio.create_task(watch_cancellation())

    def read_generation():
        from sqlalchemy import text

        with repo.engine.connect() as db:
            return db.execute(
                text(
                    "SELECT coalesce(max(generation),0) FROM por_domain.retries WHERE run_id=:id"
                ),
                {"id": run_id},
            ).scalar_one()

    generation_token = None
    try:
        past = await asyncio.to_thread(repo.failed_usage, unit_id, past)
        if past:
            state = deepcopy(state)
            runtime = state.setdefault("runtime", {})
            runtime["budget"] = merge_budget_usage(runtime.get("budget", {}), past)
        generation = await asyncio.to_thread(read_generation)
        generation_token = durable_generation.set(generation)
        if telemetry_enabled():
            identity.enter_context(correlation(generation=generation))

        def started():
            with repo.engine.begin() as db:
                repo.publish(
                    db,
                    run_id,
                    "step_started",
                    step=name,
                    unit_id=unit_id,
                    execution_id=digest({"unit": unit_id, "permit": permit}),
                    research_round=state.get("workflow", {}).get("round", 1),
                )
                repo.publish(
                    db, run_id, "status", message=f"Running {name}", granularity="stage"
                )

        async def start():
            await asyncio.to_thread(started)

        await checkpoint("por.stage_started", start)
        operation = OPERATIONS.get(name) or getattr(research, name)
        if revision_request is not None:
            operation = partial(operation, feedback=revision_request["feedback"])
        operation_started = True
        result = await _step(
            name,
            operation,
            state,
            cfg,
            Configuration.from_runnable_config(cfg).workflow_step_timeout_seconds,
        )
        operation_returned = True
        if control_failure is not None:
            raise control_failure
        if isinstance(result, ResearchRoleResult):
            # Unit history belongs in this delta once; the State baseline does not.
            usage = merge_budget_usage(past, result.budget_delta)
            artifact = {"research_result": result.model_dump(mode="json")}
            artifact["research_result"]["budget_delta"] = usage
        elif name in {"plan_research", "revise_brief"}:
            artifact = result
        elif hasattr(result, "goto"):
            artifact = {"patch": result.update or {}, "goto": result.goto}
        else:
            artifact = {"patch": result}
        role = name.removesuffix("_agent")
        if (
            name.endswith("_agent")
            and role
            in {
                "public_signal",
                "internal_knowledge",
                "risk_assessment",
                "response_strategy",
            }
            and not result
            and role
            not in {
                str(item).strip().lower()
                for item in (
                    Configuration.from_runnable_config(cfg).enabled_business_agents
                    or []
                )
            }
        ):
            artifact["observation"] = {"skipped": True, "reason": "role_disabled"}
        patch = artifact.get("patch", {})
        if "research_result" not in artifact:
            usage = merge_budget_usage(past, patch.get("runtime", {}).get("budget", {}))
            if usage:
                patch.setdefault("runtime", {})["budget"] = usage
        if name == "research_review":
            reviewed = deepcopy(state)
            apply_patch(reviewed, patch)
            artifact["followup"] = research.followup_assignments(
                reviewed, Configuration.from_runnable_config(cfg)
            )
        observed_usage = usage
        artifact["delivery_ids"] = sorted(delivery_ids)

        async def freeze_result():
            billed = await asyncio.to_thread(repo.accounted_usage, unit_id, usage, past)
            if "research_result" in artifact:
                artifact["research_result"]["budget_delta"] = billed
            elif billed:
                patch.setdefault("runtime", {})["budget"] = billed
            if "messages" in patch:
                patch["messages"] = copy_state({"messages": patch["messages"]})[
                    "messages"
                ]
            return artifact

        # DBOS saves the result and its billed usage once. On replay, the
        # permit holder only needs to restore the public settlement projection.
        artifact = await checkpoint("por.stage_result", freeze_result)
        observed_usage = (
            artifact["research_result"]["budget_delta"]
            if "research_result" in artifact
            else artifact["patch"].get("runtime", {}).get("budget", {})
        )
        cancelled = await persist(artifact=artifact, usage=observed_usage)
        if cancelled:
            raise cancelled
        return artifact
    except BaseException as exc:
        original_error = exc
        if (
            control_failure is not None
            and not (await asyncio.to_thread(repo.request, run_id))["cancel_requested"]
        ):
            for field in ("budget_usage", "context_harness", "context_settlement"):
                if hasattr(exc, field):
                    setattr(control_failure, field, getattr(exc, field))
            exc = control_failure
        kind = failure_kind(exc)
        annotate(
            failure_kind=kind,
            status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
        )
        harness = getattr(exc, "context_harness", None)
        settlement = getattr(exc, "context_settlement", None)
        settled = (
            (settlement is not None and settlement.succeeded)
            if harness
            else name in SAFE_STEPS
        )
        settled = settled or not operation_started or operation_returned
        cause = exc
        while cause is not None:
            if getattr(cause, "unknown_outcome", False):
                settled = False
            cause = getattr(cause, "__cause__", None)
        usage = (
            merge_budget_usage(past, exc.budget_usage)
            if hasattr(exc, "budget_usage")
            else observed_usage
        )
        usage = await asyncio.to_thread(repo.accounted_usage, unit_id, usage, past)
        try:
            await persist(
                artifact=artifact,
                usage=usage,
                settled=settled,
                error={
                    "kind": kind,
                    "error_type": type(exc).__name__,
                    "step": name,
                    "usage_complete": settled,
                    "delivery_ids": sorted(delivery_ids),
                },
            )
        except Exception:
            # Database/ownership failure cannot turn an unconfirmed unit into success.
            pass
        if exc is not original_error:
            raise exc from original_error
        raise
    finally:
        watcher.cancel()
        event_delivery.reset(delivery_token)
        if generation_token is not None:
            durable_generation.reset(generation_token)
        physical_calls.reset(calls_token)
        durable_unit.reset(durable_token)
        execution_observer.reset(token)
        identity.close()
        for session in sessions:
            await owned_io(session.close)


@DBOS.workflow(name="por.role")
@dbos_scope
async def role_workflow(run_id, unit_id, role, state):
    return await business_step(
        run_id, unit_id, role + "_agent", role_snapshot(state, role)
    )


@DBOS.workflow(name="por.review")
@dbos_scope
async def review_workflow(run_id, state):
    round_ = state.get("workflow", {}).get("round", 1)
    return await business_step(
        run_id, f"{run_id}:review:{round_}", "research_review", state
    )


@DBOS.workflow(name="por.report")
@dbos_scope
async def report_workflow(run_id, initial):
    state = copy_state(initial)
    request = await load_request(run_id)
    mode = request["data"]["config"]["configurable"].get("report_mode", "synthesis")
    report_steps = (
        (
            "plan_report_sections",
            "section_writer",
            "write_final_sections",
            "compile_final_report",
        )
        if mode == "sections"
        else ("generate_final_report", "compile_final_report")
    )
    for name in report_steps:
        outcome = await business_step(run_id, run_id + ":" + name, name, state)
        apply_patch(state, outcome["patch"])
    return state


@DBOS.step()
async def reserve_step(run_id):
    try:
        return await asyncio.to_thread(facts().reserve, run_id) is not None
    except ExecutionRevoked:
        if await confirm_cancel(facts(), run_id):
            raise asyncio.CancelledError()
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
    # No durable human wait holds a BUSINESS queue slot or resource reservation.
    await reserve(run_id)
    state = copy_state(initial)
    try:
        if revision_request is not None:
            outcome = await business_step(
                run_id,
                DBOS.workflow_id,
                "revise_brief",
                state,
                revision_request=revision_request,
            )
            apply_patch(state, outcome["patch"])
            return {"state": state, "revision_error": outcome["revision_error"]}
        if round_ == 1:
            outcome = await business_step(
                run_id, run_id + ":enrich_query_images", "enrich_query_images", state
            )
            apply_patch(state, outcome["patch"])
        outcome = await business_step(
            run_id, f"{run_id}:plan:{round_}", "plan_research", state
        )
        apply_patch(state, outcome["patch"])
        return {"state": state, "decision": outcome["decision"]}
    finally:
        # business_step can reacquire the reservation during recovery, even if
        # the previous Executor released it just before committing the result.
        _, cancelled = await owned_io(facts().release, run_id)
        if cancelled:
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
        timer = asyncio.timeout(remaining)
        try:
            async with timer:
                await facts().client.wait_first_async([handle])
        except TimeoutError:
            if not timer.expired():
                raise
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
