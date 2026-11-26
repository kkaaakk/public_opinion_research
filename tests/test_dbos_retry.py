"""Retry grants use real native execution and public terminal delivery."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import pytest
from dbos_support import (
    has_unconfirmed_bills_or_effects,
    record_scripted_model_attempt,
    retry_observed_failure,
)
from sqlalchemy import text

from open_deep_research.web.run_data import (
    RunConflict,
    finalizer_id,
    publish_event,
    read_request,
)
from open_deep_research.workflow import dbos_workflow as engine
from open_deep_research.workflow.dbos_config import domain_engine

pytestmark = pytest.mark.integration


def test_terminal_delivery_drains_reordered_committed_messages(
    native_case, monkeypatch
):
    """A terminal envelope can arrive first when independent sends race."""
    from dbos import DBOS

    case = native_case
    original_review, original_send = engine.research.research_review, engine.send_event
    attempts = []

    async def review(state, cfg):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Safe read failure")
        return await original_review(state, cfg)

    def enqueue_terminal_first(run_id, data):
        with domain_engine().begin() as db:
            publish_event(
                db, run_id, "_terminal", idempotency_key="terminal-first", **data
            )
            publish_event(
                db,
                run_id,
                "status",
                idempotency_key="committed-tail",
                generation=1,
                message="Producer tail",
            )

    async def send(run_id, event_type, **data):
        if event_type == "_terminal" and data["generation"] == 1:
            assert (
                next(frame for frame in data["frames"] if frame["type"] == "usage")[
                    "model_calls"
                ]
                > 0
            )
            await DBOS.run_step_async(None, enqueue_terminal_first, run_id, data)
        else:
            await original_send(run_id, event_type, **data)

    monkeypatch.setattr(engine.research, "research_review", review)
    monkeypatch.setattr(engine, "send_event", send)
    run = case.create()
    failed = wait_delivered(case, run, "failed", 0)
    case.client.retry(
        run["run_id"], "alice", "reordered-terminal", failed["state_version"]
    )
    completed = wait_delivered(case, run, "completed", 1)
    events = assert_consistent(case, run, completed)
    terminal = [e for e in events if e["generation"] == 1]
    tail = next(e for e in terminal if e.get("message") == "Producer tail")
    usage = next(e for e in terminal if e["type"] == "usage")
    assert tail["seq"] < usage["seq"] < events[-1]["seq"]
    assert len(attempts) == 2 and len(case.models) == 2


def wait_delivered(case, run, status, generation):
    return case.wait(
        run,
        lambda r: r["status"] == status
        and r["generation"] == generation
        and not r["retry_requested"]
        and r["events_complete"],
    )


def command_for(run_id, key):
    with domain_engine().connect() as db:
        row = db.execute(
            text(
                "SELECT version,generation FROM por_domain.retries WHERE run_id=:id AND key=:k"
            ),
            {"id": run_id, "k": key},
        ).one()
    return {
        "key": key,
        "version": row[0],
        "generation": row[1],
    }


def assert_consistent(case, run, result):
    run_id = run["run_id"]
    assert_history_status(case.client, run_id, result["status"])
    events = case.client.read_events(run_id, "alice", 0, 10000)
    assert result["events_complete"]
    assert events[-1]["type"] == "done"
    assert events[-1]["generation"] == result["generation"]
    assert events[-1]["status"] == result["status"]
    assert [e["generation"] for e in events if e["type"] == "done"] == list(
        range(result["generation"] + 1)
    )
    usage = next(e for e in reversed(events) if e["type"] == "usage")
    assert usage["generation"] == result["generation"]
    for key in ("model_calls", "input_tokens", "output_tokens", "total_tokens"):
        assert usage[key] == result["usage"].get(key, 0)
    return events


def assert_history_status(client, run_id, status):
    items = client.list_history("alice", limit=10, offset=0)["items"]
    assert [item["status"] for item in items if item["run_id"] == run_id] == [status]


@pytest.mark.parametrize("failure", ["model", "read", "apify", "protected"])
def test_role_recovery_reuses_native_results(native_case, monkeypatch, failure):
    from agents.exceptions import ModelBehaviorError
    from agents.mcp import MCPServerStdio
    from agents.testing import (
        ModelStep,
        ScriptedModel,
        assistant_message,
        function_call,
    )
    from fastapi.testclient import TestClient
    from mcp.types import CallToolResult, Tool
    from test_sdk_runtime import TOKENS, _tool

    from open_deep_research import budget
    from open_deep_research.mcp import tools as mcp
    from open_deep_research.runtime import business_agent, tool_executor
    from open_deep_research.web import server
    from open_deep_research.workflow.execution import stage_context, unconfirmed

    case = native_case
    failed_role = "internal_knowledge" if failure == "model" else "public_signal"
    failing_tool = {"apify": "call-actor", "protected": "publish"}.get(
        failure, "retry_read"
    )
    provider_calls = Counter()
    effects = Counter()
    receipt = {
        "runId": "ActorFixture",
        "status": "SUCCEEDED",
        "storages": {
            "datasets": {"default": {"id": "DatasetFixture", "itemCount": 49}}
        },
    }
    lost_bill = []
    prefix_committed = asyncio.Event()
    tail_committed = asyncio.Event()

    class Apify(MCPServerStdio):
        async def __aenter__(self):
            self.launched = False
            return self

        async def __aexit__(self, *_):
            if self.launched:
                await asyncio.wait_for(prefix_committed.wait(), 10)
                raise RuntimeError("Injected disconnect after the Actor run receipt")

        async def list_tools(self):
            return [
                Tool(name=name, inputSchema={"type": "object"})
                for name in ("call-actor", "get-actor-run", "get-dataset-items")
            ]

        async def call_tool(self, name, arguments):
            effects[name] += 1
            if name == "call-actor":
                self.launched = True
            elif name == "get-actor-run":
                assert arguments == {"runId": "ActorFixture"}
            else:
                assert (
                    name == "get-dataset-items"
                    and arguments["datasetId"] == "DatasetFixture"
                )
                return CallToolResult(
                    content=[], structuredContent={"items": [{"evidence": "recall"}]}
                )
            return CallToolResult(content=[], structuredContent=receipt)

    async def tool_catalog(config, role, spec):
        async def execute(query, limit):
            effects[query] += 1
            return f"Evidence for {query}"

        tools = [_tool(role, coroutine=execute, effect="read")]
        if role != failed_role or failure == "model":
            return tools
        if failure == "apify":
            spec = mcp.MCPServerSpec(
                "apify",
                "social_media",
                "protected",
                "streamable_http",
                {"url": "https://mcp.apify.com"},
            )
            remote = await mcp._load_server_tools(spec, {tool.name for tool in tools})
            assert {tool.name: tool.effect for tool in remote} == {
                "call-actor": "protected",
                "get-actor-run": "read",
                "get-dataset-items": "read",
            }
            return tools + remote
        return tools + [
            _tool(failed_role, name=failing_tool, coroutine=execute, effect=failure)
        ]

    async def respond(call):
        scope = stage_context.get()
        role = scope.name.removesuffix("_agent")
        name = "web_search" if role == "public_signal" else "rag_search"
        outputs = {
            item.get("call_id")
            for item in call.input
            if item.get("type") == "function_call_output"
        }
        if not outputs:
            provider_calls[(role, "first")] += 1
            calls = [function_call(name, {"query": role}, call_id="first")]
            if role == failed_role and failure != "model":
                args = (
                    {"actor": "fixture"} if failure == "apify" else {"query": "retry"}
                )
                calls += [
                    function_call(failing_tool, args, call_id="failed"),
                    function_call(name, {"query": "tail"}, call_id="tail"),
                ]
            return calls
        if failure == "apify" and role == failed_role and "dataset" not in outputs:
            assert "DatasetFixture" in json.dumps(call.input)
            provider_calls[(role, "dataset")] += 1
            return [
                function_call(
                    "get-dataset-items",
                    {"datasetId": "DatasetFixture"},
                    call_id="dataset",
                )
            ]
        provider_calls[(role, "finish")] += 1
        if failure == "model" and role == failed_role and scope.generation == 0:
            raise ModelBehaviorError("Injected provider failure")
        return [assistant_message("producer analysis complete")]

    @asynccontextmanager
    async def model(*_args, **_kwargs):
        yield ScriptedModel([ModelStep.respond(respond)] * 3, default_usage=TOKENS)

    original_finish = budget.finish_model_attempt

    def finish_model(run_id, call_id, usage):
        if failure == "model" and usage.get("usage_complete") is False:
            lost_bill.append(call_id)
            return
        original_finish(run_id, call_id, usage)

    original_perform = tool_executor.ToolExecutor._perform

    async def perform(executor, call):
        result = await original_perform(executor, call)
        if failure in {"read", "protected"} and call.record.tool_call_id == "failed":
            if stage_context.get().generation == 0:
                await asyncio.wait_for(prefix_committed.wait(), 10)
                if failure == "read":
                    await asyncio.wait_for(tail_committed.wait(), 10)
                raise unconfirmed("Injected loss of the tool checkpoint")
        return result

    original_step = tool_executor.ToolExecutor._step
    original_send = engine.send_event

    async def step(executor, call):
        result = await original_step(executor, call)
        if executor.role == failed_role and stage_context.get().generation == 0:
            if call.record.tool_call_id == "first":
                prefix_committed.set()
            if call.record.tool_call_id == "tail":
                tail_committed.set()
        return result

    async def send(run_id, event_type, **data):
        if failure == "read" and event_type == "_terminal" and data["generation"] == 0:
            data["close"] = True
        await original_send(run_id, event_type, **data)

    monkeypatch.setattr(business_agent, "_business_agent_tools", tool_catalog)
    monkeypatch.setattr(business_agent, "sdk_model", model)
    monkeypatch.setattr(
        mcp.MCPServerSpec,
        "create",
        lambda self: Apify({"command": sys.executable}, name="apify"),
    )
    monkeypatch.setattr(budget, "finish_model_attempt", finish_model)
    monkeypatch.setattr(tool_executor.ToolExecutor, "_perform", perform)
    monkeypatch.setattr(tool_executor.ToolExecutor, "_step", step)
    monkeypatch.setattr(engine, "send_event", send)
    monkeypatch.setattr(server, "WEB_API_TOKEN", "recovery-test")
    case.cfg["configurable"].update(
        max_input_tokens=1_000_000, max_output_tokens=100_000
    )
    run = case.create(owner="operator", approval=True)
    run_id = run["run_id"]
    frozen = read_request(run_id, "operator")
    paused = case.wait(run, lambda r: r["can_approve"])
    case.client.approve(run_id, "operator", paused["plan"]["version"], "approve")
    first = wait_delivered(
        case, run, "interrupted" if failure == "apify" else "failed", 0
    )
    role_id = f"{run_id}:{failed_role}:1"
    steps = case.client.client.list_workflow_steps(role_id)
    broken = next(step for step in steps if step["error"] is not None)
    assert broken["function_name"] == (
        "_model_call_step" if failure == "model" else "por.agent_tool"
    )
    if failure == "read":
        case.wait(
            run, lambda _: case.client.native(run_id + ":events").status == "SUCCESS"
        )
        assert case.client.native(finalizer_id(run_id)).status == "SUCCESS"
        tail = next(
            s
            for s in steps
            if s.get("output")
            and isinstance(s["output"], dict)
            and s["output"].get("record", {}).get("tool_call_id") == "tail"
        )
        assert tail["function_id"] > broken["function_id"] and effects["tail"] == 1
    if failure == "model":
        assert lost_bill and first["usage"]["usage_complete"] is False
    url = "/api/runs/" + run_id
    headers = {
        "Authorization": "Bearer recovery-test",
        "Idempotency-Key": "resume-role",
    }
    http = TestClient(server.app)
    response = http.post(
        url + "/retry", json={"state_version": first["state_version"]}, headers=headers
    )
    if failure == "protected":
        assert (
            not first["retry_safe"]
            and "Protected tool 'publish'" in first["retry_reason"]
        )
        assert (
            response.status_code == 409
            and response.json()["detail"] == first["retry_reason"]
        )
        assert effects["retry"] == 1 and case.client.native(role_id).status == "ERROR"
        return

    assert first["retry_safe"] and first["retry_reason"] is None
    assert response.status_code == 200, response.text
    grant = command_for(run_id, "resume-role")
    assert grant["generation"] == 1
    finished = wait_delivered(case, run, "completed", 1)
    assert read_request(run_id, "operator") == frozen
    assert (finished["run_id"], finished["research_session_id"], finished["plan"]) == (
        run_id,
        run["research_session_id"],
        first["plan"],
    )
    after = case.client.client.list_workflow_steps(role_id)
    assert after[: steps.index(broken)] == steps[: steps.index(broken)]
    assert after[steps.index(broken)]["error"] is None
    assert all(
        provider_calls[(role, "first")] == 1 and effects[role] == 1
        for role in ("public_signal", "internal_knowledge")
    )
    assert (
        len([runtime for runtime in case.runtimes if runtime.role != failed_role]) == 1
    )
    if failure == "apify":
        assert (
            effects["call-actor"]
            == effects["get-actor-run"]
            == effects["get-dataset-items"]
            == 1
        )
    elif failure == "read":
        assert effects["retry"] == effects["tail"] == 2
        assert finished["usage"]["tool_calls"] == 4
        assert any(
            "provider may have received it twice" in w
            for w in finished["usage"]["budget_warnings"]
        )
    else:
        assert provider_calls[(failed_role, "finish")] == 2
        assert finished["usage"]["usage_complete"] is False
    assert finished["report"]
    sse = http.get(
        url + "/events",
        headers={**headers, "Last-Event-ID": str(first["event_cursor"])},
    )
    assert sse.status_code == 200
    assert sse.text.count('"type": "done"') == 1
    assert '"status": "completed"' in sse.text
    assert '"type": "sdk_event"' in sse.text


def test_old_retry_key_never_grants_a_later_failure_and_new_key_can_retry(
    native_case, monkeypatch
):
    """Promote the final-review reproducer, including in-flight and success ACKs."""
    case = native_case
    original = engine.research.research_review
    entered = {i: threading.Event() for i in (2, 3)}
    release = {i: threading.Event() for i in (2, 3)}
    attempts = []

    async def review(state, cfg):
        attempt = len(attempts) + 1
        attempts.append(attempt)
        if attempt > 1:
            entered[attempt].set()
            while not release[attempt].is_set():
                await asyncio.sleep(0.02)
        if attempt <= 2:
            await record_scripted_model_attempt()
            raise RuntimeError("Synthetic settled safe read failure")
        return await original(state, cfg)

    monkeypatch.setattr(engine.research, "research_review", review)
    run = case.create()
    first = wait_delivered(case, run, "failed", 0)
    assert_consistent(case, run, first)
    key = "original-review-retry"
    try:
        case.client.retry(run["run_id"], "alice", key, first["state_version"])
        assert entered[2].wait(15)
        assert case.client.get(run["run_id"], "alice")["retry_requested"]
        for _ in range(3):
            current = case.client.retry(
                run["run_id"], "alice", key, first["state_version"]
            )
            assert current["generation"] == 1
            assert not current["events_complete"]
        with pytest.raises(RunConflict):
            case.client.retry(
                run["run_id"],
                "alice",
                "cannot-grant-unsettled",
                current["state_version"],
            )
        release[2].set()
        second = wait_delivered(case, run, "failed", 1)
        assert_consistent(case, run, second)
        assert second["usage"]["model_calls"] == first["usage"]["model_calls"] + 1
        with pytest.raises(RunConflict):
            case.client.retry(run["run_id"], "alice", key, second["state_version"])
        native_version = case.client.native(run["run_id"]).updated_at
        for _ in range(3):
            assert (
                case.client.retry(run["run_id"], "alice", key, first["state_version"])[
                    "status"
                ]
                == "failed"
            )
        assert case.client.native(run["run_id"]).updated_at == native_version
        assert attempts == [1, 2]
        assert len(case.models) == 2
        assert case.calls == {"public_signal": 1, "internal_knowledge": 1}

        stale = command_for(run["run_id"], key)
        for i, cmd in enumerate(
            (stale, dict(stale, generation=2), dict(stale, key="forged"))
        ):
            case.client.client.send(
                run["run_id"] + ":finalize",
                cmd,
                topic="retry",
                idempotency_key="stale-" + str(i),
            )
        with pytest.raises(RunConflict):
            case.client.retry(
                run["run_id"], "alice", "new-key-old-view", first["state_version"]
            )
        case.client.retry(
            run["run_id"], "alice", "second-grant", second["state_version"]
        )
        assert entered[3].wait(15)
        current = case.client.get(run["run_id"], "alice")
        assert current["generation"] == 2 and not current["events_complete"]
        for k, v in (
            (key, first["state_version"]),
            ("second-grant", second["state_version"]),
        ):
            case.client.retry(run["run_id"], "alice", k, v)
        case.wait(
            run,
            lambda r: any(
                e["type"] == "retry_started" and e["generation"] == 2
                for e in case.client.read_events(run["run_id"], "alice", 0, 10000)
            ),
        )
        with domain_engine().begin() as db:
            for event in ("run_status", "done"):
                publish_event(
                    db,
                    run["run_id"],
                    event,
                    generation=1,
                    status="failed",
                    id="late-old-event",
                )
        release[3].set()
        completed = wait_delivered(case, run, "completed", 2)
        events = assert_consistent(case, run, completed)
        # Usage is composed from the model_attempts ledger since por-call-ledger-v1;
        # the stage-note double count this file once asserted (28) is gone.
        assert completed["usage"]["model_calls"] == 20
        assert attempts == [1, 2, 3]
        assert len(case.models) == 2
        assert case.calls == {"public_signal": 1, "internal_knowledge": 1}
        assert not any(e.get("id") == "late-old-event" for e in events)
        assert [e["generation"] for e in events if e["type"] == "retry_started"] == [
            1,
            2,
        ]
        native_version = case.client.native(run["run_id"]).updated_at
        for k, v in (
            (key, first["state_version"]),
            ("second-grant", second["state_version"]),
        ):
            assert (
                case.client.retry(run["run_id"], "alice", k, v)["status"] == "completed"
            )
        assert case.client.native(run["run_id"]).updated_at == native_version
        with pytest.raises(RunConflict):
            case.client.retry(
                run["run_id"],
                "alice",
                "completed-new-grant",
                completed["state_version"],
            )
        assert attempts == [1, 2, 3]
    finally:
        for event in release.values():
            event.set()


def test_concurrent_distinct_keys_authorize_one_native_failure(
    native_case, monkeypatch
):
    case = native_case
    original = engine.research.research_review
    attempts = []

    async def review(state, cfg):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Safe read failure")
        return await original(state, cfg)

    monkeypatch.setattr(engine.research, "research_review", review)
    run = case.create()
    failed = wait_delivered(case, run, "failed", 0)
    barrier = threading.Barrier(2)

    def submit(key):
        barrier.wait(timeout=10)
        try:
            case.client.retry(run["run_id"], "alice", key, failed["state_version"])
            return "accepted"
        except RunConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ["first", "second"]))
    assert sorted(results) == ["accepted", "conflict"]
    completed = wait_delivered(case, run, "completed", 1)
    assert_consistent(case, run, completed)
    assert len(attempts) == 2
    assert len(case.models) == 2
    with domain_engine().connect() as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM por_domain.retries WHERE run_id=:id"),
                {"id": run["run_id"]},
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize("outcome", ["cancelled", "unknown"])
def test_old_key_cannot_bypass_cancel_or_unknown_effect(
    native_case, monkeypatch, outcome
):
    case = native_case
    original = engine.research.research_review
    attempts = []
    entered = threading.Event()

    async def review(state, cfg):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Safe read failure")
        if outcome == "cancelled":
            entered.set()
            await asyncio.Event().wait()
        return await original(state, cfg)

    async def uncertain_external_write(state, cfg, **kwargs):
        from dbos import DBOS

        from open_deep_research.configuration import Configuration
        from open_deep_research.workflow.execution import (
            admit_tool_call,
            finish_operation,
            stage_context,
            start_operation,
            unconfirmed,
        )

        run_id = stage_context.get().run_id
        await asyncio.to_thread(
            admit_tool_call,
            Configuration(),
            run_id,
            DBOS.workflow_id,
            DBOS.executor_id,
            "opaque-call",
            "external_tool",
            False,
        )
        await asyncio.to_thread(
            start_operation,
            run_id,
            DBOS.workflow_id,
            DBOS.executor_id,
            "opaque-before-ack",
            "opaque-call",
            "external_tool",
            "fingerprint",
        )
        with domain_engine().begin() as db:
            db.execute(text("CREATE TABLE external_retry_effects(id text PRIMARY KEY)"))
            db.execute(text("INSERT INTO external_retry_effects VALUES ('one')"))
        await asyncio.to_thread(
            finish_operation, run_id, "opaque-before-ack", "unknown"
        )
        raise unconfirmed("Actual committed external effect, acknowledgement unknown")

    monkeypatch.setattr(engine.research, "research_review", review)
    if outcome == "unknown":
        monkeypatch.setattr(
            engine.reporting, "generate_final_report", uncertain_external_write
        )
    run = case.create()
    failed = wait_delivered(case, run, "failed", 0)
    failed = retry_observed_failure(case, run, "only-grant", failed)
    if outcome == "cancelled":
        assert entered.wait(15)
        assert case.client.get(run["run_id"], "alice")["retry_requested"]
        case.client.retry(run["run_id"], "alice", "only-grant", failed["state_version"])
        case.client.cancel(run["run_id"], "alice")
    expected = "cancelled" if outcome == "cancelled" else "interrupted"
    result = wait_delivered(case, run, expected, 1)
    native_version = case.client.native(run["run_id"]).updated_at
    for _ in range(3):
        current = case.client.retry(
            run["run_id"], "alice", "only-grant", failed["state_version"]
        )
        assert current["status"] == expected
        assert current["usage"] == result["usage"]
    assert case.client.native(run["run_id"]).updated_at == native_version
    assert len(attempts) == 2
    assert not result["retry_safe"]
    with pytest.raises(RunConflict):
        case.client.retry(run["run_id"], "alice", "unsafe-new", result["state_version"])
    if outcome == "unknown":
        assert result["settlement"] == "unresolved"
        assert has_unconfirmed_bills_or_effects(run["run_id"])
        with domain_engine().connect() as db:
            assert (
                db.execute(
                    text("SELECT count(*) FROM external_retry_effects")
                ).scalar_one()
                == 1
            )
    else:
        assert_consistent(case, run, result)


@pytest.mark.parametrize(
    "scenario", ["retry-before", "retry-between", "retry-after", "retry-error"]
)
def test_retry_command_recovers_process_death_without_reauthorizing(
    pg_database, tmp_path, scenario
):
    from uuid import uuid4

    from test_dbos_process import configuration, effect_count, kill_owned, start, wait
    from test_python_workflow import initial

    from open_deep_research.web.durable import RunClient

    client = RunClient()
    executor = "retry-fault-" + uuid4().hex
    process = start(tmp_path, executor, scenario)
    restart = None
    try:
        wait(lambda: (tmp_path / "executor-ready").exists(), process, tmp_path)
        run = client.create(
            "alice",
            "fault",
            {"topic": "brand recall"},
            configuration(tmp_path),
            initial(),
        )[0]
        wait(
            lambda: client.get(run["run_id"], "alice")["events_complete"],
            process,
            tmp_path,
        )
        first = client.get(run["run_id"], "alice")
        assert first["status"] == "failed" and first["retry_safe"]
        assert effect_count(tmp_path, "reviews") == 1
        root_failure = client.native(run["run_id"]).completed_at
        research_id = run["run_id"] + ":research"
        research_failure = client.native(research_id).completed_at
        client.retry(run["run_id"], "alice", "original", first["state_version"])
        wait(lambda: (tmp_path / scenario).exists(), process, tmp_path)
        # The command is durable and consumed, but its native application has
        # not yet acknowledged as applied; a transient result may be recorded.
        assert command_for(run["run_id"], "original")["generation"] == 1
        steps = client.client.list_workflow_steps(run["run_id"] + ":finalize")
        if scenario == "retry-error":
            assert any(
                s["function_name"].endswith("retry_execution")
                and s["output"] == {"retryable": True}
                for s in steps
            )
        else:
            assert not any(
                s["function_name"].endswith("retry_execution")
                and (s["output"] or {}).get("last")
                for s in steps
            )
        if scenario in {"retry-after", "retry-error"}:
            wait(
                lambda: effect_count(tmp_path, "reviews") == 2
                and client.native(run["run_id"]).status == "ERROR",
                process,
                tmp_path,
            )
        if scenario in {"retry-before", "retry-between"}:
            # The Finalizer waits for the rewound leaf before rewinding its parent.
            assert client.native(run["run_id"]).completed_at == root_failure
            changed = client.native(research_id).completed_at != research_failure
            assert changed == (scenario == "retry-between")
        assert effect_count(tmp_path, "reviews") == 2
        kill_owned(process)
        client.retry(run["run_id"], "alice", "original", first["state_version"])
        assert effect_count(tmp_path, "reviews") == 2
        (tmp_path / (scenario + ".release")).write_text("continue")
        (tmp_path / "executor-ready").unlink()
        restart = start(tmp_path, executor, scenario)
        wait(
            lambda: (r := client.get(run["run_id"], "alice"))["generation"] == 1
            and r["status"] == "failed"
            and not r["retry_requested"]
            and r["events_complete"],
            restart,
            tmp_path,
        )
        second = client.get(run["run_id"], "alice")
        assert effect_count(tmp_path, "reviews") == 2
        assert effect_count(tmp_path) == effect_count(tmp_path, "effects") == 2
        assert second["usage"]["model_calls"] == first["usage"]["model_calls"] + 1
        receipt = client.client.get_event(
            run["run_id"] + ":finalize", "retry_applied", timeout_seconds=0
        )
        assert receipt["generation"] == 1
        native_version = client.native(run["run_id"]).updated_at
        client.retry(run["run_id"], "alice", "original", first["state_version"])
        assert client.native(run["run_id"]).updated_at == native_version
        assert_history_status(client, run["run_id"], "failed")
        client.retry(run["run_id"], "alice", "next-generation", second["state_version"])
        wait(
            lambda: (r := client.get(run["run_id"], "alice"))["generation"] == 2
            and r["status"] == "completed"
            and r["events_complete"],
            restart,
            tmp_path,
        )
        final = client.get(run["run_id"], "alice")
        assert effect_count(tmp_path, "reviews") == 3
        assert effect_count(tmp_path) == effect_count(tmp_path, "effects") == 2
        assert final["usage"]["model_calls"] == 20
        assert final["report"]
        assert_history_status(client, run["run_id"], "completed")
        events = client.read_events(run["run_id"], "alice", 0, 10000)
        assert [e["generation"] for e in events if e["type"] == "retry_started"] == [
            1,
            2,
        ]
        assert events[-1]["type"] == "done" and events[-1]["generation"] == 2
        assert events[-1]["status"] == "completed"
        assert [e["generation"] for e in events if e["type"] == "done"] == [0, 1, 2]
    finally:
        for child in (process, restart):
            if child:
                kill_owned(child)


def test_terminal_receipt_advancement_preserves_retry_cas(native_case, monkeypatch):
    case = native_case
    original = engine.research.research_review
    attempts = []

    async def review(state, cfg):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Safe read failure")
        return await original(state, cfg)

    monkeypatch.setattr(engine.research, "research_review", review)
    run = case.create()
    observed = case.wait(
        run,
        lambda r: r["status"] == "failed"
        and r["events_complete"]
        and r["retry_safe"],
    )
    # A late duplicate usage delivery advances the real relay cursor while the
    # observed failure and its execution authority stay unchanged.
    case.client.client.send(
        run["run_id"] + ":events",
        {"type": "usage", "generation": 0, "usage": observed["usage"]},
        topic="events",
        idempotency_key="late-terminal-usage",
    )
    current = case.wait(run, lambda r: r["state_version"] > observed["state_version"])
    assert current["generation"] == observed["generation"] == 0
    assert_history_status(case.client, run["run_id"], "failed")
    with pytest.raises(RunConflict):
        case.client.retry(run["run_id"], "alice", "fresh", observed["state_version"])
    retry_observed_failure(case, run, "fresh", observed)
    final = wait_delivered(case, run, "completed", 1)
    assert final["generation"] == 1
    assert len(attempts) == 2 and len(case.models) == 2
    assert case.calls == {"public_signal": 1, "internal_knowledge": 1}


def test_recovery_does_not_wait_for_terminal_sse_delivery(native_case, monkeypatch):
    from dbos import DBOS

    case = native_case
    original_review = engine.research.research_review
    original_write = DBOS.write_stream_async
    entered, release = threading.Event(), threading.Event()
    attempts = []

    async def review(state, cfg):
        attempts.append(1)
        if len(attempts) <= 2:
            raise RuntimeError("Safe read failure")
        return await original_review(state, cfg)

    async def delayed_event(key, event):
        # Delay real terminal stream delivery, never fake a native status.
        if event["type"] == "done" and event.get("generation") == 1:
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.02)
        return await original_write(key, event)

    monkeypatch.setattr(engine.research, "research_review", review)
    monkeypatch.setattr(DBOS, "write_stream_async", staticmethod(delayed_event))
    run = case.create()
    first = wait_delivered(case, run, "failed", 0)
    case.client.retry(run["run_id"], "alice", "first", first["state_version"])
    try:
        assert entered.wait(15)
        second = case.client.get(run["run_id"], "alice")
        assert second["status"] == "failed" and second["generation"] == 1
        assert not second["events_complete"] and second["retry_safe"]
        accepted = case.client.retry(
            run["run_id"], "alice", "second", second["state_version"]
        )
        assert accepted["generation"] == 2
        assert (
            case.client.retry(run["run_id"], "alice", "first", first["state_version"])[
                "generation"
            ]
            == 2
        )
    finally:
        release.set()
    completed = wait_delivered(case, run, "completed", 2)
    assert_consistent(case, run, completed)
    assert len(attempts) == 3
