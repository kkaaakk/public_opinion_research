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

from open_deep_research.budget import merge_budget_usage
from open_deep_research.configuration import Configuration
from open_deep_research.observability.telemetry import (
    annotate,
    correlation,
    dbos_scope,
    telemetry_enabled,
)
from open_deep_research.web.run_data import (
    TERMINAL,
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
    "plan_research": intake.plan_research,
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


@DBOS.step(preemptible=True)
@dbos_scope
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
    delivery_ids = set()

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
            annotate(
                operation_id=result,
                effect_status="started"
                if kind == "tool_started"
                else payload["status"],
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

        event_id, cancelled = await owned_io(publish)
        delivery_ids.add(event_id)
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
    try:
        if telemetry_enabled():
            # Read existing accepted generation as RunClient.get does. Keep this
            # cancellable telemetry read inside the original settlement boundary.
            def read_generation():
                from sqlalchemy import text

                with repo.engine.connect() as db:
                    return db.execute(
                        text(
                            "SELECT coalesce(max(generation),0) FROM por_domain.retries WHERE run_id=:id"
                        ),
                        {"id": run_id},
                    ).scalar_one()

            try:
                generation = await asyncio.to_thread(read_generation)
            except Exception:
                pass  # No generation claim on a failed telemetry-only read.
            else:
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

        await asyncio.to_thread(started)
        operation = OPERATIONS.get(name) or getattr(research, name)
        result = await _step(
            name,
            operation,
            state,
            cfg,
            Configuration.from_runnable_config(cfg).workflow_step_timeout_seconds,
        )
        if name == "plan_research":
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
        artifact["delivery_ids"] = sorted(delivery_ids)
        cancelled = await persist(artifact=artifact, usage=usage)
        if cancelled:
            raise cancelled
        return artifact
    except BaseException as exc:
        annotate(
            failure_kind=failure_kind(exc),
            status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
        )
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
                    "delivery_ids": sorted(delivery_ids),
                },
            )
        except Exception:
            # Database/ownership failure cannot turn an unconfirmed unit into success.
            pass
        raise
    finally:
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
@dbos_scope
async def planning_workflow(run_id, initial, round_=1):
    # No durable human wait holds a BUSINESS queue slot or resource reservation.
    await reserve(run_id)
    state = copy_state(initial)
    try:
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
        request = await load_request(run_id)
        report_mode = request["data"]["config"]["configurable"].get(
            "report_mode", "synthesis"
        )
        if report_mode == "sections":
            # Plan from the frozen, approved specification, never the earlier draft.
            apply_patch(
                state,
                (
                    await business_step(
                        run_id,
                        run_id + ":plan_report_sections",
                        "plan_report_sections",
                        state,
                    )
                )["patch"],
            )
            report_steps = (
                "section_writer",
                "write_final_sections",
                "compile_final_report",
            )
        else:
            report_steps = ("generate_final_report", "compile_final_report")
        for name in report_steps:
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
@dbos_scope
async def run_workflow(run_id):
    from langchain_core.messages import HumanMessage

    from open_deep_research.workflow.brief import brief_metadata, validate_brief

    req = await load_request(run_id)
    state = req["data"]["initial"]
    for round_ in (1, 2):
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
        if round_ == 2:
            raise ValueError(
                "Planning cannot ask again after clarification was resolved"
            )
        challenge = dict(
            questions=decision["questions"], expires_at=await approval_deadline()
        )
        challenge["version"] = digest(dict(run_id=run_id, **challenge))
        await DBOS.set_event_async("clarification", challenge)
        await DBOS.set_event_async("phase", "awaiting_clarification")
        await send_event(run_id, "run_status", status="awaiting_clarification")
        reply = await DBOS.recv_async(
            "clarification", timeout_seconds=req["data"]["approval_seconds"]
        )
        if reply is None:
            await DBOS.set_event_async("approval_end", "expired")
            await send_event(run_id, "clarification_ended", decision="expired")
            return {"clarification": "expired"}
        if (
            reply.get("owner") != req["owner"]
            or reply.get("version") != challenge["version"]
        ):
            raise ValueError(
                "Clarification continuation does not match the pending request"
            )
        await DBOS.set_event_async("clarification_resolved", challenge["version"])
        # Structured control was validated by admission. It is task data, never system instructions.
        apply_patch(
            state,
            {
                "messages": [
                    HumanMessage(
                        id="clarification-" + challenge["version"],
                        content=(
                            "Clarification questions: "
                            + dumps(challenge["questions"])
                            + "\nUser response: "
                            + (
                                "Skipped. Continue with existing information; do not invent unspecified details."
                                if reply["skip"]
                                else dumps(reply["answers"])
                            )
                        )
                    )
                ],
                "workflow": {"clarification_complete": True},
            },
        )

    brief = validate_brief(state["workflow"]["brief"])
    plan = dict(
        brief=brief,
        sections=[],
        expires_at=await approval_deadline(),
        **brief_metadata(brief, 1, run_id),
    )
    while True:
        await DBOS.set_event_async("plan", plan)
        if not req["data"]["config"]["configurable"]["allow_plan_feedback"]:
            break
        await DBOS.set_event_async("phase", "awaiting_approval")
        await send_event(
            run_id,
            "run_status",
            status="awaiting_approval",
            plan_version=plan["version"],
        )
        # Edits retain the original deadline; editing cannot extend an approval indefinitely.
        command = await DBOS.recv_async(
            "approval",
            timeout_seconds=max(0, plan["expires_at"] - await interaction_clock()),
        )
        if command is None or command.get("decision") == "reject":
            reason = "expired" if command is None else "reject"
            await DBOS.set_event_async("approval_end", reason)
            await send_event(run_id, "approval_ended", decision=reason)
            return {"approval": reason}
        if (
            command.get("owner") != req["owner"]
            or command.get("version") != plan["version"]
        ):
            raise ValueError(
                "Approval continuation does not match the current Brief version"
            )
        if command["decision"] == "edit":
            brief = validate_brief(command["brief_md"])
            plan = dict(
                brief=brief,
                sections=[],
                expires_at=plan["expires_at"],
                **brief_metadata(brief, plan["revision"] + 1, run_id),
            )
            continue
        if command["decision"] != "approve":
            raise ValueError("Unknown approval decision")
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


@DBOS.step()
async def interaction_clock():
    with facts().engine.connect() as db:
        return facts().now(db)


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
