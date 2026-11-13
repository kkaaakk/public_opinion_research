# ruff: noqa: E402
"""Real DBOS subprocess with controlled SDK inference and durable external effects."""

from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Barrier

os.environ.update(
    PYTHON_DOTENV_DISABLED="1",
    OPENAI_AGENTS_DISABLE_TRACING="1",
    LANGSMITH_TRACING="false",
    LANGCHAIN_TRACING_V2="false",
)
import dotenv

dotenv.load_dotenv = lambda *a, **kw: False
_original_connect = socket.socket.connect


def local_connect(self, address):
    if isinstance(address, tuple) and address[0] not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise AssertionError("Fault fixture attempted external network")
    return _original_connect(self, address)


socket.socket.connect = local_connect

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from dbos import DBOS, DBOSClient
from dbos_support import NEO4J, NEO4J_PASSWORD
from langchain_core.runnables import RunnableConfig
from neo4j import GraphDatabase
from sqlalchemy.exc import SQLAlchemyError
from test_python_workflow_sdk import install_sdk
from test_sdk_context import _payload
from test_sdk_runtime import TOKENS, _tool

from open_deep_research.runtime import business_agent
from open_deep_research.web import history
from open_deep_research.web.executor import launch
from open_deep_research.web.run_data import RunConflict
from open_deep_research.workflow.dbos_workflow import release_step, reserve
from open_deep_research.workflow.run_facts import RunFacts, facts


def install(directory, scenario):
    patch = pytest.MonkeyPatch()
    plan_mode = {
        "clarification": "clarify",
        "clarification-deadline": "clarify",
        "multi-clarification": "clarify-3",
        "planning-release": "clarify-2",
    }.get(scenario, "normal")

    def record_plan(prompt):
        with sqlite3.connect(directory / "planning.db") as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS plan_requests(id INTEGER PRIMARY KEY, prompt TEXT)"
            )
            db.execute("INSERT INTO plan_requests(prompt) VALUES (?)", (prompt,))

    async def record_revision(_):
        path = directory / "revision-calls"
        first_call = not path.exists()
        with path.open("a", encoding="utf-8") as log:
            log.write("call\n")
        if scenario == "revision-retry" and first_call:
            raise RuntimeError("Settled provider failure before the user's retry")

    install_sdk(
        patch,
        directory / "t",
        plan_mode,
        on_plan=record_plan if plan_mode != "normal" else None,
        on_revision=record_revision if scenario.startswith("revision-") else None,
    )
    store = history.Neo4jHistoryStore(
        GraphDatabase.driver(NEO4J, auth=("neo4j", NEO4J_PASSWORD))
    )
    patch.setattr(history, "get_history_store", lambda: store)
    if scenario == "plan-error":
        from langchain_core.runnables import RunnableLambda

        from open_deep_research.workflow import shared
        from open_deep_research.workflow.brief import PlanDecision

        service = shared.configurable_model

        class PlanningFault:
            def with_config(self, cfg):
                return self

            async def ainvoke(self, *args, **kwargs):
                return await service.ainvoke(*args, **kwargs)

            def with_structured_output(self, schema, include_raw=False):
                raw = service.with_structured_output(schema, include_raw)

                async def call(messages, config=None):
                    response = await raw.ainvoke(messages, config)
                    if schema is PlanDecision:
                        with sqlite3.connect(directory / "external.db") as db:
                            db.execute(
                                "CREATE TABLE IF NOT EXISTS service_models(id INTEGER PRIMARY KEY)"
                            )
                            attempt = db.execute(
                                "INSERT INTO service_models DEFAULT VALUES"
                            ).lastrowid
                        if attempt == 1:
                            raise RuntimeError(
                                "Synthetic transient provider failure after usage"
                            )
                    return response

                return RunnableLambda(call)

        patch.setattr(shared, "configurable_model", PlanningFault())
    if scenario == "gap-checkpoint":
        from langchain_core.runnables import RunnableLambda
        from test_python_workflow import gap

        from open_deep_research.state import ResearchReview
        from open_deep_research.workflow import shared

        original = shared.configurable_model

        class GapServices:
            def with_config(self, cfg):
                return self

            def with_structured_output(self, schema, include_raw=False):
                chain = original.with_structured_output(schema, include_raw)
                if schema is not ResearchReview:
                    return chain

                async def reply(messages, config=None):
                    value = await chain.ainvoke(messages, config)
                    done = "Current research round: 2" in str(messages[0].content)
                    value["parsed"] = ResearchReview(
                        research_complete=done,
                        next_tasks=[]
                        if done
                        else [gap("date"), gap("approval", "internal_knowledge")],
                    )
                    return value

                return RunnableLambda(reply)

            async def ainvoke(self, *args, **kwargs):
                return await original.ainvoke(*args, **kwargs)

        patch.setattr(shared, "configurable_model", GapServices())
    if (
        scenario.startswith("legacy-")
        and not (directory / (scenario + ".release")).exists()
    ):
        from functools import partial

        from reference_research import legacy_research_patch

        from open_deep_research.workflow import dbos_workflow as engine

        async def legacy_producer(state, config, *, role):
            # Run the actual SDK/Harness, but persist the frozen 31e8ad8 wire
            # contract. No journal/Artifact fabrication or external replay.
            value = await business_agent.run_business_agent(
                role=role, state=state, config=config
            )
            return legacy_research_patch(value, state)

        for role in ("public_signal", "internal_knowledge"):
            patch.setitem(
                engine.OPERATIONS, role + "_agent", partial(legacy_producer, role=role)
            )

    old_finish = RunFacts.finish_unit
    old_acquire = RunFacts.acquire
    old_operation = RunFacts.operation
    old_check = RunFacts.check
    lost_receipt_producers = Barrier(2)

    def gate(name):
        (directory / name).write_text("ready")
        while not (directory / (name + ".release")).exists():
            time.sleep(0.05)

    if scenario in {"revision-ack", "revision-before-result", "revision-retry"}:
        original_account = RunFacts.accounted_usage

        def account(self, unit, observed, carry):
            billed = original_account(self, unit, observed, carry)
            if ":brief-revision:" in unit:
                with (directory / "revision-accounting").open(
                    "a", encoding="utf-8"
                ) as log:
                    log.write("billed\n")
                if scenario == "revision-before-result" or (
                    scenario == "revision-retry"
                    and (directory / "revision-calls").read_text().splitlines()
                    == ["call", "call"]
                ):
                    gate(scenario)
            return billed

        patch.setattr(RunFacts, "accounted_usage", account)

    if scenario == "planning-release":
        from open_deep_research.workflow import dbos_workflow as engine

        original_release_step = engine.release_step
        original_release = RunFacts.release

        def release(self, run_id):
            original_release(self, run_id)
            if DBOS.workflow_id == run_id + ":planning:2" and DBOS.step_id is None:
                gate(scenario)

        async def release_step_then_gate(run_id):
            # If planning uses a native release Step, pause after its journal
            # has committed. On replay the cached Step cannot release again.
            await original_release_step(run_id)
            if DBOS.workflow_id == run_id + ":planning:2":
                await asyncio.to_thread(gate, scenario)

        patch.setattr(RunFacts, "release", release)
        patch.setattr(engine, "release_step", release_step_then_gate)

    if scenario == "clarification-deadline":
        original_recv = DBOS.recv_async

        async def recv(topic=None, timeout_seconds=60):
            if topic == "clarification":
                if (directory / (scenario + ".release")).exists():
                    assert timeout_seconds == 0
                else:
                    await asyncio.to_thread(gate, scenario)
            return await original_recv(topic, timeout_seconds)

        patch.setattr(DBOS, "recv_async", staticmethod(recv))

    if scenario.startswith("retry-"):
        from open_deep_research.budget import capture_budget_usage
        from open_deep_research.web.durable import RunClient
        from open_deep_research.workflow import dbos_workflow as engine

        original_review = engine.OPERATIONS["research_review"]
        original_rewind = DBOSClient.rewind_workflow
        original_apply = RunClient._rewind_safe
        original_sleep = DBOS.sleep_async

        async def retry_review(state, config):
            with sqlite3.connect(directory / "external.db", timeout=10) as db:
                db.execute("CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY)")
                attempt = db.execute("INSERT INTO reviews DEFAULT VALUES").lastrowid
            if scenario == "retry-nested" and attempt == 2:
                await asyncio.to_thread(gate, "nested-leaf")
            if attempt <= 2:
                capture_budget_usage({"model_calls": 1, "input_tokens": 2})
                raise RuntimeError("Synthetic safe read failure with durable witness")
            return await original_review(state, config)

        def rewind(client, workflow_id, **kwargs):
            if scenario == "retry-before" and workflow_id.endswith(":research"):
                gate(scenario)
            result = original_rewind(client, workflow_id, **kwargs)
            if (
                scenario == "retry-nested"
                and ":review:" in workflow_id
                and not (directory / "nested-rewound").exists()
            ):
                (directory / "nested-rewound").touch()
                raise SQLAlchemyError("Synthetic leaf rewind ACK loss")
            if scenario == "retry-between" and workflow_id.endswith(":research"):
                gate(scenario)
            if (
                scenario == "retry-error"
                and ":" not in workflow_id
                and not (directory / "retry-error.release").exists()
            ):
                (directory / "retry-error-native").write_text(
                    "real native commit before lost ACK"
                )
                raise SQLAlchemyError("Synthetic native control commit ACK loss")
            return result

        def apply(client, run_id, command):
            # Fault injection only: execute the entire real grant/native rewind
            # first, release its transaction, then stop before the Step ACK.
            # Never substitute a recovery result or fake a journal write.
            result = original_apply(client, run_id, command)
            if scenario == "retry-after":
                gate(scenario)
            return result

        async def sleep(seconds):
            if (
                scenario == "retry-nested"
                and DBOS.workflow_id.endswith(":finalize")
                and (directory / "nested-rewound").exists()
            ):
                await asyncio.to_thread(gate, "retry-nested")
            if (
                scenario == "retry-error"
                and DBOS.workflow_id.endswith(":finalize")
                and (directory / "retry-error-native").exists()
            ):
                # The retryable result is already in the real native Step journal.
                await asyncio.to_thread(gate, scenario)
            return await original_sleep(seconds)

        patch.setattr(DBOS, "sleep_async", staticmethod(sleep))
        patch.setitem(engine.OPERATIONS, "research_review", retry_review)
        patch.setattr(DBOSClient, "rewind_workflow", rewind)
        patch.setattr(RunClient, "_rewind_safe", apply)

    def finish(self, unit, permit, **data):
        if (
            scenario == "revision-ack"
            and ":brief-revision:" in unit
            and data.get("artifact")
        ):
            gate(scenario)
        if (
            scenario == "plan-error"
            and unit.endswith(":plan:1")
            and data.get("artifact")
        ):
            gate("plan-error")
        if (
            scenario == "runner-error"
            and unit.endswith(":public_signal:1")
            and data.get("error")
        ):
            gate("runner-error")
        value = old_finish(self, unit, permit, **data)
        if (
            scenario == "legacy-artifact"
            and unit.endswith(":public_signal:1")
            and data.get("artifact")
        ):
            # The real domain transaction has committed; native Step ACK has not.
            gate("legacy-artifact")
        return value

    patch.setattr(RunFacts, "finish_unit", finish)

    def acquire(self, run_id, unit, name, **kw):
        if (
            scenario in {"checkpoint", "legacy-checkpoint"}
            and name == "research_review"
        ):
            gate(scenario)
        if scenario == "gap-checkpoint" and unit.endswith(":public_signal:2"):
            gate("gap-checkpoint")
        return old_acquire(self, run_id, unit, name, **kw)

    def operation(self, unit, permit, identity, status="started", **kwargs):
        if (
            scenario == "revision-unconfirmed"
            and ":brief-revision:" in unit
            and identity["name"] == "llm"
            and status == "returned"
        ):
            gate(scenario)
        if (
            scenario == "lost-receipt"
            and status == "returned"
            and identity["role"] in {"public_signal", "internal_knowledge"}
            and not (directory / (scenario + ".release")).exists()
        ):
            # Both producers must be holding uncommitted receipts before the kill.
            lost_receipt_producers.wait()
            gate(scenario)
        return old_operation(self, unit, permit, identity, status, **kwargs)

    def check(self, *args, **kwargs):
        try:
            return old_check(self, *args, **kwargs)
        except SQLAlchemyError:
            if scenario == "model-wait":
                (directory / "control-failed").write_text(
                    "actual durable control connection error"
                )
            raise

    patch.setattr(RunFacts, "check", check)
    patch.setattr(RunFacts, "acquire", acquire)
    patch.setattr(RunFacts, "operation", operation)

    from open_deep_research.runtime.sdk_context import SDKContextHarness

    original_llm_end = SDKContextHarness.on_llm_end
    original_tool_end = SDKContextHarness.on_tool_end
    original_filter = SDKContextHarness.filter_model_input
    original_settle = SDKContextHarness._settle_on_exit

    if scenario in {"runner-batch", "batch-error-cache"}:
        from langchain_core.language_models.fake_chat_models import (
            FakeMessagesListChatModel,
        )

        original_generate = FakeMessagesListChatModel._generate

        def witnessed_generate(self, *args, **kwargs):
            with sqlite3.connect(directory / "external.db", timeout=10) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS lc_models(id INTEGER PRIMARY KEY)"
                )
                db.execute("INSERT INTO lc_models DEFAULT VALUES")
            return original_generate(self, *args, **kwargs)

        patch.setattr(FakeMessagesListChatModel, "_generate", witnessed_generate)

    async def settle(self, reason):
        if (
            scenario == "batch-error-cache"
            and self.scope.role == "public_signal"
            and reason == "exception"
        ):
            await asyncio.to_thread(gate, scenario)
        return await original_settle(self, reason)

    async def llm_end(self, *args, **kwargs):
        await original_llm_end(self, *args, **kwargs)
        if (
            scenario == "runner-model"
            and self.scope.role == "public_signal"
            and self.model_calls == 1
        ):
            await asyncio.to_thread(gate, scenario)
        if (
            scenario == "runner-model"
            and self.scope.role == "public_signal"
            and self.model_calls == 2
        ):
            await asyncio.to_thread(gate, "runner-second")

    async def tool_end(self, *args, **kwargs):
        if scenario == "runner-tool" and self.scope.role == "public_signal":
            await asyncio.to_thread(gate, scenario)
        await original_tool_end(self, *args, **kwargs)

    async def model_filter(self, data):
        result = await original_filter(self, data)
        if (
            scenario == "batch-error-cache"
            and self.scope.role == "public_signal"
            and self.model_calls == 2
        ):
            from open_deep_research.runtime.sdk_context import (
                ResearchGraphSettlementError,
            )

            raise ResearchGraphSettlementError(
                "Controlled context failure after actual ingestion"
            )
        if (
            scenario == "runner-batch"
            and self.scope.role == "public_signal"
            and self.model_calls == 2
        ):
            await asyncio.to_thread(gate, scenario)
        return result

    patch.setattr(SDKContextHarness, "on_llm_end", llm_end)
    patch.setattr(SDKContextHarness, "on_tool_end", tool_end)
    patch.setattr(SDKContextHarness, "filter_model_input", model_filter)
    patch.setattr(SDKContextHarness, "_settle_on_exit", settle)

    @asynccontextmanager
    async def model(*args, **kwargs):
        def record_model(role):
            with sqlite3.connect(directory / "external.db", timeout=10) as db:
                db.execute("CREATE TABLE IF NOT EXISTS models(role text)")
                db.execute("INSERT INTO models VALUES (?)", (role,))

        current = {}

        async def first(call):
            role = call.system_instructions.split("<Role>\n", 1)[1].split(
                "\n</Role>", 1
            )[0]
            current["role"] = role
            record_model(role)
            if scenario == "runner-error" and role == "public_signal":
                raise RuntimeError("Synthetic provider exception")
            if scenario == "model-wait" and role == "public_signal":
                (directory / "model-wait").write_text("ready")
                while not (directory / "model-wait.release").exists():
                    await asyncio.sleep(0.05)
            if role in {"public_signal", "internal_knowledge"}:
                return [
                    function_call(
                        "web_search" if role == "public_signal" else "rag_search",
                        {"query": role},
                        call_id="effect-" + role,
                    )
                ]
            return [assistant_message("analysis")]

        def second(call):
            record_model(current["role"])
            return [assistant_message("research complete")]

        async def reply(call):
            # A provider sees the prepared input, not an in-process script cursor.
            # DBOS may replay the first response without invoking this model.
            if any(item.get("type") == "function_call_output" for item in call.input):
                current["role"] = call.system_instructions.split("<Role>\n", 1)[
                    1
                ].split("\n</Role>", 1)[0]
                return second(call)
            return await first(call)

        yield ScriptedModel(
            [ModelStep.respond(reply) for _ in range(10)],
            default_usage=TOKENS,
            emit_traces=os.environ.get("POR_TRACING_ENABLED", "").lower() == "true",
        )

    async def tools(config, role, spec):
        if role not in {"public_signal", "internal_knowledge"}:
            return []

        async def effect(query, config: RunnableConfig, limit=3):
            op = config.get("metadata", {}).get("operation_id")
            assert op
            with sqlite3.connect(directory / "external.db", timeout=5) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS effects(operation text PRIMARY KEY, role text)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS invocations(operation text, role text)"
                )
                db.execute("INSERT INTO invocations VALUES (?,?)", (op, role))
                db.execute(
                    "INSERT INTO effects VALUES (?,?) ON CONFLICT DO NOTHING",
                    (op, role),
                )
            return _payload(role)

        return [_tool(role, coroutine=effect)]

    patch.setattr(business_agent, "sdk_model", model)
    patch.setattr(business_agent, "_business_agent_tools", tools)
    return patch, store


@DBOS.step(preemptible=False)
async def ownership_step(request):
    directory = Path(request["directory"])
    unit = request["run_id"] + ":ownership"
    permit, artifact, _ = await asyncio.to_thread(
        facts().acquire, request["run_id"], unit, "ownership"
    )
    if artifact is not None:
        return artifact
    (directory / "old-active").write_text(DBOS.executor_id)
    while not (directory / "ownership.release").exists():
        await asyncio.sleep(0.05)
    try:
        await asyncio.to_thread(facts().check, unit, permit)
    except RunConflict:
        (directory / "old-denied").write_text("stale executor denied")
        raise
    raise AssertionError("A stale executor was admitted")


@DBOS.workflow(name="por.test.ownership")
async def ownership_workflow(request):
    return await ownership_step(request)


@DBOS.step(preemptible=True)
async def queue_wait(request):
    directory = Path(request["directory"])
    (directory / (request["run_id"] + ".entered")).write_text(DBOS.executor_id)
    while not (directory / (request["run_id"] + ".release")).exists():
        await asyncio.sleep(0.05)


@DBOS.workflow(name="por.test.queue")
async def queue_workflow(request):
    await reserve(request["run_id"])
    try:
        await queue_wait(request)
    finally:
        await release_step(request["run_id"])


def main():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("directory", type=Path)
    p.add_argument("--scenario", default="normal")
    p.add_argument("--executor", required=True)
    args = p.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    patch, store = install(args.directory, args.scenario)
    launch(
        args.executor,
        queues=os.environ.get("TEST_EXECUTOR_QUEUES", "").split(",")
        if os.environ.get("TEST_EXECUTOR_QUEUES")
        else None,
    )
    (args.directory / "executor-ready").write_text("ready")
    try:
        while not (args.directory / "stop").exists():
            time.sleep(0.1)
    finally:
        DBOS.destroy()
        store.close()
        from open_deep_research.observability.telemetry import shutdown_telemetry

        shutdown_telemetry()
        patch.undo()


if __name__ == "__main__":
    main()
