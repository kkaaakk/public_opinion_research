"""Explicit local-only acceptance: real DBOS/Graph + official SDK mock HTTP.

Inference and search payloads are controlled test data. Traces use the normal
application initializer/exporter, not a fabricated OTLP receiver or trace tree.
Operator credentials stay in the process environment and are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-local-services", action="store_true")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--gap", action="store_true")
    args = parser.parse_args()
    if not args.enable_local_services:
        parser.error("Explicit --enable-local-services is required")
    if urlsplit(args.base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Acceptance requires an isolated loopback Langfuse")
    for name in (
        "TEST_DBOS_DATABASE_URL",
        "TEST_DBOS_NEO4J_URI",
        "TEST_DBOS_NEO4J_PASSWORD",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    ):
        if not os.environ.get(name):
            parser.error("Missing local acceptance configuration: " + name)
    os.environ.update(
        PYTHON_DOTENV_DISABLED="1",
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
        OPENAI_AGENTS_DISABLE_TRACING="1",
        POR_TRACING_ENABLED="true",
        POR_TRACE_SAMPLE_RATE="1",
        LANGFUSE_BASE_URL=args.base_url,
        LANGFUSE_FLUSH_INTERVAL="0.1",
        POR_TRACE_TIMEOUT_SECONDS="0.4",
        POR_TRACE_SHUTDOWN_SECONDS="2",
    )
    import dotenv

    dotenv.load_dotenv = lambda *a, **k: False
    original_connect = socket.socket.connect

    def local_connect(self, address):
        if isinstance(address, tuple) and address[0] not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise AssertionError("Acceptance attempted non-loopback network")
        return original_connect(self, address)

    socket.socket.connect = local_connect

    import httpx2
    import pytest
    from agents import OpenAIResponsesModel
    from dbos import DBOS
    from dbos_support import NativeCase
    from langchain_core.runnables import RunnableLambda
    from openai import AsyncOpenAI
    from opentelemetry import trace
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from test_python_workflow import gap
    from test_sdk_models import _reply

    from open_deep_research.observability.telemetry import (
        shutdown_telemetry,
        telemetry_enabled,
    )
    from open_deep_research.runtime import business_agent
    from open_deep_research.state import ResearchReview
    from open_deep_research.web.durable import close_run_client
    from open_deep_research.web.executor import launch
    from open_deep_research.workflow import shared
    from open_deep_research.workflow.dbos_config import settings
    from open_deep_research.workflow.run_facts import facts

    url = make_url(os.environ["TEST_DBOS_DATABASE_URL"])
    if url.host not in {"127.0.0.1", "localhost"}:
        parser.error("Acceptance requires isolated loopback PostgreSQL")
    name = "por_test_" + uuid4().hex
    admin = create_engine(
        url, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3}
    )
    with admin.connect() as db:
        db.execute(text("CREATE DATABASE " + name))
    os.environ["DBOS_DATABASE_URL"] = url.set(database=name).render_as_string(
        hide_password=False
    )
    settings.cache_clear()
    patch = pytest.MonkeyPatch()
    case = None
    requests = Counter()
    invocations = Counter()
    trace_ids = set()
    barriers = {}
    started = time.monotonic()
    try:
        DBOS.migrate(os.environ["DBOS_DATABASE_URL"], schema="por_dbos")
        facts().initialize()
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        case = NativeCase(patch, args.summary.parent / (name + "-data"), "normal")
        case.cfg["configurable"].update(
            research_graph_backend="neo4j",
            research_graph_uri=os.environ["TEST_DBOS_NEO4J_URI"],
            research_graph_username="neo4j",
            research_graph_password=os.environ["TEST_DBOS_NEO4J_PASSWORD"],
        )
        case.state["messages"][0].content += " PRIVATE_PROMPT_SENTINEL"

        @asynccontextmanager
        async def model_context(*a, **k):
            assert k["max_retries"] == 0
            turn = 0

            async def respond(request):
                nonlocal turn
                turn += 1
                body = json.loads(request.content)
                role = (
                    body["instructions"]
                    .split("<Role>\n", 1)[1]
                    .split("\n</Role>", 1)[0]
                )
                requests[role] += 1
                context = trace.get_current_span().get_span_context()
                assert context.is_valid
                trace_ids.add(f"{context.trace_id:032x}")
                producer = role in {"public_signal", "internal_knowledge"}
                if turn == 1:
                    invocations[role] += 1
                    if producer:
                        wave = invocations[role]
                        gate, entered = barriers.setdefault(
                            wave, (asyncio.Event(), set())
                        )
                        entered.add(role)
                        if len(entered) == 2:
                            gate.set()
                        await asyncio.wait_for(gate.wait(), 10)
                await asyncio.sleep(0.25)
                payload = _reply(
                    "responses", turn if producer else 2, "pr32-offline-model"
                )
                if producer and turn == 1:
                    payload["output"][0].update(
                        name="web_search" if role == "public_signal" else "rag_search",
                        call_id="lookup-" + role,
                        arguments=json.dumps(
                            {"query": "PRIVATE_TOOL_ARGS_SENTINEL-" + role}
                        ),
                    )
                else:
                    payload["output"][0]["content"][0]["text"] = (
                        "PRIVATE_RESPONSE_SENTINEL " + role
                    )
                payload["usage"].update(
                    input_tokens=10, output_tokens=4, total_tokens=14
                )
                payload["usage"]["input_tokens_details"]["cached_tokens"] = 2
                payload["usage"]["output_tokens_details"]["reasoning_tokens"] = 1
                return httpx2.Response(200, json=payload)

            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(respond)
            ) as transport:
                client = AsyncOpenAI(
                    api_key="offline-fixture-only",
                    http_client=transport,
                    max_retries=0,
                    base_url="http://127.0.0.1:1/offline",
                )
                yield OpenAIResponsesModel(
                    model="pr32-offline-model", openai_client=client
                )

        patch.setattr(business_agent, "sdk_model", model_context)
        original = shared.configurable_model

        class GapServices:
            def with_config(self, *a, **k):
                return self

            def with_structured_output(self, schema, include_raw=False):
                chain = original.with_structured_output(schema, include_raw)
                if schema is not ResearchReview:
                    return chain

                async def reply(messages, config=None):
                    result = await chain.ainvoke(messages, config)
                    done = not args.gap or "Current research round: 2" in str(
                        messages[0].content
                    )
                    result["parsed"] = ResearchReview(
                        research_complete=done,
                        next_tasks=[]
                        if done
                        else [gap("date"), gap("approval", "internal_knowledge")],
                    )
                    return result

                return RunnableLambda(reply)

            async def ainvoke(self, *a, **k):
                return await original.ainvoke(*a, **k)

        patch.setattr(shared, "configurable_model", GapServices())
        launch("pr32-acceptance-" + uuid4().hex)
        assert telemetry_enabled()
        run = case.create()
        final = case.wait(
            run,
            lambda r: r["archive_status"] == "saved" and r["events_complete"],
            timeout=120,
        )
        assert final["status"] == "completed" and final["settlement"] == "settled"
        expected = 2 if args.gap else 1
        assert case.calls == {"public_signal": expected, "internal_knowledge": expected}
        assert sum(requests.values()) == (10 if args.gap else 6)
        assert len(trace_ids) == 1
        summary = {
            "run_id": run["run_id"],
            "trace_id": next(iter(trace_ids)),
            "status": final["status"],
            "settlement": final["settlement"],
            "archive_status": final["archive_status"],
            "sdk_requests": dict(requests),
            "sdk_invocations": dict(invocations),
            "tool_calls": dict(case.calls),
            "expected_sdk_llm_tokens": sum(requests.values()) * 14,
            "usage": final["usage"],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "live_langfuse": args.base_url,
        }
        args.summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    key: summary[key]
                    for key in (
                        "run_id",
                        "status",
                        "sdk_requests",
                        "tool_calls",
                        "expected_sdk_llm_tokens",
                    )
                }
            )
        )
    finally:
        DBOS.destroy(workflow_completion_timeout_sec=3)
        shutdown_telemetry()
        if case is not None:
            case.history.close()
        close_run_client()
        patch.undo()
        settings.cache_clear()
        assert name.startswith("por_test_") and len(name) == 41
        with admin.connect() as db:
            db.execute(text("DROP DATABASE " + name + " WITH (FORCE)"))
        admin.dispose()


if __name__ == "__main__":
    main()
