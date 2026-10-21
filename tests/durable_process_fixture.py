"""Run real processes for durable fault tests; all model inference is scripted."""

import asyncio
import json
import os
import socket
import sqlite3
import sys
import threading
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

os.environ.update(
    PYTHON_DOTENV_DISABLED="1",
    LANGSMITH_TRACING="false",
    LANGCHAIN_TRACING_V2="false",
    OPENAI_AGENTS_DISABLE_TRACING="1",
    RESEARCH_GRAPH_ENABLED="false",
)
import dotenv  # noqa: E402

# python-dotenv in the locked environment predates the environment-only guard.
dotenv.load_dotenv = lambda *args, **kwargs: False

# Never allow the subprocess to call a paid/nonlocal service, even if its
# parent happens to have credentials in the environment.
_connect = socket.socket.connect


def local_connect(self, address):
    if isinstance(address, tuple) and address[0] not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise AssertionError("Fault fixture attempted an external connection")
    return _connect(self, address)


socket.socket.connect = local_connect

import pytest  # noqa: E402
from agents.testing import (  # noqa: E402
    ModelStep,
    ScriptedModel,
    assistant_message,
    function_call,
)
from test_python_workflow_sdk import install_sdk  # noqa: E402
from test_sdk_runtime import TOKENS, _compress, _tool  # noqa: E402

from open_deep_research.runtime import business_agent  # noqa: E402
from open_deep_research.web import server, worker  # noqa: E402
from open_deep_research.web.run_store import RunStore  # noqa: E402


def install(directory):
    """Keep Workflow, Runtime, Runner, Harness and SQL real; replace inference/I/O."""
    patch = pytest.MonkeyPatch()
    install_sdk(patch, directory / "transcripts")

    @asynccontextmanager
    async def model(*args, **kwargs):
        def first(call):
            role = call.system_instructions.split("<Role>\n", 1)[1].split(
                "\n</Role>", 1
            )[0]
            if role in {"public_signal", "internal_knowledge"}:
                name = "web_search" if role == "public_signal" else "rag_search"
                return [function_call(name, {"query": role}, call_id=f"effect-{role}")]
            return [assistant_message("analysis")]

        yield ScriptedModel(
            [ModelStep.respond(first), [assistant_message("research complete")]],
            default_usage=TOKENS,
        )

    async def tools(config, role, spec):
        async def effect(query, limit=3):
            with sqlite3.connect(directory / "effects.db") as db:
                db.execute("CREATE TABLE IF NOT EXISTS effects (role TEXT)")
                db.execute("INSERT INTO effects VALUES (?)", (role,))
            await asyncio.sleep(0.1)
            return f"Synthetic evidence for {role} [source: fixture]"

        return [_tool(role, coroutine=effect)]

    patch.setattr(business_agent, "sdk_model", model)
    patch.setattr(business_agent, "_business_agent_tools", tools)
    patch.setattr(
        business_agent, "_compress_research", lambda *args, **kwargs: _compress(*args)
    )

    class Archive:
        def save(self, records):
            for record in records:
                (directory / f"archive-{record.run_id}.json").write_text(
                    record.model_dump_json(), encoding="utf-8"
                )
            return 1

    patch.setattr(worker, "get_history_store", lambda: Archive())
    original_workflow = worker.PythonWorkflow.ainvoke

    async def diagnostic(self, *args, **kwargs):
        try:
            return await original_workflow(self, *args, **kwargs)
        except BaseException:
            traceback.print_exc()
            raise

    patch.setattr(worker.PythonWorkflow, "ainvoke", diagnostic)
    return patch


def main():
    """Start a local Web process or a scripted production Worker process."""
    mode, directory_text = sys.argv[1:3]
    directory = Path(directory_text)
    os.environ.update(
        RUN_DATABASE_URL=f"sqlite:///{(directory / 'runs.db').as_posix()}",
        RUN_LEASE_SECONDS="1.5",
    )
    if mode == "web":
        import uvicorn

        class History:
            def list(self, **kwargs):
                records = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in directory.glob("archive-*.json")
                ]
                return dict(
                    items=[
                        {**record, "preview": record["report"][:160]}
                        for record in records
                    ],
                    total=len(records),
                )

            def get(self, history_id, **kwargs):
                records = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in directory.glob("archive-*.json")
                ]
                return next(
                    (record for record in records if record["id"] == history_id), None
                )

        server.get_history_store = lambda: History()
        server.WEB_API_TOKEN = "process-test-token"
        uvicorn.run(
            server.app, host="127.0.0.1", port=int(sys.argv[3]), log_level="error"
        )
        return
    patch = install(directory)
    store = RunStore(os.environ["RUN_DATABASE_URL"], lease_seconds=1.5)
    if mode == "checkpoint":
        original = store.progress

        def checkpoint(*args, **facts):
            original(*args, **facts)
            if (
                len(args) > 2
                and args[2] == "checkpoint"
                and facts.get("cursor") == "research_review"
            ):
                (directory / "ready").write_text("checkpoint", encoding="utf-8")
                threading.Event().wait()

        store.progress = checkpoint
    if mode == "lost-receipt":
        original = store.operation

        def lost_receipt(*args, **facts):
            if facts.get("status") != "started":
                (directory / "ready").write_text(
                    "tool returned; receipt not committed", encoding="utf-8"
                )
                threading.Event().wait()
            return original(*args, **facts)

        store.operation = lost_receipt
    instance = worker.RunWorker(
        store, poll_seconds=0.05, session_path=str(directory / "sdk.db")
    )
    try:
        if mode in {"checkpoint", "lost-receipt", "once"}:
            claim = store.claim("fault-test")
            asyncio.run(instance.execute(claim))
            sys.stdout.write(
                str(store.get(claim["run_id"], claim["owner"])["data"]["error"]) + "\n"
            )
        else:
            asyncio.run(instance.serve())
    finally:
        store.close()
        patch.undo()


if __name__ == "__main__":
    main()
