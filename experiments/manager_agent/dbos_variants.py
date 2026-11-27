"""Small DBOSRunner counterexamples using the wheel installed from the root lock."""

import asyncio
from unittest.mock import patch

from agents import (
    Agent,
    ModelSettings,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    function_tool,
)
from agents.exceptions import UserError
from agents.models.multi_provider import MultiProvider
from agents.testing import ModelStep, ScriptedModel, function_call
from dbos import DBOS, SetWorkflowID
from dbos_openai_agents import DBOSRunner
from manager import AdmittedModel, final_step, fixed_step, tool_outputs
from postgres_lab import Lab

pause_at = ""


async def witness(lab, marker):
    await asyncio.to_thread(lab.observe, marker)


async def crash_window(lab, marker):
    if pause_at == marker:
        await witness(lab, "paused:" + marker)
        await asyncio.Event().wait()


def counted_model(lab, name, steps):
    async def count(purpose):
        await witness(lab, f"physical:{purpose}")

    return AdmittedModel(ScriptedModel(steps), count, name)


def child_call(name):
    return function_call(
        name, {"input": f"Find {name} sources"}, call_id=f"{name}-call"
    )


def parent_model(lab, *calls):
    def respond(call):
        # A cached model Step does not advance the new process's mock provider.
        outputs = tool_outputs(call)
        if not outputs:
            return fixed_step(*calls)
        answer = "; ".join(f"{key}={value}" for key, value in sorted(outputs.items()))
        return final_step(answer)

    return counted_model(lab, "manager", [ModelStep.respond(respond)] * 2)


class CountHook(RunHooks):
    def __init__(self, lab):
        self.lab = lab

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        await witness(self.lab, f"hook:{agent.name}")


@DBOS.workflow(name="poc.role")
async def role_workflow(schema, question):
    lab = Lab(schema)
    model = counted_model(lab, "public", [final_step(f"verified: {question}")])
    result = await DBOSRunner.run(
        Agent(name="public", model=model),
        question,
        context={"scope": "serialized specialist input"},
        hooks=CountHook(lab),
        run_config=RunConfig(tracing_disabled=True),
        max_turns=2,
    )
    return result.final_output, result.context_wrapper.usage.requests


@DBOS.workflow(name="poc.child_workflow")
async def workflow_b(schema):
    lab = Lab(schema)

    @function_tool(failure_error_function=None)
    async def public(ctx: RunContextWrapper[dict], input: str) -> str:
        assert ctx.context["owner"] == "manager"
        with SetWorkflowID(f"{DBOS.workflow_id}/public"):
            handle = await DBOS.start_workflow_async(role_workflow, schema, input)
        answer, calls = await handle.get_result()
        await crash_window(lab, "after-child-workflow")
        return f"{answer} (child requests={calls})"

    result = await Runner.run(
        Agent(
            name="manager",
            model=parent_model(lab, child_call("public")),
            tools=[public],
        ),
        "approved brief",
        context={"owner": "manager"},
        hooks=CountHook(lab),
        run_config=RunConfig(tracing_disabled=True),
        max_turns=3,
    )
    return {
        "output": result.final_output,
        "parent_requests": result.context_wrapper.usage.requests,
    }


class Order:
    """Force each legal sibling schedule without relying on timing."""

    def __init__(self, first):
        self.first = first
        self.arrived = set()
        self.both = asyncio.Event()
        self.first_done = asyncio.Event()

    async def before(self, name):
        self.arrived.add(name)
        if len(self.arrived) == 2:
            self.both.set()
        await asyncio.wait_for(self.both.wait(), 10)
        if name != self.first:
            await asyncio.wait_for(self.first_done.wait(), 10)

    def after(self, name):
        if name == self.first:
            self.first_done.set()


@DBOS.step(name="poc.shared_tool_slot")
async def shared_tool_slot(schema, role):
    await witness(Lab(schema), f"tool-body:{role}")
    return f"{role} evidence"


@DBOS.workflow(name="poc.runner_variant")
async def workflow_c(schema, mode):
    lab = Lab(schema)
    names = ("public",) if mode in {"explicit", "named"} else ("public", "knowledge")
    order = Order("public" if pause_at else "knowledge")
    first_child_responded = asyncio.Event()

    class ParentHooks(CountHook):
        def __init__(self, lab):
            super().__init__(lab)
            self.turn = 0

        async def on_llm_start(self, context, agent, system_prompt, input_items):
            self.turn += 1
            await super().on_llm_start(context, agent, system_prompt, input_items)
            if self.turn == 2:
                await crash_window(lab, "after-child-steps")

        async def on_tool_start(self, context, agent, tool):
            if mode == "turnstile" and tool.name == "knowledge":
                await asyncio.wait_for(first_child_responded.wait(), 10)

    class ChildHooks(CountHook):
        async def on_llm_start(self, context, agent, system_prompt, input_items):
            await super().on_llm_start(context, agent, system_prompt, input_items)
            if mode == "model-order":
                await order.before(agent.name)

        async def on_llm_end(self, context, agent, response):
            if mode == "model-order":
                order.after(agent.name)
            if mode == "turnstile" and agent.name == "public":
                first_child_responded.set()

    def tool_order_child(name):
        @function_tool(failure_error_function=None)
        async def collect() -> str:
            await order.before(name)
            value = await shared_tool_slot(schema, name)
            order.after(name)
            return value

        def complete(call):
            return final_step(tool_outputs(call)[f"{name}-collect"])

        return Agent(
            name=name,
            model=counted_model(
                lab,
                name,
                [
                    fixed_step(function_call("collect", {}, call_id=f"{name}-collect")),
                    ModelStep.respond(complete),
                ],
            ),
            tools=[collect],
        )

    models = {
        name: counted_model(lab, name, [final_step(f"{name} evidence")])
        for name in names
    }
    children = [
        tool_order_child(name)
        if mode == "tool-order"
        else Agent(name=name, model=models[name] if mode == "explicit" else name)
        for name in names
    ]
    parent = Agent(
        name="manager",
        model=parent_model(lab, *(child_call(name) for name in names)),
        model_settings=ModelSettings(parallel_tool_calls=True),
        tools=[
            child.as_tool(
                child.name,
                "Collect",
                hooks=ChildHooks(lab),
                failure_error_function=None,
            )
            for child in children
        ],
    )

    def mock_provider_transport(provider, name):
        # Replace only the provider's model factory result. DBOSRunner's installed
        # DBOSModelProvider, wrapper, Turnstile and Runner remain on the execution path.
        return models[name]

    with patch.object(MultiProvider, "get_model", mock_provider_transport):
        try:
            result = await DBOSRunner.run(
                parent,
                "approved brief",
                hooks=ParentHooks(lab),
                run_config=RunConfig(tracing_disabled=True),
                max_turns=3,
            )
        except UserError as error:
            if mode == "turnstile" and isinstance(error.__cause__, KeyError):
                # Preserve the demonstrated failure across the process boundary.
                raise RuntimeError(
                    f"Turnstile missing call {error.__cause__}"
                ) from error
            raise
    return {
        "output": result.final_output,
        "sdk_requests": result.context_wrapper.usage.requests,
    }
