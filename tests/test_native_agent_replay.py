"""Exercise the production role's native model and tool checkpoints offline."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from dbos import DBOS, DBOSClient, SetWorkflowID
from test_sdk_runtime import TOKENS, _runtime, _tool

from open_deep_research.llm import generation
from open_deep_research.runtime import sdk_runtime
from open_deep_research.web.run_data import RunJSON
from open_deep_research.workflow.execution import StageContext, stage_context


def test_agent_recovery_restores_parallel_tool_results_and_usage(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'agent.sqlite').as_posix()}"
    workflow_id = "replay-agent"
    calls = []
    attempts = 0

    async def begin():
        assert DBOS.step_id is not None
        calls.append("sdk_model")

    async def observe(self, kind, payload, *, call_id=None):
        if kind == "tool" and payload["status"] == "started":
            assert DBOS.step_id is not None
            calls.append(payload["name"])
        if kind == "tool_result":
            assert call_id == payload["tool_call_id"]

    monkeypatch.setattr(generation, "begin_model_call", begin)
    monkeypatch.setattr(StageContext, "observe", observe)
    monkeypatch.setattr(
        sdk_runtime, "settings", lambda: SimpleNamespace(session_path=None)
    )

    async def search(query: str, limit: int = 3):
        # Two calls in one response exercise the SDK's parallel Tool Steps.
        if query == "first":
            await asyncio.sleep(0.05)
        calls.append(query)
        return f"evidence {query}"

    def final(call):
        outputs = {
            item["call_id"]: item["output"]
            for item in call.input
            if item.get("type") == "function_call_output"
        }
        assert outputs == {"call-1": "evidence first", "call-2": "evidence second"}
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
            outcome = await runtime.run("collect evidence")
            if attempts == 1:
                # Stop after the Agent result was built but before the enclosing
                # Workflow returns it. Recovery must rebuild Python state.
                raise RuntimeError("simulated executor failure before result")
            assert not model.calls
            return outcome.budget, outcome.report
        finally:
            stage_context.reset(token)

    def launch():
        DBOS(
            config={
                "name": "por-test-agent",
                "system_database_url": url,
                "application_version": "agent-replay-test",
                "serializer": RunJSON(),
            }
        )
        DBOS.launch()

    async def first_attempt():
        with SetWorkflowID(workflow_id):
            await role()

    launch()
    client = DBOSClient(system_database_url=url, serializer=RunJSON())
    try:
        with pytest.raises(RuntimeError, match="simulated executor failure"):
            asyncio.run(first_attempt())
        before = client.list_workflow_steps(workflow_id)
        assert sum("_model_call_step" in s["function_name"] for s in before) == 2
        assert sum(s["function_name"] == "por.agent_tool" for s in before) == 2
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
        usage, report = handle.get_result()
        assert usage["model_calls"] == usage["tool_calls"] == 2
        assert usage["total_tokens"] == 28
        assert "finished with two sources" in report
        assert calls == observed_effects
        assert client.list_workflow_steps(workflow_id) == before
        assert attempts == 2
    finally:
        DBOS.destroy(workflow_completion_timeout_sec=2)
        client.destroy()
