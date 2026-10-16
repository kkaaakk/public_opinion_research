# Public Opinion Research development guide

## Current architecture and migration boundary

The active preparation branch is `codex/openai-agents-migration`, based on
`backup/pre-deerflow` / `41cbc134f748750ffbfc7c7dde65c7552ace6296`.
The application still runs its existing LangGraph/LangChain implementation.
OpenAI Agents SDK is the selected future runtime; it is **not installed or wired
into this branch**. Do not start that migration as part of repository cleanup.
DeerFlow is retired. Its local environments, Docker deployment, branch
`codex/deerflow-phase0` and tag `phase0-deerflow` were removed at the user's
request. The original opinion project backup and `v1-pre-deerflow` remain.

Read [the preparation audit](docs/audits/pre-openai-sdk-cleanup.md) and
[the migration plan](docs/architecture/openai-agents-sdk-migration-plan.md)
before changing runtime boundaries. The applicable findings from the historical Agent Server audit are preserved
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
