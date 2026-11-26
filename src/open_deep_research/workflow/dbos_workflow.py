"""Native durable business orchestration. SDK Runner owns every Agent loop.

A Workflow moves business control flow, a Step owns one external or
non-deterministic result, a Datasource transaction owns one business database
commit, DBOSRunner owns the two Agent loops, and every other function is plain
Python that recomputes deterministically on replay.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy

from dbos import DBOS, SetEnqueueOptions, SetWorkflowID, SetWorkflowTimeout
from dbos import error as dbos_error

from open_deep_research.configuration import Configuration
from open_deep_research.llm.generation import ModelOutputError
from open_deep_research.observability.telemetry import (
    annotate,
    business_span,
    correlation,
    dbos_scope,
)
from open_deep_research.state import ResearchRoleResult
from open_deep_research.time_utils import get_today_str
from open_deep_research.web.run_data import (
    RunConflict,
    db_time,
    digest,
    dumps,
    public_payload,
    read_request,
    retry_generation,
    stage_budget,
    timestamp,
)
from open_deep_research.workflow import intake, reporting, research
from open_deep_research.workflow.dbos_config import (
    ACTIVE,
    BUSINESS,
    CONTROL,
    RESUMABLE_VERSIONS,
    VERSION,
    domain_engine,
    native_client,
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
from open_deep_research.workflow.transactions import commit_stage


def _interaction_now():
    with domain_engine().connect() as db:
        return db_time(db)


async def send_event(run_id, event_type, **data):
    # A replayed send retains its original message, generation and timestamp.
    if "generation" not in data:
        data["generation"] = await asyncio.to_thread(run_generation, run_id)
    if "observed_at" not in data:
        data["observed_at"] = timestamp(await asyncio.to_thread(_interaction_now))
    await DBOS.send_async(
        run_id + ":events", dict(type=event_type, **data), topic="events"
    )


def run_generation(run_id):
    with domain_engine().connect() as db:
        return retry_generation(db, run_id)


def stage_scope():
    """Return the stage identity of the code currently running in a Workflow."""
    scope = stage_context.get()
    if scope is None:
        raise RuntimeError("No active stage scope in this Workflow")
    return scope


async def publish_stage(scope, result):
    """Commit public business output, then publish the native facts reading it.

    The generated report is exposed before its publication is attempted, so a
    failed or retried stage transaction never makes an existing report
    unreadable. The completed-stage index stays a native event: it describes
    delivery progress, never the business outcome itself.
    """
    if report := result.get("patch", {}).get("report", {}).get("final"):
        await DBOS.set_event_async("report", report)
    await asyncio.to_thread(commit_stage, scope, "completed", result=result)
    if scope.name.endswith("_agent"):
        # Role and analysis reports have their own native result.
        return
    steps = await DBOS.get_event_async(DBOS.workflow_id, "completed_stages", 0) or {}
    await DBOS.set_event_async(
        "completed_stages", steps | {scope.name: stage_budget(result)}
    )


@asynccontextmanager
async def observe_stage(
    run_id,
    name,
    state,
    config,
    *,
    stage_id=None,
    reserve_report=True,
    expires_at=None,
):
    """Publish stage progress and apply the existing business deadline."""
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
        round_,
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
                await asyncio.to_thread(commit_stage, scope, "running")
                async with stage_timeout(scope.config.workflow_step_timeout_seconds):
                    yield config
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
                    unknown_outcome=unknown,
                )
                if isinstance(exc, ModelOutputError):
                    error["invalid_model_output"] = True
                # A cancelled Workflow cannot allocate another Step, but the
                # transaction must still finish before the cancellation lands.
                await owned_io(commit_stage, scope, "failed", error=error)
                if not is_cancellation(exc):
                    await DBOS.set_event_async("failure", error)
                raise
    finally:
        stage_context.reset(token)


def business_config(request):
    config = deepcopy(request["data"]["config"])
    config["configurable"]["allow_plan_feedback"] = False
    config["initial_budget"] = (
        request["data"]["initial"].get("runtime", {}).get("budget", {})
    )
    # The Run freezes one research date when it is admitted; no stage re-reads
    # the clock for it. A Run admitted before the freeze falls back rather than
    # failing to drain.
    config["research_date"] = request["data"].get("research_date") or get_today_str()
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
        await publish_stage(stage_scope(), artifact)
        return artifact


@DBOS.workflow(name="por.plan")
@dbos_scope
async def planning_workflow(run_id, initial, round_=1, *, revision_request=None):
    source = f"{run_id}:plan:{round_}"
    state = copy_state(initial, source=source)
    request = await load_request(run_id)
    config = business_config(request)
    if revision_request is not None:
        async with observe_stage(
            run_id,
            "revise_brief",
            state,
            config,
            expires_at=revision_request["expires_at"],
        ) as cfg:
            revised = await intake.revise_brief(
                state, cfg, feedback=revision_request["feedback"]
            )
            await publish_stage(stage_scope(), revised)
            apply_patch(state, revised["patch"], source=source)
            return {"state": state, "revision_error": revised["revision_error"]}
    if round_ == 1:
        has_uploads = bool(request["data"]["request"].get("images"))
        async with observe_stage(
            run_id,
            "enrich_query_images",
            state,
            config,
            stage_id=run_id + ":enrich_query_images",
        ) as cfg:
            image_state = (
                {**state, "messages": [await uploaded_query_message(run_id)]}
                if has_uploads else state
            )
            patch = await intake.enrich_query_images(image_state, cfg)
            await publish_stage(stage_scope(), {"patch": patch})
            if has_uploads and patch.get("messages"):
                await DBOS.set_event_async(
                    "query_image_context", patch["messages"][0].content
                )
            apply_patch(state, patch, source=f"{run_id}:enrich_query_images")
    async with observe_stage(
        run_id,
        "plan_research",
        state,
        config,
        stage_id=f"{run_id}:plan:{round_}",
    ) as cfg:
        planned = await intake.plan_research(state, cfg)
        await publish_stage(stage_scope(), planned)
        apply_patch(state, planned["patch"], source=source)
        return {"state": state, "decision": planned["decision"]}


async def uploaded_query_message(run_id):
    """Read the admitted image bytes for the intake stage without journaling them."""
    from base64 import b64encode

    from langchain_core.messages import HumanMessage

    from open_deep_research.web.run_data import read_request_images

    images = await asyncio.to_thread(read_request_images, run_id)
    if not images:
        raise RuntimeError("Accepted query images are unavailable")
    return HumanMessage(
        content=[
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64encode(content).decode('ascii')}"},
            }
            for mime, content in images
        ],
    )


async def review_round(run_id, state, config, round_):
    """Review one evidence round inside ``por.research``.

    Review is one bounded model call and plain follow-up logic, not a Workflow:
    it reads the role reports the parallel stages already committed, and its
    result becomes the next round's packets.
    """
    source = f"{run_id}:review:{round_}"
    async with observe_stage(
        run_id, "research_review", state, config, stage_id=source
    ) as cfg:
        with business_span("research_review"):
            patch = await research.research_review(state, cfg)
        await publish_stage(stage_scope(), {"patch": patch})
    apply_patch(state, patch, source=source)
    return research.followup_assignments(
        state, Configuration.from_runnable_config(cfg)
    )


@DBOS.workflow(name="por.research")
@dbos_scope
async def research_workflow(run_id, initial):
    source = f"{run_id}:research"
    state = copy_state(initial, source=source)
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
            "agents": {"type": "override", "value": {}},
        },
        source=source,
    )
    config = business_config(await load_request(run_id))
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
        failures = {}
        try:
            while remaining:
                completed = await DBOS.wait_first_async(list(remaining.values()))
                role = next(
                    k
                    for k, h in remaining.items()
                    if h.workflow_id == completed.workflow_id
                )
                del remaining[role]
                try:
                    results[role] = read_research_result(
                        await completed.get_result(), role, packets[role]
                    )
                except Exception as exc:
                    # Leave the other producer running. It can finish useful
                    # work, and its native result can survive a parent rewind.
                    failures[role] = exc
        except BaseException:
            for handle in remaining.values():
                await DBOS.cancel_workflow_async(
                    handle.workflow_id, cancel_children=True
                )
            raise
        if failures:
            raise next(failures[role] for role in RESEARCH_ROLES if role in failures)
        # Only committed role results are joined, so a failed review never
        # repeats research an Agent already finished.
        assemble_research_results(state, results, packets)
        round_ = state["workflow"]["round"]
        packets = await review_round(run_id, state, config, round_)
        if packets:
            await send_event(
                run_id,
                "research_followup",
                round=round_ + 1,
                roles=list(packets),
            )
    for role, analyze in (
        ("risk_assessment", research.risk_assessment),
        ("response_strategy", research.response_strategy),
    ):
        # Keep the public stage ID and the progress/report contract used by Studio
        # and History. The model call itself is checkpointed by por.llm.
        event = f"analysis:{role}"
        await DBOS.set_event_async(event, {"running": True})
        async with observe_stage(
            run_id, role + "_agent", state, config, stage_id=f"{run_id}:{role}"
        ) as cfg:
            with business_span(role):
                patch = await analyze(state, cfg)
            artifact = {"patch": patch}
            if not patch:
                artifact["observation"] = {"skipped": True, "reason": "role_disabled"}
            await publish_stage(stage_scope(), artifact)
            await DBOS.set_event_async(event, artifact)
            apply_patch(state, patch, source=f"{run_id}:{role}")
    async with observe_stage(
        run_id, "generate_final_report", state, config, reserve_report=False
    ) as cfg:
        await DBOS.set_event_async("report_started", True)
        with business_span("report_generation", step="generate_final_report"):
            patch = await reporting.generate_final_report(state, cfg)
        await publish_stage(stage_scope(), {"patch": patch})
        apply_patch(state, patch, source=f"{run_id}:report")
    return state


@DBOS.step(name="por.load_request")
async def load_request(run_id):
    from open_deep_research.web.durable import validate_request

    req = await asyncio.to_thread(read_request, run_id)
    validate_request(req)
    return req


@DBOS.step(name="por.clock")
async def clock():
    """Return the database clock for every durable interaction deadline."""
    return await asyncio.to_thread(_interaction_now)


async def approval_deadline(request):
    """Extend the frozen approval window from one committed clock reading."""
    return await clock() + request["data"]["approval_seconds"]


async def receive_interaction(topic, owner, interaction):
    deadline = interaction["expires_at"]
    while True:
        # recv replays its recorded timeout. Use the live clock if the Executor
        # stopped before arming this wait, including after discarding a message.
        remaining = deadline - await asyncio.to_thread(_interaction_now)
        command = await DBOS.recv_async(topic, timeout_seconds=max(0, remaining))
        if command is None or await clock() >= deadline:
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
            expires_at=await approval_deadline(req),
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
            source=f"{run_id}:clarification:{round_}",
        )

    brief = validate_brief(state["workflow"]["brief"])
    plan = dict(
        brief=brief,
        expires_at=await approval_deadline(req),
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
            apply_patch(
                state,
                {"workflow": {"brief": plan["brief"]}},
                source=f"{run_id}:approval:{plan['version']}",
            )
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
            if revised is None or await clock() >= plan["expires_at"]:
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
        source=f"{run_id}:brief",
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


def public_event(event, fallback_at):
    """Shape one relayed message for egress without reading the clock again."""
    return public_payload(dict(event, timestamp=event.get("observed_at") or fallback_at))


@DBOS.workflow(name="por.events")
@dbos_scope
async def events_workflow(run_id):
    """Merge native mailboxes into one append-only Stream for all readers."""
    delivery = {"offset": 0, "generation": 0}
    terminal = None
    # Producers stamp observed_at when they send. The relay never asks the
    # clock itself: a fallback is resolved once, and only a legacy message
    # that predates the stamp can ever reach it.
    fallback_at = timestamp(await asyncio.to_thread(_interaction_now))

    async def append(event):
        # DBOS assigns the native offset. This single writer counts its
        # checkpointed appends so GET can expose a delivered-prefix watermark.
        await DBOS.write_stream_async("events", public_event(event, fallback_at))
        delivery["offset"] += 1

    while True:
        event = await DBOS.recv_async(
            "events", timeout_seconds=0 if terminal is not None else 86400
        )
        if event is None:
            # recv orders by millisecond, with no tie breaker. A terminal
            # notification is a fence: consume the committed mailbox prefix
            # before writing any terminal frames.
            if terminal is None:
                continue
            observed = terminal.get("observed_at") or fallback_at
            for frame in terminal["frames"]:
                await append(
                    dict(
                        frame,
                        observed_at=observed,
                        generation=delivery["generation"],
                    )
                )
            # The final frame is done. Its one-based offset lets history read
            # that frame directly from the delivered prefix.
            delivery.update(terminal=delivery["offset"], pause=None)
            await DBOS.set_event_async("delivery", delivery)
            if terminal["close"] and (
                delivery["generation"]
                == await asyncio.to_thread(run_generation, run_id)
            ):
                await DBOS.close_stream_async("events")
                return
            terminal = None
            continue

        generation = event["generation"]
        if generation < delivery["generation"]:
            continue
        if generation > delivery["generation"]:
            delivery = {"offset": delivery["offset"], "generation": generation}
            terminal = None
        kind = event["type"]
        if kind == "_terminal":
            if not delivery.get("terminal"):
                terminal = event
            continue
        if kind == "run_status" and (
            version := event.get("plan_version") or event.get("clarification_version")
        ):
            delivery["pause"] = {"phase": event["status"], "version": version}
        await append(event)
        await DBOS.set_event_async("delivery", delivery)


@DBOS.workflow(name="por.cancel")
async def cancel_workflow(run_id):
    # Accepted transactionally with the request intent. DBOS recovers this
    # command if the API or Executor exits before cancellation reaches the Run.
    await DBOS.cancel_workflow_async(run_id, cancel_children=True)


@DBOS.step(
    name="por.completion_facts",
    retries_allowed=True,
    max_attempts=5,
    interval_seconds=1,
)
async def completion_facts(run_id):
    from open_deep_research.web.durable import get_run_client

    def read():
        # The root can be cancelled before the cascade reaches its children.
        # A cancelled child can lose ownership without publishing a stage end.
        if native_client().list_workflows(
            workflow_id_prefix=run_id + ":",
            name=["por.plan", "por.research", "por.role"],
            status=list(ACTIVE),
            load_input=False,
            load_output=False,
            limit=1,
        ):
            return None
        result = get_run_client().outcome(run_id)
        return {
            key: result[key]
            for key in (
                "status",
                "native_status",
                "settlement",
                "usage",
                "error",
                "current_step",
            )
        }

    return await asyncio.to_thread(read)


async def wait_for_terminal(run_id):
    handle = await DBOS.retrieve_workflow_async(run_id, existing_workflow=False)
    await DBOS.wait_first_async([handle])


def commit_status(step):
    """Read the stage transition a committed ``por.commit_stage`` recorded.

    The transaction returns its transition, and that value is journaled with the
    Step, so one unified transaction stays distinguishable in the journal. A
    failed transaction recorded no output; it is a target in its own right and
    never needs to be classified further.
    """
    if step.get("function_name") != "por.commit_stage" or step.get("error") is not None:
        return None
    output = step.get("output")
    return output.get("status") if isinstance(output, dict) else None


MODEL_STEPS = {"por.llm", "_model_call_step"}
REWINDABLE_FAILURES = {"ERROR", "CANCELLED"}


def failure_target(workflow, steps=None):
    """Select the first Step that must run again after the committed prefix."""
    client = native_client()
    if steps is None:
        steps = client.list_workflow_steps(workflow.workflow_id)
    by_id = {step["function_id"]: step for step in steps}
    anchor = next(
        (
            step
            for step in reversed(steps)
            if commit_status(step) in {"running", "completed"}
        ),
        None,
    )
    start = anchor["function_id"] + 1 if anchor else 1
    # Bounded LLM callers may catch a provider error before making another
    # successful attempt in the same stage. Keep those handled errors too.
    last_answer = max(
        (
            step["function_id"]
            for step in steps
            if step["function_name"] == "por.llm" and step.get("error") is None
        ),
        default=0,
    )
    last_model = None
    for step in steps:
        if step["function_id"] < start:
            continue
        errored = step.get("error") is not None
        handled = step["function_name"] == "por.llm" and start < last_answer
        if (
            step["function_id"] > start
            or (errored and not handled)
            or commit_status(step) == "failed"
        ):
            break
        if step["function_name"] == "por.llm" and not errored:
            last_model = step
        start += 1

    failure = client.get_event(workflow.workflow_id, "failure", 0) or {}
    output = last_model.get("output") if last_model else None
    rejected = isinstance(output, (list, tuple)) and len(output) == 3 and output[2]
    if (
        last_model
        and by_id.get(start, {}).get("error") is None
        and (rejected or failure.get("invalid_model_output"))
    ):
        start = last_model["function_id"]
    step_name = by_id.get(start, {}).get("function_name")
    stage_name = (anchor or {}).get("output", {}).get("stage")
    return dict(
        workflow_id=workflow.workflow_id,
        queue=CONTROL if workflow.name == "por.run" else BUSINESS,
        start_step=start,
        model_reserve=stage_name != "generate_final_report"
        if step_name in MODEL_STEPS
        else None,
        completed_at=workflow.completed_at,
    )


def retry_targets(run_id, root):
    client = native_client()
    workflows = client.list_workflows(
        workflow_id_prefix=run_id + ":",
        name=["por.plan", "por.research", "por.role"],
        load_input=False,
        load_output=False,
    )
    if any(wf.status in ACTIVE for wf in workflows):
        raise RunConflict("A research workflow is still running")
    by_id = {wf.workflow_id: wf for wf in workflows}
    targets = []

    def visit(workflow):
        if workflow.app_version not in RESUMABLE_VERSIONS:
            raise RunConflict(
                "This release cannot replay the failed Workflow's journal"
            )
        if workflow.status not in REWINDABLE_FAILURES:
            raise RunConflict("A required child Workflow has no recoverable result")
        steps = client.list_workflow_steps(workflow.workflow_id)
        target = failure_target(workflow, steps)
        for child_id in dict.fromkeys(
            step["child_workflow_id"] for step in steps if step.get("child_workflow_id")
        ):
            child = by_id.get(child_id)
            if child is None or child.parent_workflow_id != workflow.workflow_id:
                raise RunConflict("A recorded child Workflow is missing")
            if child.status != "SUCCESS":
                visit(child)
        targets.append(target)

    visit(root)
    return targets


@DBOS.step(name="por.reopen_events", retries_allowed=True, max_attempts=5)
async def reopen_events(run_id):
    """Resume the same append-only Stream at its recorded close."""

    def reopen():
        client = native_client()
        relay_id = run_id + ":events"
        relay = client.retrieve_workflow(relay_id).get_status()
        if relay.status in ACTIVE:
            return
        steps = client.list_workflow_steps(relay_id, load_output=False)
        start = steps[-1]["function_id"] if steps else 0
        if not steps or steps[-1]["function_name"] != "DBOS.closeStream":
            start += 1
        client.rewind_workflow(
            relay_id, start_step=start, application_version=VERSION, queue_name=CONTROL
        )

    await asyncio.to_thread(reopen)


@DBOS.step(name="por.retry_execution", retries_allowed=True, max_attempts=5)
async def retry_execution(run_id, target):
    """Apply one authorized cut, fenced by the prior native completion."""

    def rewind():
        with domain_engine().begin() as db:
            req = read_request(run_id, db=db, lock=True)
            if req["cancel_requested"]:
                return False
            client = native_client()
            wf = client.retrieve_workflow(target["workflow_id"]).get_status()
            # If rewind committed before this Step's ACK, the old completion
            # is already gone and the original grant must not rewind it again.
            if (
                wf.status in REWINDABLE_FAILURES
                and wf.completed_at == target["completed_at"]
            ):
                client.rewind_workflow(
                    wf.workflow_id,
                    start_step=target["start_step"],
                    application_version=VERSION,
                    queue_name=target["queue"],
                    queue_partition_key=req["owner"]
                    if target["queue"] == BUSINESS
                    else None,
                )
            return True

    return await asyncio.to_thread(rewind)


async def apply_retry(run_id, generation, targets):
    await reopen_events(run_id)
    # Rewind removes unconsumed mailbox entries. Send progress only after the
    # relay is ready to receive the new generation.
    await send_event(run_id, "retry_started", generation=generation)
    outcome = "applied"
    for target in targets:
        try:
            if not await retry_execution(run_id, target):
                outcome = "cancelled"
                break
        except Exception:
            outcome = "blocked"
            break
        # Let each child release its queue slot before rewinding its parent.
        if target["workflow_id"] != run_id:
            await wait_for_terminal(target["workflow_id"])
    await DBOS.set_event_async(
        "retry_applied",
        dict(generation=generation, outcome=outcome),
    )


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
    yield dict(type="done", status=status)


@DBOS.workflow(name="por.finalize")
@dbos_scope
async def finalize_workflow(run_id, generation=0, targets=()):
    if targets:
        await apply_retry(run_id, generation, targets)
    await wait_for_terminal(run_id)
    while True:
        try:
            result = await completion_facts(run_id)
        except Exception:
            await DBOS.sleep_async(5)
            continue
        if result is not None and result["settlement"] != "pending":
            break
        await DBOS.sleep_async(1)
    if generation == await asyncio.to_thread(run_generation, run_id):
        await send_event(
            run_id,
            "_terminal",
            generation=generation,
            frames=list(terminal_frames(result)),
            close=result["native_status"] != "ERROR" or result["status"] == "cancelled",
        )
