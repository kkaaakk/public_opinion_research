import asyncio
import json

import pytest
from agents import (
    Agent,
    ModelSettings,
    RunConfig,
    Runner,
    RunState,
    SQLiteSession,
    function_tool,
)
from agents.run_config import ModelInputData
from agents.testing import ModelStep, ScriptedModel, function_call
from manager import ResearchContext, final_step, fixed_step, tool_outputs


def test_session_callback_and_model_filter_have_different_scope():
    async def scenario():
        session = SQLiteSession("manager-poc")
        history_seen = []

        def select_input(history, incoming):
            history_seen.extend(history)
            return incoming

        def filter_input(data):
            revised = [
                {**item, "content": "FILTERED INPUT"}
                if item.get("role") == "user"
                else item
                for item in data.model_data.input
            ]
            return ModelInputData(
                input=revised, instructions=data.model_data.instructions
            )

        model = ScriptedModel([final_step("old answer"), final_step("new answer")])
        manager = Agent(name="manager", model=model)
        run_config = RunConfig(tracing_disabled=True)
        try:
            await Runner.run(
                manager, "old request", session=session, run_config=run_config
            )
            result = await Runner.run(
                manager,
                "new request",
                session=session,
                run_config=RunConfig(
                    tracing_disabled=True,
                    session_input_callback=select_input,
                    call_model_input_filter=filter_input,
                ),
            )
            sent = json.dumps(model.calls[1].input)
            assert "old request" in json.dumps(history_seen)
            assert (
                "FILTERED INPUT" in sent
                and "new request" not in sent
                and "old request" not in sent
            )
            assert "new request" in json.dumps(result.to_input_list())
            saved = json.dumps(await session.get_items())
            assert (
                "old request" in saved
                and "new request" in saved
                and "FILTERED INPUT" not in saved
            )
        finally:
            session.close()

    asyncio.run(scenario())


def test_explicit_shared_child_session_combines_parallel_histories():
    async def scenario():
        parent_session = SQLiteSession("parent")
        child_session = SQLiteSession("shared-child")
        first_inputs = {}
        both_read = asyncio.Event()

        def nested_model(name):
            async def respond(call):
                first_inputs[name] = json.dumps(call.input)
                if len(first_inputs) == 2:
                    both_read.set()
                await asyncio.wait_for(both_read.wait(), 5)
                return final_step(f"{name} child answer")

            return ScriptedModel([ModelStep.respond(respond)])

        children = [
            Agent(name=name, model=nested_model(name)).as_tool(
                name, "Collect", session=child_session, failure_error_function=None
            )
            for name in ("public", "knowledge")
        ]
        model = ScriptedModel(
            [
                fixed_step(
                    function_call(
                        "public", {"input": "public private prompt"}, call_id="p"
                    ),
                    function_call(
                        "knowledge", {"input": "knowledge private prompt"}, call_id="k"
                    ),
                ),
                final_step("done"),
            ]
        )
        try:
            await parent_session.add_items(
                [{"role": "user", "content": "parent prefix"}]
            )
            await child_session.add_items(
                [{"role": "user", "content": "shared prefix"}]
            )
            await Runner.run(
                Agent(
                    name="manager",
                    model=model,
                    tools=children,
                    model_settings=ModelSettings(parallel_tool_calls=True),
                ),
                "approved brief",
                session=parent_session,
                run_config=RunConfig(tracing_disabled=True),
            )
            assert all(
                "shared prefix" in item and "parent prefix" not in item
                for item in first_inputs.values()
            )
            assert "knowledge private prompt" not in first_inputs["public"]
            assert "public private prompt" not in first_inputs["knowledge"]
            next_model = ScriptedModel([final_step("done")])
            await Runner.run(
                Agent(name="next", model=next_model),
                "new work",
                session=child_session,
                run_config=RunConfig(tracing_disabled=True),
            )
            mixed = json.dumps(next_model.calls[0].input)
            assert all(
                f"{name} private prompt" in mixed for name in ("public", "knowledge")
            )
            assert all(
                f"{name} child answer" in mixed for name in ("public", "knowledge")
            )
            assert "parent prefix" not in mixed
            parent_items = await parent_session.get_items()
            assert "public private prompt" in json.dumps(
                parent_items
            )  # Manager's own tool arguments.
            parent_text = [
                item.get("content")
                for item in parent_items
                if item.get("role") == "user"
            ]
            assert parent_text == ["parent prefix", "approved brief"]
            assert "shared prefix" not in json.dumps(parent_items)
        finally:
            parent_session.close()
            child_session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("approve", [True, False])
def test_serialized_run_state_resumes_nested_approval_instead_of_replaying(approve):
    async def scenario():
        invocations = []
        ctx = ResearchContext("pause", {}, None, None)

        @function_tool(needs_approval=True, failure_error_function=None)
        def publish() -> str:
            invocations.append("publish")
            return "approved result"

        def validate_child(call):
            output = tool_outputs(call)["publish"]
            assert output == ("approved result" if approve else "declined by fixture")
            return final_step("specialist complete")

        initial_root = ScriptedModel(
            [fixed_step(function_call("worker", {"input": "work"}, call_id="worker"))]
        )
        initial_child = ScriptedModel(
            [fixed_step(function_call("publish", {}, call_id="publish"))]
        )
        resumed_root = ScriptedModel([final_step("manager complete")])
        resumed_child = ScriptedModel([ModelStep.respond(validate_child)])

        def assemble(root_model, specialist_model):
            child = Agent(name="child", model=specialist_model, tools=[publish])
            tool = child.as_tool("worker", "Collect", failure_error_function=None)
            return Agent(name="manager", model=root_model, tools=[tool])

        config = RunConfig(tracing_disabled=True)
        paused = await Runner.run(
            assemble(initial_root, initial_child),
            "brief",
            context=ctx,
            run_config=config,
        )
        assert len(paused.interruptions) == 1
        assert invocations == []
        new_agent = assemble(resumed_root, resumed_child)
        snapshot = json.loads(json.dumps(paused.to_state().to_json()))
        state = await RunState.from_json(
            new_agent, snapshot, context_override={"run_id": "pause"}
        )
        (restored_interruption,) = state.get_interruptions()
        if approve:
            state.approve(restored_interruption)
        else:
            state.reject(restored_interruption, rejection_message="declined by fixture")
        result = await Runner.run(new_agent, state, context=ctx, run_config=config)
        assert result.final_output == "manager complete", (
            result.interruptions,
            invocations,
            len(resumed_root.calls),
            len(resumed_child.calls),
        )
        assert result.context_wrapper.context is ctx
        assert result.interruptions == []
        assert invocations == (["publish"] if approve else [])
        assert all(
            len(model.calls) == 1
            for model in (initial_root, initial_child, resumed_root, resumed_child)
        )
        assert result.context_wrapper.usage.requests == 4
        assert len(result.context_wrapper.usage.request_usage_entries) == 4
        assert paused.context_wrapper.usage.requests == 2

    asyncio.run(scenario())
