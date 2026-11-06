"""Isolated real-process POC for the unmodified official DBOSRunner."""

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

os.environ.update(PYTHON_DOTENV_DISABLED="1", OPENAI_AGENTS_DISABLE_TRACING="1")

from agents import Agent, Model, RunConfig, function_tool  # noqa: E402
from agents.items import ModelResponse  # noqa: E402
from agents.testing import assistant_message, function_call  # noqa: E402
from agents.usage import Usage  # noqa: E402
from dbos import DBOS  # noqa: E402
from dbos_openai_agents import DBOSRunner  # noqa: E402

from open_deep_research.web.run_data import RunJSON  # noqa: E402

directory = Path(sys.argv[1])
window = sys.argv[2]


def witness(kind):
    with sqlite3.connect(directory / "calls.db") as db:
        db.execute("CREATE TABLE IF NOT EXISTS calls(kind TEXT)")
        db.execute("INSERT INTO calls VALUES (?)", (kind,))


async def gate(name):
    if window == name and not (directory / "released").exists():
        (directory / "ready").touch()
        while True:
            await asyncio.sleep(0.05)


class Producer(Model):
    async def get_response(self, *args, **kwargs):
        items = kwargs.get("input", args[1] if len(args) > 1 else [])
        final = any(i.get("type") == "function_call_output" for i in items)
        witness("model-final" if final else "model-first")
        output = (
            [assistant_message("complete")]
            if final
            else [function_call("read", "{}", call_id="read-1")]
        )
        return ModelResponse(
            output=output,
            usage=Usage(requests=1, input_tokens=7, output_tokens=3, total_tokens=10),
            response_id=None,
        )

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


@function_tool
@DBOS.step()
async def read() -> str:
    """Read one synthetic source."""
    witness("tool")
    return "source evidence"


async def model_input(data):
    if any(i.get("type") == "function_call_output" for i in data.model_data.input):
        await gate("tool-committed")
    return data.model_data


from agents import RunHooks  # noqa: E402


class Gates(RunHooks):
    async def on_tool_start(self, *args):
        await gate("model-committed")


@DBOS.workflow(name="poc.producer")
async def producer():
    if window == "budget":
        from agents.testing import ScriptedModel
        from langchain_core.language_models.fake_chat_models import (
            FakeMessagesListChatModel,
        )
        from langchain_core.messages import AIMessage
        from test_sdk_runtime import _runtime

        from open_deep_research.budget import ainvoke_model_with_budget
        from open_deep_research.configuration import Configuration
        from open_deep_research.runtime.sdk_context import SDKContextHarness
        from open_deep_research.workflow.durability import durable_unit

        original = SDKContextHarness.filter_model_input

        async def context_calls(self, data):
            result = await original(self, data)
            for _ in range(2):
                witness("context-model")
                await ainvoke_model_with_budget(
                    FakeMessagesListChatModel(
                        responses=[
                            AIMessage(
                                content="context",
                                usage_metadata={
                                    "input_tokens": 1,
                                    "output_tokens": 1,
                                    "total_tokens": 2,
                                },
                            )
                        ]
                    ),
                    [],
                )
            return result

        SDKContextHarness.filter_model_input = context_calls
        token = durable_unit.set("budget-role")
        try:
            runtime = _runtime(
                config=Configuration(
                    research_graph_enabled=False, budget_enabled=True, max_model_calls=5
                ),
                initial_budget={"model_calls": 2},
                model=ScriptedModel([[assistant_message("must not execute")]]),
                tools=[],
            )
            result = await runtime.run("bounded research")
            assert not runtime.model.calls
            await gate("budget")
            return {
                "model_calls": result.budget["model_calls"],
                "settled": runtime.harness.last_settlement.succeeded,
            }
        finally:
            durable_unit.reset(token)
            SDKContextHarness.filter_model_input = original
    result = await DBOSRunner.run(
        Agent(name="producer", model=Producer(), tools=[read]),
        "research",
        hooks=Gates(),
        run_config=RunConfig(
            tracing_disabled=True, call_model_input_filter=model_input
        ),
    )
    return result.final_output


async def main():
    DBOS(
        config={
            "name": "runner-poc",
            "application_version": "poc-v1",
            "system_database_url": os.environ["DBOS_DATABASE_URL"],
            "dbos_system_schema": "por_dbos",
            "executor_id": "poc-executor",
            "serializer": RunJSON(),
            "log_level": "WARN",
        }
    )
    DBOS.launch()
    while True:
        await asyncio.sleep(0.1)


if __name__ == "__main__":
    asyncio.run(main())
