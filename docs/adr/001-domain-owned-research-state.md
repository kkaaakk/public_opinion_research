# ADR-001: Domain-owned research state

## Status

Accepted (superseded in part by the context simplification)

## Context

The public-opinion workflow stored workflow progress, role outputs, report data,
budget and graph/memory metrics as independent top-level channels. Parallel roles
therefore needed many field-specific reducers, and every subgraph boundary
manually copied the same collection of fields.

## Decision

Keep `messages` as the native top-level channel and group all other serializable
data into `workflow`, `agents`, `report` and `runtime`. Each aggregate has one
parallel-safe reducer.

`SDKContextHarness` owns the invocation-local tool record, rolling summary and
compacted turn window; DBOS Steps persist them without a second history. Role
reports and `runtime.budget` are the only cross-workflow business data. There is
no Research Graph, Working Context, Context Manager or Agent private memory.

## Alternatives considered

- Keep flat channels and add helper accessors: smaller diff, but leaves ownership
  and packet duplication unresolved.
- Store runtime objects in State: simpler wiring, but breaks serialization,
  checkpointing and deterministic merge behavior.
- Add manager/service layers per concern: explicit, but creates more concepts than
  the problem requires.

## Consequences

- Parallel role, budget, task and section updates retain dedicated merge
  semantics inside four aggregate reducers.
- `research_mode` is removed and follow-up execution derives from `workflow.round`.
- `notes` and `raw_notes` are not global state channels; compression keeps raw
  notes local to one runtime invocation.
- Internal callers must use the aggregate schema; this is an intentional internal
  API break with no change to the four-agent business workflow.
