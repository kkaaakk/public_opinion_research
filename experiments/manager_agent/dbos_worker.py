"""Separate DBOS worker for real PostgreSQL and offline inference tests."""

import argparse
import asyncio

from agents import Agent, RunConfig, Runner, function_tool
from agents.exceptions import UserError
from agents.testing import ScriptedModel
from dbos import DBOS
from manager import (
    BRIEF,
    RAW,
    ROLES,
    AdmittedModel,
    BudgetExhausted,
    ResearchContext,
    ResearchHooks,
    UnknownEffect,
    config,
    final_step,
    research_agents,
)
from offline import offline
from postgres_lab import APP, EXECUTOR, VERSION, Lab

QUEUE = "poc-research"
pause_at = ""


async def gate(lab, marker):
    if pause_at == marker:
        await asyncio.to_thread(lab.observe, "paused:" + marker)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.to_thread(lab.observe, "cancelled:" + marker)
            raise


async def collect(lab, role, key):
    evidence_id = f"{role}:{key}"
    saved = await asyncio.to_thread(lab.reused_evidence, evidence_id)
    if saved is not None:
        return saved
    if role == "public":
        reference = await asyncio.to_thread(lab.admit_operation, evidence_id)
        if reference is None:
            reference = await asyncio.to_thread(lab.external_call, evidence_id)
            await gate(lab, "after-effect")
            await asyncio.to_thread(
                lab.execute,
                "UPDATE operation SET reference = %s WHERE id = %s",
                reference,
                evidence_id,
            )
            await gate(lab, "after-receipt")
            saved = RAW
        else:
            saved = await asyncio.to_thread(lab.external_lookup, evidence_id, reference)
    else:
        saved = RAW
    await asyncio.to_thread(lab.store_evidence, evidence_id, saved)
    return saved


@DBOS.step(name="poc.nested_probe")
async def nested_probe(schema):
    await asyncio.to_thread(Lab(schema).observe, "nested-step-body")


@DBOS.step(name="poc.research_stage", preemptible=True)
async def research_stage(schema):
    lab = Lab(schema)
    await asyncio.to_thread(lab.observe, "research-entered")
    await asyncio.to_thread(lab.check_operations)
    # This probe executes inline because the active DBOS context is already a Step.
    await nested_probe(schema)

    def bind_mock(role):
        @function_tool(name_override=ROLES[role][1], failure_error_function=None)
        async def read(key: str) -> str:
            return await collect(lab, role, key)

        return read

    async def admit(purpose):
        return await asyncio.to_thread(lab.admit, purpose)

    async def record(attempt, usage):
        await asyncio.to_thread(lab.settle, attempt, usage)

    knowledge_done = asyncio.Event()

    class StageHooks(ResearchHooks):
        async def on_agent_end(self, context, agent, output):
            await super().on_agent_end(context, agent, output)
            if agent.name == "knowledge":
                knowledge_done.set()

    async def after_evidence():
        if pause_at == "after-evidence":
            await asyncio.wait_for(knowledge_done.wait(), 10)
            await gate(lab, "after-evidence")

    hooks = StageHooks()
    context = ResearchContext("postgres-research", {"policy": "approved"}, lab, lab)
    manager, _ = research_agents(
        {role: bind_mock(role) for role in ROLES},
        context,
        gap=False,
        hooks=hooks,
        admit=admit,
        record=record,
        first_public=after_evidence,
    )
    try:
        result = await Runner.run(
            manager,
            BRIEF,
            context=context,
            hooks=hooks,
            run_config=config(),
            max_turns=5,
        )
    except UserError as error:
        # SDK run_data can reference agents and local tools that DBOS cannot pickle.
        cause = error.__cause__
        if isinstance(cause, (BudgetExhausted, UnknownEffect)):
            raise type(cause)(str(cause)) from error
        raise
    return {
        "summary": result.final_output.model_dump(),
        "sdk_requests": result.context_wrapper.usage.requests,
    }


@DBOS.step(name="poc.report")
async def report_stage(schema, research):
    lab = Lab(schema)

    async def admit(purpose):
        return await asyncio.to_thread(lab.admit, purpose)

    async def record(attempt, usage):
        await asyncio.to_thread(lab.settle, attempt, usage)

    persisted = await asyncio.to_thread(
        lab.execute, "SELECT id FROM evidence ORDER BY id"
    )
    assert research["summary"]["evidence_ids"] == [key for (key,) in persisted]
    model = AdmittedModel(
        ScriptedModel([final_step("validated report")]), admit, "report", record
    )
    result = await Runner.run(
        Agent(name="report", model=model),
        research["summary"]["summary"],
        run_config=RunConfig(tracing_disabled=True),
        max_turns=1,
    )
    return {**research, "report": result.final_output}


@DBOS.workflow(name="poc.outer")
async def outer_workflow(schema):
    research = await research_stage(schema)
    await gate(Lab(schema), "after-stage")
    return await report_stage(schema, research)


async def main(schema, marker):
    global pause_at
    pause_at = marker
    lab = Lab(schema)
    import dbos_variants

    dbos_variants.pause_at = marker
    DBOS(
        config={
            "name": APP,
            "application_version": VERSION,
            "executor_id": EXECUTOR,
            "system_database_url": lab.url,
            "dbos_system_schema": lab.journal,
            "log_level": "WARNING",
        }
    )
    DBOS.launch()
    await DBOS.register_queue_async(QUEUE, polling_interval_sec=0.2)
    try:
        print("executor ready", flush=True)
        await asyncio.Event().wait()
    finally:
        DBOS.destroy()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("schema")
    parser.add_argument("--pause-at", default="")
    args = parser.parse_args()
    with offline():
        asyncio.run(main(args.schema, args.pause_at))
