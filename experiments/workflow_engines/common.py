"""Offline business boundary shared by the isolated engine POCs.

Inference comes from the official SDK ScriptedModel and existing test fixtures.
PythonWorkflow, SDKAgentRuntime, Harness, reducers, tools and Graph adapters run
unchanged. The SQLite guard is deliberately separate from the engine journal:
unknown external effects cannot be made safe by workflow replay alone.
"""

# Imports below the guard intentionally run only after dotenv/network isolation.
# ruff: noqa: E402
from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
os.environ.update(
    PYTHON_DOTENV_DISABLED="1",
    LANGSMITH_TRACING="false",
    LANGCHAIN_TRACING_V2="false",
    OPENAI_AGENTS_DISABLE_TRACING="1",
    RAG_MEMORY_WRITE_ENABLED="false",
)
import dotenv

dotenv.load_dotenv = lambda *args, **kwargs: False
_original_connect = socket.socket.connect


def local_connect(self, address):
    if isinstance(address, tuple) and address[0] not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise AssertionError("POC forbids non-loopback connections")
    return _original_connect(self, address)


socket.socket.connect = local_connect

import pytest
from langchain_core.runnables import RunnableLambda
from test_python_workflow import ROLES, gap
from test_python_workflow_sdk import install_sdk
from test_sdk_context import _payload
from test_sdk_runtime import _tool

from open_deep_research.budget import budget_capture
from open_deep_research.research_graph.store import clear_research_graph_store_cache
from open_deep_research.runtime import business_agent
from open_deep_research.state import ResearchReview
from open_deep_research.web.run_store import digest, dumps, loads
from open_deep_research.workflow import shared
from open_deep_research.workflow.execution import execution_observer
from open_deep_research.workflow.python_workflow import (
    WORKFLOW_VERSION,
    PlanApprovalRequired,
    PythonWorkflow,
)

UNSAFE = {
    "research_agents",
    "risk_assessment_agent",
    "response_strategy_agent",
    "compile_final_report",
}


def ledger(directory):
    db = sqlite3.connect(Path(directory) / "guard.sqlite", timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("CREATE TABLE IF NOT EXISTS segments (identity TEXT PRIMARY KEY)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS operations (identity TEXT PRIMARY KEY, status TEXT)"
    )
    db.execute("CREATE TABLE IF NOT EXISTS audit (kind TEXT, data TEXT)")
    db.commit()
    return db


def record(directory, kind, data):
    with ledger(directory) as db:
        db.execute("INSERT INTO audit VALUES (?,?)", (kind, dumps(data)))


def audit(directory, kind):
    with ledger(directory) as db:
        return [
            loads(row[0])
            for row in db.execute("SELECT data FROM audit WHERE kind=?", (kind,))
        ]


def effects(directory):
    with sqlite3.connect(Path(directory) / "external.sqlite") as db:
        db.execute("CREATE TABLE IF NOT EXISTS effects (role TEXT)")
        return db.execute("SELECT role FROM effects").fetchall()


def packet(directory, run_id, *, scenario="normal", guarded=True, graph="memory"):
    return dict(
        directory=str(directory),
        run_id=run_id,
        scenario=scenario,
        guarded=guarded,
        graph=graph,
        cursor="enrich_query_images",
        state=None,
    )


def install(directory, scenario, graph, run_id):
    patch = pytest.MonkeyPatch()
    mode = scenario if scenario in {"cancel", "rolling", "clarify"} else "normal"
    state, cfg, runtimes, models, calls, blocked = install_sdk(
        patch, Path(directory) / "transcripts", mode
    )
    cfg["configurable"].update(
        research_run_id=run_id, thread_id=run_id, research_graph_backend=graph
    )
    state["research"]["run_id"] = run_id
    patch.setenv("RESEARCH_GRAPH_BACKEND", graph)
    if graph == "neo4j":
        # URI/credential come only from the disposable local service environment.
        assert os.environ.get("RESEARCH_GRAPH_URI", "").startswith("bolt://127.0.0.1:")
    original = shared.configurable_model.with_structured_output

    def structured(schema, include_raw=False):
        if schema is not ResearchReview or scenario != "gap":
            return original(schema, include_raw)

        async def review(messages, config=None):
            raw = await shared.configurable_model.raw.ainvoke(messages, config)
            first = "Current research round: 1\n" in str(messages[0].content)
            value = ResearchReview(
                research_complete=not first,
                next_tasks=[
                    gap("verify-date"),
                    gap("verify-policy", role="internal_knowledge"),
                ]
                if first
                else [],
            )
            return {"raw": raw, "parsed": value, "parsing_error": None}

        return RunnableLambda(review)

    patch.setattr(shared.configurable_model, "with_structured_output", structured)

    async def tools(config, role, spec):
        if role not in ROLES[:2]:
            return []

        async def external_effect(query, limit=3):
            # This table is an external authority with NO idempotency key. Every
            # actual invocation writes a row, making duplicate effects visible.
            with sqlite3.connect(Path(directory) / "external.sqlite", timeout=10) as db:
                db.execute("CREATE TABLE IF NOT EXISTS effects (role TEXT)")
                db.execute("INSERT INTO effects VALUES (?)", (role,))
            return _payload(role)

        return [_tool(role, coroutine=external_effect)]

    patch.setattr(business_agent, "_business_agent_tools", tools)
    return patch, state, cfg, runtimes, models, calls, blocked


async def fault_gate(directory, point):
    if os.environ.get("POC_BLOCK_AT") == point:
        Path(directory, "ready").write_text(point, encoding="utf-8")
        await asyncio.Event().wait()


async def segment(value):
    """Run to the next *actual* PythonWorkflow checkpoint, retaining its rules.

    The stop exception is the existing explicit ownership-return exception. It
    is caught locally and never presented as a failed/retried engine step.
    This experimental bridge retains the cursor implementation; it is not a
    claim that the production cursor can already be deleted.
    """
    directory, run_id, cursor = value["directory"], value["run_id"], value["cursor"]
    state = loads(value["state"]) if value["state"] else None
    round_number = (state or {}).get("workflow", {}).get("round", 0)
    # Followup packets advance the domain round inside the Producer step; the
    # committed parent State still carries the previous round until its join.
    if (
        cursor == "research_agents"
        and (state or {}).get("workflow", {}).get("review") is not None
    ):
        round_number += 1
    step_key = f"{run_id}:{cursor}:{round_number}"
    if value["guarded"] and cursor in UNSAFE:
        try:
            with ledger(directory) as db:
                db.execute("INSERT INTO segments VALUES (?)", (step_key,))
        except sqlite3.IntegrityError:
            record(directory, "quarantined", {"step": step_key})
            return {
                **value,
                "cursor": "interrupted",
                "error": "unknown external effects; reconcile",
            }
    patch, default, cfg, runtimes, models, _, blocked = install(
        directory, value["scenario"], value["graph"], run_id
    )
    state = state or default
    saved = {}

    async def observer(kind, **facts):
        if kind == "checkpoint":
            saved.update(cursor=facts["cursor"], state=dumps(facts["state"]))
            record(
                directory,
                "checkpoint",
                {
                    "from": cursor,
                    "cursor": facts["cursor"],
                    "budget": facts["state"].get("runtime", {}).get("budget", {}),
                },
            )
            raise PlanApprovalRequired()
        if kind in {"tool_started", "tool_finished"}:
            identity = facts["identity"]
            op_id = digest(
                {
                    "step": step_key,
                    "role": identity["role"],
                    "name": identity["name"],
                    "call": identity["tool_call_id"],
                }
            )
            if kind == "tool_started":
                if value["guarded"]:
                    with ledger(directory) as db:
                        db.execute(
                            "INSERT INTO operations VALUES (?, 'started')", (op_id,)
                        )
                return op_id
            await fault_gate(directory, "lost-receipt")
            if value["guarded"]:
                with ledger(directory) as db:
                    db.execute(
                        "UPDATE operations SET status=? WHERE identity=?",
                        (facts["status"], op_id),
                    )
        return None

    async def signal_blocked():
        await blocked.wait()
        Path(directory, "ready").write_text("sdk-blocked", encoding="utf-8")

    watcher = asyncio.create_task(signal_blocked())
    token = execution_observer.set(observer)
    record(directory, "segment_enter", {"cursor": cursor})
    try:
        with budget_capture():
            try:
                result = await PythonWorkflow().ainvoke(
                    state, cfg, resume={"version": WORKFLOW_VERSION, "cursor": cursor}
                )
                saved.update(cursor="done", state=dumps(result))
            except PlanApprovalRequired:
                pass
        assert saved, f"no committed boundary at {cursor}"
        output = {**value, **saved}
        if output["cursor"] == "approval":
            output["plan_version"] = plan_version(output)
            Path(directory, "plan.json").write_text(
                json.dumps({"version": output["plan_version"]}), encoding="utf-8"
            )
        return output
    except asyncio.CancelledError as exc:
        observed_state = getattr(exc, "workflow_state", state)
        record(
            directory,
            "cancelled",
            {
                "budget": observed_state.get("runtime", {}).get("budget", {}),
                "settled": all(
                    rt.harness.last_settlement is not None
                    and rt.harness.last_settlement.succeeded
                    for rt in runtimes
                ),
                "runtimes": len(runtimes),
            },
        )
        raise
    finally:
        for rt in runtimes:
            record(
                directory,
                "runtime",
                {
                    "role": rt.role,
                    "invocation": rt.harness.sdk_run_id,
                    "filter_calls": sum(
                        event[0] == "input_filter" for event in rt.harness.events
                    ),
                    "evidence": list(rt.workspace.working_context.active_evidence_ids),
                    "receipts": len(rt.workspace.receipts),
                    "summaries": sum(
                        event[0] == "rolling_compact" for event in rt.harness.events
                    ),
                    "settled": bool(
                        rt.harness.last_settlement
                        and rt.harness.last_settlement.succeeded
                    ),
                },
            )
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        execution_observer.reset(token)
        patch.undo()
        clear_research_graph_store_cache()


def plan_version(value):
    state = loads(value["state"])
    return digest(
        {"brief": state["workflow"]["brief"], "sections": state["report"]["sections"]}
    )
