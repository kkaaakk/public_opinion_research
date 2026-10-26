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
    install_sdk(patch, directory / "t")
    store = history.Neo4jHistoryStore(
        GraphDatabase.driver(NEO4J, auth=("neo4j", NEO4J_PASSWORD))
    )
    patch.setattr(history, "get_history_store", lambda: store)
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
    old_acquire = RunFacts.acquire
    old_operation = RunFacts.operation
    old_check = RunFacts.check

    def gate(name):
        (directory / name).write_text("ready")
        while not (directory / (name + ".release")).exists():
            time.sleep(0.05)

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
            if attempt <= 2:
                capture_budget_usage({"model_calls": 1, "input_tokens": 2})
                raise RuntimeError("Synthetic safe read failure with durable witness")
            return await original_review(state, config)

        def rewind(client, workflow_id, **kwargs):
            if scenario == "retry-before" and workflow_id.endswith(":research"):
                gate(scenario)
            result = original_rewind(client, workflow_id, **kwargs)
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

    def acquire(self, run_id, unit, name, **kw):
        if scenario == "checkpoint" and name == "research_review":
            gate("checkpoint")
        if scenario == "gap-checkpoint" and unit.endswith(":public_signal:2"):
            gate("gap-checkpoint")
        return old_acquire(self, run_id, unit, name, **kw)

    def operation(self, unit, permit, identity, status="started"):
        if scenario == "lost-receipt" and status == "returned":
            gate("lost-receipt")
        return old_operation(self, unit, permit, identity, status)

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

    @asynccontextmanager
    async def model(*args, **kwargs):
        async def first(call):
            role = call.system_instructions.split("<Role>\n", 1)[1].split(
                "\n</Role>", 1
            )[0]
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

        yield ScriptedModel(
            [ModelStep.respond(first), [assistant_message("research complete")]],
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
