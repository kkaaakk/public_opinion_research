# DeepSeek Harness Trajectory source boundary

- Repository: https://github.com/deepseek-ai/deepseek-harness
- Default branch at vendoring: `master`
- Commit: `639ed015397290b3745d163aafe02ffee4aa3f84`
- Vendored on: 2026-09-30
- License: MIT, Copyright (c) 2026 DeepSeek. See `upstream/LICENSE`.

`upstream/` is the complete `packages/client/ui-trajectory/` snapshot, including
its original tests, React view, layout, timeline, search index, virtual rows,
Inspector, CSS modules, snapshot builder and DSH session definitions. The
application imports the original `TrajectoryView.tsx` directly. Runtime imports
from the DSH session/Cordis plugin entry point are unused.

The build configuration alteration is `upstream/tsconfig.json`: its DSH monorepo
project references are replaced by a local Vite-compatible tsconfig. The exact
original is preserved as `upstream/tsconfig.dsh.json`. The original renderers, timeline, search and CSS are retained. Phase 2 adds
narrow semantic compatibility edits in these files:

- `upstream/src/client/layout.ts`: explicit backend Locations take precedence
  for user/context/orphan tools; real compaction markers can be shown as context.
- `upstream/src/client/TrajectoryView.tsx`: retain the LangChain cache-inclusion
  marker while deriving request usage.
- `upstream/src/client/TrajectoryTable.tsx`: use reported total input directly
  for that marker, omit an unknown uncached subtotal, and retain the assistant
  accessibility label for an unsettled model request.

These changes adapt the ledger contract; they do not replace the DSH views,
Inspector, toolbar, styling or primitive implementations.

`upstream-primitives/` contains the transitive source closure of the original
DSH primitives used by Trajectory: JsonTree, CodeBlock, MarkdownText, Tooltip,
StateDot, icons, file display helpers and their local imports. Unused primitives
were omitted. `upstream-theme/` contains the
unmodified `packages/client/ui-theme/src/styles/` token sheets needed by these
components.

Local files stay outside those directories:

- `compat/primitives.ts`: narrow import face for the vendored primitives.
- `adapter/reducer.ts`: hydrate backend-projected snapshots and Map fields; no runtime identity inference.
- `src/app/App.tsx`: research form, SSE reader, and DSH View mount.

To update manually: fetch the new upstream commit; diff
`packages/client/ui-trajectory/`, the selected primitive files and theme
styles; replace the upstream snapshots; preserve the original license; reapply
the tsconfig shim; run `npm run typecheck`, `npm test`, `npm run build`, backend
tests, and a browser smoke test.

## Architectural reference (not vendored backend code)

Phase 2 audited the same pinned SHA above. These modules inform the design;
none of their Cordis/Agent/LLM/persistence runtime implementations were copied:

- `packages/core/session/src/{index,types,request-header,repair,tool-history}.ts`:
  immutable append envelopes, request snapshots and conservative missing outcomes.
- `packages/core/agent-loop/src/{agent,assistant-stream,tool-calls}.ts` and
  `packages/core/agent/src/{types,runtime-types,projection}.ts`:
  turn/step boundaries, attempt settlement and execution ownership.
- `packages/llm/llm/src/assistant-stream.ts`: preserve timed delta boundaries in
  one durable settlement rather than one permanent row per token.
- `packages/session/session-persistence-jsonl/src/{index,storage,format,lease}.ts`
  and `packages/session-query/session-query/src/cold-read.ts`:
  writer ownership, validated-prefix reads, checkpoints and read-only recovery.
- `packages/compaction/compaction/src/types.ts`: real compaction brackets;
  this project's context transformations retain their own implementation.
- `packages/client/ui-conversation/src/client/contract/`, its location index,
  and the vendored trajectory definitions/snapshot builder/layout:
  typed projection values, request inspection and window-independent schemas.
- `docs/subsystems/{session,core,persistence,session-query}.md` and
  `docs/agent-lifecycle.md`: source-backed lifecycle and persistence contracts.

Differences are intentional: LangGraph keeps execution/memory authority,
LangSmith keeps spans, budget.py keeps totals, and storage errors are fail-open.
The project uses plain UTF-8 JSONL and OS leases rather than DSH generation
files, compressed frames, migrations or the complete session controller.
