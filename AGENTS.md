# Public Opinion Research development guide

## Current architecture and migration boundary

Phase 5 retires the LangGraph business workflow and handwritten Agent loop.
The only new research chain is FastAPI/CLI Run API -> native DBOS 3.3.0
Workflow/Step/Queue -> SDKAgentRuntime -> OpenAI Agents SDK 0.24.0 Runner ->
Research Graph Context Harness -> existing business tools.

Read `docs/audits/openai-agents-phase5.md`, the migration plan,
`docs/audits/openai-agents-phase4-dbos.md` and
`docs/architecture/dbos-deployment.md` before changing execution ownership,
resource reservations, external operation receipts, archive compensation or auth.
DBOS journal/status is the execution authority. Domain facts must not become a
second scheduler, lease system or Run state machine. Never replay an interrupted
Agent/tool unit with unknown external effects. Only the durable Finalizer applies
retry grants; an old key cannot authorize another failure generation.

Runtime/backend selectors are no longer Configuration fields. Input aliases
`python`/`dbos` and `openai_agents` are validated and discarded; retired
`langgraph`/`legacy` values fail closed, including environment configuration.
There is no fallback or cross-engine Run redelivery. `get_workflow` is a rejection
stub for old callers; use the durable Run API or `deep-research` CLI.
Application version is `dbos-brief-spec-v1` (see `docs/architecture/research-brief.md`; pending `dbos-phase5-studio-v1` journals must drain on their matching release), SDK runtime version `sdk-phase5-v1`.
The Studio relay waits for its terminal-event manifest before acknowledging
delivery. Pending `dbos-phase5-v1` journals must drain on their matching release;
the new Executor must not replay those older relay command sequences.
Pending Phase 4 DBOS journals must drain on their matching release/Executor.
Old SQL Runs also drain on their original release; no migration/replay is implied.
Legacy report archives retain their owner policy and read/import support.

The SDK owns all four business Agent loops. Planning, intake, review, report,
extraction/compaction and memory model calls remain bounded single-purpose
LangChain business services. They do not execute an Agent/tool loop. RAG and
Research Graph retain separate ownership. Unknown model aliases use a 32,768-token
application window unless explicitly configured; this is not a provider claim.
DBOSRunner turn-level replay, a new workflow engine, multi-tenant identity and
cross-service exactly-once guarantees are outside this migration.

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
  reporting and native DBOS workflows; pure State reducers remain explicit.
- `runtime/`: SDK Runner integration, Harness and role assembly; `public_opinion_agents/`:
  the four business roles and their prompts, contracts and tool policies.
- `social_media/domain.py` and `social_media/records.py`: shared domain rules
  and record helpers. `social_media_mcp.py` and `social_media_apify_api.py` own
  their distinct MCP/HTTP, pagination, normalization and persistence semantics.
- `rag/`: knowledge indexing/retrieval and its knowledge graph. `research_graph/`:
  research evidence, findings, provenance and run-scoped context. Keep their
  data ownership separate.
- `memory/`: long-term business memory, independently of conversation sessions,
  checkpoints and report archives. `web/history.py` owns report history.
- `models/`, `llm/`, `mcp/`, `tools/`: provider adapters, invocation,
  protocol clients and tool admission. `web/runs.py` owns Run authentication.
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
.venv/Scripts/python.exe -m compileall -q src/open_deep_research
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
