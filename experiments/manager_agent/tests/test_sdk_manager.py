import asyncio
import json
from collections import Counter

import pytest
from agents import Agent, ModelSettings, RunConfig, RunHooks, Runner, function_tool
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError, UserError
from agents.retry import ModelRetryAdvice, ModelRetrySettings, retry_policies
from agents.testing import ModelStep, ScriptedModel, function_call
from manager import (
    BRIEF,
    RAW,
    AdmittedModel,
    BudgetExhausted,
    MemoryBudget,
    ResearchContext,
    ResearchHooks,
    ResearchTask,
    UnknownEffect,
    child_observation,
    config,
    final_step,
    fixed_step,
    in_memory_research,
    model_input,
    tool_outputs,
)


@pytest.mark.parametrize(("gap", "streamed"), [(True, False), (False, True)])
def test_real_manager_dispatch_parallel_followup_and_streaming(gap, streamed):
    async def scenario():
        child_events = []
        parent_events = []
        hooks = ResearchHooks()
        budget = MemoryBudget(20)
        manager, models, ctx = in_memory_research(
            budget,
            gap=gap,
            hooks=hooks,
            stream=child_events.append if streamed else None,
        )
        kwargs = dict(context=ctx, hooks=hooks, run_config=config(), max_turns=5)
        if streamed:
            result = Runner.run_streamed(manager, BRIEF, **kwargs)
            async for event in result.stream_events():
                parent_events.append(event)
        else:
            result = await Runner.run(manager, BRIEF, **kwargs)

        expected = ["analysis:initial", "knowledge:initial", "public:initial"]
        if gap:
            expected.insert(2, "public:followup")
        assert result.final_output.evidence_ids == expected
        assert sorted(ctx.evidence) == expected
        assert {role: len(model.calls) for role, model in models.items()} == {
            "manager": 4 if gap else 3,
            "public": 4 if gap else 2,
            "knowledge": 2,
            "analysis": 2,
        }

        calls = [call for model in models.values() for call in model.calls]
        assert all(
            "Input filter applied." in call.system_instructions for call in calls
        )
        assert all(call.streamed == streamed for call in calls)
        assert len(budget.admitted) == len(calls)
        usage = result.context_wrapper.usage
        assert (
            usage.requests,
            usage.input_tokens,
            usage.output_tokens,
            usage.total_tokens,
        ) == (len(calls), 11 * len(calls), 3 * len(calls), 14 * len(calls))
        assert len(usage.request_usage_entries) == len(calls)
        assert {entry.total_tokens for entry in usage.request_usage_entries} == {14}
        assert len({id(wrapper) for wrapper in ctx.wrappers}) == (5 if gap else 4)
        assert all(
            wrapper.context is ctx and wrapper.usage is usage
            for wrapper in ctx.wrappers
        )
        assert Counter(ctx.observations)[("start", "public")] == (2 if gap else 1)
        assert Counter(ctx.observations)[("end", "manager")] == 1

        manager_calls = models["manager"].calls
        assert manager_calls[0].model_settings.parallel_tool_calls is True
        assert manager_calls[0].model_settings.max_tokens == 96
        assert all(RAW not in json.dumps(call.input) for call in manager_calls)
        assert all(
            BRIEF not in json.dumps(models[role].calls[0].input) for role in ctx_roles()
        )
        assert RAW in json.dumps(models["public"].calls[1].input)
        assert models["public"].calls[0].model_settings.max_tokens == 64
        if gap:
            first_new_input = json.dumps(models["public"].calls[2].input)
            assert (
                "Verify missing data" in first_new_input
                and "public:initial" in first_new_input
            )
            assert "Find public signals" not in first_new_input
            assert RAW not in first_new_input

        assert ("ask_public", "public-first") in ctx.invocations
        assert ("ask_knowledge", "knowledge-first") in ctx.invocations
        assert ("ask_analysis", "analysis-first") in ctx.invocations
        if streamed:
            assert {event["tool_call"].call_id for event in child_events} == {
                "public-first",
                "knowledge-first",
                "analysis-first",
            }
            assert {event["agent"].name for event in child_events} == set(ctx_roles())
            assert any(
                event["event"].type == "run_item_stream_event" for event in child_events
            )
            projected = [
                record for event in child_events if (record := child_observation(event))
            ]
            assert sorted(projected, key=lambda record: record["role"]) == [
                {
                    "type": "agent_started",
                    "role": role,
                    "parent_call_id": f"{role}-first",
                }
                for role in sorted(ctx_roles())
            ]
            assert any(event.type == "run_item_stream_event" for event in parent_events)
            assert {
                event.new_agent.name
                for event in parent_events
                if event.type == "agent_updated_stream_event"
            } == {"manager"}
        for model in models.values():
            model.assert_complete()

    asyncio.run(scenario())


def ctx_roles():
    return ("public", "knowledge", "analysis")


def test_specialist_cannot_invoke_a_siblings_tool():
    async def scenario():
        side_effects = []

        @function_tool(failure_error_function=None)
        def read_internal() -> str:
            side_effects.append("read")
            return "internal material"

        knowledge = Agent(
            name="knowledge", model=ScriptedModel(), tools=[read_internal]
        )
        public_model = ScriptedModel(
            [
                fixed_step(function_call("read_internal", {}, call_id="forbidden")),
            ]
        )
        public = Agent(name="public", model=public_model)
        root = ScriptedModel(
            [
                fixed_step(function_call("public", {"input": "task"}, call_id="child")),
            ]
        )
        manager = Agent(
            name="manager",
            model=root,
            tools=[
                public.as_tool(
                    "public", "Collect public sources", failure_error_function=None
                ),
                knowledge.as_tool(
                    "knowledge", "Collect internal sources", failure_error_function=None
                ),
            ],
        )
        with pytest.raises(ModelBehaviorError, match="read_internal"):
            await Runner.run(
                manager,
                "brief",
                run_config=RunConfig(
                    tracing_disabled=True,
                    tool_not_found_behavior="raise_error",
                ),
            )
        assert not side_effects
        assert len(root.calls) == len(public_model.calls) == 1

    asyncio.run(scenario())


def test_run_config_inheritance_and_explicit_hooks():
    async def scenario():
        context = ResearchContext("policy-run", {}, None, None)
        model1 = ScriptedModel([final_step("child inherited settings")])
        model2 = ScriptedModel([final_step("child explicit settings")])
        child1 = Agent(name="one", model=model1)
        child2 = Agent(name="two", model=model2)
        explicit_hooks = ResearchHooks()
        tools = [
            child1.as_tool("one", "First child"),
            child2.as_tool(
                "two",
                "Second child",
                hooks=explicit_hooks,
                run_config=RunConfig(
                    tracing_disabled=True, model_settings=ModelSettings(max_tokens=23)
                ),
            ),
        ]
        main = ScriptedModel(
            [
                fixed_step(
                    function_call("one", {"input": "one"}, call_id="child-1"),
                    function_call("two", {"input": "two"}, call_id="child-2"),
                ),
                final_step("done"),
            ]
        )
        result = await Runner.run(
            Agent(name="manager", model=main, tools=tools),
            "brief",
            context=context,
            hooks=ResearchHooks(),
            run_config=RunConfig(
                tracing_disabled=True,
                call_model_input_filter=model_input,
                model_settings=ModelSettings(max_tokens=91),
            ),
        )
        assert result.context_wrapper.usage.requests == 4
        assert model1.calls[0].model_settings.max_tokens == 91
        assert model2.calls[0].model_settings.max_tokens == 23
        assert ("filter", "one") in context.observations
        assert ("filter", "two") not in context.observations
        assert ("start", "one") not in context.observations
        assert ("start", "two") in context.observations
        assert ("start", "manager") in context.observations

    asyncio.run(scenario())


def test_structured_parameters_are_validated_before_child_inference():
    async def scenario():
        child = ScriptedModel()
        tool = Agent(name="public", model=child).as_tool(
            "public",
            "Read sources",
            parameters=ResearchTask,
            failure_error_function=None,
        )
        root = ScriptedModel(
            [fixed_step(function_call("public", {"question": "x"}, call_id="bad"))]
        )
        with pytest.raises(ModelBehaviorError, match="Invalid JSON input"):
            await Runner.run(
                Agent(name="manager", model=root, tools=[tool]),
                "brief",
                run_config=config_without_context(),
            )
        assert not child.calls

    asyncio.run(scenario())


def config_without_context():
    return RunConfig(tracing_disabled=True)


@pytest.mark.parametrize("propagate", [False, True])
def test_nested_model_failure_is_an_error_output_unless_disabled(propagate):
    async def scenario():
        child = ScriptedModel(
            [ModelStep.raise_error(RuntimeError("fixture provider failed"))]
        )
        specialist = Agent(name="public", model=child)
        error_options = {"failure_error_function": None} if propagate else {}
        tool = specialist.as_tool("public", "Research public signals", **error_options)

        def check_error(call):
            output = str(tool_outputs(call)["child"])
            assert "error" in output.lower() and "fixture provider failed" not in output
            return final_step("handled")

        root = ScriptedModel(
            [
                fixed_step(function_call("public", {"input": "task"}, call_id="child")),
                ModelStep.respond(check_error),
            ]
        )
        ctx = ResearchContext("failure", {}, None, None)
        run = Runner.run(
            Agent(name="manager", model=root, tools=[tool]),
            "brief",
            context=ctx,
            hooks=ResearchHooks(),
            run_config=config_without_context(),
        )
        if propagate:
            with pytest.raises(UserError, match="fixture provider failed") as failure:
                await run
            assert isinstance(failure.value.__cause__, RuntimeError)
            assert len(root.calls) == 1
            assert ctx.wrappers[0].usage.requests == 1
        else:
            assert (await run).final_output == "handled"
            assert len(root.calls) == 2
            assert ctx.wrappers[0].usage.requests == 2
        assert len(child.calls) == 1

    asyncio.run(scenario())


def test_tool_control_exception_propagates_through_both_boundaries():
    async def scenario():
        @function_tool(failure_error_function=None)
        def collect_external() -> str:
            raise UnknownEffect("receipt unresolved")

        child = ScriptedModel(
            [fixed_step(function_call("collect_external", {}, call_id="collect"))]
        )
        tool = Agent(name="public", tools=[collect_external], model=child).as_tool(
            "public", "Collect", failure_error_function=None
        )
        root = ScriptedModel(
            [fixed_step(function_call("public", {"input": "task"}, call_id="child"))]
        )
        ctx = ResearchContext("unknown", {}, None, None)
        with pytest.raises(UserError, match="receipt unresolved") as failure:
            await Runner.run(
                Agent(name="manager", model=root, tools=[tool]),
                "brief",
                context=ctx,
                hooks=ResearchHooks(),
                run_config=config_without_context(),
            )
        assert isinstance(failure.value.__cause__, UnknownEffect)
        assert ctx.wrappers[0].usage.requests == 2
        assert len(root.calls) == len(child.calls) == 1

    asyncio.run(scenario())


def test_global_atomic_budget_exhaustion_preserves_report_slot():
    async def scenario():
        budget = MemoryBudget(3)
        ctx = ResearchContext("budget", {}, budget, None)
        children = [ScriptedModel([final_step("child done")]) for _ in range(2)]
        tools = [
            Agent(name=f"child{i}", model=AdmittedModel(model, budget.admit)).as_tool(
                f"child{i}", "Research", failure_error_function=None
            )
            for i, model in enumerate(children)
        ]
        root = ScriptedModel(
            [
                fixed_step(
                    *[
                        function_call(tool.name, {"input": "work"}, call_id=tool.name)
                        for tool in tools
                    ]
                )
            ]
        )
        with pytest.raises(UserError, match="research") as failure:
            await Runner.run(
                Agent(
                    name="manager", model=AdmittedModel(root, budget.admit), tools=tools
                ),
                "brief",
                context=ctx,
                hooks=ResearchHooks(),
                run_config=config_without_context(),
            )
        assert isinstance(failure.value.__cause__, BudgetExhausted)
        assert len(root.calls) == 1
        assert sum(len(child.calls) for child in children) == 1
        assert budget.admitted == ["research", "research"]
        report = ScriptedModel([final_step("final report")])
        answer = await Runner.run(
            Agent(name="report", model=AdmittedModel(report, budget.admit, "report")),
            "prepared evidence",
            run_config=config_without_context(),
        )
        assert answer.final_output == "final report"
        assert budget.admitted == ["research", "research", "report"]

    asyncio.run(scenario())


def test_one_logical_model_hook_can_cover_two_native_retry_attempts():
    async def scenario():
        ctx = ResearchContext("retry", {}, None, None)
        model = ScriptedModel(
            [
                ModelStep.raise_error(
                    RuntimeError("offline retryable model error"),
                    retry_advice=ModelRetryAdvice(
                        suggested=True, retry_after=0, replay_safety="safe"
                    ),
                ),
                final_step("done"),
            ]
        )
        agent = Agent(
            name="worker",
            model=model,
            model_settings=ModelSettings(
                retry=ModelRetrySettings(
                    max_retries=1, policy=retry_policies.provider_suggested()
                ),
            ),
        )
        result = await Runner.run(
            agent,
            "brief",
            context=ctx,
            hooks=ResearchHooks(),
            run_config=config_without_context(),
        )
        assert result.final_output == "done"
        assert len(model.calls) == 2
        assert ctx.observations.count(("model", "worker")) == 1
        usage = result.context_wrapper.usage
        assert usage.requests == 2
        assert [entry.total_tokens for entry in usage.request_usage_entries] == [0, 14]

    asyncio.run(scenario())


def test_hooks_can_block_call_and_max_turns_is_child_local():
    async def scenario():
        class AdmissionHook(RunHooks):
            async def on_llm_start(self, context, agent, system_prompt, input_items):
                raise BudgetExhausted("hook blocked")

        uncalled = ScriptedModel()
        with pytest.raises(BudgetExhausted, match="hook blocked"):
            await Runner.run(
                Agent(name="hook", model=uncalled),
                "brief",
                hooks=AdmissionHook(),
                run_config=config_without_context(),
            )
        assert not uncalled.calls

        @function_tool(failure_error_function=None)
        def research() -> str:
            return "partial"

        child = ScriptedModel(
            [fixed_step(function_call("research", {}, call_id="read"))]
        )
        tool = Agent(name="worker", model=child, tools=[research]).as_tool(
            "worker",
            "Collect",
            max_turns=1,
            failure_error_function=None,
        )
        root = ScriptedModel(
            [fixed_step(function_call("worker", {"input": "task"}, call_id="child"))]
        )
        ctx = ResearchContext("turns", {}, None, None)
        with pytest.raises(MaxTurnsExceeded):
            await Runner.run(
                Agent(name="manager", model=root, tools=[tool]),
                "brief",
                context=ctx,
                hooks=ResearchHooks(),
                max_turns=4,
                run_config=config_without_context(),
            )
        assert len(child.calls) == len(root.calls) == 1
        assert ctx.wrappers[0].usage.requests == 2

    asyncio.run(scenario())


def test_parent_cancellation_reaches_nested_model_and_usage_retains_completed_calls():
    async def scenario():
        entered = asyncio.Event()
        exited = asyncio.Event()

        async def blocked(call):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()

        child = ScriptedModel([ModelStep.respond(blocked)])
        tool = Agent(name="worker", model=child).as_tool("worker", "Collect")
        root = ScriptedModel(
            [fixed_step(function_call("worker", {"input": "task"}, call_id="child"))]
        )
        ctx = ResearchContext("cancel", {}, None, None)
        task = asyncio.create_task(
            Runner.run(
                Agent(name="manager", model=root, tools=[tool]),
                "brief",
                context=ctx,
                hooks=ResearchHooks(),
                run_config=config_without_context(),
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(exited.wait(), 5)
        assert len(root.calls) == len(child.calls) == 1
        assert ctx.wrappers[0].usage.requests == 1

    asyncio.run(scenario())
