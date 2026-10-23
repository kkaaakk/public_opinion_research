"""Native durable business orchestration. SDK Runner owns every Agent loop."""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from functools import partial

from agents import SQLiteSession
from dbos import DBOS, SetEnqueueOptions, SetWorkflowID, SetWorkflowTimeout
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.budget import merge_budget_usage
from open_deep_research.configuration import Configuration
from open_deep_research.web.run_data import (
    RunConflict,
    digest,
    public_payload,
    timestamp,
)
from open_deep_research.workflow import intake, reporting, research
from open_deep_research.workflow.business_steps import SAFE_STEPS, _step, failure_kind
from open_deep_research.workflow.dbos_config import (
    BUSINESS,
    FACT_WRITE_GRACE_SECONDS,
    SETTLEMENT_GRACE_SECONDS,
)
from open_deep_research.workflow.execution import (
    ExecutionControlError,
    execution_observer,
    owned_io,
)
from open_deep_research.workflow.python_state import (
    RESEARCH_ROLES,
    apply_patch,
    copy_state,
    merge_research_patches,
    role_snapshot,
)
from open_deep_research.workflow.run_facts import facts
from open_deep_research.workflow.state_utils import agents_for_new_run

OPERATIONS = {
    "enrich_query_images": intake.enrich_query_images,
    "clarify_with_user": intake.clarification_step,
    "write_research_brief": intake.research_brief_step,
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


@DBOS.step(preemptible=True)
async def business_step(run_id, unit_id, name, state):
    repo = facts()
    req = await asyncio.to_thread(repo.request, run_id)
    repo.validate(req)
    while not await asyncio.to_thread(repo.reserve, run_id):
        await asyncio.sleep(0.5)
    permit, artifact, past = await asyncio.to_thread(
        repo.acquire, run_id, unit_id, name, safe=name in SAFE_STEPS
    )
    if artifact is not None:
        return artifact
    cfg = deepcopy(req["data"]["config"])
    cfg["configurable"]["allow_plan_feedback"] = False
    sessions = []

    async def observe_business(kind, **payload):
        if kind == "effect_guard":
            return repo.effect_guard(unit_id, permit)
        if kind in {"tool_started", "tool_finished"}:
            result, cancelled = await owned_io(
                repo.operation,
                unit_id,
                permit,
                payload["identity"],
                status="started" if kind == "tool_started" else payload["status"],
            )
            if kind == "tool_started" and cancelled:
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
            allow_cancel=kind in {"model_completed", "agent_completed"},
        )
        # Never persist raw prompt/tool arguments/output/exception text publicly.
        allowed = {
            k: v
            for k, v in payload.items()
            if k in {"role", "name", "model_calls", "invocation_id"}
        }

        def publish():
            with repo.engine.begin() as db:
                repo.publish(
                    db,
                    run_id,
                    "sdk_event",
                    event="agent_started" if kind == "sdk_session" else kind,
                    **allowed,
                )

        _, cancelled = await owned_io(publish)
        if cancelled:
            raise cancelled
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
        try:
            return await observe_business(kind, **payload)
        except (RunConflict, SQLAlchemyError) as exc:
            raise ExecutionControlError(
                "Durable execution permission/storage unavailable"
            ) from exc

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

    observed_usage = past
    token = execution_observer.set(observer)
    try:

        def started():
            with repo.engine.begin() as db:
                repo.publish(db, run_id, "step_started", step=name)
                repo.publish(
                    db, run_id, "status", message=f"Running {name}", granularity="stage"
                )

        await asyncio.to_thread(started)
        operation = OPERATIONS.get(name) or getattr(research, name)
        result = await _step(
            name,
            operation,
            state,
            cfg,
            Configuration.from_runnable_config(cfg).workflow_step_timeout_seconds,
        )
        if hasattr(result, "goto"):
            artifact = {"patch": result.update or {}, "goto": result.goto}
        else:
            artifact = {"patch": result}
        patch = artifact["patch"]
        if "messages" in patch:
            # Message UUID generation belongs to this persisted Step, not replay.
            patch["messages"] = copy_state({"messages": patch["messages"]})["messages"]
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
        cancelled = await persist(artifact=artifact, usage=usage)
        if cancelled:
            raise cancelled
        return artifact
    except BaseException as exc:
        harness = getattr(exc, "context_harness", None)
        settlement = getattr(exc, "context_settlement", None)
        settled = (
            (settlement is not None and settlement.succeeded)
            if harness
            else name in SAFE_STEPS
        )
        usage = (
            merge_budget_usage(past, exc.budget_usage)
            if hasattr(exc, "budget_usage")
            else observed_usage
        )
        try:
            await persist(
                usage=usage,
                settled=settled,
                error={
                    "kind": failure_kind(exc),
                    "step": name,
                    "usage_complete": settled,
                },
            )
        except Exception:
            # Database/ownership failure cannot turn an unconfirmed unit into success.
            pass
        raise
    finally:
        execution_observer.reset(token)
        for session in sessions:
            await owned_io(session.close)


@DBOS.workflow(name="por.role")
async def role_workflow(run_id, unit_id, role, state):
    return await business_step(
        run_id, unit_id, role + "_agent", role_snapshot(state, role)
    )


@DBOS.step()
async def reserve_step(run_id):
    req = await asyncio.to_thread(facts().request, run_id)
    facts().validate(req)
    return await asyncio.to_thread(facts().reserve, run_id)


@DBOS.step()
async def release_step(run_id):
    await asyncio.to_thread(facts().release, run_id)


async def reserve(run_id):
    while not await reserve_step(run_id):
        await DBOS.sleep_async(1)


@DBOS.workflow(name="por.plan")
async def planning_workflow(run_id, initial):
    await reserve(run_id)
    state = copy_state(initial)
    try:
        for name in (
            "enrich_query_images",
            "clarify_with_user",
            "write_research_brief",
            "plan_report_sections",
        ):
            outcome = await business_step(run_id, run_id + ":" + name, name, state)
            apply_patch(state, outcome["patch"])
            if outcome.get("goto") == "__end__":
                return {"state": state, "end": True}
            if (
                name == "write_research_brief"
                and outcome.get("goto") != "plan_report_sections"
            ):
                break
        return {"state": state, "end": False}
    finally:
        await release_step(run_id)


@DBOS.step()
async def cleanup_children(run_id):
    # DBOS terminal status can precede SDK's bounded drain. This is domain settlement.
    for _ in range(int(SETTLEMENT_GRACE_SECONDS * 10)):
        units = await asyncio.to_thread(facts().units, run_id)
        if all(u["settled"] is not None for u in units):
            return
        await asyncio.sleep(0.1)
    await asyncio.to_thread(facts().quarantine_pending, run_id)


@DBOS.workflow(name="por.research")
async def research_workflow(run_id, initial):
    await reserve(run_id)
    state = copy_state(initial)
    apply_patch(
        state,
        {
            "workflow": {
                "type": "override",
                "value": {
                    "brief": state.get("workflow", {}).get("brief", ""),
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
            patches = {}
            try:
                while remaining:
                    completed = await DBOS.wait_first_async(list(remaining.values()))
                    role = next(
                        k
                        for k, h in remaining.items()
                        if h.workflow_id == completed.workflow_id
                    )
                    patches[role] = (await completed.get_result())["patch"]
                    del remaining[role]
            except BaseException:
                for handle in remaining.values():
                    await DBOS.cancel_workflow_async(
                        handle.workflow_id, cancel_children=True
                    )
                await cleanup_children(run_id)
                raise
            merge_research_patches(state, patches)
            round_ = state["workflow"]["round"]
            reviewed = await business_step(
                run_id, f"{run_id}:review:{round_}", "research_review", state
            )
            apply_patch(state, reviewed["patch"])
            packets = reviewed["followup"]
            if packets:
                await send_event(run_id, "research_followup", round=round_ + 1)
        for role in ("risk_assessment", "response_strategy"):
            unit = run_id + ":" + role
            with SetWorkflowID(unit):
                handle = await DBOS.start_workflow_async(
                    role_workflow, run_id, unit, role, state
                )
            apply_patch(state, (await handle.get_result())["patch"])
        for name in ("section_writer", "write_final_sections", "compile_final_report"):
            apply_patch(
                state,
                (await business_step(run_id, run_id + ":" + name, name, state))[
                    "patch"
                ],
            )
        return state
    finally:
        await release_step(run_id)


@DBOS.step()
async def load_request(run_id):
    req = await asyncio.to_thread(facts().request, run_id)
    facts().validate(req)
    return req


@DBOS.step()
async def approval_deadline():
    with facts().engine.connect() as db:
        return facts().now(db) + facts().settings.approval_seconds


@DBOS.workflow(name="por.run")
async def run_workflow(run_id):
    req = await load_request(run_id)
    await send_event(run_id, "run_status", status="running")
    with (
        SetWorkflowID(run_id + ":planning"),
        SetEnqueueOptions(queue_partition_key=req["owner"]),
        SetWorkflowTimeout(req["data"]["run_seconds"]),
    ):
        handle = await DBOS.enqueue_workflow_async(
            BUSINESS, planning_workflow, run_id, req["data"]["initial"]
        )
    planned = await handle.get_result()
    state = planned["state"]
    if not planned["end"]:
        plan = {
            "brief": state.get("workflow", {}).get("brief", ""),
            "sections": state.get("report", {}).get("sections", []),
        }
        plan.update(version=digest(plan), expires_at=await approval_deadline())
        await DBOS.set_event_async("plan", plan)
        if req["data"]["config"]["configurable"]["allow_plan_feedback"]:
            await DBOS.set_event_async("phase", "awaiting_approval")
            await send_event(
                run_id,
                "run_status",
                status="awaiting_approval",
                plan_version=plan["version"],
            )
            decision = await DBOS.recv_async(
                "approval", timeout_seconds=req["data"]["approval_seconds"]
            )
            if decision is None or decision != {
                "owner": req["owner"],
                "version": plan["version"],
                "decision": "approve",
            }:
                await DBOS.set_event_async(
                    "approval_end", "expired" if decision is None else "reject"
                )
                await send_event(
                    run_id,
                    "approval_ended",
                    decision="expired" if decision is None else "reject",
                )
                return {"approval": "expired" if decision is None else "reject"}
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
        state = await handle.get_result()
    return state


@DBOS.workflow(name="por.events")
async def events_workflow(run_id):
    cursor = 0
    generation = 0
    while True:
        event = await DBOS.recv_async("events", timeout_seconds=86400)
        if event is None:
            continue
        if "generation" in event and event["generation"] < generation:
            continue
        if event["type"] == "retry_started":
            generation = event["generation"]
        if event["type"] == "history_saved" and event.get("generation", 0) < generation:
            continue
        cursor += 1
        event = dict(
            event, protocol_version=1, run_id=run_id, attempt_id=run_id, seq=cursor
        )
        event = await public_event(event)
        await DBOS.write_stream_async("events", event)
        await DBOS.set_event_async("cursor", cursor)
        if event["type"] == "done":
            await DBOS.set_event_async(
                "done", {"generation": event.get("generation", 0)}
            )
        if event["type"] == "run_status":
            await DBOS.set_event_async("phase", event["status"])
        if event["type"] == "history_saved":
            # Preserve the old archive receipt followed by a final done event.
            cursor += 1
            done = await public_event(
                dict(event, type="done", seq=cursor, archive_status="saved")
            )
            await DBOS.write_stream_async("events", done)
            await DBOS.set_event_async("cursor", cursor)
            await DBOS.set_event_async(
                "done", {"generation": event.get("generation", 0)}
            )
            await DBOS.set_event_async(
                "history", {"id": event["id"], "generation": event.get("generation", 0)}
            )
            if event["status"] in {"completed", "cancelled"}:
                await DBOS.close_stream_async("events")
                return


@DBOS.step(retries_allowed=True, max_attempts=10, interval_seconds=0.5, backoff_rate=1)
async def public_event(event):
    with facts().engine.connect() as db:
        return public_payload(dict(event, timestamp=timestamp(facts().now(db))))


@DBOS.step()
async def completion_facts(run_id, cleanup_expired=False):
    from open_deep_research.web.durable import RunClient

    client = RunClient(facts())
    result = await asyncio.to_thread(client.get, run_id, None)
    if result["status"] == "awaiting_approval" and result["plan"]:
        if await asyncio.to_thread(facts().expire_approval, run_id, result["plan"]):
            await asyncio.to_thread(
                facts().client.cancel_workflow, run_id, cancel_children=True
            )
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
async def retry_execution(run_id):
    from open_deep_research.web.durable import RunClient

    await asyncio.to_thread(RunClient(facts())._rewind_safe, run_id)
    return True


@DBOS.workflow(name="por.finalize")
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
                        while not await retry_execution(run_id):
                            await DBOS.sleep_async(1)
                        applied_generation = command["generation"]
                        await DBOS.set_event_async("retry_applied", applied_generation)
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
        )
        retry = None
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
                retry = await DBOS.recv_async("retry", timeout_seconds=30)
                if retry:
                    break
        if not result["retry_safe"]:
            return
        while retry is None:
            retry = await DBOS.recv_async("retry", timeout_seconds=86400)
        while not await retry_execution(run_id):
            await DBOS.sleep_async(1)
        applied_generation = retry["generation"]
        await DBOS.set_event_async("retry_applied", applied_generation)
