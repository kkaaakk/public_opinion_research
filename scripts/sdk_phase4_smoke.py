"""Verify the SQL Worker, local Neo4j archive/evidence and real MCP with offline models."""

import argparse
import asyncio
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from sdk_phase2_smoke import _configure_offline_smoke


async def main():
    """Use only explicitly enabled loopback services and remove this smoke's data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-local-services", action="store_true")
    parser.add_argument(
        "--output", type=Path, default=Path(".tmp/agents-phase4/services.json")
    )
    args = parser.parse_args()
    if not args.enable_local_services:
        parser.error("Explicit --enable-local-services is required")
    _configure_offline_smoke()
    from dotenv import dotenv_values
    from langchain_mcp_adapters.tools import load_mcp_tools
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from pytest import MonkeyPatch
    from sqlalchemy import delete, select
    from sqlalchemy.engine import make_url
    from test_python_workflow_sdk import install_sdk

    from open_deep_research.research_graph.store import (
        Neo4jResearchGraphStore,
        clear_research_graph_store_cache,
    )
    from open_deep_research.web import worker
    from open_deep_research.web.history import Neo4jHistoryStore
    from open_deep_research.web.run_store import RunNotFound, RunStore

    url = os.environ.get("PHASE4_TEST_DATABASE_URL", "")
    if not url or make_url(url).host not in {"127.0.0.1", "::1"}:
        raise ValueError(
            "PHASE4_TEST_DATABASE_URL must name a dedicated loopback test database"
        )
    password = dotenv_values(".env").get("RESEARCH_GRAPH_PASSWORD")
    if not password:
        raise RuntimeError("No configured local Research Graph password")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    script = args.output.parent / "synthetic_mcp.py"
    script.write_text(
        """from mcp.server.fastmcp import FastMCP
server = FastMCP("phase4-synthetic")
@server.tool()
def lookup(query: str) -> str:
    return "Synthetic internal brand recall evidence: " + query
if __name__ == "__main__":
    server.run()
""",
        encoding="utf-8",
    )
    sql = RunStore(url)
    graph = Neo4jResearchGraphStore(
        uri="bolt://127.0.0.1:7687", username="neo4j", password=password
    )
    archive = Neo4jHistoryStore(graph.driver)
    owner = "phase4_smoke_" + uuid4().hex
    created = []
    result = {}
    try:
        with MonkeyPatch.context() as patch:
            patch.setenv("RESEARCH_GRAPH_BACKEND", "neo4j")
            patch.setenv("RESEARCH_GRAPH_URI", "bolt://127.0.0.1:7687")
            patch.setenv("RESEARCH_GRAPH_USERNAME", "neo4j")
            patch.setenv("RESEARCH_GRAPH_PASSWORD", password)
            patch.setattr(worker, "get_history_store", lambda: archive)
            async with stdio_client(
                StdioServerParameters(command=sys.executable, args=[str(script)])
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tool = next(
                        t for t in await load_mcp_tools(session) if t.name == "lookup"
                    )
                    tool.name, tool.metadata = (
                        "knowledge__lookup",
                        {"tool_domain": "rag"},
                    )
                    state, cfg, runtimes, models, calls, _ = install_sdk(
                        patch, args.output.parent, local_tool=tool
                    )
                    cfg["configurable"]["allow_plan_feedback"] = True
                    with ThreadPoolExecutor(max_workers=6) as pool:
                        deliveries = list(
                            pool.map(
                                lambda _: sql.create(
                                    owner,
                                    "create",
                                    {"topic": "synthetic brand recall"},
                                    cfg,
                                    state,
                                ),
                                range(8),
                            )
                        )
                    assert len({r[0]["run_id"] for r in deliveries}) == 1
                    assert sum(r[1] for r in deliveries) == 1
                    run_id = deliveries[0][0]["run_id"]
                    created.append(run_id)
                    assert (
                        graph._execute(
                            "MATCH (n:ResearchNode {run_id:$run_id}) RETURN count(n) AS count",
                            run_id=run_id,
                        )[0]["count"]
                        == 0
                    )
                    instance = worker.RunWorker(
                        sql, session_path=str(args.output.parent / "service-sdk.db")
                    )
                    await instance.execute(sql.claim("planner"))
                    paused = sql.get(run_id, owner)
                    assert paused["status"] == "awaiting_approval"
                    assert not models
                    sql.approve(
                        run_id, owner, paused["data"]["plan"]["version"], "approve"
                    )
                    claim = sql.claim("researcher")
                    assert claim is not None
                    assert sql.claim("duplicate-delivery") is None
                    await instance.execute(claim)
                    complete = sql.get(run_id, owner)
                    assert complete["status"] == "completed", complete["data"]["error"]
                    assert len(models) == 4
                    assert all(rt.harness.last_settlement.succeeded for rt in runtimes)
                    await instance.archive()
                    complete = sql.get(run_id, owner)
                    assert complete["data"]["archive_status"] == "saved"
                    history_id = complete["data"]["history_id"]
                    assert archive.get(history_id, owner=owner)["report"]
                    assert archive.get(history_id, owner="foreign-owner") is None
                    try:
                        sql.get(run_id, "foreign-owner")
                    except RunNotFound:
                        pass
                    else:
                        raise AssertionError("Foreign SQL owner was authorized")
                    events = sql.read_events(run_id, owner, 0, 1000)
                    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
                    rows = graph._execute(
                        "MATCH (n:ResearchNode {run_id:$run_id}) RETURN n.entity_type AS type, count(n) AS count",
                        run_id=run_id,
                    )
                    counts = {r["type"]: r["count"] for r in rows}
                    assert counts["Source"] == counts["Evidence"] == 2
                    with sql.engine.connect() as db:
                        receipts = (
                            db.execute(
                                select(sql.operations).where(
                                    sql.operations.c.run_id == run_id
                                )
                            )
                            .mappings()
                            .all()
                        )
                    assert len(receipts) == 2 and all(
                        r["status"] == "returned" for r in receipts
                    )
                    result = dict(
                        result="passed",
                        queue="mysql/InnoDB",
                        workflow="python",
                        sdk="0.24.0",
                        roles=len(runtimes),
                        tool_receipts=len(receipts),
                        events=len(events),
                        graph_counts=counts,
                        usage=complete["data"]["usage"],
                        approval_planner_replayed=False,
                        history="neo4j",
                        mcp="real stdio",
                        provider_api_calls=0,
                    )
    finally:
        clear_research_graph_store_cache()
        for run_id in created:
            graph._execute(
                "MATCH (n:ResearchNode {run_id:$run_id}) DETACH DELETE n", run_id=run_id
            )
            graph._execute(
                "MATCH (h:ResearchHistory {run_id:$run_id}) DELETE h", run_id=run_id
            )
        with sql.engine.begin() as db:
            for table in (sql.operations, sql.attempts, sql.events, sql.runs):
                db.execute(delete(table).where(table.c.run_id.in_(created)))
        graph.close()
        sql.close()
    result["created_runs_cleaned"] = True
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
