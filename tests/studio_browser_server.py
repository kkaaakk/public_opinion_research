"""Test-only real HTTP/DBOS/Neo4j server. Every inference/tool is controlled.

Never use this entry point for research. It creates and drops only a UUID test
database, and reuses the existing isolated native-contract Neo4j service.
"""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import os
import socket
import tempfile
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

os.environ.update(
    PYTHON_DOTENV_DISABLED="1",
    OPENAI_AGENTS_DISABLE_TRACING="1",
    LANGSMITH_TRACING="false",
    LANGCHAIN_TRACING_V2="false",
)
import dotenv

dotenv.load_dotenv = lambda *_a, **_kw: False
original_connect = socket.socket.connect


def local_connect(self, address):
    if isinstance(address, tuple) and address[0] not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise AssertionError("Browser fixture attempted external network")
    return original_connect(self, address)


socket.socket.connect = local_connect
import pytest
import uvicorn
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from dbos import DBOS
from dbos_support import BASE_URL, NEO4J, NEO4J_PASSWORD
from neo4j import GraphDatabase
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from test_python_workflow_sdk import install_sdk
from test_sdk_runtime import TOKENS

from open_deep_research.budget import capture_budget_usage
from open_deep_research.runtime import business_agent
from open_deep_research.web import history, runs, server
from open_deep_research.web.durable import close_run_client
from open_deep_research.web.executor import launch
from open_deep_research.workflow import dbos_workflow
from open_deep_research.workflow.dbos_config import settings
from open_deep_research.workflow.run_facts import facts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    url = make_url(BASE_URL)
    assert url.host in {"127.0.0.1", "localhost"}
    name = "por_test_" + uuid4().hex
    admin = create_engine(url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    with admin.connect() as db:
        db.execute(text("CREATE DATABASE " + name))
    os.environ["DBOS_DATABASE_URL"] = url.set(database=name).render_as_string(
        hide_password=False
    )
    settings.cache_clear()
    patch = pytest.MonkeyPatch()
    with tempfile.TemporaryDirectory(prefix="por-studio-browser-") as directory:
        _, cfg, _, _, _, _ = install_sdk(patch, Path(directory))
        cfg["configurable"].update(
            research_graph_backend="neo4j",
            research_graph_uri=NEO4J,
            research_graph_username="neo4j",
            research_graph_password=NEO4J_PASSWORD,
            allow_clarification=False,
        )
        archive = history.Neo4jHistoryStore(
            GraphDatabase.driver(NEO4J, auth=("neo4j", NEO4J_PASSWORD))
        )
        patch.setattr(history, "get_history_store", lambda: archive)
        patch.setattr(server, "get_history_store", lambda: archive)
        patch.setattr(server, "WEB_API_TOKEN", "studio-browser-test-only")

        def admitted(body):
            config = deepcopy(cfg)
            config["configurable"].update(
                allow_plan_feedback=body.get("require_approval", False),
                organization_context=body["topic"],
            )
            return config

        patch.setattr(runs, "accepted_config", admitted)

        @asynccontextmanager
        async def model(*_args, **kwargs):
            scenario = kwargs["config"]["configurable"].get("organization_context", "")
            role_ = ""

            async def first(call):
                nonlocal role_
                role_ = call.system_instructions.split("<Role>\n", 1)[1].split(
                    "\n</Role>", 1
                )[0]
                await asyncio.sleep(0.3)
                if role_ in {"public_signal", "internal_knowledge"}:
                    return [
                        function_call(
                            "web_search" if role_ == "public_signal" else "rag_search",
                            {"query": role_},
                            call_id="browser-" + role_,
                        )
                    ]
                return [
                    assistant_message("Controlled analysis based on persisted evidence")
                ]

            async def second(_call):
                if "cancel-run" in scenario and role_ == "public_signal":
                    await asyncio.Event().wait()
                await asyncio.sleep(0.4)
                return [assistant_message("Controlled producer conclusion")]

            yield ScriptedModel(
                [ModelStep.respond(first), ModelStep.respond(second)],
                default_usage=TOKENS,
            )

        patch.setattr(business_agent, "sdk_model", model)
        original_review = dbos_workflow.OPERATIONS["research_review"]
        failed = set()

        async def review(state, config):
            run_id = state["research"]["run_id"]
            if (
                "retry-once" in config["configurable"].get("organization_context", "")
                and run_id not in failed
            ):
                failed.add(run_id)
                capture_budget_usage({"model_calls": 1})
                raise RuntimeError("Controlled settled review failure")
            return await original_review(state, config)

        patch.setitem(dbos_workflow.OPERATIONS, "research_review", review)
        DBOS.migrate(os.environ["DBOS_DATABASE_URL"], schema="por_dbos")
        facts().initialize()
        launch("studio-browser-" + uuid4().hex)
        try:
            uvicorn.run(
                server.app, host="127.0.0.1", port=args.port, log_level="warning"
            )
        finally:
            # Only this process's UUID DB can be removed. Existing business stores stay intact.
            DBOS.destroy(destroy_registry=True)
            close_run_client()
            archive.close()
            patch.undo()
            assert (
                name.startswith("por_test_")
                and len(name) == 41
                and all(c in "0123456789abcdef" for c in name[9:])
            )
            with admin.connect() as db:
                db.execute(text("DROP DATABASE " + name + " WITH (FORCE)"))
            admin.dispose()


if __name__ == "__main__":
    main()
