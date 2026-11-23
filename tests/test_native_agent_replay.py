"""Exercise the production role's native model and tool checkpoints offline."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from agents.exceptions import UserError
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from dbos import DBOS, DBOSClient, SetWorkflowID
from sdk_model_support import Reply, install_sequence
from test_sdk_runtime import TOKENS, _runtime, _tool

from open_deep_research.budget import ModelBudgetExceeded
from open_deep_research.llm import generation
from open_deep_research.llm.errors import ModelInvocationError
from open_deep_research.runtime import sdk_runtime
from open_deep_research.web.run_data import RunJSON
from open_deep_research.workflow.execution import StageContext, stage_context


@pytest.mark.parametrize(
    "ending", ["completed", "denied", "provider-error", "tool-budget-error"]
)
def test_agent_recovery_restores_parallel_tool_results_and_usage(
    tmp_path, monkeypatch, ending, memory_ledger
):
    url = f"sqlite:///{(tmp_path / 'agent.sqlite').as_posix()}"
    workflow_id = "replay-agent"
    serializer = RunJSON()
    calls = []
    receipts = []
    attempts = 0
    # The sibling must have committed its Tool Step before the failing call
    # fails, whatever order the event loop schedules the two Steps in. The
    # sibling's ``tool_result`` observation is published after its Step has
    # committed, so this is a fact of the fixture instead of a bet on ``sleep``.
    second_finished = asyncio.Event()

    async def begin():
        assert DBOS.step_id is not None
        calls.append("sdk_model")
        if ending == "denied" and calls.count("sdk_model") == 2:
            raise ModelBudgetExceeded("Physical call receipts exhausted the research budget")

    async def end(_identity, usage):
        receipts.append(usage)

    async def observe(self, kind, payload, *, call_id=None):
        if kind == "tool" and payload["status"] == "started":
            assert DBOS.step_id is not None
            calls.append(payload["name"])
        if kind == "tool_result":
            assert call_id == payload["tool_call_id"]
            if payload["tool_call_id"] == "call-2" and payload["status"] == "succeeded":
                second_finished.set()

    monkeypatch.setattr(generation, "begin_model_call", begin)
    monkeypatch.setattr(generation, "end_model_call", end)
    monkeypatch.setattr(StageContext, "observe", observe)
    monkeypatch.setattr(
        sdk_runtime, "settings", lambda: SimpleNamespace(session_path=None)
    )
    if ending == "tool-budget-error":
        install_sequence(monkeypatch, Reply("tool summary", 5, 3))

    async def search(query: str, limit: int = 3):
        # Two calls in one response exercise the SDK's parallel Tool Steps.
        if query == "first":
            # Bounded so a serial SDK loop fails loudly instead of hanging.
            await asyncio.wait_for(second_finished.wait(), timeout=10)
            calls.append(query)
            if ending == "tool-budget-error":
                await generation.generate_text("openai:fixture", query, max_tokens=64)
                raise ModelBudgetExceeded("Tool summary ran out of allowance")
            return f"evidence {query}"
        calls.append(query)
        return f"evidence {query}"

    def final(call):
        outputs = {
            item["call_id"]: item["output"]
            for item in call.input
            if item.get("type") == "function_call_output"
        }
        assert outputs == {"call-1": "evidence first", "call-2": "evidence second"}
        if ending == "provider-error":
            raise RuntimeError("provider error")
        return [assistant_message("finished with two sources")]

    @DBOS.workflow(name=f"por.test.agent_replay.{uuid4().hex}")
    async def role():
        nonlocal attempts
        attempts += 1
        tool = _tool(coroutine=search, effect="read")
        model = ScriptedModel(
            [
                [
                    function_call(tool.name, {"query": "first"}, call_id="call-1"),
                    function_call(tool.name, {"query": "second"}, call_id="call-2"),
                ],
                ModelStep.respond(final),
            ],
            default_usage=TOKENS,
        )
        runtime = _runtime(tools=[tool], model=model)
        scope = StageContext(
            workflow_id,
            workflow_id + ":role",
            "public_signal_agent",
            0,
            runtime.config,
            {},
        )
        token = stage_context.set(scope)
        try:
            if ending.endswith("error"):
                error_type = (
                    UserError
                    if ending == "tool-budget-error"
                    else ModelInvocationError
                )
                with pytest.raises(error_type) as caught:
                    await runtime.run("collect evidence")
                if ending == "tool-budget-error":
                    assert isinstance(caught.value.__cause__, ModelBudgetExceeded)
                usage, report = caught.value.budget_usage, None
            else:
                outcome = await runtime.run("collect evidence")
                usage, report = outcome.budget, outcome.report
            assert runtime.harness.last_settlement.succeeded
            statuses = {
                key: record.status for key, record in runtime.harness.results.items()
            }
            assert statuses["call-1"] == (
                "failed" if ending == "tool-budget-error" else "succeeded"
            )
            if attempts == 1 or ending != "tool-budget-error":
                # Live execution, or a replay whose batch has no sibling failure
                # to cancel it: the committed result is always consumed.
                assert statuses["call-2"] == "succeeded"
            else:
                # Attempt two replays both Step outcomes without re-running a
                # body. The sibling's replayed error can surface before this
                # replay consumes its committed receipt, and the SDK then
                # abandons the replay: cancelled is this attempt's truthful
                # terminal state — no body ran and no allowance was added —
                # while the committed success stays journaled for the next
                # recovery and is asserted against ``before`` below.
                assert statuses["call-2"] in {"succeeded", "cancelled"}
            assert "running" not in statuses.values()
            if attempts == 1:
                # Stop after the Agent result was built but before the enclosing
                # Workflow returns it. Recovery must rebuild Python state.
                raise RuntimeError("simulated executor failure before result")
            assert not model.calls
            return usage, report, statuses
        finally:
            stage_context.reset(token)

    def launch():
        DBOS(
            config={
                "name": "por-test-agent",
                "system_database_url": url,
                "application_version": "agent-replay-test",
                "serializer": serializer,
            }
        )
        DBOS.launch()

    async def first_attempt():
        with SetWorkflowID(workflow_id):
            await role()

    launch()
    client = DBOSClient(system_database_url=url, serializer=serializer)
    try:
        with pytest.raises(RuntimeError, match="simulated executor failure"):
            asyncio.run(first_attempt())
        before = client.list_workflow_steps(workflow_id)
        assert sum("_model_call_step" in s["function_name"] for s in before) == (
            1 if ending == "tool-budget-error" else 2
        )
        assert sum(s["function_name"] == "por.agent_tool" for s in before) == 2
        assert all(
            step["output"]["model_input"] is None
            for step in before if step["function_name"] == "por.agent_context"
        )
        assert calls.count("sdk_model") == 2
        assert calls.count("web_search") == 2
        assert [item for item in calls if item in {"first", "second"}] == [
            "second",
            "first",
        ]
        observed_effects = list(calls)
        DBOS.destroy()
        launch()
        # Rewind the native Workflow result with its full Step journal intact.
        handle = client.rewind_workflow(
            workflow_id,
            start_step=max(s["function_id"] for s in before) + 1,
        )
        usage, report, statuses = handle.get_result()
        # The replay restores the journaled receipts without re-running a body.
        # When the sibling's replayed error won the race, its abandoned replay
        # did not consume the second committed receipt in this attempt's local
        # projection; the journal below still holds it for the next recovery.
        consumed = 1 + (statuses["call-2"] == "succeeded")
        assert usage["tool_calls"] == consumed
        assert usage["search_calls"] == consumed
        assert usage["model_calls"] == len(receipts) == (1 if ending == "denied" else 2)
        assert usage["total_tokens"] == {
            "completed": 28,
            "denied": 14,
            "provider-error": 14,
            "tool-budget-error": 22,
        }[ending]
        if ending == "completed":
            assert "finished with two sources" in report
        elif ending == "denied":
            assert "evidence first" in report and "evidence second" in report
            assert usage["degradation_reasons"]
        elif ending == "provider-error":
            assert report is None
            assert receipts[-1]["usage_complete"] is False
        else:
            assert report is None
            assert any(
                isinstance(step["error"], ModelBudgetExceeded)
                and step["error"].budget_usage["tool_calls"] == 1
                for step in before if step["function_name"] == "por.agent_tool"
            )
        # Whichever way the replay raced, the journal keeps call-2's committed
        # success receipt: a recovery never loses a committed result.
        committed = next(
            step["output"]
            for step in before
            if step["function_name"] == "por.agent_tool"
            and step["output"]
            and step["output"]["record"]["tool_call_id"] == "call-2"
        )
        assert committed["record"]["status"] == "succeeded"
        assert calls == observed_effects
        assert serializer.serialize(client.list_workflow_steps(workflow_id)) == (
            serializer.serialize(before)
        )
        assert attempts == 2
    finally:
        DBOS.destroy(workflow_completion_timeout_sec=2)
        client.destroy()
