# 配置并验证本地 Neo4j 研究图，检查过程不调用模型。
"""Configure and verify the local Neo4j Research Graph without model calls."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import uuid
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


# 只更新选定本地设置，保留无关凭据、原文件内容及换行风格。
def update_env(settings: dict[str, str]) -> None:
    """Update selected local settings while preserving unrelated credentials."""
    raw = ENV_FILE.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    lines = raw.decode("utf-8").splitlines(keepends=True)
    pending = dict(settings)
    result = []
    written = set()
    for line in lines:
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        key = match.group(1) if match else None
        if key in settings:
            if key not in written:
                result.append(f"{key}={pending.pop(key)}{newline}")
                written.add(key)
        else:
            result.append(line)
    if pending:
        if result and not result[-1].endswith(("\r", "\n")):
            result[-1] += newline
        result.append(f"{newline}# Local Docker Neo4j connection{newline}")
        result.extend(f"{key}={value}{newline}" for key, value in pending.items())
    ENV_FILE.write_bytes("".join(result).encode("utf-8"))


# 在首次启动容器前准备连接地址和凭据，并保存到环境文件。
def configure() -> None:
    """Prepare connection credentials before the first container launch."""
    if not ENV_FILE.exists():
        ENV_FILE.write_bytes((ROOT / ".env.example").read_bytes())
    password = dotenv_values(ENV_FILE).get("RESEARCH_GRAPH_PASSWORD") or secrets.token_hex(24)
    update_env({
        "RESEARCH_GRAPH_ENABLED": "true",
        "RESEARCH_GRAPH_BACKEND": "neo4j",
        "RESEARCH_GRAPH_URI": "bolt://127.0.0.1:7687",
        "RESEARCH_GRAPH_USERNAME": "neo4j",
        "RESEARCH_GRAPH_PASSWORD": password,
        "RESEARCH_GRAPH_DATABASE": "neo4j",
        "RAG_NEO4J_URI": "bolt://127.0.0.1:7687",
        "RAG_NEO4J_USERNAME": "neo4j",
        "RAG_NEO4J_PASSWORD": password,
        "RAG_NEO4J_DATABASE": "neo4j",
    })
    print("Neo4j connection configured; password saved in .env (not printed).")
    print("Research Graph is enabled by default. Start Neo4j before running research.")


# 验证真实适配器的来源写入、检索与角色路由，不发送模型请求。
def verify(*, enable: bool) -> None:
    """Check the real adapter, provenance writes, retrieval and role routing."""
    # The saved local file is authoritative for this setup check, including
    # when the launching PowerShell still has the old disabled override.
    load_dotenv(ENV_FILE, override=True)
    os.environ["LANGSMITH_TRACING"] = "false"
    from neo4j.exceptions import ConstraintError

    from open_deep_research.configuration import Configuration
    from open_deep_research.public_opinion_agents.registry import (
        PUBLIC_OPINION_AGENT_SPECS,
    )
    from open_deep_research.research_graph.models import (
        Claim,
        Evidence,
        Finding,
        ResearchDeltaGraph,
        ResearchGraphScope,
        Source,
    )
    from open_deep_research.research_graph.store import (
        clear_research_graph_store_cache,
        create_research_graph_store,
    )
    from open_deep_research.research_graph.strategy import create_context_strategy
    from open_deep_research.research_graph.workspace import ResearchWorkspace

    config = Configuration.from_runnable_config({}).model_copy(
        update={"research_graph_enabled": True}
    )
    if config.research_graph_backend != "neo4j":
        raise ValueError("Set RESEARCH_GRAPH_BACKEND=neo4j before this check.")
    run_id = f"neo4j-smoke-{uuid.uuid4().hex}"
    store = create_research_graph_store(config, run_id=run_id)

    # 阻止校验过程意外调用模型，发现调用即抛错。
    def no_model_calls(*_args, **_kwargs):
        raise RuntimeError("This check must not call any model.")

    try:
        for role, spec in PUBLIC_OPINION_AGENT_SPECS.items():
            strategy = create_context_strategy(spec.context_strategy, graph_enabled=True)
            workspace = ResearchWorkspace(
                strategy=strategy, research_state={}, role=role,
                configurable=config, model_factory=no_model_calls, run_id=run_id,
            )
            if workspace.store is not store or not strategy.graph_enabled:
                raise RuntimeError(f"Research Graph initialization failed for {role}.")
            print(f"{role}: {strategy.name}, Neo4j connected")

        scope = ResearchGraphScope(run_id=run_id, role="public_signal", task_id="smoke-task")
        delta = ResearchDeltaGraph(
            scope=scope,
            sources=[Source(source_id="smoke-source", run_id=run_id, title="Local Neo4j smoke check")],
            claims=[Claim(claim_id="smoke-claim", run_id=run_id, statement="Local Neo4j smoke check")],
            evidences=[Evidence(
                evidence_id="smoke-evidence", run_id=run_id, source_id="smoke-source",
                statement="Local Neo4j smoke check", supports_claim_ids=["smoke-claim"],
            )],
            findings=[Finding(
                finding_id="smoke-finding", run_id=run_id, summary="Local Neo4j smoke check",
                claim_ids=["smoke-claim"], evidence_ids=["smoke-evidence"],
            )],
        )
        receipt = store.write_delta(delta)
        graph = store.retrieve("Local Neo4j smoke check", run_id=run_id)
        relations = {edge.relation_type for edge in graph.edges}
        expected = {"EXTRACTED_FROM", "SUPPORTS", "DERIVED_FROM", "SUPPORTED_BY"}
        if not receipt.finding_ids or not expected.issubset(relations):
            raise RuntimeError("Neo4j provenance write/retrieval check failed.")
        if store.retrieve("Local Neo4j smoke check", run_id=f"{run_id}-other").nodes:
            raise RuntimeError("Neo4j run isolation check failed.")
        print(f"Provenance write/read: {len(graph.nodes)} nodes, {len(graph.edges)} edges; isolation OK")

        edge = graph.edges[0]
        duplicate_queries = [
            (
                "CREATE (:ResearchNode {run_id: $run_id, node_id: $node_id})",
                {"run_id": run_id, "node_id": receipt.source_ids[0]},
            ),
            (
                "MATCH (a:ResearchNode {run_id: $run_id, node_id: $source_id}), "
                "(b:ResearchNode {run_id: $run_id, node_id: $target_id}) "
                "CREATE (a)-[:ResearchEdge {run_id: $run_id, edge_id: $edge_id}]->(b)",
                {"run_id": run_id, "source_id": edge.source_id,
                 "target_id": edge.target_id, "edge_id": edge.edge_id},
            ),
        ]
        for query, parameters in duplicate_queries:
            try:
                with store.driver.session(database=config.research_graph_database) as session:
                    with session.begin_transaction() as transaction:
                        transaction.run(query, parameters).consume()
                        transaction.rollback()
            except ConstraintError:
                continue
            raise RuntimeError("Neo4j accepted a duplicate graph identity.")
        print("Node and relationship composite uniqueness: OK")
    finally:
        try:
            # Delete only this invocation's synthetic fixture, never user runs.
            store.driver.execute_query(
                "MATCH (n:ResearchNode {run_id: $run_id}) DETACH DELETE n",
                parameters_={"run_id": run_id}, database_=config.research_graph_database,
            )
        finally:
            clear_research_graph_store_cache()
    if enable:
        update_env({"RESEARCH_GRAPH_ENABLED": "true"})
        print("RESEARCH_GRAPH_ENABLED=true saved in .env. Restart the Web server to apply.")
    print("Paid model calls: 0. Temporary smoke data removed.")


# 解析命令行并执行指定的本地配置或验证操作。
def main() -> None:
    """Run the requested local setup operation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("configure", "verify"))
    parser.add_argument("--enable", action="store_true", help="Enable Research Graph after a successful check")
    args = parser.parse_args()
    if args.operation == "configure":
        configure()
    else:
        verify(enable=args.enable)


if __name__ == "__main__":
    main()
