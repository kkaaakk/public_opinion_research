"""Run the Python Workflow against local Neo4j with offline inference only."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

from sdk_phase2_smoke import _configure_offline_smoke


async def main():
    """Verify a fresh synthetic run and delete only that run in finally."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-local-neo4j", action="store_true")
    parser.add_argument(
        "--output", type=Path, default=Path(".tmp/agents-phase3/neo4j-smoke.json")
    )
    args = parser.parse_args()
    if not args.enable_local_neo4j:
        parser.error(
            "Explicit --enable-local-neo4j is required; no database was opened"
        )
    _configure_offline_smoke()
    from dotenv import dotenv_values
    from pytest import MonkeyPatch
    from test_python_workflow_sdk import install_sdk

    from open_deep_research.research_graph.store import (
        Neo4jResearchGraphStore,
        clear_research_graph_store_cache,
    )
    from open_deep_research.workflow.python_workflow import PythonWorkflow

    # Load only the existing loopback database credential, never model keys.
    password = dotenv_values(".env").get("RESEARCH_GRAPH_PASSWORD")
    if not password:
        raise RuntimeError("No configured local Research Graph password")
    run_id = "phase3_smoke_" + uuid4().hex
    store = Neo4jResearchGraphStore(
        uri="bolt://127.0.0.1:7687", username="neo4j", password=password
    )
    verified_empty = False
    output = {}
    try:
        count_query = (
            "MATCH (n:ResearchNode {run_id: $run_id}) RETURN count(n) AS count"
        )
        assert store._execute(count_query, run_id=run_id)[0]["count"] == 0
        verified_empty = True
        with MonkeyPatch.context() as patch:
            state, config, runtimes, models, calls, _ = install_sdk(
                patch, args.output.parent
            )
            state["research"]["run_id"] = run_id
            config["configurable"].update(
                research_graph_backend="neo4j",
                research_graph_uri="bolt://127.0.0.1:7687",
                research_graph_username="neo4j",
                research_graph_password=password,
            )
            result = await PythonWorkflow().ainvoke(state, config)
            assert "FINAL brand recall report" in result["report"]["final"]
            assert len(runtimes) == len(models) == 4
            assert calls == {"public_signal": 1, "internal_knowledge": 1}
            assert all(rt.harness.last_settlement.succeeded for rt in runtimes)
            assert all(
                rt.workspace.working_context.active_evidence_ids for rt in runtimes
            )
            rows = store._execute(
                "MATCH (n:ResearchNode {run_id: $run_id}) RETURN n.entity_type AS type, count(n) AS count",
                run_id=run_id,
            )
            counts = {row["type"]: row["count"] for row in rows}
            assert counts["Source"] == counts["Evidence"] == 2
            output = {
                "result": "passed",
                "run_id": run_id,
                "workflow": "python",
                "node_counts": counts,
                "roles": [rt.role for rt in runtimes],
                "budget": result["runtime"]["budget"],
                "provider_api_calls": 0,
                "models": "offline SDK ScriptedModel and LangChain fixtures",
            }
    finally:
        clear_research_graph_store_cache()
        try:
            if verified_empty:
                store._execute(
                    "MATCH (n:ResearchNode {run_id: $run_id}) DETACH DELETE n",
                    run_id=run_id,
                )
                assert store._execute(count_query, run_id=run_id)[0]["count"] == 0
        finally:
            store.close()
    output["created_run_cleaned"] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    sys.stdout.write(json.dumps(output, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
