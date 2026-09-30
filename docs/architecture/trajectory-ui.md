# Local Trajectory developer view

The web page uses the MIT-licensed DeepSeek Harness Trajectory React source,
not a parallel tracing backend. Source provenance, SHA and the only upstream
compatibility change are recorded in
`src/open_deep_research/web/frontend/src/trajectory/UPSTREAM.md`.

```text
one LangGraph execution
  → astream_events(version="v2")
  → web/trajectory.py (bounded redaction and presentation projection)
  → /api/research SSE {type: "trajectory", event: ...}
  → frontend/trajectory/adapter/reducer.ts
  → DSH TrajectorySnapshot
  → original TrajectoryView → Timeline / Table / Inspector
```

The same root `on_chain_stream` events continue to supply status, report and
`budget.py` usage to the existing SSE contract. The graph is never run twice.
LangSmith remains the deep execution trace system; `budget.py` remains the
authority for total usage and budget policy. The local Trajectory is an
ephemeral browser presentation of the current run. It stores no traces and
does not read LangSmith or require `LANGSMITH_API_KEY`.

The adapter keeps real `run_id` and `parent_ids`; a request is one Turn, model
and agent boundaries become Steps, and nested Tool calls are attached to their
parent Tool. Model usage comes only from provider-reported `usage_metadata`.
TTFT appears only after a real nonempty model stream chunk. Missing timing or
usage remains unavailable; no value is estimated. Some providers and graph
nodes do not emit token-level stream events, cache usage, or explicit retry
events, so those fields remain unavailable unless the runtime reports them.

`web/trajectory.py` is the single serialization and redaction boundary. It
limits depth, collection size and text length and strips credential keys,
Bearer tokens and URL passwords before browser transport. Tool and model
details stay available within those bounds. A projection failure is logged
without interrupting research. Browser Stop aborts the stream, keeps existing
records and closes in-flight records as cancelled.

Build the frontend with `npm --prefix src/open_deep_research/web/frontend ci`
and `npm run web:build`; FastAPI serves the built app at `/` and assets below
`/static/dist/`. Use `npm run web:dev` for Vite with `/api` proxied to the
FastAPI server. Only the React app is the active web entry point.
