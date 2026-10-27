"""Native PostgreSQL/Neo4j tracing, with the original controlled SDK fixtures."""

import threading
import time
from uuid import uuid4

import pytest
from agents.testing import ScriptedModel
from dbos import DBOS
from dbos_support import NativeCase
from opentelemetry.trace import StatusCode
from sqlalchemy import text

from open_deep_research.observability.telemetry import dbos_scope
from open_deep_research.workflow import dbos_workflow as engine
from open_deep_research.workflow.run_facts import facts

pytestmark = pytest.mark.integration

_step_attempts = {}


@DBOS.step(
    name="por.test.trace_step",
    retries_allowed=True,
    max_attempts=2,
    interval_seconds=0.01,
)
async def native_retry_step(run_id):
    _step_attempts[run_id] = _step_attempts.get(run_id, 0) + 1
    if _step_attempts[run_id] == 1:
        raise RuntimeError("secret-native-retry-error")
    return _step_attempts[run_id]


@DBOS.workflow(name="por.test.trace_retry")
@dbos_scope
async def native_retry_workflow(run_id):
    return await native_retry_step(run_id)


@pytest.fixture
def traced_case(trace_capture, pg_database, monkeypatch, tmp_path, request):
    import test_python_workflow_sdk as fixture

    from open_deep_research.web.executor import launch

    def traced_model(*args, **kwargs):
        return ScriptedModel(*args, **kwargs, emit_traces=True)

    monkeypatch.setattr(fixture, "ScriptedModel", traced_model)
    case = NativeCase(monkeypatch, tmp_path, getattr(request, "param", "normal"))
    launch("otel-native-" + uuid4().hex)
    try:
        yield case
    finally:
        repo = facts()
        with repo.engine.connect() as db:
            run_ids = list(
                db.execute(text("SELECT id FROM por_domain.requests")).scalars()
            )
        for run_id in run_ids:
            for workflow in repo.client.list_workflows(
                workflow_id_prefix=run_id,
                status=["PENDING", "ENQUEUED"],
                load_input=False,
                load_output=False,
            ):
                repo.client.cancel_workflow(workflow.workflow_id, cancel_children=True)
        time.sleep(0.3)
        DBOS.destroy(workflow_completion_timeout_sec=2)
        case.history.close()


def saved(case, run, status="completed", generation=0):
    return case.wait(
        run,
        lambda r: r["status"] == status
        and r["archive_status"] == "saved"
        and r["generation"] == generation
        and r["events_complete"],
        timeout=60,
    )


def test_dbos_native_step_retry_keeps_safe_native_events(traced_case, trace_capture):
    run_id = "retry-probe-" + uuid4().hex
    handle = DBOS.start_workflow(native_retry_workflow, run_id)
    assert handle.get_result() == 2
    spans = trace_capture.spans()
    steps = [s for s in spans if s.name == "por.test.trace_step"]
    assert len(steps) == 1 and steps[0].status.status_code == StatusCode.OK
    assert len(steps[0].events) == 1
    assert steps[0].events[0].name == "dbos.step.retry"
    assert steps[0].events[0].attributes == {"dbos.step.attempt": 0}
    assert steps[0].attributes["por.dbos_step_id"] == 1
    assert "secret-native-retry-error" not in steps[0].to_json()


def test_cancel_during_telemetry_read_still_settles_the_acquired_safe_unit(
    traced_case, monkeypatch
):
    import asyncio

    entered = threading.Event()
    original = asyncio.to_thread

    async def delayed(operation, *args, **kwargs):
        if getattr(operation, "__name__", "") == "read_generation":
            entered.set()
            await asyncio.Event().wait()
        return await original(operation, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", delayed)
    case = traced_case
    run = case.create()
    case.wait(run, lambda _: entered.is_set())
    case.client.cancel(run["run_id"], "alice")
    result = saved(case, run, "cancelled")
    units = facts().units(run["run_id"])
    assert units and all(unit["settled"] is True for unit in units)
    assert result["settlement"] == "settled"
    assert not case.models and not case.calls


def test_native_hierarchy_parallel_roles_usage_and_read_only_replay(
    traced_case, trace_capture
):
    case = traced_case
    run = case.create()
    result = saved(case, run)
    spans = trace_capture.spans()
    indexed = {s.context.span_id: s for s in spans}
    root = next(s for s in spans if s.name == "por.run")
    # Native parent contexts survive both DBOS queues and role workflows.
    native = [s for s in spans if s.name in {"por.plan", "por.research", "por.role"}]
    assert len(native) == 6
    assert all(s.context.trace_id == root.context.trace_id for s in native)
    roles = [s for s in native if s.name == "por.role"]
    producers = [
        s
        for s in roles
        if s.attributes["por.workflow_id"].endswith(
            (":public_signal:1", ":internal_knowledge:1")
        )
    ]
    assert len(producers) == 2
    assert producers[0].parent.span_id == producers[1].parent.span_id
    assert (
        producers[0].start_time < producers[1].end_time
        and producers[1].start_time < producers[0].end_time
    )
    tools = [s for s in spans if s.attributes.get("openinference.span.kind") == "TOOL"]
    llms = [s for s in spans if s.attributes.get("openinference.span.kind") == "LLM"]
    assert len(tools) == 2 and len(llms) == 6
    for tool in tools:
        parent = indexed[tool.parent.span_id]
        assert (
            parent.attributes["por.invocation_id"]
            == tool.attributes["por.invocation_id"]
        )
        assert tool.attributes["por.run_id"] == run["run_id"]
        assert tool.attributes["por.unit_id"].startswith(run["run_id"] + ":")
        assert (
            tool.attributes["por.execution_id"] and tool.attributes["por.operation_id"]
        )
        assert tool.attributes["por.generation"] == 0
        assert tool.attributes["por.effect_status"] == "returned"
        assert tool.attributes["por.status"] == "succeeded"
    assert {
        "research_review",
        "report_generation",
        "context_build",
        "research_graph_write",
    } <= {s.name for s in spans}
    assert result["usage"]["model_calls"] == 28
    assert case.calls == {"public_signal": 1, "internal_knowledge": 1}
    # Read/SSE projections do not create spans or repeat work.
    count = len(spans)
    for _ in range(3):
        case.client.get(run["run_id"], "alice")
        case.client.read_events(run["run_id"], "alice", 0, 1000)
    assert len(trace_capture.spans()) == count
    assert len(case.models) == 4


def test_safe_retry_distinguishes_generation_without_repeating_producers(
    traced_case, trace_capture, monkeypatch
):
    case = traced_case
    original = engine.OPERATIONS["research_review"]
    attempts = []

    async def review(state, cfg):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("secret-review-error")
        return await original(state, cfg)

    monkeypatch.setitem(engine.OPERATIONS, "research_review", review)
    run = case.create()
    first = saved(case, run, "failed")
    case.client.retry(run["run_id"], "alice", "safe-otel-retry", first["state_version"])
    final = saved(case, run, generation=1)
    spans = trace_capture.spans()
    reviews = [s for s in spans if s.name == "research_review"]
    assert {s.attributes["por.generation"] for s in reviews} == {0, 1}
    assert len({s.attributes["por.execution_id"] for s in reviews}) == 2
    assert len({s.context.trace_id for s in reviews}) == 1
    assert reviews[0].status.status_code == StatusCode.ERROR
    assert all("secret-review-error" not in s.to_json() for s in spans)
    assert final["usage"]["model_calls"] == 28
    assert len(case.models) == 4 and case.calls == {
        "public_signal": 1,
        "internal_knowledge": 1,
    }


@pytest.mark.parametrize("traced_case", ["cancel"], indirect=True)
def test_native_cancellation_keeps_settlement_and_tool_counts(
    traced_case, trace_capture
):
    case = traced_case
    run = case.create()
    case.wait(run, lambda _: case.blocked.is_set())
    case.client.cancel(run["run_id"], "alice")
    saved(case, run, "cancelled")
    spans = trace_capture.spans()
    assert any(
        s.attributes.get("por.status") == "cancelled"
        and s.status.status_code == StatusCode.ERROR
        for s in spans
    )
    assert case.calls == {"public_signal": 1, "internal_knowledge": 1}
    assert any(
        s.name == "por.role" and s.status.status_code == StatusCode.ERROR for s in spans
    )


def test_followup_rounds_keep_native_call_and_invocation_identity(
    traced_case, trace_capture, monkeypatch
):
    from test_python_workflow import gap

    case = traced_case
    original = engine.OPERATIONS["research_review"]

    async def review(state, cfg):
        result = await original(state, cfg)
        if state["workflow"]["round"] == 1:
            result["workflow"]["review"] = {
                "research_complete": False,
                "next_tasks": [gap("date"), gap("approval", "internal_knowledge")],
            }
        return result

    monkeypatch.setitem(engine.OPERATIONS, "research_review", review)
    run = case.create()
    saved(case, run)
    spans = trace_capture.spans()
    tools = [s for s in spans if s.attributes.get("openinference.span.kind") == "TOOL"]
    assert len(tools) == 4
    assert {s.attributes["por.research_round"] for s in tools} == {1, 2}
    assert (
        len(
            {
                (s.attributes["por.invocation_id"], s.attributes["por.tool_call_id"])
                for s in tools
            }
        )
        == 4
    )
    assert len({s.attributes["por.graph_call_id"] for s in tools}) == 4
    assert case.calls == {"public_signal": 2, "internal_knowledge": 2}


@pytest.mark.parametrize("scenario", ["checkpoint", "lost-receipt"])
def test_actual_process_recovery_joins_persisted_context_and_never_replays_effects(
    trace_capture,
    pg_database,
    otlp_receiver,
    monkeypatch,
    tmp_path,
    scenario,
):
    from test_dbos_process import configuration, effect_count, kill_owned, start, wait
    from test_python_workflow import initial

    from open_deep_research.web.durable import RunClient

    monkeypatch.setenv("LANGFUSE_BASE_URL", otlp_receiver.url)
    monkeypatch.setenv("LANGFUSE_FLUSH_INTERVAL", "0.05")
    client = RunClient()
    executor = "otel-fault-" + uuid4().hex
    process = start(tmp_path, executor, scenario)
    restarted = None
    try:
        wait(lambda: (tmp_path / "executor-ready").exists(), process, tmp_path)
        run = client.create(
            "alice",
            "trace-kill",
            {"topic": "brand recall"},
            configuration(tmp_path),
            initial(),
        )[0]
        root_trace = next(
            s.context.trace_id for s in trace_capture.spans() if s.name == "run_submit"
        )
        wait(lambda: (tmp_path / scenario).exists(), process, tmp_path)
        wait(lambda: bool(otlp_receiver.spans), process, tmp_path)
        kill_owned(process)
        before = effect_count(tmp_path)
        assert before >= 1
        if scenario == "checkpoint":
            assert before == 2
        old_span_ids = {bytes(s.span_id) for s in otlp_receiver.spans}
        (tmp_path / (scenario + ".release")).write_text("continue")
        (tmp_path / "executor-ready").unlink()
        restarted = start(tmp_path, executor, scenario)
        expected = "completed" if scenario == "checkpoint" else "interrupted"
        wait(
            lambda: client.get(run["run_id"], "alice")["status"] == expected,
            restarted,
            tmp_path,
        )
        wait(
            lambda: any(
                bytes(s.span_id) not in old_span_ids and s.name == "por.run"
                for s in otlp_receiver.spans
            ),
            restarted,
            tmp_path,
        )
        new = [s for s in otlp_receiver.spans if bytes(s.span_id) not in old_span_ids]
        root = next(s for s in new if s.name == "por.run")
        assert int.from_bytes(root.trace_id, "big") == root_trace
        attrs = {a.key: a.value.string_value for a in root.attributes}
        assert attrs["por.run_id"] == run["run_id"]
        assert attrs["por.workflow_id"] == run["run_id"]
        assert all(
            h.get("x-langfuse-ingestion-version") == "4" for h in otlp_receiver.headers
        )
        assert effect_count(tmp_path) == effect_count(tmp_path, "effects") == before
        result = client.get(run["run_id"], "alice")
        if scenario == "checkpoint":
            assert result["usage"]["model_calls"] == 28
        else:
            assert result["settlement"] == "unresolved" and not result["retry_safe"]
    finally:
        for child in (process, restarted):
            if child is not None and child.poll() is None:
                kill_owned(child)
