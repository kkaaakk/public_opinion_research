# Observability architecture

Public Opinion Research uses two independent observability channels with a
deliberate boundary:

```text
LangSmith         → Execution observability (Trace / Span / debug / latency / error)
Agent Observer    → Token & cost observability (usage / cache / budget / cost)
```

All integrations live in `src/open_deep_research/observability/`. Business
modules call thin boundaries only; neither channel may change research
behavior, RAG results, prompts, or agent decisions.

## 1. LangSmith — execution tracing (primary)

LangSmith is the project's tracing system. Because this is a LangGraph /
LangChain application, **native auto-tracing** provides most of the coverage —
no manual span is created for anything LangChain already captures:

| Chain segment | How it is captured | Notes |
| --- | --- | --- |
| Root trace (`public_opinion_research`) | LangGraph auto-tracing | Compiled with `name="public_opinion_research"`; one user request = one trace. |
| Graph nodes (`enrich_query_images`, `clarify_with_user`, …) | LangGraph auto-tracing | Node runs are children of the trace root. |
| Agents (`public_signal_agent`, `internal_knowledge_agent`, `risk_assessment_agent`, `response_strategy_agent`) | LangGraph auto-tracing | Distinguished by node name plus `agent_name` / `agent_role` metadata. |
| `research_phase` multi-agent subgraph | LangGraph auto-tracing | Compiled as `public_opinion_agents`; correlation metadata is attached in `research_phase`. |
| LLM calls (planner, researcher, writer, reviewer, compression, query rewrite) | LangChain auto-tracing | One LLM span per provider call; retries show as separate attempts. Structured-output calls carry `structured_output=true` and a `component`. |
| Tools (`web_search`, `rag_search`, social media, MCP) | LangChain tool tracing | One Tool span per execution, with `tool_domain` and (for MCP) `mcp_server` metadata. |
| Retriever / RAG stages | Thin `trace_span` from `observability.langsmith` | Only for stages LangChain cannot auto-capture (see below). |
| Latency, errors, retries, interrupt/resume | LangSmith + LangGraph native | Errors keep status `error`; retries are visible per attempt; `thread_id` links resume segments. |

### RAG stage spans

LangChain cannot see inside the local RAG pipeline, so a small number of
high-value spans wrap the diagnostic stages in
`rag/service.py`, `rag/retriever.py`, and `rag/query_rewriter.py`:

```text
rag_search (Tool, auto-traced)
├── query_rewrite            (metadata: model, original/rewritten query length)
│   └── LLM                  (auto-traced)
├── embed_query              (embedding provider/model)
├── rag_retrieval            (retriever_type, top_k, vectorstore_provider)
│   ├── vector_retrieval     (vectorstore_provider, top_k, result_count)
│   ├── bm25_retrieval       (keyword index, top_k, result_count)
│   ├── hybrid_merge         (retrieval_mode, vector/bm25 counts, rrf constant)
│   ├── graph_retrieval      (graph backend, max_neighbors, result_count)
│   └── structured_metadata_boost
├── rerank                   (reranker_provider/model, input/output count)
└── result_selection         (input/output count, top_k)
```

The Research Graph (per-run memory) adds a `research_graph_retrieval` span
with `graph_backend`, `max_nodes` / `max_edges`, and node/edge counts.

Span metadata is **bounded by design**: counts, provider names, and lengths
only. Query text, retrieved chunks, tool results, and agent reports are never
copied into metadata.

### Enabling LangSmith

LangSmith is configured exclusively through the official environment
variables; the project does not add a parallel configuration system.

```env
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT=public-opinion-research
```

Behavior:

* `LANGSMITH_TRACING=false` or unset → no tracing, zero runtime impact.
* Tracing enabled but **no API key** → `observability.langsmith` normalizes
  tracing off for the process, logs one warning, and the run proceeds normally.
* `LANGSMITH_PROJECT` empty → normalized to `public-opinion-research`.
* LangSmith network failures are fail-open: uploads happen in a background
  thread and can never fail a research task.
* Tests disable tracing by default via `tests/conftest.py`
  (`LANGSMITH_TRACING=false`).

### Correlation metadata

Lightweight, identifier-only correlation facts travel with every run
(`observability.langsmith.correlation_metadata`):

```text
workflow=public_opinion_research
thread_id            (LangGraph native; also links interrupt/resume)
logical_run_id       (deterministic per thread_id, no global registry)
research_run_id
agent_name / agent_role / node_name / node_kind
component            (e.g. rag_query_rewrite, report_planner)
structured_output
environment / git_commit   (only when set: APP_ENV, GIT_COMMIT)
```

Values are emitted only when present, truncated to bounded lengths, and never
include graph state, prompts, documents, tool output, or credentials. There is
no `dict[thread_id, trace]` registry and no non-serializable object is written
into LangGraph state or checkpoints.

### Privacy and data boundaries

* LangSmith auto-tracing keeps its normal debug capability for model
  input/output. The project does not duplicate model input/output into
  metadata and does not copy it to the Agent Observer.
* Custom spans record only the bounded facts listed above.
* LangSmith receives no API keys, DSNs, cookies, or authorization headers;
  MCP/DB credentials stay in configuration and are never logged.
* Whole LangGraph state, memory stores, RAG chunks, web pages, MCP payloads,
  and social media content are not attached to spans.

## 2. Agent Observer — token & cost observability

`observability/agent_observer.py` is an **optional, disabled-by-default**
sidecar adapter (`agent_observer_enabled=false`) that reports provider usage
and tool facts to a local Agent Observer endpoint:

* provider-returned `usage_metadata` (input / output / cache-read /
  cache-creation) per model call;
* tool call/result facts with bounded sizes and durations;
* minimal Run/Agent context required to attribute those facts.

Responsibilities that are *not* Agent Observer's anymore:

* graph topology visualization (removed — LangGraph/LangSmith provide the
  real topology);
* execution tracing hierarchy (LangSmith's role).

Token accounting itself — the authoritative semantics — lives in
`open_deep_research/budget.py` on LangChain's official callbacks
(`ModelAttemptCounter` + `UsageMetadataCallbackHandler` + `add_usage`). It is
completely independent of both LangSmith and the Agent Observer sidecar:

* only provider-reported usage is recorded; missing fields stay null/`N/A`
  and are never estimated;
* no double counting for nested model wrappers or retries;
* LangSmith's token display is informational and never feeds budget policy.

## 3. Failure isolation

| Scenario | Expected behavior |
| --- | --- |
| No `LANGSMITH_API_KEY` | Research runs normally; tracing normalized off. |
| `LANGSMITH_TRACING=false` | Research runs normally; no spans created. |
| LangSmith network unreachable | Upload errors are logged in the background; research continues. |
| Agent Observer endpoint down / package absent | Sidecar is a no-op; research continues. |
| Tracing code raises | All span/metadata helpers catch and degrade; business exceptions still propagate. |

## 4. Troubleshooting

* **No traces appear**: check `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY`,
  and `LANGSMITH_PROJECT`; a missing key is normalized off with a warning at
  startup.
* **Traces land in the wrong project**: `LANGSMITH_PROJECT` must be set before
  the first LangSmith call; the project defaults to
  `public-opinion-research` when empty.
* **Missing RAG stages**: they only appear when the `rag_search` tool executes
  with tracing enabled.
* **Agent spans missing `agent_role`**: node metadata is attached in
  `deep_researcher.py` (`node_metadata(...)`); custom graphs must pass it too.
* **Duplicate LLM/Tool spans**: never wrap model/tool calls with
  `@traceable` or another callback handler; native tracing already covers
  them. Custom spans are only for RAG stages LangChain cannot see.
