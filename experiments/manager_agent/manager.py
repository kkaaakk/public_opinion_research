"""A deterministic research scenario driven by the Agents SDK Runner."""

import asyncio
import json
from dataclasses import dataclass, field

from agents import Agent, Model, ModelSettings, RunConfig, RunHooks, Runner, Usage
from agents.run_config import ModelInputData
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents.usage import RequestUsage
from pydantic import BaseModel

BRIEF = "APPROVED BRIEF: assess the delivery disruption using external and internal evidence."
RAW = "RAW EVIDENCE FOR SPECIALIST ONLY"
ROLES = {
    "public": ("Search public signals for independent evidence.", "read_public"),
    "knowledge": ("Search internal policy and operational records.", "read_knowledge"),
    "analysis": ("Check claims against cited evidence IDs.", "check_citations"),
}


class ResearchTask(BaseModel):
    question: str
    prior_evidence_ids: list[str]


class SpecialistResult(BaseModel):
    findings: str
    evidence_ids: list[str]
    gap: bool


class ResearchSummary(BaseModel):
    summary: str
    evidence_ids: list[str]


class BudgetExhausted(Exception):
    pass


class UnknownEffect(Exception):
    pass


@dataclass
class MemoryBudget:
    """Per-process admission example with calls reserved for the report."""

    limit: int
    report_reserve: int = 1
    admitted: list[str] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def admit(self, purpose: str) -> None:
        async with self.lock:
            limit = (
                self.limit if purpose == "report" else self.limit - self.report_reserve
            )
            if len(self.admitted) >= limit:
                raise BudgetExhausted(purpose)
            self.admitted.append(purpose)


class AdmittedModel(Model):
    """Place admission at both inference entry points, before the mock provider."""

    def __init__(self, model, admit, purpose="research", record=None):
        self.model = model
        self.admit = admit
        self.purpose = purpose
        self.record = record

    async def get_response(self, *args, **kwargs):
        attempt = await self.admit(self.purpose)
        response = await self.model.get_response(*args, **kwargs)
        if self.record is not None:
            await self.record(attempt, response.usage)
        return response

    async def stream_response(self, *args, **kwargs):
        if self.record is not None:
            raise NotImplementedError(
                "The PostgreSQL receipt fixture runs without streaming."
            )
        await self.admit(self.purpose)
        async for event in self.model.stream_response(*args, **kwargs):
            yield event


def fixed_step(*items):
    return ModelStep(
        output=items,
        usage=Usage(
            requests=1,
            input_tokens=11,
            output_tokens=3,
            total_tokens=14,
            request_usage_entries=[
                RequestUsage(
                    input_tokens=11,
                    output_tokens=3,
                    total_tokens=14,
                    input_tokens_details={"cached_tokens": 0, "cache_write_tokens": 0},
                    output_tokens_details={"reasoning_tokens": 0},
                )
            ],
        ),
    )


def final_step(value):
    text = value.model_dump_json() if isinstance(value, BaseModel) else value
    return fixed_step(assistant_message(text))


def tool_step(role, call_id, question, prior=()):
    args = ResearchTask(question=question, prior_evidence_ids=list(prior))
    return function_call(f"ask_{role}", args.model_dump(), call_id=call_id)


def build_input(options):
    return ResearchTask.model_validate(options["params"]).model_dump_json()


def tool_outputs(call):
    return {
        item["call_id"]: item["output"]
        for item in call.input
        if item.get("type") == "function_call_output"
    }


def child_observation(payload):
    """Project an SDK child update to fields safe for a public event."""

    event = payload["event"]
    call = payload["tool_call"]
    if event.type != "agent_updated_stream_event" or call is None:
        return None
    return {
        "type": "agent_started",
        "role": payload["agent"].name,
        "parent_call_id": call.call_id,
    }


async def compact_result(result):
    return result.final_output_as(
        SpecialistResult, raise_if_incorrect_type=True
    ).model_dump_json()


@dataclass
class ResearchContext:
    run_id: str
    config: dict[str, str]
    budget: object
    evidence: object
    observations: list = field(default_factory=list)
    wrappers: list = field(default_factory=list)
    invocations: list = field(default_factory=list)


class ResearchHooks(RunHooks):
    async def on_agent_start(self, context, agent):
        context.context.wrappers.append(context)
        context.context.observations.append(("start", agent.name))

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        context.context.observations.append(("model", agent.name))

    async def on_tool_end(self, context, agent, tool, result):
        context.context.observations.append(("tool", agent.name, tool.name))

    async def on_agent_end(self, context, agent, output):
        context.context.observations.append(("end", agent.name))


def model_input(data):
    data.context.observations.append(("filter", data.agent.name))
    return ModelInputData(
        input=data.model_data.input,
        instructions=f"{data.model_data.instructions or ''}\nInput filter applied.",
    )


def config():
    return RunConfig(tracing_disabled=True, call_model_input_filter=model_input)


def specialist(role, model, tool):
    instructions, _ = ROLES[role]
    return Agent(
        name=role,
        instructions=instructions,
        model=model,
        model_settings=ModelSettings(max_tokens=64),
        tools=[tool],
        output_type=SpecialistResult,
    )


def as_research_tool(agent, **kwargs):
    return agent.as_tool(
        f"ask_{agent.name}",
        ROLES[agent.name][0],
        parameters=ResearchTask,
        input_builder=build_input,
        failure_error_function=None,
        max_turns=4,
        **kwargs,
    )


def research_agents(
    tools, context, *, gap, hooks, admit, stream=None, first_public=None, record=None
):
    """Assemble three scoped agents and a mock manager that branches on returned evidence."""

    async def extract(result):
        invocation = result.agent_tool_invocation
        context.invocations.append((invocation.tool_name, invocation.tool_call_id))
        return await compact_result(result)

    arrived = set()
    concurrent = asyncio.Event()

    def read_step(role, suffix, *, parallel=False):
        async def respond(call):
            assert {tool.name for tool in call.tools} == {ROLES[role][1]}
            if parallel:
                arrived.add(role)
                if arrived == {"public", "knowledge"}:
                    concurrent.set()
                await asyncio.wait_for(concurrent.wait(), 5)
            return fixed_step(
                function_call(
                    ROLES[role][1], {"key": suffix}, call_id=f"{role}-{suffix}"
                )
            )

        return ModelStep.respond(respond)

    def result_step(role, suffix, *, missing=False, before_return=None):
        async def respond(call):
            value = tool_outputs(call)[f"{role}-{suffix}"]
            assert RAW in value
            if before_return is not None:
                await before_return()
            return final_step(
                SpecialistResult(
                    findings=f"{role} findings for {suffix}",
                    evidence_ids=[f"{role}:{suffix}"],
                    gap=missing,
                )
            )

        return ModelStep.respond(respond)

    models = {
        "public": ScriptedModel(
            [
                read_step("public", "initial", parallel=True),
                result_step(
                    "public", "initial", missing=gap, before_return=first_public
                ),
            ]
            + (
                [read_step("public", "followup"), result_step("public", "followup")]
                if gap
                else []
            )
        ),
        "knowledge": ScriptedModel(
            [
                read_step("knowledge", "initial", parallel=True),
                result_step("knowledge", "initial"),
            ]
        ),
        "analysis": ScriptedModel(
            [read_step("analysis", "initial"), result_step("analysis", "initial")]
        ),
    }

    def choose_followup(call):
        outputs = tool_outputs(call)
        public = SpecialistResult.model_validate_json(outputs["public-first"])
        internal = SpecialistResult.model_validate_json(outputs["knowledge-first"])
        if public.gap:
            return fixed_step(
                tool_step(
                    "public", "public-more", "Verify missing data", public.evidence_ids
                )
            )
        return fixed_step(
            tool_step(
                "analysis",
                "analysis-first",
                "Check findings",
                public.evidence_ids + internal.evidence_ids,
            )
        )

    def send_to_analysis(call):
        results = [
            SpecialistResult.model_validate_json(value)
            for value in tool_outputs(call).values()
        ]
        ids = [key for result in results for key in result.evidence_ids]
        return fixed_step(
            tool_step("analysis", "analysis-first", "Check findings", ids)
        )

    def finish(call):
        outputs = tool_outputs(call)
        result = SpecialistResult.model_validate_json(outputs["analysis-first"])
        assert result.evidence_ids == ["analysis:initial"]
        ids = sorted(
            {
                key
                for value in outputs.values()
                for key in SpecialistResult.model_validate_json(value).evidence_ids
            }
        )
        return final_step(
            ResearchSummary(summary="Research complete", evidence_ids=ids)
        )

    plan = [
        fixed_step(
            tool_step("public", "public-first", "Find public signals"),
            tool_step("knowledge", "knowledge-first", "Find internal policy"),
        ),
        ModelStep.respond(choose_followup),
    ]
    if gap:
        plan.append(ModelStep.respond(send_to_analysis))
    models["manager"] = ScriptedModel([*plan, ModelStep.respond(finish)])
    children = [
        as_research_tool(
            specialist(
                role, AdmittedModel(models[role], admit, record=record), tools[role]
            ),
            custom_output_extractor=extract,
            hooks=hooks,
            on_stream=stream,
        )
        for role in ROLES
    ]
    manager = Agent(
        name="manager",
        instructions="Plan and reconcile a research brief. Return only supported evidence IDs.",
        model=AdmittedModel(models["manager"], admit, record=record),
        model_settings=ModelSettings(parallel_tool_calls=True, max_tokens=96),
        tools=children,
        output_type=ResearchSummary,
    )
    return manager, models


def in_memory_research(budget, **kwargs):
    from agents import RunContextWrapper, function_tool

    context = ResearchContext("fixture-run", {"policy": "approved"}, budget, {})

    def mock_read(role):
        @function_tool(name_override=ROLES[role][1], failure_error_function=None)
        async def read(ctx: RunContextWrapper[ResearchContext], key: str) -> str:
            assert ctx.context is context
            assert ctx.context.run_id == "fixture-run"
            assert ctx.context.config["policy"] == "approved"
            assert ctx.context.budget is budget
            ctx.context.evidence[f"{role}:{key}"] = RAW
            return RAW

        return read

    manager, models = research_agents(
        {role: mock_read(role) for role in ROLES}, context, admit=budget.admit, **kwargs
    )
    return manager, models, context


async def main():
    hooks = ResearchHooks()
    manager, models, context = in_memory_research(
        MemoryBudget(20), gap=True, hooks=hooks
    )
    result = await Runner.run(
        manager, BRIEF, context=context, hooks=hooks, run_config=config()
    )
    for model in models.values():
        model.assert_complete()
    print(
        json.dumps(
            {
                "result": result.final_output.model_dump(),
                "model_calls": {
                    role: len(model.calls) for role, model in models.items()
                },
                "sdk_requests": result.context_wrapper.usage.requests,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    from offline import offline

    with offline():
        asyncio.run(main())
