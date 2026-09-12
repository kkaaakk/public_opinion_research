# ADR-001: Domain-owned Deep Research state

## Status

Accepted

## Context

The public-opinion graph stored workflow progress, role outputs, private memory,
Working Context, report data, budget, and metrics as independent top-level
LangGraph channels. Parallel nodes therefore needed many field-specific reducers,
and every subgraph boundary manually copied the same collection of fields.

## Decision

Keep `messages` as LangGraph's native top-level channel and group all other
serializable data into `workflow`, `agents`, `research`, `report`, and `runtime`.
Each aggregate has one parallel-safe reducer. `AgentRuntime` owns ReAct history,
tool execution, rolling compaction, private memory, and role-report finalization.
`ResearchWorkspace` owns current-run Graph storage/retrieval and role-scoped
Working Context. Graph objects remain external and State stores only `run_id`.

## Alternatives considered

- Keep flat channels and add helper accessors: smaller diff, but leaves ownership
  and `Send` payload duplication unresolved.
- Store runtime objects in State: simpler wiring, but breaks serialization,
  checkpointing, and deterministic merge behavior.
- Add manager/service layers per concern: explicit, but creates more concepts than
  the problem requires.

## Consequences

- Parallel role, Working Context, budget, metric, task, and section updates retain
  dedicated merge semantics inside five aggregate reducers.
- `research_mode` is removed and follow-up execution derives from `workflow.round`.
- `notes` and `raw_notes` are not global state channels; standard compression keeps
  raw notes local to one runtime invocation.
- Internal callers must use the new aggregate schema; this is an intentional
  internal API break with no change to the four-agent business workflow.
