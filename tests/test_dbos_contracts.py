"""Public native DBOS contracts replacing SQL RunStore/Worker white-box tests."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from dbos_support import (
    has_unconfirmed_bills_or_effects,
    record_scripted_model_attempt,
    retry_observed_failure,
    stage_observations,
)
from fastapi.testclient import TestClient
from sdk_model_support import install_inference
from sqlalchemy import text

from open_deep_research.prompts import response_strategy_prompt, risk_assessment_prompt
from open_deep_research.state import ResearchReview, ResearchRoleResult
from open_deep_research.web.run_data import (
    RunConflict,
    RunNotFound,
    dumps,
    read_request,
)
from open_deep_research.workflow.dbos_config import domain_engine, native_client
from open_deep_research.workflow.python_state import ANALYSIS_ROLES, RESEARCH_ROLES

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("native_case", ["admission"], indirect=True)
def test_parallel_producers_wait_for_the_shared_token_bill(native_case):
    """Two formal producers must not reject each other's in-flight bill."""
    case = native_case
    case.cfg["configurable"].update(
        max_input_tokens=1_000_000,
        max_output_tokens=100_000,
    )
    run = case.create()
    result = case.wait(run, timeout=60)

    assert result["status"] == "completed"
    windows = case.runtimes[0].model_windows
    roles = {role for role, _start, _end in windows}
    assert roles == {"public_signal", "internal_knowledge"}
    # Each producer completed both bounded model turns instead of being
    # stopped by the other producer's unconfirmed bill.
    assert len(windows) == 4
    intervals = sorted((start, end) for _role, start, end in windows)
    for (_start, end), (next_start, _next_end) in zip(intervals, intervals[1:]):
        assert next_start >= end


@pytest.mark.parametrize("native_case", ["tool_budget"], indirect=True)
@pytest.mark.parametrize("limit", ["max_tool_calls", "max_search_calls"])
def test_parallel_producers_share_one_run_tool_allowance(native_case, limit):
    """Two Producers cannot each spend the same final allowance."""
    case = native_case
    case.cfg["configurable"].update({limit: 1})
    run = case.create()
    result = case.wait(run, timeout=60)

    assert result["status"] == "completed", result["error"]
    assert sum(case.calls.values()) == 1
    records = {
        rt.role: rt.harness.results[f"lookup-{rt.role}"]
        for rt in case.runtimes
        if rt.role in {"public_signal", "internal_knowledge"}
    }
    assert set(records) == {"public_signal", "internal_knowledge"}
    assert sorted(record.status for record in records.values()) == [
        "failed",
        "succeeded",
    ]
    rejected = next(record for record in records.values() if record.status == "failed")
    assert "Budget guard skipped" in str(rejected.observation)
    final = case.client.client.list_workflows(
        workflow_ids=[run["run_id"]], load_input=False, load_output=True
    )[0].output
    assert final["runtime"]["budget"]["tool_calls"] == 1
    with domain_engine().connect() as db:
        assert (
            db.scalar(
                text("SELECT count(*) FROM por_domain.tool_calls WHERE run_id=:r"),
                {"r": run["run_id"]},
            )
            == 1
        )


@pytest.mark.parametrize("native_case", ["tool_budget_multi"], indirect=True)
def test_parallel_producers_cannot_exceed_multiple_tool_calls(native_case):
    """Concurrent batches across Producers share one Run-wide ceiling."""
    case = native_case
    case.cfg["configurable"].update(max_tool_calls=3)
    run = case.create()
    result = case.wait(run, timeout=60)

    assert result["status"] == "completed", result["error"]
    assert sum(case.calls.values()) == 3
    records = [
        rt.harness.results[f"lookup-{rt.role}{suffix}"]
        for rt in case.runtimes
        if rt.role in {"public_signal", "internal_knowledge"}
        for suffix in ("", "-2")
    ]
    assert len(records) == 4
    assert [record.status for record in records].count("succeeded") == 3
    assert [record.status for record in records].count("failed") == 1
    final = case.client.client.list_workflows(
        workflow_ids=[run["run_id"]], load_input=False, load_output=True
    )[0].output
    assert final["runtime"]["budget"]["tool_calls"] == 3


@pytest.mark.parametrize("native_case", ["cancel"], indirect=True)
def test_studio_cancel_records_failed_model_and_agent_after_cancel_intent(native_case):
    case = native_case
    run = case.create()
    case.wait(run, lambda _r: case.blocked.is_set())
    case.client.cancel(run["run_id"], "alice")
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    assert result["status"] == "cancelled" and result["settlement"] == "settled"
    events = case.client.read_events(run["run_id"], "alice", 0, 1000)
    cancelled = [
        e
        for e in events
        if e["type"] == "sdk_event" and e.get("role") == "public_signal"
    ]
    assert any(
        e["event"] == "model_failed" and e["status"] == "cancelled" for e in cancelled
    )
    assert any(
        e["event"] == "agent_failed" and e["status"] == "cancelled" for e in cancelled
    )
    assert any(
        e.get("stage") == "settlement" and e["status"] == "settled" for e in cancelled
    )
    assert case.calls == {"public_signal": 1, "internal_knowledge": 1}


def test_cancel_delivers_when_native_role_loses_ownership_before_stage_end(
    native_case, monkeypatch
):
    import threading

    from open_deep_research.workflow import dbos_workflow

    case = native_case
    original = dbos_workflow._announce_stage
    announced, release = threading.Event(), threading.Event()

    def announce(scope, research_round):
        date = original(scope, research_round)
        if scope.name == "public_signal_agent":
            announced.set()
            assert release.wait(20)
        return date

    # The stage message commits before DBOS records the Step. Cancelling
    # here clears native ownership and no step_failed message is produced.
    monkeypatch.setattr(dbos_workflow, "_announce_stage", announce)
    run = case.create()
    run_id = run["run_id"]
    role_id = run_id + ":public_signal:1"
    try:
        assert announced.wait(15)
        case.client.cancel(run_id, "alice")
        case.wait(run, lambda _: case.client.native(role_id).status == "CANCELLED")
    finally:
        release.set()

    result = case.wait(
        run,
        lambda r: r["events_complete"] and r["archive_status"] == "saved",
        timeout=20,
    )
    assert result["status"] == "cancelled" and result["settlement"] == "settled"
    events = case.client.read_events(run_id, "alice", 0, 1000)
    role_events = [event["type"] for event in events if event.get("unit_id") == role_id]
    assert role_events.count("step_started") == 1 and "step_failed" not in role_events
    assert events[-1]["type"] == "done" and events[-1]["status"] == "cancelled"
    assert events[-1]["seq"] == result["event_cursor"]
    assert not case.client.stream_finished(run_id, "alice", result["event_cursor"] - 1)
    assert case.client.stream_finished(run_id, "alice", result["event_cursor"])


def test_studio_native_sdk_metadata_is_identified_replayable_and_read_only(native_case):
    """Verify observer facts over actual Runner/DBOS, not synthetic UI events."""
    from open_deep_research.web.observations import execution_view

    case = native_case
    run = case.create()
    result = case.wait(
        run, lambda r: r["archive_status"] == "saved" and r["events_complete"]
    )
    before = len(case.models)
    cursor = 0
    events = []
    while cursor < result["event_cursor"]:
        page = case.client.read_events(run["run_id"], "alice", cursor, 17)
        assert page
        events.extend(page)
        cursor = page[-1]["seq"]
    assert [e["seq"] for e in events] == list(range(1, cursor + 1))
    assert all(e["generation"] == 0 for e in events)
    sdk = [e for e in events if e["type"] == "sdk_event"]
    invocations = {e["invocation_id"] for e in sdk}
    assert len(invocations) == 2
    assert {e["role"] for e in sdk if e["event"] == "agent_started"} == set(
        RESEARCH_ROLES
    )
    assert all(e["unit_id"].startswith(run["run_id"] + ":") for e in sdk)
    for invocation in invocations:
        local = [e for e in sdk if e["invocation_id"] == invocation]
        assert sum(e["event"] == "agent_started" for e in local) == 1
        assert sum(e["event"] == "agent_completed" for e in local) == 1
        assert all(e.get("execution_id") and e.get("observed_at") for e in local)
    starts = [e for e in sdk if e["event"] == "model_started"]
    ends = [e for e in sdk if e["event"] == "model_completed"]
    assert len(starts) == len(ends) == 4
    assert {(e["invocation_id"], e["model_turn"]) for e in starts} == {
        (e["invocation_id"], e["model_turn"]) for e in ends
    }
    assert all(
        e["usage"] == {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}
        for e in ends
    )
    assert all(e["duration_ms"] >= 0 for e in ends)
    tools = [e for e in sdk if e["event"] == "tool_result"]
    assert len(tools) == 2 and all(e["status"] == "succeeded" for e in tools)
    assert all(e["tool_call_id"] for e in tools)
    assert any(e.get("stage") == "settlement" and e["status"] == "settled" for e in sdk)
    assert not any(
        {"prompt", "arguments", "output", "observation", "system_prompt"} & e.keys()
        for e in sdk
    )
    catalog = execution_view(case.client, run["run_id"], "alice", "", 0, 200)
    assert any(
        w["name"] == "por.run" and w["status"] == "SUCCESS"
        for w in catalog["workflows"]
    )
    assert catalog["steps"]
    for role in RESEARCH_ROLES:
        role_steps = case.client.client.list_workflow_steps(
            f"{run['run_id']}:{role}:1", load_output=False
        )
        assert any(step["function_name"] == "por.agent_context" for step in role_steps)
    assert len(case.models) == before == 2
    assert result["usage"]["model_calls"] == 18


@pytest.mark.parametrize("disabled_role", ["internal_knowledge", "response_strategy"])
def test_studio_disabled_role_is_an_explicit_skip(
    native_case, monkeypatch, disabled_role
):
    from contextlib import asynccontextmanager

    from agents.testing import ScriptedModel, assistant_message, function_call
    from test_sdk_runtime import TOKENS

    from open_deep_research.runtime import business_agent

    case = native_case
    case.cfg["configurable"]["enabled_business_agents"].remove(disabled_role)

    @asynccontextmanager
    async def model(*_args, **_kwargs):
        yield ScriptedModel(
            [
                [function_call("web_search", {"query": "brand"}, call_id="skip-proof")],
                [assistant_message("public evidence")],
            ],
            default_usage=TOKENS,
        )

    if disabled_role == "internal_knowledge":
        monkeypatch.setattr(business_agent, "sdk_model", model)
    run = case.create()
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    assert result["status"] == "completed", result["error"]
    events = case.client.read_events(run["run_id"], "alice", 0, 1000)
    skipped = [e for e in events if e["type"] == "step_skipped"]
    step = disabled_role + "_agent"
    assert len(skipped) == 1 and skipped[0]["step"] == step
    assert skipped[0]["reason"] == "role_disabled"
    assert not any(
        e["type"] == "sdk_event" and e.get("role") == disabled_role for e in events
    )
    assert not any(
        e["type"] == "step_completed" and e.get("step") == step for e in events
    )
    assert {runtime.role for runtime in case.runtimes} == set(RESEARCH_ROLES) - {
        disabled_role
    }
    assert {report["role"] for report in result["role_reports"]} == set(
        (*RESEARCH_ROLES, *ANALYSIS_ROLES)
    ) - {disabled_role}
    assert step not in result["completed_steps"]


def test_sse_terminal_receipt_cannot_close_over_an_older_cursor(
    native_case, monkeypatch
):
    """Force the relay/read interleaving that previously dropped the final done."""
    from open_deep_research.web.runs import event_stream

    case = native_case
    run = case.create()
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    end = result["seq"]
    before = len(case.models)
    original = case.client.client.read_stream
    reads = []

    def raced(workflow_id, key, *, offset, **kwargs):
        if workflow_id == run["run_id"] + ":events" and offset == end - 1:
            reads.append(offset)
            if len(reads) == 1:
                # The empty page was read just before the last Stream commit.
                # The delivery Event already exposes the newer watermark.
                return (item for item in ())
        return original(workflow_id, key, offset=offset, **kwargs)

    class Subscription:
        async def is_disconnected(self):
            return False

    monkeypatch.setattr(case.client.client, "read_stream", raced)

    async def receive():
        return [
            frame
            async for frame in event_stream(
                case.client, run["run_id"], "alice", end - 1, Subscription()
            )
        ]

    frames = asyncio.run(receive())
    assert reads == [end - 1, end - 1]
    data = [frame for frame in frames if frame.startswith("id:")]
    assert len(data) == 1
    assert f"id: {end}\n" in data[0] and '"type": "done"' in data[0]
    assert len(case.models) == before == 2


@pytest.mark.parametrize(
    "native_case", ["normal", "complete", "rolling"], indirect=True
)
def test_approved_brief_reaches_two_agents_analysis_report_and_history(
    native_case, monkeypatch
):
    case = native_case
    calls = []

    def observe(request):
        if request.schema_name == "ResearchReview":
            calls.append("review")
        if request.prompt == risk_assessment_prompt:
            calls.append("risk")
            assert request.tools == [] and request.schema_name is None
            context = request.messages[-1]["content"]
            assert approved["plan"]["brief"] in context
            assert all(f"## {role}\n" in context for role in RESEARCH_ROLES)
            assert '"research_complete": true' in context
        if request.prompt == response_strategy_prompt:
            calls.append("strategy")
            assert request.tools == [] and request.schema_name is None
            assert "risk_assessment analysis" in request.messages[-1]["content"]
        if "Create the complete enterprise" in request.prompt:
            calls.append("report")
            assert all(f"{role} analysis" in request.prompt for role in ANALYSIS_ROLES)

    install_inference(monkeypatch, observe)
    run = case.create(approval=True)
    approved = case.wait(run, lambda r: r["status"] == "awaiting_approval")
    assert not case.runtimes
    case.client.approve(run["run_id"], "alice", approved["plan"]["version"], "approve")
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    assert result["status"] == "completed", result["error"]
    assert "FINAL brand recall report" in result["report"]
    assert calls == ["review", "risk", "strategy", "report"]
    assert len(case.runtimes) == len(case.models) == 2
    assert {runtime.role for runtime in case.runtimes} == set(RESEARCH_ROLES)
    # Includes the rolling-compaction scenario: the full locked Brief is visible
    # once on every SDK model request, including after history compaction.
    for model in case.models:
        for request in model.calls:
            content = "\n".join(
                item.get("content", "")
                for item in request.input
                if isinstance(item, dict) and isinstance(item.get("content"), str)
            )
            assert content.count(result["plan"]["brief"]) == 1
    assert all(rt.harness.last_settlement.succeeded for rt in case.runtimes)
    assert all(
        rt.harness.results[f"lookup-{rt.role}"].status == "succeeded"
        for rt in case.runtimes
    )
    assert (
        result["usage"]["model_calls"]
        == 7 + sum(rt.budget_usage["model_calls"] for rt in case.runtimes) + 5
    )
    # Initial usage plus four Runner turns and seven bounded model calls.
    if case.mode != "rolling":
        assert result["usage"]["total_tokens"] == 203
        assert result["usage"]["model_calls"] == 18

    run_id = run["run_id"]
    client = case.client.client
    state = client.retrieve_workflow(run_id).get_result()
    assert set(state["agents"]) == set((*RESEARCH_ROLES, *ANALYSIS_ROLES))
    assert {
        wf.workflow_id
        for wf in client.list_workflows(
            workflow_id_prefix=run_id + ":", name="por.role"
        )
    } == {f"{run_id}:{role}:1" for role in RESEARCH_ROLES}
    # Risk, strategy and the final report are the three bounded model steps
    # inside the research Workflow; each one is checkpointed with its output.
    model_steps = [
        step
        for step in client.list_workflow_steps(run_id + ":research")
        if step["function_name"] == "por.llm"
    ]
    assert len(model_steps) == 3 and all(step["output"] for step in model_steps)

    stages = {unit["name"]: unit for unit in stage_observations(run_id)}
    for role in ANALYSIS_ROLES:
        unit = stages[role + "_agent"]
        assert unit["id"] == f"{run_id}:{role}"
        assert {
            field: unit["usage"][field]
            for field in (
                "model_calls",
                "input_tokens",
                "output_tokens",
                "total_tokens",
            )
        } == {
            "model_calls": 1,
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 10,
        }
        assert state["agents"][role]["report"] == f"{role} analysis"
        assert unit["artifact"] and not unit["error"]

    for role in RESEARCH_ROLES:
        unit = stages[role + "_agent"]
        event = next(
            e
            for e in case.client.read_events(run_id, "alice", 0, 1000)
            if e["type"] == "agent_report" and e["unit_id"] == unit["id"]
        )
        assert (
            not {"patch", "research_result", "memory", "working_context"} & event.keys()
        )
        saved_result = case.client.client.retrieve_workflow(unit["id"]).get_result()
        role_result = ResearchRoleResult.model_validate(saved_result["research_result"])
        assert role_result.role + "_agent" == unit["name"]
        assert role_result.round == 1
        for field in ("model_calls", "input_tokens", "output_tokens", "total_tokens"):
            assert role_result.budget_delta[field] == unit["usage"][field]
        assert role_result.evidence_collected
        assert role_result.report == state["agents"][role]["report"]
    saved = case.history.get(result["history_id"], owner="alice")
    assert saved["run_id"] == run_id
    assert {entry["role"]: entry["content"] for entry in saved["role_reports"]} == {
        role: data["report"] for role, data in state["agents"].items()
    }
    assert saved["report"] == result["report"]


def test_analysis_respects_the_reserved_report_model_call(native_case, monkeypatch):
    case = native_case
    # Seven starting calls, Plan, two three-call producers, and Review leave
    # only the reserved final-report call.
    case.cfg["configurable"].update(max_model_calls=16, reserve_final_report_call=True)

    def reject_analysis(request):
        assert request.prompt not in (risk_assessment_prompt, response_strategy_prompt)

    install_inference(monkeypatch, reject_analysis)
    run = case.create()
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    assert result["status"] == "completed", result["error"]
    assert result["report"] and result["usage"]["model_calls"] == 16
    reports = {entry["role"]: entry["content"] for entry in result["role_reports"]}
    for role in ANALYSIS_ROLES:
        assert "research model budget was exhausted" in reports[role]
        assert reports[role] in result["usage"]["degradation_reasons"]
    stages = {unit["name"]: unit for unit in stage_observations(run["run_id"])}
    assert all(
        stages[role + "_agent"]["usage"].get("model_calls", 0) == 0
        for role in ANALYSIS_ROLES
    )
    assert stages["generate_final_report"]["usage"]["model_calls"] == 1


def test_concurrent_acceptance_is_idempotent_and_conflicts_are_atomic(native_case):
    case = native_case
    key = uuid4().hex

    def submit(_):
        return case.client.create(
            "alice", key, {"topic": "brand recall"}, case.cfg, case.state
        )

    with ThreadPoolExecutor(max_workers=5) as pool:
        values = list(pool.map(submit, range(8)))
    assert sum(created for _, created in values) == 1
    run_id = values[0][0]["run_id"]
    assert {run["run_id"] for run, _ in values} == {run_id}
    native = case.client.client.list_workflows(
        name=["por.run", "por.events", "por.finalize"],
        load_input=False,
        load_output=False,
    )
    assert len(native) == 3
    assert {w.name: w.workflow_id for w in native} == {
        "por.run": run_id,
        "por.events": run_id + ":events",
        "por.finalize": run_id + ":finalize",
    }
    with pytest.raises(RunConflict, match="another request"):
        case.client.create("alice", key, {"topic": "different"}, case.cfg, case.state)
    case.wait(values[0][0])


@pytest.mark.parametrize(
    "operation", ["get", "read_events", "cancel", "approve", "retry"]
)
def test_all_controls_remain_owner_scoped(native_case, operation):
    case = native_case
    run = case.create(approval=True)
    args = {
        "get": (),
        "read_events": (0,),
        "cancel": (),
        "approve": ("0" * 64, "approve"),
        "retry": ("r", 1),
    }[operation]
    with pytest.raises(RunNotFound):
        getattr(case.client, operation)(run["run_id"], "bob", *args)
    with pytest.raises(RunNotFound):
        case.create(owner="bob", session=run["research_session_id"])
    case.client.cancel(run["run_id"], "alice")


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_approval_immutable_once_and_no_replanning(native_case, decision):
    case = native_case
    run = case.create(approval=True)
    planned = case.wait(run, lambda r: r["status"] == "awaiting_approval")
    assert not case.runtimes
    before = [u["id"] for u in stage_observations(run["run_id"])]
    with pytest.raises(RunConflict):
        case.client.approve(run["run_id"], "alice", "0" * 64, decision)
    case.client.approve(run["run_id"], "alice", planned["plan"]["version"], decision)
    result = case.wait(run)
    assert result["status"] == ("completed" if decision == "approve" else "cancelled")
    with pytest.raises(RunConflict):
        case.client.approve(
            run["run_id"], "alice", planned["plan"]["version"], decision
        )
    after = [u["id"] for u in stage_observations(run["run_id"])]
    assert set(before) <= set(after)
    assert len([k for k in after if ":plan:" in k]) == 1
    if decision == "reject":
        assert not case.runtimes


def test_sse_cursor_replay_is_read_only_and_redacted(native_case):
    case = native_case
    run = case.create()
    case.wait(run, lambda r: r["archive_status"] == "saved")
    events = case.client.read_events(run["run_id"], "alice", 0, 1000)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    types = {e["type"] for e in events}
    assert {
        "run_status",
        "step_started",
        "step_completed",
        "stream",
        "report",
        "usage",
        "settlement",
        "done",
        "history_saved",
    } <= types
    assert all(e["protocol_version"] == 1 for e in events)
    tools = [e for e in events if e["type"] == "sdk_event" and e.get("event") == "tool"]
    assert len(tools) == 4
    assert [e["status"] for e in tools].count("started") == 2
    assert [e["status"] for e in tools].count("returned") == 2
    assert (
        sum(
            e["type"] == "sdk_event" and e.get("event") == "agent_started"
            for e in events
        )
        == 2
    )
    assert any(
        e["type"] == "status" and e.get("granularity") == "stage" for e in events
    )
    assert events[-1]["type"] == "done" and events[-1]["archive_status"] == "saved"
    before = len(case.models)
    assert case.client.read_events(run["run_id"], "alice", 3, 1000) == events[3:]
    assert len(case.models) == before
    for cursor in (-1, 1000000):
        with pytest.raises(RunConflict):
            case.client.read_events(run["run_id"], "alice", cursor)
    assert "arguments" not in dumps(events)
    assert "Private Agent Memory" not in dumps(events)


@pytest.mark.parametrize(
    "native_case", ["model_error", "double_error", "timeout", "cancel"], indirect=True
)
def test_sdk_failure_and_cancel_preserve_budget_and_settlement(native_case):
    case = native_case
    run = case.create()
    if case.mode == "cancel":
        end = time.monotonic() + 15
        while not case.blocked.is_set() and time.monotonic() < end:
            time.sleep(0.05)
        assert case.blocked.is_set()
        case.client.cancel(run["run_id"], "alice")
    result = case.wait(run)
    assert result["status"] in {"failed", "cancelled", "interrupted"}
    assert not result["report"]
    assert result["usage"]["model_calls"] >= 7 + sum(
        rt.budget_usage["model_calls"] for rt in case.runtimes
    )
    assert all(rt.harness.last_settlement.succeeded for rt in case.runtimes)
    assert all(
        len(rt.harness.results) == 1
        and rt.harness.results[f"lookup-{rt.role}"].status == "succeeded"
        for rt in case.runtimes
    )
    assert len(case.models) == 2


def test_session_mutex_uses_native_status_and_authorization(pg_database):
    from test_python_workflow import config, initial

    from open_deep_research.web.durable import RunClient
    from open_deep_research.web.run_data import RunNotFound

    client = RunClient()
    first = client.create("alice", "a", {"topic": "a"}, config(), initial())[0]
    session = first["research_session_id"]
    with pytest.raises(RunConflict, match="active research"):
        client.create(
            "alice", "b", {"topic": "b"}, config(), initial(), session_id=session
        )
    with pytest.raises(RunNotFound):
        client.create(
            "bob", "b", {"topic": "b"}, config(), initial(), session_id=session
        )

    native_client().cancel_workflow(first["run_id"])
    second, created = client.create(
        "alice", "b", {"topic": "b"}, config(), initial(), session_id=session
    )
    assert created and second["research_session_id"] == session
    assert second["native_status"] == "ENQUEUED"


def test_tool_operation_identity_never_authorizes_unknown_replay(
    native_case, monkeypatch
):
    import threading

    from open_deep_research.runtime import tool_executor
    from open_deep_research.workflow import execution

    case = native_case
    original = tool_executor.start_operation
    entered, release = threading.Event(), threading.Event()
    admitted = []

    def guard(*args):
        original(*args)
        admitted.append(args)
        entered.set()
        assert release.wait(20)

    effect = tool_executor.tool_effect
    monkeypatch.setattr(
        tool_executor,
        "tool_effect",
        lambda t: "protected" if t.name == "web_search" else effect(t),
    )
    monkeypatch.setattr(tool_executor, "start_operation", guard)
    run = case.create()
    try:
        assert entered.wait(20)
        run_id, workflow_id, executor, op, ledger_op, name, fingerprint = admitted[0]
        with pytest.raises(execution.ExecutionControlError, match="already ran for this call"):
            original(*admitted[0])
        with pytest.raises(execution.ExecutionControlError, match="no longer owns"):
            original(
                run_id,
                workflow_id,
                "other-executor",
                "another-id",
                ledger_op,
                name,
                fingerprint,
            )
        with domain_engine().connect() as db:
            row = (
                db.execute(
                    text("SELECT * FROM por_domain.operations WHERE id=:id"), {"id": op}
                )
                .mappings()
                .one()
            )
            assert len(row["fingerprint"]) == 64 and row["outcome"] is None
            assert "arguments" not in row and "usage" not in row
    finally:
        release.set()
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    assert result["status"] == "completed" and case.calls["public_signal"] == 1
    execution.finish_operation(run_id, op, "returned")
    with pytest.raises(execution.ExecutionControlError):
        execution.finish_operation(run_id, op, "unknown")


def test_api_contract_create_auth_cancel_and_sse(native_case, monkeypatch):
    from open_deep_research.web import runs, server

    case = native_case
    from contextlib import asynccontextmanager

    from agents.testing import (
        ModelStep,
        ScriptedModel,
        assistant_message,
        function_call,
    )
    from test_sdk_runtime import TOKENS

    from open_deep_research.runtime import business_agent

    @asynccontextmanager
    async def api_model(*args, **kwargs):
        def first(call):
            role = call.system_instructions.split("<Role>\n", 1)[1].split(
                "\n</Role>", 1
            )[0]
            if role in {"public_signal", "internal_knowledge"}:
                return [
                    function_call(
                        "web_search" if role == "public_signal" else "rag_search",
                        {"query": role},
                        call_id="api-" + role,
                    )
                ]
            assert "[Evidence]" in str(call.input)
            return [assistant_message("analysis")]

        yield ScriptedModel(
            [ModelStep.respond(first), [assistant_message("complete")]],
            default_usage=TOKENS,
        )

    monkeypatch.setattr(business_agent, "sdk_model", api_model)
    monkeypatch.setattr(runs, "get_run_client", lambda: case.client)
    monkeypatch.setattr(server, "WEB_API_TOKEN", "test-db-os-token")
    # Inference is already installed; the Web entry freezes the isolated test config.
    from open_deep_research import config_file
    from open_deep_research.configuration import Configuration

    configuration = Configuration.from_runnable_config(case.cfg)
    monkeypatch.setattr(
        config_file, "load_configuration", lambda _path=None: configuration
    )
    with TestClient(server.app) as http:
        # Create body carries business input only; the frozen test config keeps
        # every model and research setting out of the request.
        body = {"topic": "brand recall"}
        assert http.post("/api/runs", json=body).status_code == 401
        headers = {"Authorization": "Bearer test-db-os-token", "Idempotency-Key": "api"}
        response = http.post("/api/runs", json=body, headers=headers)
        assert response.status_code == 202, response.text
        run = response.json()
        repeated = http.post("/api/runs", json=body, headers=headers)
        assert repeated.status_code == 200
        assert repeated.json()["run_id"] == run["run_id"]
        conflict = http.post(
            "/api/runs", json=dict(body, topic="another topic"), headers=headers
        )
        assert conflict.status_code == 409
        case.wait(dict(run, owner="operator"), lambda r: r["archive_status"] == "saved")
        replay = http.get("/api/runs/" + run["run_id"] + "/events", headers=headers)
        assert "id: 1" in replay.text
        assert "history_saved" in replay.text
        end = int(replay.text.rsplit("id: ", 1)[-1].splitlines()[0])
        resumed = http.get(
            "/api/runs/" + run["run_id"] + "/events?after=0",
            headers=dict(headers, **{"Last-Event-ID": str(end - 1)}),
        )
        assert resumed.status_code == 200
        assert resumed.text.count("\ndata: ") == 1 and resumed.text.startswith(
            f"id: {end}\n"
        )
        assert '"type": "done"' in resumed.text
        assert (
            http.get("/api/runs/" + run["run_id"], headers=headers).json()[
                "execution_engine"
            ]
            == "dbos"
        )
        bad = dict(body, owner="attacker")
        assert http.post("/api/runs", json=bad, headers=headers).status_code == 400


@pytest.mark.parametrize("pg_database", [{"RUN_APPROVAL_SECONDS": 1}], indirect=True)
def test_approval_timeout_never_runs_roles(native_case):
    run = native_case.create(approval=True)
    result = native_case.wait(run)
    assert result["status"] == "cancelled"
    assert not native_case.runtimes
    assert result["settlement"] == "settled"


@pytest.mark.parametrize("changed", ["MAX_MODEL_CALLS", "sdk_version"])
def test_configuration_and_sdk_drift_fail_before_external_work(
    native_case, monkeypatch, changed
):
    case = native_case
    run = case.create(approval=True)
    plan = case.wait(run, lambda r: r["status"] == "awaiting_approval")["plan"]
    if changed == "MAX_MODEL_CALLS":
        monkeypatch.setenv("MAX_MODEL_CALLS", "101")
    else:
        with domain_engine().begin() as db:
            req = read_request(run["run_id"], db=db)
            req["data"]["sdk"] = "unsupported-sdk"
            db.execute(
                text("UPDATE por_domain.requests SET value=:v WHERE id=:id"),
                {"id": run["run_id"], "v": dumps(req["data"])},
            )
    # New human-control admission rejects drift before a continuation is even sent.
    with pytest.raises(RunConflict, match="Accepted configuration/version changed"):
        case.client.approve(run["run_id"], "alice", plan["version"], "approve")
    pending = case.client.get(run["run_id"], "alice")
    assert pending["status"] == "awaiting_approval"
    assert pending["usage"]["model_calls"] == 8  # saved 7 + exactly one unified Plan
    assert not case.runtimes and not case.calls and not pending["retry_safe"]
    case.client.cancel(run["run_id"], "alice")
    result = case.wait(run)
    assert result["status"] == "cancelled"
    assert not case.runtimes and not case.calls
    assert result["usage"]["model_calls"] == 8


def test_archive_compensates_independently_without_rerunning_research(
    native_case, monkeypatch
):
    import threading

    from open_deep_research.web.history import HistoryUnavailable

    case = native_case
    unavailable = threading.Event()
    unavailable.set()
    original = case.history.save

    def save(records):
        if unavailable.is_set():
            raise HistoryUnavailable("Synthetic archive outage")
        return original(records)

    monkeypatch.setattr(case.history, "save", save)
    run = case.create()
    completed = case.wait(run)
    assert completed["status"] == "completed"
    case.wait(
        run,
        lambda r: any(
            e["type"] == "archive_pending"
            for e in case.client.read_events(run["run_id"], "alice", 0, 1000)
        ),
    )
    assert completed["report"]
    before = len(case.models)
    unavailable.clear()
    result = case.wait(run, lambda r: r["archive_status"] == "saved", timeout=50)
    assert result["report"] == completed["report"]
    assert len(case.models) == before == 2
    assert result["usage"] == completed["usage"]


def test_safe_read_retry_is_idempotent_and_reuses_successful_roles(
    native_case, monkeypatch
):
    from open_deep_research.workflow import dbos_workflow as engine

    case = native_case
    original = engine.research.research_review
    attempts = []

    async def flaky(state, cfg):
        if not attempts:
            attempts.append("failure")
            await record_scripted_model_attempt()
            raise RuntimeError("Synthetic safe read failure")
        return await original(state, cfg)

    monkeypatch.setattr(engine.research, "research_review", flaky)
    run = case.create()
    failed = case.wait(run, lambda r: r["events_complete"])
    assert failed["status"] == "failed"
    assert failed["retry_safe"]
    assert len(case.models) == 2
    failed = case.wait(run, lambda r: r["archive_status"] == "saved")
    from open_deep_research.web.history import HistoryRecord

    prior = HistoryRecord.model_validate(
        case.history.get(failed["history_id"], owner="alice")
    )
    assert prior.run_state_version == 1
    key = uuid4().hex
    failed = retry_observed_failure(case, run, key, failed)
    case.client.retry(run["run_id"], "alice", key, failed["state_version"])
    result = case.wait(run, lambda r: r["status"] == "completed")
    assert len(case.models) == 2
    assert result["usage"]["model_calls"] == 19
    assert result["generation"] == 1
    saved = case.wait(run, lambda r: r["archive_status"] == "saved")
    assert case.history.get(saved["history_id"], owner="alice")["status"] == "completed"
    # A delayed acknowledgement/commit for the earlier failed outcome cannot win.
    case.history.save([prior])
    latest = case.history.get(saved["history_id"], owner="alice")
    assert latest["status"] == "completed"
    assert latest["run_state_version"] == 2
    assert latest["report"] == result["report"]
    assert latest["failed_stage"] == ""
    assert "_por_archive_lock" not in latest
    forged = prior.model_copy(
        update={"owner": "bob", "run_state_version": 999, "report": "foreign overwrite"}
    )
    case.history.save([forged])
    unchanged = case.history.get(saved["history_id"], owner="alice")
    assert unchanged["owner"] == "alice"
    assert unchanged["report"] == result["report"]
    assert unchanged["run_state_version"] == 2


@pytest.mark.parametrize("pg_database", [{"RUN_QUEUE_CAPACITY": 1}], indirect=True)
def test_queue_capacity_and_policy_mismatch_are_explicit(pg_database, monkeypatch):
    from test_python_workflow import config, initial

    from open_deep_research.web.durable import RunClient
    from open_deep_research.workflow import domain_schema
    from open_deep_research.workflow.dbos_config import ExecutionSettings

    client = RunClient()
    client.create("alice", "first", {"topic": "a"}, config(), initial())
    with pytest.raises(RunConflict, match="full"):
        client.create("bob", "second", {"topic": "b"}, config(), initial())
    monkeypatch.setattr(
        domain_schema,
        "settings",
        lambda: ExecutionSettings(pg_database, queue_capacity=2),
    )
    with pytest.raises(RunConflict, match="policy"):
        domain_schema.verify_policy()


def test_domain_artifact_and_native_enqueue_rollback_are_atomic(
    pg_database, monkeypatch
):
    from test_python_workflow import config, initial

    from open_deep_research.web.durable import RunClient

    client = RunClient()

    def fail(*args, **kw):
        raise RuntimeError("Synthetic native-message transaction failure")

    with monkeypatch.context() as patch:
        from open_deep_research.web import durable

        patch.setattr(durable, "publish_event", fail)
        with pytest.raises(RuntimeError):
            client.create("alice", "a", {"topic": "a"}, config(), initial())
    with domain_engine().connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM por_domain.requests")).scalar_one()
            == 0
        )
    assert not native_client().list_workflows(
        name=["por.run", "por.events", "por.finalize"],
        load_input=False,
        load_output=False,
    )
    run, created = client.create("alice", "a", {"topic": "a"}, config(), initial())
    assert created and run["native_status"] == "ENQUEUED"


@pytest.mark.parametrize(
    "path,method",
    [
        ("", "post"),
        ("/unknown", "get"),
        ("/unknown/events", "get"),
        ("/unknown/cancel", "post"),
        ("/unknown/approval", "post"),
        ("/unknown/retry", "post"),
        ("/unknown/resume", "post"),
    ],
)
def test_every_run_api_control_requires_auth(native_case, monkeypatch, path, method):
    from open_deep_research.web import runs, server

    monkeypatch.setattr(server, "WEB_API_TOKEN", "test-auth-token")
    monkeypatch.setattr(runs, "get_run_client", lambda: native_case.client)
    with TestClient(server.app) as http:
        body = (
            {"plan_version": "0" * 64, "decision": "approve"}
            if path.endswith("approval")
            else {"state_version": 1}
        )
        result = (
            http.post("/api/runs" + path, json=body)
            if method == "post"
            else http.get("/api/runs" + path)
        )
        assert result.status_code == 401


@pytest.mark.parametrize(
    ("targets", "keep_requesting"),
    [
        ((), False),
        (("public_signal",), False),
        (("internal_knowledge",), False),
        (RESEARCH_ROLES, False),
        (RESEARCH_ROLES, True),
    ],
    ids=["sufficient", "public-only", "internal-only", "both", "round-cap"],
)
def test_gap_loop_dispatches_only_targeted_roles_and_merges_reports(
    native_case, monkeypatch, targets, keep_requesting
):
    from test_python_workflow import gap

    case = native_case
    case.cfg["configurable"]["max_research_rounds"] = 2
    reviews = []
    analysis_order = []

    def review(request):
        if request.prompt == risk_assessment_prompt:
            analysis_order.append("risk")
            assert (
                f'"research_complete": {str(reviews[-1].research_complete).lower()}'
                in request.messages[-1]["content"]
            )
        if request.prompt == response_strategy_prompt:
            analysis_order.append("strategy")
        if request.schema_name != "ResearchReview":
            return None
        tasks = (
            [gap(f"evidence-{role}-{len(reviews) + 1}", role) for role in targets]
            if not reviews or keep_requesting
            else []
        )
        result = ResearchReview(
            research_complete=not tasks,
            next_tasks=tasks,
        )
        reviews.append(result)
        analysis_order.append("review")
        return result

    install_inference(monkeypatch, review)
    run = case.create()
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    assert result["status"] == "completed", result["error"]
    expected_round = 2 if targets else 1
    assert len(reviews) == expected_round
    assert analysis_order == ["review"] * expected_round + ["risk", "strategy"]
    assert len(case.runtimes) == 2 + len(targets)
    assert case.calls == {role: 1 + (role in targets) for role in RESEARCH_ROLES}
    final = case.client.client.list_workflows(
        workflow_ids=[run["run_id"]], load_input=False, load_output=True
    )[0].output
    assert final["workflow"]["round"] == expected_round
    assert final["workflow"]["review"].research_complete is not keep_requesting
    assert {t.task_id for t in final["workflow"]["completed_tasks"]} == {
        f"evidence-{role}-1" for role in targets
    }
    assert final["workflow"]["pending_tasks"] == []
    for role in RESEARCH_ROLES:
        report = final["agents"][role]["report"]
        assert "Research round: 1" in report
        assert ("Research round: 2" in report) == (role in targets)
    assert len(result["role_reports"]) == 4 + len(targets)
    followups = [
        (event["round"], event["roles"])
        for event in case.client.read_events(run["run_id"], "alice", 0, 1000)
        if event["type"] == "research_followup"
    ]
    assert followups == ([(2, list(targets))] if targets else [])
    assert all(rt.harness.last_settlement.succeeded for rt in case.runtimes)


@pytest.mark.parametrize(
    "pg_database",
    [{"RUN_SDK_SESSION_PATH": "dbos-sdk-sessions.sqlite"}],
    indirect=True,
)
def test_sdk_session_is_private_to_role_invocation_not_workflow_recovery(
    native_case, tmp_path
):
    import sqlite3
    from pathlib import Path

    from open_deep_research.workflow.dbos_config import settings

    case = native_case
    run = case.create()
    case.wait(run)
    session_path = Path(settings().session_path)
    assert session_path.is_relative_to(tmp_path)
    with sqlite3.connect(session_path) as db:
        rows = db.execute(
            "SELECT session_id FROM agent_sessions WHERE session_id LIKE ?",
            (run["run_id"] + "%",),
        ).fetchall()
        assert len(rows) == 2
        assert len({r[0] for r in rows}) == 2
        for role in RESEARCH_ROLES:
            assert sum(role in row[0] for row in rows) == 1
    assert len(case.models) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"owner": "attacker"},
        {"dbos_database_url": "bad"},
        {"max_model_calls": 99999},
        {"organization_context": "override"},
        {"topic": ""},
        {"workflow_backend": "langgraph"},
        {"business_agent_runtime": "legacy"},
        {"research_session_id": "../foreign"},
        {"model": "unsupported:model"},
    ],
)
def test_api_rejects_unadmitted_parameters_before_sdk_work(
    native_case, monkeypatch, payload
):
    from open_deep_research.web import runs, server

    monkeypatch.setattr(server, "WEB_API_TOKEN", "test-rejection-token")
    monkeypatch.setattr(runs, "get_run_client", lambda: native_case.client)
    body = (
        dict(
            topic="brand recall",
            workflow_backend="python",
            business_agent_runtime="openai_agents",
            model="openai:gpt-4.1",
        )
        | payload
    )
    with TestClient(server.app) as http:
        response = http.post(
            "/api/runs",
            json=body,
            headers={
                "Authorization": "Bearer test-rejection-token",
                "Idempotency-Key": "reject",
            },
        )
        assert response.status_code == 400, response.text
    assert not native_case.models


def test_cancellation_during_real_receipt_commit_drains_harness(
    native_case, monkeypatch
):
    import threading

    from open_deep_research.runtime import tool_executor

    case = native_case
    original = tool_executor.finish_operation
    entered, release = threading.Event(), threading.Event()
    effect = tool_executor.tool_effect
    monkeypatch.setattr(
        tool_executor,
        "tool_effect",
        lambda t: "protected" if t.name == "web_search" else effect(t),
    )

    def finish(*args):
        if args[-1] == "returned":
            entered.set()
            assert release.wait(20)
        original(*args)

    monkeypatch.setattr(tool_executor, "finish_operation", finish)
    run = case.create()
    try:
        assert entered.wait(20)
        case.client.cancel(run["run_id"], "alice")
        cancelling = case.wait(run, lambda r: r["native_status"] == "CANCELLED")
        assert (
            cancelling["settlement"] == "pending"
            and cancelling["status"] == "cancelling"
        )
    finally:
        release.set()
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    assert result["status"] == "cancelled" and result["settlement"] == "settled"
    assert case.calls["public_signal"] == 1
    with domain_engine().connect() as db:
        assert (
            db.scalar(text("SELECT outcome FROM por_domain.operations")) == "returned"
        )
    assert result["usage"]["model_calls"] >= 10


@pytest.mark.parametrize("native_case", ["cancel"], indirect=True)
def test_persisted_cancel_intent_survives_request_ack_loss(native_case, monkeypatch):
    case = native_case
    run = case.create()
    case.wait(run, lambda _: case.blocked.is_set())
    with monkeypatch.context() as patch:
        original = case.client.get

        def lost(*args):
            raise RuntimeError("Response lost after cancellation transaction")

        patch.setattr(case.client, "get", lost)
        with pytest.raises(RuntimeError, match="Response lost"):
            case.client.cancel(run["run_id"], "alice")
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    assert result["native_status"] == "CANCELLED" and result["status"] == "cancelled"
    assert result["settlement"] == "settled" and len(case.models) == 2
    assert original(run["run_id"], "alice")["cancel_requested"]

def test_approval_expiry_gap_before_recv_is_still_enforced(native_case, monkeypatch):
    from open_deep_research.workflow import dbos_workflow as engine

    original = engine.send_event

    async def delayed(run_id, event_type, **data):
        if event_type == "run_status" and data.get("status") == "awaiting_approval":
            # Real native wait has not yet been armed; no framework API is mocked.
            await asyncio.sleep(3)
        return await original(run_id, event_type, **data)

    monkeypatch.setattr(engine, "send_event", delayed)
    run = native_case.create(approval=True)
    c = native_case
    paused = c.wait(run, lambda r: r["status"] == "awaiting_approval")
    run_id = run["run_id"]
    c.client.approve(run_id, "alice", paused["plan"]["version"], "approve")
    result = c.wait(run, lambda r: r["archive_status"] == "saved")
    assert result["status"] == "cancelled" and result["settlement"] == "settled"
    assert not c.runtimes and not result["plan"]["locked"]
    assert c.client.client.get_event(run_id, "approval_end", 0) == "expired"
    with pytest.raises(RunConflict):
        c.client.approve(run_id, "alice", paused["plan"]["version"], "approve")


def test_retry_after_failed_result_publication_keeps_committed_model(
    native_case, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    from open_deep_research.workflow import dbos_workflow as engine

    original = engine._publish_stage
    failures = []

    def publish(scope, artifact):
        if scope.name == "plan_research" and len(failures) < 3:
            failures.append(scope.stage_id)
            raise OperationalError(
                "Stage publication unavailable", None, RuntimeError()
            )
        return original(scope, artifact)

    monkeypatch.setattr(engine, "_publish_stage", publish)
    case = native_case
    run = case.create()
    failed = case.wait(
        run, lambda r: r["status"] == "failed" and r["archive_status"] == "saved"
    )
    assert failures == [run["run_id"] + ":plan:1"] * 3
    workflow_id = run["run_id"] + ":planning:1"

    def steps():
        return case.client.client.list_workflow_steps(workflow_id)

    model = [s for s in steps() if s["function_name"] == "por.llm"]
    assert len(model) == 1 and model[0]["output"]
    assert any(
        s["function_name"] == "por.publish_stage" and s["error"] for s in steps()
    )
    retry_observed_failure(case, run, "result-accounting", failed)
    done = case.wait(
        run, lambda r: r["status"] == "completed" and r["archive_status"] == "saved"
    )
    assert done["generation"] == 1
    assert done["usage"]["model_calls"] == 18
    assert done["usage"]["total_tokens"] == 203
    assert len(case.plan_prompts) == 1
    assert [s for s in steps() if s["function_name"] == "por.llm"] == model


def test_committed_analysis_publication_ack_loss_preserves_usage(
    native_case, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    from open_deep_research.workflow import dbos_workflow as engine

    original = engine._publish_stage
    lost = []

    def publish(scope, artifact):
        original(scope, artifact)
        if scope.name == "response_strategy_agent" and not lost:
            lost.append(scope.stage_id)
            raise OperationalError(
                "Synthetic acknowledgement loss", None, Exception("after commit")
            )

    monkeypatch.setattr(engine, "_publish_stage", publish)
    run = native_case.create()
    result = native_case.wait(run)
    assert lost
    assert result["status"] == "completed"
    assert result["usage"]["model_calls"] == 18
    assert result["usage"]["total_tokens"] == 203
    assert len(native_case.models) == 2
    native_case.wait(run, lambda r: r["archive_status"] == "saved")
    events = native_case.client.read_events(run["run_id"], "alice", 0, 1000)
    assert (
        sum(
            e["type"] == "agent_report" and e.get("role") == "response_strategy"
            for e in events
        )
        == 1
    )


def test_tool_control_storage_errors_are_fatal_not_model_observations(
    native_case, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    from open_deep_research.runtime import tool_executor

    def fail(*args):
        raise OperationalError(
            "Synthetic admission outage", None, Exception("unavailable")
        )

    monkeypatch.setattr(tool_executor, "tool_effect", lambda tool: "protected")
    monkeypatch.setattr(tool_executor, "start_operation", fail)
    run = native_case.create()
    result = native_case.wait(run)
    assert result["status"] == "failed"
    assert result["error"]["kind"] == "execution_control_error"
    assert not result["report"]
    assert not native_case.calls
    assert len(native_case.models) == 2
    assert all(rt.budget_usage["model_calls"] == 1 for rt in native_case.runtimes)


def test_cancel_during_safe_retry_preserves_inflight_model_bill(
    native_case, monkeypatch
):
    import threading

    from sdk_model_support import Reply, install_inference

    from open_deep_research.llm.generation import generate_text
    from open_deep_research.workflow import dbos_workflow as engine

    entered, release = threading.Event(), threading.Event()
    attempts = []

    async def infer(request):
        if request.model_ref != "openai:blocked-review":
            return None
        entered.set()
        await asyncio.to_thread(release.wait, 20)
        return Reply("review", 0, 0)

    install_inference(monkeypatch, infer)

    async def review(state, config):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Safe review failure")
        await generate_text("openai:blocked-review", "review")

    monkeypatch.setattr(engine.research, "research_review", review)
    case = native_case
    run = case.create()
    failed = case.wait(
        run, lambda r: r["status"] == "failed" and r["archive_status"] == "saved"
    )
    assert failed["retry_safe"]
    retry_observed_failure(case, run, "retry", failed)
    try:
        assert entered.wait(20)
        assert has_unconfirmed_bills_or_effects(run["run_id"])
        case.client.cancel(run["run_id"], "alice")
        case.client.cancel(run["run_id"], "alice")
    finally:
        release.set()
    result = case.wait(
        run, lambda r: r["generation"] == 1 and r["archive_status"] == "saved"
    )
    assert result["status"] == "cancelled" and not result["retry_safe"]
    assert result["settlement"] == "settled"
    assert result["usage"]["model_calls"] == failed["usage"]["model_calls"] + 1
    assert len(case.models) == 2


def test_native_workflow_matches_frozen_business_reference(
    native_case, monkeypatch, tmp_path
):
    """Compare joined research and synthesis using the same Brief and model costs."""
    from reference_workflow import PythonWorkflow
    from test_python_workflow_sdk import install_sdk

    from open_deep_research.budget import merge_budget_usage
    from open_deep_research.workflow.brief import brief_metadata

    case = native_case
    run = case.create()
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    native = case.client.client.list_workflows(
        workflow_ids=[run["run_id"]], load_input=False, load_output=True
    )[0].output
    with monkeypatch.context() as patch:
        state, cfg, *_ = install_sdk(patch, tmp_path / "reference")
        state["workflow"]["brief"] = result["plan"]["brief"]
        cfg["configurable"]["research_run_id"] = run["run_id"]
        # The oracle starts at unchanged research ownership, after the new Plan.
        # Account exactly the native planning artifacts rather than forging zero cost.
        for unit in stage_observations(run["run_id"]):
            if unit["name"] == "plan_research":
                state["runtime"]["budget"] = merge_budget_usage(
                    state["runtime"]["budget"], unit["usage"]
                )
        reference = asyncio.run(
            PythonWorkflow().ainvoke(state, cfg, start_at="research")
        )
    meta = brief_metadata(result["plan"]["brief"], 1, run["run_id"])
    assert native["workflow"] == dict(
        reference["workflow"],
        brief_version=meta["version"],
        brief_hash=meta["content_hash"],
        brief_revision=1,
    )
    assert native["report"] == reference["report"]
    assert native["runtime"]["budget"] == reference["runtime"]["budget"]
    for role in (
        "public_signal",
        "internal_knowledge",
        "risk_assessment",
        "response_strategy",
    ):
        assert native["agents"][role] == reference["agents"][role]


def test_message_identity_generation_stays_outside_workflow_replay(
    native_case, monkeypatch
):
    from dbos import DBOS

    from open_deep_research.workflow import python_state

    original = python_state.uuid4
    observed = []

    def identity():
        in_workflow = DBOS.workflow_id is not None
        in_step = DBOS.step_id is not None
        assert not in_workflow or in_step, (
            "Random message ID generated by Workflow replay"
        )
        observed.append((in_workflow, in_step))
        return original()

    monkeypatch.setattr(python_state, "uuid4", identity)
    case = native_case
    case.state["messages"][0].id = None
    run = case.create()
    result = case.wait(run)
    assert result["status"] == "completed", result["error"]
    assert (False, False) in observed
    assert (True, True) in observed
    final = case.client.client.list_workflows(
        workflow_ids=[run["run_id"]], load_input=False, load_output=True
    )[0].output
    assert all(message.id for message in final["messages"])


def test_operator_approval_cannot_be_bypassed_or_replanned(native_case, monkeypatch):
    case = native_case
    monkeypatch.setenv("ALLOW_PLAN_FEEDBACK", "true")
    run = case.create(approval=False)
    paused = case.wait(run, lambda r: r["status"] == "awaiting_approval")
    assert not case.models
    monkeypatch.setenv("ALLOW_PLAN_FEEDBACK", "false")
    case.client.approve(run["run_id"], "alice", paused["plan"]["version"], "approve")
    result = case.wait(run)
    assert result["status"] == "completed"
    assert result["usage"]["model_calls"] == 18
    assert result["plan"] == dict(paused["plan"], locked=True)


def test_paused_runs_do_not_starve_another_runs_archive(native_case):
    case = native_case
    runs = [case.create(key="paused-" + str(i), approval=True) for i in range(21)]
    for run in runs:
        case.wait(run, lambda r: r["status"] == "awaiting_approval")
    last = runs[-1]
    plan = case.client.get(last["run_id"], "alice")["plan"]
    case.client.approve(last["run_id"], "alice", plan["version"], "approve")
    saved = case.wait(last, lambda r: r["archive_status"] == "saved")
    assert saved["status"] == "completed"
    assert len(case.models) == 2
    assert all(
        case.client.get(r["run_id"], "alice")["status"] == "awaiting_approval"
        for r in runs[:-1]
    )


def test_distinct_retry_keys_cannot_reaccept_one_pending_outcome(
    native_case, monkeypatch
):
    import threading

    from open_deep_research.workflow import dbos_workflow as engine

    case = native_case
    original = engine.research.research_review
    entered, release = threading.Event(), threading.Event()
    attempts = []

    async def review(state, cfg):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Safe read failed")
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.02)
        return await original(state, cfg)

    monkeypatch.setattr(engine.research, "research_review", review)
    run = case.create()
    failed = case.wait(
        run, lambda r: r["status"] == "failed" and r["archive_status"] == "saved"
    )
    retry_observed_failure(case, run, "first", failed)
    try:
        assert entered.wait(15)
        observed = case.client.get(run["run_id"], "alice")
        with pytest.raises(RunConflict):
            case.client.retry(
                run["run_id"], "alice", "second", observed["state_version"]
            )
    finally:
        release.set()
    result = case.wait(run, lambda r: r["status"] == "completed")
    assert result["generation"] == 1
    assert len(attempts) == 2
    assert len(case.models) == 2


def test_native_vision_attempt_and_usage_are_settled_once(native_case, monkeypatch):
    from langchain_core.messages import HumanMessage
    from sdk_model_support import install_inference
    from test_query_images import _data_url
    from test_vision_budget import VisionModel


    case = native_case
    model = VisionModel()
    install_inference(monkeypatch, model)
    case.state["messages"] = [
        HumanMessage(
            content=[
                {"type": "text", "text": "brand recall"},
                {"type": "image_url", "image_url": {"url": _data_url()}},
            ]
        )
    ]
    case.cfg["configurable"].update(
        rag_query_image_enabled=True, rag_vision_model="openai:vision-fixture"
    )
    run = case.create()
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    assert result["status"] == "completed", result["error"]
    assert result["usage"]["model_calls"] == 19
    assert result["usage"]["total_tokens"] == 218
    unit = next(
        u
        for u in stage_observations(run["run_id"])
        if u["id"] == run["run_id"] + ":enrich_query_images"
    )
    assert unit["settled"] and unit["artifact"]
    assert unit["usage"]["model_calls"] == 1
    assert unit["usage"]["total_tokens"] == 15
    finished = next(
        e
        for e in case.client.read_events(run["run_id"], "alice", 0, 1000)
        if e["type"] == "step_completed" and e["unit_id"] == unit["id"]
    )
    assert "runtime" not in finished and "patch" not in finished
    plans = case.client.client.list_workflows(
        workflow_id_prefix=run["run_id"],
        name="por.plan",
        load_input=False,
        load_output=False,
    )
    outputs = [
        step["output"]
        for p in plans
        for step in case.client.client.list_workflow_steps(p.workflow_id)
        if step["function_name"] == "por.stage_result"
    ]
    assert any(
        value.get("patch", {}).get("runtime", {}).get("budget") == unit["usage"]
        for value in outputs
    )
    assert case.client.get(run["run_id"], "alice")["usage"] == result["usage"]
    assert len(model.requests) == 1


@pytest.mark.parametrize("provider_error", [False, True])
def test_native_cancelled_vision_keeps_reservation_until_actual_usage_settles(
    native_case, monkeypatch, provider_error
):
    import threading

    from langchain_core.messages import HumanMessage
    from sdk_model_support import install_inference
    from test_query_images import _data_url
    from test_vision_budget import BlockingVision

    from open_deep_research.workflow import intake

    case = native_case
    owners = []
    original_io = intake.owned_io

    async def record_owner(*args, **kwargs):
        owners.append(asyncio.current_task())
        return await original_io(*args, **kwargs)

    monkeypatch.setattr(intake, "owned_io", record_owner)
    model = BlockingVision(
        started=threading.Event(),
        release=threading.Event(),
        finished=threading.Event(),
        error=provider_error,
        wait_seconds=20,
    )
    install_inference(monkeypatch, model)
    case.state["messages"] = [
        HumanMessage(
            content=[
                {"type": "image_url", "image_url": {"url": _data_url()}},
                {"type": "image_url", "image_url": {"url": _data_url()}},
            ]
        )
    ]
    case.cfg["configurable"].update(
        rag_query_image_enabled=True,
        rag_vision_model="openai:vision-fixture",
        workflow_step_timeout_seconds=10,
    )
    baseline = dict(case.state["runtime"]["budget"])
    run = case.create()
    try:
        assert model.started.wait(3)
        case.client.cancel(run["run_id"], "alice")
        # The public cancel intent precedes DBOS's asynchronous local delivery.
        # Observe that delivery without replacing the native cancellation path.
        deadline = time.monotonic() + 10
        while (
            not any(owner.cancelling() for owner in owners)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert any(owner.cancelling() for owner in owners)
        pending = case.client.get(run["run_id"], "alice")
        assert pending["status"] == "cancelling"
        assert pending["settlement"] == "pending"
        unit = next(
            u
            for u in stage_observations(run["run_id"])
            if u["name"] == "enrich_query_images"
        )
        assert unit["settled"] is None and unit["artifact"] is None
    finally:
        model.release.set()
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    assert result["status"] == "cancelled"
    assert result["settlement"] == "settled"
    assert result["usage"]["model_calls"] == baseline["model_calls"] + 1
    for field, delta in (
        ("input_tokens", 12),
        ("output_tokens", 3),
        ("total_tokens", 15),
    ):
        assert result["usage"][field] == baseline[field] + (
            0 if provider_error else delta
        )
    unit = next(
        u
        for u in stage_observations(run["run_id"])
        if u["name"] == "enrich_query_images"
    )
    assert unit["usage"]["model_calls"] == 1 and not has_unconfirmed_bills_or_effects(
        run["run_id"]
    )
    assert unit["usage"]["total_tokens"] == (0 if provider_error else 15)
    assert model.finished.is_set() and len(model.requests) == 1
    assert len(case.models) == 0 and not any(case.calls.values())
    assert case.client.get(run["run_id"], "alice")["usage"] == result["usage"]


@pytest.mark.parametrize("slower", ["public_signal", "internal_knowledge"])
def test_research_results_are_joined_after_both_native_children(
    native_case, monkeypatch, slower
):
    from copy import deepcopy
    from threading import Event

    from open_deep_research.workflow import dbos_workflow as engine

    case = native_case
    original = engine._publish_stage
    fast_committed = Event()
    order = []
    joined = []
    original_assemble = engine.assemble_research_results

    def assemble(state, results, packets):
        original_assemble(state, results, packets)
        joined.append(deepcopy(state))

    monkeypatch.setattr(engine, "assemble_research_results", assemble)

    def finish(scope, artifact):
        value = artifact.get("research_result")
        if value and value["role"] == slower:
            assert fast_committed.wait(10), "Native role children must remain parallel"
        result = original(scope, artifact)
        if value:
            order.append(value["role"])
            if value["role"] != slower:
                fast_committed.set()
        return result

    monkeypatch.setattr(engine, "_publish_stage", finish)
    run = case.create()
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    assert result["status"] == "completed", result["error"]
    assert order == [
        r for r in ("public_signal", "internal_knowledge") if r != slower
    ] + [slower]
    output = case.client.client.list_workflows(
        workflow_ids=[run["run_id"]], load_input=False, load_output=True
    )[0].output
    # DBOS JSON persistence sorts object keys. Observe the actual join before
    # serialization, then compare persisted values rather than JSON key order.
    assert len(joined) == 1
    assert set(joined[0]["agents"]) == {"public_signal", "internal_knowledge"}
    for role in ("public_signal", "internal_knowledge"):
        assert output["agents"][role] == joined[0]["agents"][role]
    assert result["usage"]["model_calls"] == 18
    assert len(result["role_reports"]) == 4
    assert len(case.runtimes) == 2
