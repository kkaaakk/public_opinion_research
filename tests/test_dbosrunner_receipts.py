"""Real ledger commit/ACK failures must not replay paid calls or lose usage."""

import threading

import pytest
from sqlalchemy.exc import OperationalError

from open_deep_research import budget
from open_deep_research.runtime import tool_executor
from open_deep_research.workflow import execution

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("boundary", ["model", "protected_tool"])
@pytest.mark.parametrize("window", ["before_commit", "after_commit"])
def test_finished_receipt_retries_control_only(
    native_case, monkeypatch, boundary, window
):
    case = native_case
    injected = []
    lock = threading.Lock()
    module = budget if boundary == "model" else tool_executor
    name = "finish_model_attempt" if boundary == "model" else "finish_operation"
    original = getattr(module, name)
    original_effect = tool_executor.tool_effect

    def effect(tool):
        return "protected" if tool.name == "web_search" else original_effect(tool)

    if boundary == "protected_tool":
        monkeypatch.setattr(tool_executor, "tool_effect", effect)

    def fail_once(*args):
        with lock:
            inject = not injected
            if inject:
                injected.append(args)
        if inject and window == "before_commit":
            raise OperationalError(
                "Synthetic receipt write unavailable", None, Exception("before commit")
            )
        original(*args)
        if inject:
            raise OperationalError(
                "Synthetic receipt ACK lost", None, Exception("after commit")
            )

    monkeypatch.setattr(module, name, fail_once)
    run = case.create()
    done = case.wait(
        run, lambda r: r["archive_status"] == "saved" and r["events_complete"]
    )
    assert injected and done["status"] == "completed", done["error"]
    assert done["usage"]["model_calls"] == 26 and done["usage"]["total_tokens"] == 291
    assert sum(len(m.calls) for m in case.models) == 6
    assert case.calls == {"public_signal": 1, "internal_knowledge": 1}
    events = case.client.read_events(run["run_id"], "alice", 0, 1000)
    returned = [
        e
        for e in events
        if e["type"] == "sdk_event"
        and e.get("event") == "tool"
        and e.get("status") == "returned"
    ]
    assert len(returned) == 2
    original(*injected[0])
    with pytest.raises(execution.ExecutionControlError, match="[Cc]onflicting|match"):
        original(
            *injected[0][:-1],
            {"model_calls": 999} if boundary == "model" else "unknown",
        )
    assert case.client.read_events(run["run_id"], "alice", 0, 1000) == events


def test_failed_generations_keep_the_final_report_reservation(native_case, monkeypatch):
    from dbos_support import retry_observed_failure
    from sdk_model_support import install_inference

    from open_deep_research.llm.generation import generate_text
    from open_deep_research.workflow import dbos_workflow as engine

    case = native_case
    calls = []

    def infer(request):
        if request.model_ref == "openai:invalid-plan":
            calls.append(1)
            return "invalid brief"

    install_inference(monkeypatch, infer)

    async def invalid_plan(state, config):
        await generate_text("openai:invalid-plan", "brief")
        raise ValueError("Controlled invalid brief response")

    monkeypatch.setattr(engine.intake, "plan_research", invalid_plan)
    case.cfg["configurable"].update(max_model_calls=10, reserve_final_report_call=True)
    run = case.create()

    def observed(generation):
        return case.wait(
            run,
            lambda r: r["status"] == "failed"
            and r["generation"] == generation
            and r["archive_status"] == "saved"
            and r["events_complete"]
            and not r["retry_requested"],
        )

    first = observed(0)
    assert first["usage"]["model_calls"] == 8 and len(calls) == 1
    retry_observed_failure(case, run, "first-bad-response", first)
    second = observed(1)
    assert second["usage"]["model_calls"] == 9 and len(calls) == 2
    retry_observed_failure(case, run, "preserve-report-reserve", second)
    final = observed(2)
    assert final["error"]["kind"] == "budget_exhausted"
    assert final["usage"]["model_calls"] == 9 and len(calls) == 2


def test_parallel_models_share_run_limit_and_final_report_reservation(native_case, monkeypatch):
    import asyncio
    from uuid import uuid4

    from dbos import DBOS
    from sdk_model_support import install_inference

    from open_deep_research.configuration import Configuration
    from open_deep_research.llm.generation import generate_text

    case = native_case
    case.cfg["configurable"].update(max_model_calls=10, reserve_final_report_call=True)
    run = case.create(approval=True)
    paused = case.wait(run, lambda r: r["status"] == "awaiting_approval")
    assert paused["usage"]["model_calls"] == 8
    barrier = threading.Barrier(2)
    invoked = []

    def infer(request):
        if request.model_ref == "openai:shared-budget":
            invoked.append(execution.stage_context.get().reserve_report)
            return "provider response"

    install_inference(monkeypatch, infer)

    @DBOS.workflow(name=f"por.test.shared_budget.{uuid4().hex}")
    async def inference(reserve):
        scope = execution.StageContext(
            run["run_id"],
            DBOS.workflow_id,
            "test_model",
            0,
            Configuration.from_runnable_config(case.cfg),
            case.state["runtime"]["budget"],
            reserve_report=reserve,
        )
        token = execution.stage_context.set(scope)
        try:
            if reserve:
                await asyncio.to_thread(barrier.wait, 10)

            await generate_text("openai:shared-budget", "request")
            return True
        except budget.ModelBudgetExceeded:
            return False
        finally:
            execution.stage_context.reset(token)

    handles = [DBOS.start_workflow(inference, True) for _ in range(2)]
    assert sorted(h.get_result() for h in handles) == [False, True]
    assert invoked == [True]
    assert DBOS.start_workflow(inference, False).get_result()
    assert not DBOS.start_workflow(inference, False).get_result()
    assert invoked == [True, False]
    assert case.client.get(run["run_id"], "alice")["usage"]["model_calls"] == 10
