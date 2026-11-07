"""Real ledger commit/ACK failures must not replay paid calls or lose usage."""

import threading

import pytest
from sqlalchemy.exc import OperationalError

from open_deep_research.web.run_data import ExecutionRevoked, RunConflict
from open_deep_research.workflow.run_facts import RunFacts, facts

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("boundary", ["sdk_model", "llm", "web_search"])
@pytest.mark.parametrize("window", ["before_commit", "after_commit"])
def test_finished_receipt_retries_control_only(
    native_case, monkeypatch, boundary, window
):
    case = native_case
    original = RunFacts.operation
    injected = []
    lock = threading.Lock()

    def fail_once(repo, unit, permit, identity, status="started", usage=None):
        inject = False
        if status == "returned" and identity["name"] == boundary:
            with lock:
                if not injected:
                    injected.append((unit, permit, identity, usage))
                    inject = True
        if inject and window == "before_commit":
            raise OperationalError(
                "Synthetic receipt write unavailable", None, Exception("before commit")
            )
        result = original(repo, unit, permit, identity, status, usage)
        if inject:
            raise OperationalError(
                "Synthetic receipt ACK lost", None, Exception("after commit")
            )
        return result

    monkeypatch.setattr(RunFacts, "operation", fail_once)
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
    unit, permit, identity, usage = injected[0]
    # Exact ACK may be observed again; neither a different bill nor a stale
    # executor may use this as authorization to change a committed receipt.
    assert original(facts(), unit, permit, identity, "returned", usage)
    with pytest.raises(RunConflict):
        original(facts(), unit, permit, identity, "returned", {"model_calls": 999})
    with pytest.raises(ExecutionRevoked):
        original(facts(), unit, "revoked-permit", identity, "returned", usage)
    after = case.client.read_events(run["run_id"], "alice", 0, 1000)
    assert after == events


def test_failed_generations_keep_the_final_report_reservation(native_case, monkeypatch):
    from dbos_support import retry_observed_failure
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import AIMessage, HumanMessage

    from open_deep_research.budget import ainvoke_model_with_budget
    from open_deep_research.workflow import dbos_workflow as engine

    case = native_case
    calls = []
    raw = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="invalid brief",
                usage_metadata={
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "total_tokens": 10,
                },
            )
        ]
    )

    class Inference:
        async def ainvoke(self, payload, config=None):
            calls.append(1)
            return await raw.ainvoke(payload, config)

    async def invalid_plan(state, config):
        await ainvoke_model_with_budget(Inference(), [HumanMessage(content="brief")])
        raise ValueError("Controlled invalid brief response")

    monkeypatch.setitem(engine.OPERATIONS, "plan_research", invalid_plan)
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
