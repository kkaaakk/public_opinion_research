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

The only altered package file is `upstream/tsconfig.json`: its DSH monorepo
project references are replaced by a local Vite-compatible tsconfig. The exact
original is preserved as `upstream/tsconfig.dsh.json`. No Trajectory component,
layout, timeline, search or CSS implementation was changed.

`upstream-primitives/` contains the transitive source closure of the original
DSH primitives used by Trajectory: JsonTree, CodeBlock, MarkdownText, Tooltip,
StateDot, icons, file display helpers and their local imports. Unused primitives
were omitted. `upstream-theme/` contains the
unmodified `packages/client/ui-theme/src/styles/` token sheets needed by these
components.

Local files stay outside those directories:

- `compat/primitives.ts`: narrow import face for the vendored primitives.
- `adapter/reducer.ts`: LangGraph event DTO to DSH `TrajectorySnapshot`.
- `src/app/App.tsx`: research form, SSE reader, and DSH View mount.

To update manually: fetch the new upstream commit; diff
`packages/client/ui-trajectory/`, the selected primitive files and theme
styles; replace the upstream snapshots; preserve the original license; reapply
the tsconfig shim; run `npm run typecheck`, `npm test`, `npm run build`, backend
tests, and a browser smoke test.
