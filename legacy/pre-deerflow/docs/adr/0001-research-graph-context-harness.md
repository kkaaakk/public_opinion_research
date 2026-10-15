# ADR-0001: Separate Research Graph Memory from the ReAct Transcript

## Status

Accepted

## Context

Public-opinion agents currently keep assignments, ReAct reasoning, raw tool
results, evidence, and final reports in the same message lifecycle.  Replaying
that lifecycle on every model call and again during final compression causes
context growth and repeated per-URL summarization calls.  The workflow must
retain autonomous ReAct research, provenance, dynamic follow-up research, and
the existing four business agents.

## Decision

Keep LangGraph as the workflow/orchestration layer and add a small Context
Strategy interface around the existing ReAct loop:

- `research_graph_producer` performs raw/batch tool ingestion, structured graph
  extraction, scoped graph writes, graph retrieval, Context Manager update,
  Micro Compact, and optional Rolling Compact.
- `research_graph_consumer` retrieves a scoped bounded subgraph for analysis and
  does not treat ordinary consumer analysis as source evidence.
- `standard` preserves the existing lifecycle when Research Graph mode is
  disabled.

Use the official Neo4j Python driver for deterministic source/provenance writes
and `neo4j-graphrag` 1.19.x types/schema boundary.  The experimental Knowledge
Graph Builder is not placed directly in the business loop because its default
chunk granularity can reintroduce one LLM call per chunk.  A project-side
token-aware batch adapter owns source boundaries and stable IDs.  The official
GraphRAG retriever/writer/resolver APIs remain isolated extension points rather
than replacing LangGraph answer generation.

## Consequences

### Positive

- Raw history can be compacted only after graph persistence and Context Manager
  success without losing the ToolMessage protocol.
- `Finding -> Claim -> Evidence -> Source` and task coverage remain traversable.
- Retrieval is scoped by `run_id`; follow-up tasks reuse the same graph without
  carrying the first round's complete transcript.
- Existing standard-mode tests and behavior remain compatible.
- Producer Tavily searches no longer fan out into one summarization model call
  per URL.

### Negative

- Graph mode adds a Neo4j operational dependency and several structured model
  calls per successful tool batch.
- The project adapter owns a small amount of Cypher and ID normalization because
  the experimental KG Builder API is not a stable fit for source-bound batches.
- Graph mode must be explicitly configured; a failed Neo4j connection fails
  clearly rather than silently changing execution semantics.

### Neutral

- Tests can use the in-memory adapter and injected model/store fakes.
- Transcript files are append-only diagnostics and are never re-injected into
  model context.

## Alternatives Considered

**Keep full `role_messages` and improve final compression**

Rejected: it leaves repeated context on every ReAct call and still retains the
per-page summary fan-out.

**Use `GraphRAG()` for final answers**

Rejected: it would replace the existing LangGraph business-agent reasoning and
would not preserve the required Producer/Consumer boundaries.

**Use the experimental `SimpleKGPipeline` directly for every tool result**

Rejected: default chunk-level extraction can multiply LLM calls and does not
provide the source-bound batch contract required here.

**Use a global cross-run knowledge graph**

Rejected: this release intentionally isolates retrieval to the current
`run_id`; long-term cross-run knowledge is a separate future decision.

## References

- [Neo4j GraphRAG for Python](https://neo4j.com/docs/neo4j-graphrag-python/current/)
- [GraphRAG Knowledge Graph Builder](https://neo4j.com/docs/neo4j-graphrag-python/current/user_guide_kg_builder.html)
- [GraphRAG Vector + Cypher retrievers](https://neo4j.com/docs/neo4j-graphrag-python/current/user_guide_rag.html)
