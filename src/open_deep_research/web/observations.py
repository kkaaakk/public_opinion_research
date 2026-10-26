"""Read-only, owner-scoped Studio views. No execution commands or raw trace data."""

from __future__ import annotations

import json
from urllib.parse import urlsplit, urlunsplit

from neo4j import GraphDatabase

from open_deep_research.configuration import Configuration
from open_deep_research.web.run_data import RunNotFound, public_payload


def execution_view(store, run_id, owner, workflow_id, offset, limit, workflow_offset=0):
    """Read public DBOS catalog APIs, explicitly excluding inputs/outputs/errors."""
    store.facts.request(run_id, owner)
    workflow_id = workflow_id or run_id
    if workflow_id != run_id and not workflow_id.startswith(run_id + ":"):
        raise RunNotFound("Workflow not found")
    workflows = store.client.list_workflows(
        workflow_id_prefix=run_id,
        load_input=False,
        load_output=False,
        limit=limit + 1,
        offset=workflow_offset,
    )
    selected = store.client.list_workflows(
        workflow_ids=[workflow_id],
        load_input=False,
        load_output=False,
    )
    if not selected:
        raise RunNotFound("Workflow not found")
    steps = store.client.list_workflow_steps(
        workflow_id,
        load_output=False,
        offset=offset,
        limit=limit + 1,
    )
    workflow_fields = (
        "workflow_id",
        "name",
        "status",
        "queue_name",
        "app_version",
        "parent_workflow_id",
        "created_at",
        "updated_at",
        "completed_at",
        "dequeued_at",
    )
    step_fields = (
        "function_id",
        "function_name",
        "child_workflow_id",
        "started_at_epoch_ms",
        "completed_at_epoch_ms",
    )
    return public_payload(
        {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "workflows": [
                {key: getattr(w, key, None) for key in workflow_fields}
                for w in workflows[:limit]
                if w.workflow_id == run_id or w.workflow_id.startswith(run_id + ":")
            ],
            "workflows_has_more": len(workflows) > limit,
            "next_workflow_offset": workflow_offset + min(len(workflows), limit),
            "steps": [{key: s.get(key) for key in step_fields} for s in steps[:limit]],
            "steps_has_more": len(steps) > limit,
            "next_offset": offset + min(len(steps), limit),
            "note": "Native journal commands; absent completion time is not proof of failure. Payloads are withheld.",
        }
    )


NODE_PROPERTIES = frozenset(
    {
        "title",
        "url",
        "source_type",
        "tool_name",
        "published_at",
        "retrieved_at",
        "source_id",
        "statement",
        "quote",
        "evidence_type",
        "stance",
        "confidence",
        "summary",
        "status",
        "finding_type",
        "evidence_ids",
        "claim_ids",
        "source_ids",
        "finding_ids",
        "uncertainty_ids",
        "supports_claim_ids",
        "contradicts_claim_ids",
        "contextualizes_claim_ids",
        "name",
        "date",
        "severity",
    }
)


def _source_url(value):
    try:
        parsed = urlsplit(str(value))
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            return None
        # Queries, fragments and userinfo may contain access tokens.
        host = parsed.hostname
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port:
            host += ":" + str(parsed.port)
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return None


def public_node(row):
    try:
        props = json.loads(row.get("properties_json") or "{}")
    except (TypeError, ValueError):
        props = {}
    safe = {}
    if not isinstance(props, dict):
        props = {}
    for key, value in props.items():
        if key not in NODE_PROPERTIES:
            continue
        if key == "url":
            safe[key] = _source_url(value)
        elif isinstance(value, str):
            safe[key] = value[:6000]
        elif isinstance(value, (int, float, bool)) or value is None:
            safe[key] = value
        elif isinstance(value, list):
            safe[key] = [v[:160] for v in value[:100] if isinstance(v, str)]
    return public_payload(
        {
            "node_id": row["node_id"],
            "node_type": row["node_type"],
            "role": row.get("role"),
            "research_round": row.get("research_round"),
            "task_id": row.get("task_id"),
            "properties": safe,
        }
    )


def evidence_view(
    store,
    run_id,
    owner,
    *,
    after="",
    limit=40,
    kind="",
    query="",
    node_id="",
    edge_after="",
):
    """Read the existing ResearchNode/ResearchEdge schema without model calls."""
    req = store.facts.request(run_id, owner)
    cfg = Configuration.from_runnable_config(req["data"]["config"])
    if not cfg.research_graph_enabled or cfg.research_graph_backend != "neo4j":
        return {
            "run_id": run_id,
            "available": False,
            "nodes": [],
            "edges": [],
            "has_more": False,
            "edges_has_more": False,
            "reason": "Research Graph disabled or process-local; no durable evidence view is available.",
        }
    with GraphDatabase.driver(
        cfg.research_graph_uri,
        auth=(cfg.research_graph_username, cfg.research_graph_password),
    ) as driver:
        rows, _, _ = driver.execute_query(
            """
            MATCH (n:ResearchNode {run_id: $run_id})
            WHERE n.node_id > $after
              AND ($kind = '' OR n.entity_type = $kind)
              AND ($node_id = '' OR n.node_id = $node_id)
              AND ($query = '' OR toLower(coalesce(n.properties_json, '')) CONTAINS toLower($query)
                   OR toLower(n.node_id) CONTAINS toLower($query))
            RETURN n.node_id AS node_id, n.entity_type AS node_type,
                   n.role AS role, n.research_round AS research_round,
                   n.task_id AS task_id, n.properties_json AS properties_json
            ORDER BY n.node_id LIMIT $limit
            """,
            run_id=run_id,
            after=after,
            kind=kind,
            query=query,
            node_id=node_id,
            limit=limit + 1,
            database_=cfg.research_graph_database,
            routing_="r",
        )
        nodes = [public_node(dict(row)) for row in rows[:limit]]
        edges = []
        if node_id and nodes:
            edges, _, _ = driver.execute_query(
                """
                MATCH (s:ResearchNode {run_id: $run_id})
                      -[e:ResearchEdge {run_id: $run_id}]->
                      (t:ResearchNode {run_id: $run_id})
                WHERE (s.node_id = $node_id OR t.node_id = $node_id) AND e.edge_id > $after
                RETURN e.edge_id AS edge_id, e.relation_type AS relation_type,
                       s.node_id AS source_id, t.node_id AS target_id
                ORDER BY e.edge_id LIMIT $limit
                """,
                run_id=run_id,
                node_id=node_id,
                after=edge_after,
                limit=limit + 1,
                database_=cfg.research_graph_database,
                routing_="r",
            )
        return public_payload(
            {
                "run_id": run_id,
                "available": True,
                "nodes": nodes,
                "next_cursor": nodes[-1]["node_id"] if nodes else after,
                "has_more": len(rows) > limit,
                "edges": [dict(row) for row in edges[:limit]],
                "edges_has_more": len(edges) > limit,
                "next_edge_cursor": edges[min(len(edges), limit) - 1]["edge_id"]
                if edges
                else edge_after,
            }
        )
