"""Native DBOS authority: direct business Steps/child Workflows, no old cursor."""

from __future__ import annotations

import asyncio
import hashlib
import time
from copy import deepcopy

from business import Business, gate
from codec import DomainFailure, dumps
from dbos import DBOS, SetEnqueueOptions, SetWorkflowID
from domain import Domain
from env import DB_URL
from neo4j import GraphDatabase

from open_deep_research.web.history import (
    HistoryStage,
    Neo4jHistoryStore,
    history_record,
)
from open_deep_research.workflow.python_state import apply_patch, merge_research_patches
from open_deep_research.workflow.research import followup_assignments

DOMAIN = None


def domain():
    global DOMAIN
    if DOMAIN is None:
        DOMAIN = Domain(DB_URL)
    return DOMAIN


@DBOS.step(
    preemptible=True,
    retries_allowed=True,
    max_attempts=3,
    interval_seconds=0.1,
    should_retry=lambda e: getattr(e, "kind", "") == "TransientRead",
)
async def planning_step(request):
    domain().fact(request["run_id"], "planning", {})
    if (
        request.get("scenario") == "retry"
        and len(domain().facts(request["run_id"], "planning")) == 1
    ):
        raise DomainFailure(
            "Synthetic read failed before contacting model", "TransientRead"
        )
    return await Business(request, domain()).plan()


@DBOS.workflow(name="native_plan")
async def planning_workflow(request):
    return await planning_step(request)


@DBOS.step(preemptible=True)
async def role_step(request, role, state):
    return await Business(request, domain()).role(role, state)


@DBOS.workflow(name="native_role")
async def role_workflow(request, role, state):
    return await role_step(request, role, state)


@DBOS.step(preemptible=True)
async def review_step(request, state):
    return await Business(request, domain()).review(state)


@DBOS.step(preemptible=True)
async def report_step(request, state):
    return await Business(request, domain()).report(state)


@DBOS.step(preemptible=True)
async def checkpoint_gate(request):
    await gate(request, "producer_complete")


@DBOS.step()
async def approval_deadline(seconds):
    from sqlalchemy import text

    with domain().engine.connect() as db:
        return (
            float(
                db.execute(
                    text("SELECT extract(epoch from clock_timestamp())")
                ).scalar_one()
            )
            + seconds
        )


async def emit(kind, **value):
    await DBOS.write_stream_async("events", {"type": kind, **value})


@DBOS.workflow(name="native_research")
async def research_workflow(request, state):
    state = deepcopy(state)
    packets = {role: state for role in ("public_signal", "internal_knowledge")}
    while packets:
        handles = {}
        for role, packet in packets.items():
            with SetWorkflowID(
                f"{request['run_id']}:{role}:{packet['workflow']['round']}"
            ):
                handles[role] = await DBOS.start_workflow_async(
                    role_workflow, request, role, packet
                )
        patches = {}
        remaining = dict(handles)
        while remaining:
            finished = await DBOS.wait_first_async(list(remaining.values()))
            role = next(
                role
                for role, h in remaining.items()
                if h.workflow_id == finished.workflow_id
            )
            try:
                patches[role] = await finished.get_result()
            except BaseException:
                for other in remaining.values():
                    if other.workflow_id != finished.workflow_id:
                        await DBOS.cancel_workflow_async(
                            other.workflow_id, cancel_children=True
                        )
                await settlement_barrier(
                    request, [h.workflow_id for h in remaining.values()]
                )
                raise
            del remaining[role]
        merge_research_patches(state, patches)
        await checkpoint_gate(request)
        apply_patch(state, await review_step(request, state))
        packets = followup_assignments(
            state, {"configurable": {"max_research_rounds": 3}}
        )
    for role in ("risk_assessment", "response_strategy"):
        with SetWorkflowID(f"{request['run_id']}:{role}:{state['workflow']['round']}"):
            handle = await DBOS.start_workflow_async(
                role_workflow, request, role, state
            )
        apply_patch(state, await handle.get_result())
    apply_patch(state, await report_step(request, state))
    return state


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=0.2, backoff_rate=1)
async def archive_step(request, state):
    await gate(request, "archive_wait")
    if request.get("scenario") == "archive_fail":
        raise DomainFailure("Synthetic archive outage", "ArchiveUnavailable")
    record = history_record(
        run_id=request["run_id"],
        request={"topic": request["topic"]},
        report=state["report"]["final"],
        usage=state["runtime"]["budget"],
        started_at="2026-10-22T00:00:00+00:00",
        source="dbos_native",
        role_reports=[
            HistoryStage(role=r, content=a["report"])
            for r, a in state["agents"].items()
            if a.get("report")
        ],
    )
    record.owner = request["owner"]
    store = Neo4jHistoryStore(
        GraphDatabase.driver(
            "bolt://127.0.0.1:17688", auth=("neo4j", "native-poc-password")
        )
    )
    try:
        store.save([record])
    finally:
        store.close()
    return record.id


@DBOS.workflow(name="native_control")
async def control_workflow(request):
    await emit("planning", run_id=request["run_id"])
    with (
        SetWorkflowID(request["run_id"] + ":plan"),
        SetEnqueueOptions(queue_partition_key=request["owner"]),
    ):
        handle = await DBOS.enqueue_workflow_async(
            "business", planning_workflow, request
        )
    state = await handle.get_result()
    version = hashlib.sha256(
        dumps(
            {
                "brief": state["workflow"]["brief"],
                "sections": state["report"]["sections"],
            }
        ).encode()
    ).hexdigest()
    deadline = await approval_deadline(request.get("approval_seconds", 120))
    plan = {
        "version": version,
        "owner": request["owner"],
        "expires_at": deadline,
        "brief": state["workflow"]["brief"],
        "sections": state["report"]["sections"],
    }
    await DBOS.set_event_async("plan", plan)
    if request.get("approval"):
        await DBOS.set_event_async("phase", "awaiting_approval")
        await emit("awaiting_approval", plan_version=version)
        decision = await DBOS.recv_async(
            "approval", timeout_seconds=request.get("approval_seconds", 120)
        )
        if decision is None:
            raise DomainFailure("Plan approval expired", "ApprovalExpired")
        if decision != {
            "version": version,
            "owner": request["owner"],
            "decision": "approve",
        }:
            raise DomainFailure("Plan rejected or invalid decision", "ApprovalRejected")
    await DBOS.set_event_async("phase", "research")
    await emit("research_started")
    with (
        SetWorkflowID(request["run_id"] + ":research"),
        SetEnqueueOptions(queue_partition_key=request["owner"]),
    ):
        handle = await DBOS.enqueue_workflow_async(
            "business", research_workflow, request, state
        )
    result = await handle.get_result()
    report = {
        "report": result["report"]["final"],
        "budget": result["runtime"]["budget"],
        "run_id": request["run_id"],
        "round": result["workflow"]["round"],
    }
    await DBOS.set_event_async("report", report)
    await emit("report", **report)
    history = await archive_step(request, result)
    await DBOS.set_event_async("phase", "done")
    await emit("history_saved", id=history)
    await DBOS.close_stream_async("events")
    return {**report, "history_id": history}


@DBOS.step(preemptible=True)
async def queue_probe_step(request):
    domain().fact(
        request["run_id"], "queue_enter", {"at": time.time(), "owner": request["owner"]}
    )
    await gate(request, "queue_wait")
    domain().fact(request["run_id"], "queue_exit", {"at": time.time()})
    return request["run_id"]


@DBOS.workflow(name="native_queue_probe")
async def queue_probe(request):
    return await queue_probe_step(request)


@DBOS.step()
async def ownership_step(request):
    key = request["run_id"] + ":permit"
    permit, artifact = domain().acquire(key, allow_incomplete=True)
    if artifact is not None:
        return artifact
    await gate(request, request.get("gate", "zombie_wait"))
    try:
        domain().check(key, permit)
        op = domain().intent(
            key,
            permit,
            {"name": "synthetic-effect", "tool_call_id": "once", "arguments": "{}"},
        )
        value = domain().effect(key, op, "one authorized effect")
        domain().receipt(
            key,
            permit,
            {"name": "synthetic-effect", "tool_call_id": "once", "arguments": "{}"},
            "returned",
        )
        domain().artifact(key, permit, {"value": value, "executor": DBOS.executor_id})
        return {"value": value, "executor": DBOS.executor_id}
    except Exception as exc:
        if getattr(exc, "kind", "") == "StaleExecution":
            domain().fact(key, "stale_denied", {})
        raise


@DBOS.workflow(name="native_ownership_probe")
async def ownership_probe(request):
    return await ownership_step(request)


@DBOS.step()
async def settlement_barrier(request, roles):
    for _ in range(330):
        waiting = []
        for role in roles:
            prefix = role
            if (
                domain().facts(prefix, "role_enter")
                and not domain().facts(prefix, "harness")
                and not domain().facts(prefix, "interrupted")
            ):
                waiting.append(role)
        if not waiting:
            return
        await asyncio.sleep(0.1)
    raise DomainFailure("SDK cleanup remains unresolved", "UnresolvedSettlement")
