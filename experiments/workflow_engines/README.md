# Workflow engine evaluation POC

These experiments are intentionally outside the production package and default
pytest testpaths. Start from Phase 4 commit
`5ca71c12f15013654a26f619071218a2be151a00` in an isolated worktree. They do not
change the Web/API, frontend, production configuration, root dependency lock,
Runtime selection or business Agent count.

Read [the evaluation](../../docs/audits/workflow-engine-evaluation.md) for the
recommendation, measured code inventory, native-integration failures and limits.

## What runs

- Real DBOS 3.3.0 Queue/Workflow/Steps, durable messages/events and cancellation.
- Real Temporal 1.34.0 Workflow/Activities, default sandbox, Update approval,
  heartbeat, retries, and a local Temporal Server.
- The unchanged Phase 4 PythonWorkflow, SDKAgentRuntime, Runner.run,
  SDKContextHarness, Research Graph adapter and domain reducers.
- Official SDK ScriptedModel plus existing inference fixtures. The LangChain
  FunctionTool business adapter is real. The underlying synthetic tool writes
  every external effect into a separate SQLite authority with no deduplication.
- A disposable Neo4j instance, not a mock graph, for process-restart evidence.
- A real worker process kill after a committed engine step, and after a tool
  effect but before the application receipt. The latter is run both with and
  without the domain guard; unguarded replay must produce duplicate effects.

No provider model, paid search API or remote MCP is called. `common.py` disables
dotenv/tracing before importing project modules and rejects non-loopback Python
socket connections. Test subprocesses remove real API key/token/secret values.
The only password below is a synthetic credential for the disposable container.
No API key setup is needed.

## Environment

The commands below assume PowerShell, Python 3.11, uv, Docker and the isolated
worktree as the current directory. Do not run `uv sync` against someone else's
active environment. The root lock is the baseline; engine additions are only
installed into this worktree's disposable `.venv`.

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.tmp/uv-cache'
$env:UV_LINK_MODE = 'copy'
uv sync --extra dev --locked --python 3.11
uv pip install --python .venv/Scripts/python.exe -r experiments/workflow_engines/requirements-poc.txt
$env:PYTHON_DOTENV_DISABLED = '1'
$env:LANGSMITH_TRACING = 'false'
$env:LANGCHAIN_TRACING_V2 = 'false'
$env:OPENAI_AGENTS_DISABLE_TRACING = '1'
```

The official Temporal Agents plugin is **not** in requirements: version 1.1.0
requires `openai-agents>=0.22,<0.23`. Check the real solver failure without
altering the environment:

```powershell
uv pip install --dry-run --python .venv/Scripts/python.exe 'temporalio-openai-agents==1.1.0' 'openai-agents==0.24.0'
```

The existing markitdown-mcp/mcp constraint conflict remains. `uv pip check` is
expected to report it; it must not be described as passing.

## Disposable services

Start only a new container with this unique name. If the name is already in use,
inspect its ownership before reusing or deleting anything. No existing MySQL or
Neo4j instance is changed, and no user data or mounted volumes are used.

```powershell
docker run -d --name por-engine-eval-neo4j --label por-evaluation=workflow-engine-evaluation -e NEO4J_AUTH=neo4j/evaluation-only-password -p 127.0.0.1:17687:7687 --memory=1g neo4j:5.26.31
```

Wait until that disposable container accepts Bolt connections. Download the
[official Temporal CLI 1.9.1](https://github.com/temporalio/cli/releases/tag/v1.9.1)
into `.tmp/temporal-cli`. On Windows amd64 the archive is
`temporal_cli_1.9.1_windows_amd64.tar.gz`; verify SHA256
`42637464c337a3da203fbd4e8c688e4b2d511fd42bd19c337c0422e923a7b619`
before extraction. CLI 1.9.1 embeds Server 1.32.0. No production Server is needed
for this test, but the dev Server does not demonstrate production HA.

```powershell
New-Item -ItemType Directory -Force .tmp/temporal-cli | Out-Null
Invoke-WebRequest 'https://github.com/temporalio/cli/releases/download/v1.9.1/temporal_cli_1.9.1_windows_amd64.tar.gz' -OutFile .tmp/temporal-cli/cli.tar.gz
$archiveHash = (Get-FileHash .tmp/temporal-cli/cli.tar.gz -Algorithm SHA256).Hash.ToLowerInvariant()
if ($archiveHash -ne '42637464c337a3da203fbd4e8c688e4b2d511fd42bd19c337c0422e923a7b619') { throw 'Unexpected CLI archive hash' }
tar -xzf .tmp/temporal-cli/cli.tar.gz -C .tmp/temporal-cli
$engineServer = Start-Process -FilePath (Join-Path (Get-Location) '.tmp/temporal-cli/temporal.exe') -ArgumentList @('server','start-dev','--ip','127.0.0.1','--port','17233','--headless','--db-filename','.tmp/temporal-server.sqlite','--log-level','error') -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -RedirectStandardOutput '.tmp/temporal-server.log' -RedirectStandardError '.tmp/temporal-server.err' -PassThru
```

Use a fresh test database/server namespace if reproducing the experiment more
than once. The tests generate unique workflow IDs and fresh per-test directories.
Do not start a second server on the same port.

## Run the contracts

```powershell
$env:ENGINE_POC_LIVE = '1'
.venv/Scripts/python.exe -m pytest experiments/workflow_engines/test_engines.py -q --tb=short --disable-warnings --basetemp .tmp/poc-reproduction --junitxml .tmp/poc-reproduction.xml
```

This is an explicit local-service suite. Without `ENGINE_POC_LIVE=1` it is skipped,
and the default project suite does not collect it. An actual recorded run must
have no skips/xfails. Allow several minutes for process startup, restart and real
heartbeat timeouts. `test_engines.py` owns and kills only its spawned processes.

The `normal` case asserts the exact fixture ledger including initial values:
28 model calls, 235 input tokens, 76 output tokens, 311 total tokens and two tool
effects. It also asserts role-scoped memory, actual input-filter execution,
Graph receipts, consumer evidence and successful Harness settlement. The Gap
case must execute round 2 and both follow-up tasks, not just return a canned
complete review. The rolling case must perform real compaction.

For a single manual run:

```powershell
$env:RESEARCH_GRAPH_URI = 'bolt://127.0.0.1:17687'
$env:RESEARCH_GRAPH_USERNAME = 'neo4j'
$env:RESEARCH_GRAPH_PASSWORD = 'evaluation-only-password'
.venv/Scripts/python.exe experiments/workflow_engines/dbos_poc.py start .tmp/manual-dbos --run-id manual-dbos --graph neo4j
.venv/Scripts/python.exe experiments/workflow_engines/temporal_poc.py start .tmp/manual-temporal --run-id manual-temporal --graph neo4j
.venv/Scripts/python.exe experiments/workflow_engines/dbos_poc.py probe .tmp/manual-native-probe
```

The native probe deliberately records a successful bare DBOSRunner call and the
real Harness incompatibility caused by DBOSRunner cloning the Agent. A test of
that documented failure passing does not mean the integration is compatible.
The probe runs in one event loop, and neither production SDK nor Harness is
patched on disk.

## Baseline and inventory

```powershell
.venv/Scripts/python.exe -m pytest -q --tb=short --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe experiments/workflow_engines/inventory.py
.venv/Scripts/python.exe -m ruff check --select E,F,I --ignore E501 experiments/workflow_engines
.venv/Scripts/python.exe -m ruff format --check experiments/workflow_engines
.venv/Scripts/python.exe -m compileall -q experiments/workflow_engines
```

The inventory reads pinned Git source and reports physical lines and SLOC,
excluding blank lines, standalone comments and docstrings. It lists nonoverlapping
function/method ranges. It does not classify every line as deletable, and the
report explicitly separates measured sizes from future engineering estimates.

## Scope of the bridge

`common.segment` stops at the next real checkpoint through the existing observer
and ownership-return exception. Both Producers remain inside one durable Step
or Activity, using the original in-process join. This preserves Phase 4's coarse
recovery granularity. It does not prove distributed per-role Activities, a durable
SDK model/tool-turn history, SDK Session wiring, API/SSE replacement or complete
RunStore equivalence.

The small SQLite guard retains domain knowledge: stable business segment/round
identity and tool intent/receipt. On an uncertain repeated segment it returns
`interrupted`. It does not implement the full Run API, global/owner/session
quotas, quarantined capacity, every-tool attempt fencing, cross-store projection,
History archive compensation or reconciliation UI/CLI. It is not a new production
RunStore, and no production path imports it.

Cancellation here occurs after tools have returned, while an SDK model waits.
Remote/in-flight/synchronous tool cancellation and database receipt-commit races
are not claimed as verified engine behaviors. The existing production tests
cover those old-path contracts separately.

## Cleanup

Stop only the dev Server process you created, then verify that the container is
this experiment's disposable instance before removing it. Do not delete existing
services, prune Docker volumes, run git clean, or remove user configuration.

```powershell
Stop-Process -Id $engineServer.Id
$containerLabel = docker inspect --format '{{ index .Config.Labels "por-evaluation" }}' por-engine-eval-neo4j
if ($containerLabel -ne 'workflow-engine-evaluation') { throw 'Container ownership does not match' }
docker rm -f por-engine-eval-neo4j
```

Keep the experiment worktree and code for review. Runtime SQLite files, logs,
transcripts, downloaded binaries and `.venv` stay ignored under `.tmp`/`.venv`;
none should be added to Git. The implementation does not read or change the
primary worktree's .env or operator configuration.
