# Public Opinion Research development guide

## Current architecture and migration boundary

Phase 5 retires the LangGraph business workflow and handwritten Agent loop.
The research chain starts at the FastAPI/CLI Run API and uses native DBOS 3.3.0
Workflow/Step/Queue. Public Signal and Internal Knowledge use SDKAgentRuntime,
official DBOSRunner 0.3.0, OpenAI Agents SDK 0.24.0, the SDK Context Harness and
existing business tools. Risk Assessment, Response Strategy and the final report
use bounded SDK model Steps inside the research Workflow.

Read `docs/audits/openai-agents-phase5.md`, the migration plan,
`docs/audits/openai-agents-phase4-dbos.md` and
`docs/architecture/dbos-deployment.md` before changing execution ownership,
resource reservations, external operation receipts, archive compensation or auth.
DBOS journal/status is the execution authority. Business persistence is limited
to request and retry authorization, actual model and tool usage, and protected
external effects. See [domain persistence and cutover](docs/audits/dbos-domain-simplification.md). Never replay an interrupted
Agent/tool unit with unknown external effects. Only the durable Finalizer applies
retry grants; an old key cannot authorize another failure generation.

Runtime/backend selectors are no longer Configuration fields. Input aliases
`python`/`dbos` and `openai_agents` are validated and discarded; retired
`langgraph`/`legacy` values fail closed, including environment configuration.
There is no fallback or cross-engine Run redelivery. `get_workflow` is a rejection
stub for old callers; use the durable Run API or `deep-research` CLI.
Application version is `por-call-ledger-v1`. Drain `por-agent-context-v1`,
`por-inline-report-v1`, `por-research-agents-v1`, `por-sdk-models-v1` and
`por-vertical-v1` with their matching releases before cutover.
Pending `dbos-events-v1` and earlier
journals must drain with their matching release before cutover. Native Stream offsets
drive SSE resume. The single writer records one delivery Event per published
prefix. Finalizer reads native results and the effect ledger before publishing
terminal frames and compensates History independently. Pending
`dbos-native-recovery-v1` journals require matching executors and explicit schema cutover. Pending
`dbosrunner-native-interaction-v1` and earlier journals must drain on their
matching release. The earlier PR #44 preview `dbosrunner-brief-idempotency-v1`, if deployed,
also needs its matching release. See `docs/architecture/dbos-deployment.md`.
SDK runtime version is `sdk-vertical-v1`.
Pending `dbos-phase5-v1` journals must drain on their matching release;
the new Executor must not replay those older relay command sequences.
Pending Phase 4 DBOS journals must drain on their matching release/Executor.
Old SQL Runs also drain on their original release; no migration/replay is implied.
Report archives live in PostgreSQL (`por_domain.history`) under the existing owner policy.

The runtime no longer has a Research Graph, Working Context, Context Manager,
Agent private memory, MySQL long-term memory or Neo4j history. Do not reintroduce
a graph store, a second memory lifecycle or a compatibility switch. Research
State carries only `messages`, `workflow`, `agents` (role reports), `report` and
`runtime.budget`. Each SDK invocation keeps its rolling summary and compacted
turn window invocation-local; DBOS Steps persist them without a second history.

The SDK owns the two research Agent loops. Planning, intake, review, risk,
strategy, report and rolling compaction use the same SDK Model directly with
bounded DBOS Steps. RAG retains ownership of indexing and retrieval and uses the
shared SDK path for inference.
Unknown model aliases use a 32,768-token
application window unless explicitly configured; this is not a provider claim.
DBOSRunner owns SDK model checkpoints; explicit tool and bounded Context Steps
rebuild the Harness. Plan/Review/Report use native dynamic model Steps. A new
workflow engine, multi-tenant identity and cross-service exactly-once guarantees
remain outside this migration. Read `docs/architecture/agent-loop.md` and the historical
`docs/architecture/dbosrunner-unification.md` before changing these boundaries; domain report projections must never resume work.

The tool layer has four owners and no adapter between them: `@business_tool`
declares one SDK `FunctionTool` plus its trusted `domain`/`effect`,
`ToolManager.discover()`/`bind()` admit and bind a role's catalog,
`ToolExecutor` owns permission, budget admission, `por.agent_tool`,
external-operation receipts and settlement, and DBOS journals the result.
`SDKAgentRuntime` must not re-validate what discovery already proved;
`ToolExecutor._authorize()` still re-reads the domain at invocation because that
is the execution boundary, not the assembly one. An uncommitted tool Step can
only be handled per effect: `read`/`idempotent` re-run with one ledger row and an
explicit duplicate-request warning, `protected` fails closed when its Operation
receipt already exists. Never add a response cache, a second executor or a
per-effect hierarchy to narrow that window. Read
`docs/audits/tool-execution-recovery.md` and `docs/architecture/tools.md` before
changing tool admission, identity derivation or recovery.

DeerFlow is retired. Its local environments, Docker deployment, branch
`codex/deerflow-phase0` and tag `phase0-deerflow` were removed at the user's
request. The original opinion project backup and `v1-pre-deerflow` remain.

Read [the preparation audit](docs/audits/pre-openai-sdk-cleanup.md) and
[the migration plan](docs/architecture/openai-agents-sdk-migration-plan.md)
before changing runtime boundaries. Read [the Phase 0 results](docs/audits/openai-agents-phase0.md)
for tested SDK interfaces, tool coverage and remaining limitations.
Read [the Phase 1 results](docs/audits/openai-agents-phase1.md) for Runtime
selection, budget compatibility and explicit unsupported SDK paths. SDK input
uses native dictionaries and official Models. LangChain Core message, tool and
text-splitting utilities remain behind the business boundary. Read the Phase 2 audit before
changing batch staging, context scoping or native history compaction.
Do not add another agent loop or
silently fall back to legacy after an SDK failure.
The applicable findings from the historical Agent Server audit are preserved
in the migration plan, which links to the original Git evidence.

## Source ownership

- `src/open_deep_research/workflow/`: intake, planning, research orchestration,
  reporting and native DBOS workflows; pure State reducers remain explicit.
- `runtime/`: SDK Runner integration, Harness and role assembly; `public_opinion_agents/`:
  the two research agents and their prompts, contracts and tool policies.
- `tools/plugins.py` owns plugin manifest, Skill and MCP assembly; `plugins/social-research/`
  declares the Apify hosted MCP server plus the xiaohongshu, douyin and weibo research
  skills. There is no local social adapter or HTTP collector: social platform data is
  collected through Apify MCP and the shared `ToolExecutor`. Do not reintroduce a
  local social-media backend or a per-platform MCP server.
- `rag/`: enterprise knowledge indexing and hybrid retrieval. Report history is
  owned by `web/history.py` in PostgreSQL; there is no runtime evidence graph.
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
