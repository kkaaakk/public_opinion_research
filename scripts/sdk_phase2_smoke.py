"""Run an explicitly enabled local Neo4j smoke with offline inference fixtures.

Requires the repository dev environment and tests directory. No provider model
or paid credential is used. Only this smoke's fresh run is removed on exit.
"""

import argparse
import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from uuid import uuid4


def _configure_offline_smoke():
    """Disable provider credentials, tracing and nonlocal connections."""
    os.environ.update(
        PYTHON_DOTENV_DISABLED="1",
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
        OPENAI_AGENTS_DISABLE_TRACING="1",
    )
    for name in list(os.environ):
        if name.endswith(("_API_KEY", "_TOKEN")):
            os.environ.pop(name, None)
    connect = socket.socket.connect

    def _local_only(self, address):
        """Permit loopback connectivity only during this standalone smoke."""
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
            raise AssertionError("Smoke attempted external network")
        return connect(self, address)

    socket.socket.connect = _local_only
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))


async def main():
    """Validate four SDK roles against an existing loopback Neo4j, then clean up."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--enable-local-neo4j",
        action="store_true",
        help="Allow synthetic writes to the existing loopback Research Graph",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".tmp/agents-phase2/neo4j-smoke-result.json"),
    )
    args = parser.parse_args()
    if not args.enable_local_neo4j:
        parser.error(
            "Explicit --enable-local-neo4j is required; no database was opened."
        )
    _configure_offline_smoke()
    from agents.testing import (
        ModelStep,
        ScriptedModel,
        assistant_message,
        function_call,
    )
    from dotenv import dotenv_values
    from test_sdk_phase2_integration import Services, scripted_tool_runtime, workspace
    from test_sdk_runtime import ROLES, _runtime

    from open_deep_research.budget import merge_budget_usage
    from open_deep_research.configuration import Configuration
    from open_deep_research.research_graph.store import Neo4jResearchGraphStore
    from open_deep_research.runtime.business_agent import (
        _build_business_agent_assignment,
    )

    # Read only local database credentials. No provider credential is loaded.
    values = dotenv_values(".env")
    password = values.get("RESEARCH_GRAPH_PASSWORD")
    if not password:
        raise RuntimeError("No configured local Research Graph password")
    cfg = Configuration(
        research_graph_enabled=True,
        research_graph_backend="neo4j",
        research_graph_uri="bolt://127.0.0.1:7687",
        research_graph_username="neo4j",
        research_graph_password=password,
        research_graph_transcript_dir=".tmp/agents-phase2/neo4j-transcript",
        recent_raw_steps=0,
        max_structured_output_retries=1,
    )
    run_id = "phase2_smoke_" + uuid4().hex
    assert run_id.startswith("phase2_smoke_") and len(run_id) == 45
    store = Neo4jResearchGraphStore(
        uri=cfg.research_graph_uri,
        username=cfg.research_graph_username,
        password=cfg.research_graph_password,
    )
    verified_empty = False
    budget = {}
    observed = []
    try:
        assert (
            store._execute(
                "MATCH (n:ResearchNode {run_id: $run_id}) RETURN count(n) AS count",
                run_id=run_id,
            )[0]["count"]
            == 0
        )
        verified_empty = True
        services = Services()
        producers = [
            workspace(
                Path(".tmp/agents-phase2"),
                role,
                services=services,
                store=store,
                config=cfg,
                run_id=run_id,
            )
            for role in ROLES[:2]
        ]
        runtimes = [
            scripted_tool_runtime(
                ws,
                role=ws.role,
                steps=[
                    [
                        function_call(
                            "web_search"
                            if ws.role == "public_signal"
                            else "rag_search",
                            {"query": "brand-" + ws.role},
                            call_id="lookup",
                        )
                    ],
                    [assistant_message("producer complete")],
                ],
            )
            for ws in producers
        ]
        reports = await asyncio.gather(
            *(runtime.run("Investigate brand evidence") for runtime in runtimes)
        )
        state = {
            "research": {"run_id": run_id},
            "workflow": {"brief": "Investigate brand evidence", "round": 1},
            "agents": {
                role: {"report": result.report} for role, result in zip(ROLES, reports)
            },
        }
        for result in reports:
            budget = merge_budget_usage(budget, result.budget)
        for role in ROLES[2:]:
            ws = workspace(
                Path(".tmp/agents-phase2"),
                role,
                services=services,
                store=store,
                config=cfg,
                run_id=run_id,
            )

            def inspect(call):
                text = json.dumps(call.input)
                assert (
                    "brand-public_signal" in text and "brand-internal_knowledge" in text
                )
                assert "[Evidence]" in text
                if role == "response_strategy":
                    assert "risk_assessment GRAPH REPORT" in text
                observed.append(role)
                return [assistant_message("consumer complete")]

            runtime = _runtime(
                role,
                workspace=ws,
                config=cfg,
                tools=[],
                model=ScriptedModel([ModelStep.respond(inspect)]),
            )
            result = await runtime.run(
                _build_business_agent_assignment(state, role, cfg)
            )
            state["agents"][role] = {"report": result.report}
            budget = merge_budget_usage(budget, result.budget)
            assert runtime.harness.last_settlement.succeeded
        nodes = store._execute(
            "MATCH (n:ResearchNode {run_id: $run_id}) RETURN n.entity_type AS type, count(n) AS count",
            run_id=run_id,
        )
        counts = {r["type"]: r["count"] for r in nodes}
        assert counts["Source"] == 2 and counts["Evidence"] == 2
        assert observed == list(ROLES[2:])
        assert all(runtime.harness.last_settlement.succeeded for runtime in runtimes)
        output = {
            "run_id": run_id,
            "result": "passed",
            "backend": "Neo4j "
            + store._execute(
                "CALL dbms.components() YIELD versions RETURN versions[0] AS version"
            )[0]["version"],
            "models": "offline ScriptedModel and deterministic LangChain fixture",
            "node_counts": counts,
            "consumers": observed,
            "budget": budget,
            "provider_api_calls": 0,
        }
    finally:
        try:
            if verified_empty:
                store._execute(
                    "MATCH (n:ResearchNode {run_id: $run_id}) DETACH DELETE n",
                    run_id=run_id,
                )
                assert (
                    store._execute(
                        "MATCH (n:ResearchNode {run_id: $run_id}) RETURN count(n) AS count",
                        run_id=run_id,
                    )[0]["count"]
                    == 0
                )
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
