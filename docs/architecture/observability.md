# Agent Observer integration

Public Opinion Research keeps Observer integration in
`src/open_deep_research/observability/`. Business modules call thin boundaries;
they do not construct Observer events themselves.

## Runtime inventory

| Runtime path | Boundary | Notes |
| --- | --- | --- |
| Main graph model calls | `observe_model_ainvoke` | Plain and tool-enabled calls; structured calls carry `structured_output=true`. |
| RAG query rewrite | `observe_model_invoke` | Sync model execution runs inside the current async context when called by `rag_search`. |
| Vision/multimodal loader | `observe_model_invoke` | Runtime image extraction is observed; standalone indexing has no Run context and remains unbound. |
| Webpage summarization | `observe_model_ainvoke` | Structured wrapper is visible even when the provider exposes no raw usage. |
| Built-in, RAG, MCP, and social tools | `observe_tool_ainvoke` via `execute_tool_safely` | Records one call/result pair with bounded metadata and no full result content. |

Nested model/tool boundary calls use task-local `ContextVar` depth guards. The
outer boundary is the observed logical/provider call; a nested wrapper does not
create a second call. Provider retries hidden inside LangChain remain a single
logical call unless the provider exposes reliable attempt metadata.

## Usage and privacy

Usage is copied only from `usage_metadata` or `response_metadata` returned by the
provider. Missing input/output/cache/context fields remain `N/A` (`null` in the
protocol); they are never estimated. Tool events retain the tool name, argument
shape summary, duration, success/failure, and bounded result byte measurements.
Full tool content is not enabled by the Public Opinion integration.

## LangGraph lifecycle

`ObserverRunLifecycle` binds an active physical segment to the LangGraph
`thread_id` and removes it at terminal state. LangGraph's runtime metadata is
used as follows:

- `ExecutionInfo.node_attempt` becomes Span metadata `attempt` and `retry`;
- each actual node attempt receives a new Span ID;
- `__pregel_resuming` identifies `Command(resume=...)` invocations;
- the first segment checkpoint and thread derive a stable serializable
  `logical_run_id`.

The Observer SDK supports `run_interrupted` and `span_interrupted`. An
interrupt closes the current physical segment as `INTERRUPTED`. Resume starts a
new physical segment with the same `logical_run_id`; no Python Run object or
unbounded correlation registry is written to LangGraph state/checkpoints.

The native `deep_researcher` factory remains the exported LangGraph graph, so
CLI/Studio receive a supported Pregel graph. P0-2 remains unchanged:
`role_reports` is the formal current-run input to Section Writer, while
`agent_memories` is compact/private memory.
