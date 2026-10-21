# Public Opinion Research development guide

## Current architecture and migration boundary

The preparation baseline is `codex/openai-agents-migration` at
`2c876398639a11c9b2a66232419da8374e187e83`, descended from `backup/pre-deerflow`
/ `41cbc134f748750ffbfc7c7dde65c7552ace6296`.
Phase 0 installs `openai-agents==0.24.0` and validates the adapter in
`runtime/sdk_context.py` and `models/sdk.py`. The Phase 1 branch
`codex/openai-agents-phase1` starts from merged Phase 0 at `7a72aadc`.
All four business roles now have an executable, offline-tested SDK Agent/Runner
path in `runtime/sdk_runtime.py`, selected with
`configurable.business_agent_runtime="openai_agents"` (or the corresponding
`BUSINESS_AGENT_RUNTIME` environment variable). The default remains `legacy`.
LangGraph still owns parallel research, join, review/gap loops, risk/strategy
ordering and state reducers; Web/API entrypoints are unchanged.
Phase 2 integrates the Research Graph Context Harness across all four SDK roles:
completed tool batches use staged persistence/receipt/context settlement,
Consumers refresh committed evidence, and SDK-native history uses bounded rolling
summaries through the official input filter. See
[the Phase 2 audit](docs/audits/openai-agents-phase2.md) for provider, budget,
concurrency and external-service validation limits. Unknown model aliases use a
32,768-token application window unless an explicit capacity is configured; this
is not a provider capability claim. The default Runtime remains `legacy`.
The selected target is OpenAI Agents SDK + deterministic Python workflow + the
custom Research Graph Context Harness, without DBOS or LangGraph orchestration.
Keep the agreed phases: 0 SDK feasibility; 1 four-role Runtime; 2 full Research
Graph Context Harness integration; 3 deterministic Python Workflow; 4 Web/SSE/
Session integration; 5 old Runtime retirement and dependency cleanup. Do not
advance later phases as incidental cleanup. Phase 1 reuses Phase 0 collection,
input filtering and exit settlement; Phase 2 extends those same seams without
replacing LangGraph or introducing another Agent Loop.
DeerFlow is retired. Its local environments, Docker deployment, branch
`codex/deerflow-phase0` and tag `phase0-deerflow` were removed at the user's
request. The original opinion project backup and `v1-pre-deerflow` remain.

Read [the preparation audit](docs/audits/pre-openai-sdk-cleanup.md) and
[the migration plan](docs/architecture/openai-agents-sdk-migration-plan.md)
before changing runtime boundaries. Read [the Phase 0 results](docs/audits/openai-agents-phase0.md)
for tested SDK interfaces, tool coverage and remaining limitations.
Read [the Phase 1 results](docs/audits/openai-agents-phase1.md) for Runtime
selection, budget compatibility and explicit unsupported SDK paths. SDK input
uses native dictionaries and official Models; LangChain tool/report/Graph
services remain behind the business boundary. Read the Phase 2 audit before
changing batch staging, context scoping or native history compaction.
Do not add another agent loop or
silently fall back to legacy after an SDK failure.
The applicable findings from the historical Agent Server audit are preserved
in the migration plan, which links to the original Git evidence.

## Source ownership

- `src/open_deep_research/workflow/`: intake, planning, research orchestration,
  reporting and current LangGraph construction.
- `runtime/`: current agent loop and role assembly; `public_opinion_agents/`:
  the four business roles and their prompts, contracts and tool policies.
- `social_media/domain.py` and `social_media/records.py`: shared domain rules
  and record helpers. `social_media_mcp.py` and `social_media_apify_api.py` own
  their distinct MCP/HTTP, pagination, normalization and persistence semantics.
- `rag/`: knowledge indexing/retrieval and its knowledge graph. `research_graph/`:
  research evidence, findings, provenance and run-scoped context. Keep their
  data ownership separate.
- `memory/`: long-term business memory, independently of conversation sessions,
  checkpoints and report archives. `web/history.py` owns report history.
- `models/`, `llm/`, `mcp/`, `tools/`, `security/`: provider adapters, invocation,
  protocol clients, tool admission and authentication.
- `web/`: FastAPI and the existing static UI; `evaluation/` and `tests/`:
  evaluation assets and offline contracts. Root `package.json` is docs tooling.

Prefer small changes inside these existing modules over empty packages or
mechanical renames. Preserve compatibility exports until their consumers,
including external scripts, have been assessed. Keep Golden Sets, meaningful
baselines, reproducible runners and independent learning materials.

## Validation and local data

Use the root dependency lock and root `.venv`:

```powershell
uv sync --extra dev --locked --python 3.11
.venv/Scripts/python.exe -m pytest
.venv/Scripts/python.exe -m ruff check --select E,F,I --ignore E501 src/open_deep_research tests/conftest.py tests/test_*.py
.venv/Scripts/python.exe -m compileall -q src/open_deep_research src/security
```

PowerShell does not expand the final Ruff glob; pass
`(Get-ChildItem tests/test_*.py | ForEach-Object { $_.FullName })` there.
Mypy is an existing development tool without a CI gate/configuration; its
35-error historical baseline and reproducible invocation are in the audit.
Do not call a failing check successful, delete tests or weaken assertions.
Features and fixes require regression tests. Do not call paid models during
offline checks; disable dotenv/tracing and use the audit's socket guard.

Do not commit `.env`, operator configuration, runtime databases, caches or
local outputs. Small configuration copies are retained under the ignored
`.tmp/local-config-archive/`; the original opinion `.env` is restored.
Never use `git clean` against local assets. Preserve the saved pre-cleanup
changes and their safety ref; restoration instructions are in the audit.
Do not edit `CLAUDE.md`.
