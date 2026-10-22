# DBOS native architecture POC

This executable is independent of Phase 4's RunStore, RunWorker, PythonWorkflow
and Run API. Production source and the root lock remain unchanged. Read
[DESIGN.md](DESIGN.md) and [the audit](../../docs/audits/dbos-native-architecture-poc.md).

## Setup in the isolated worktree only

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.tmp/uv-cache'
$env:UV_LINK_MODE = 'copy'
uv sync --extra dev --locked --python 3.11
uv pip install --python .venv/Scripts/python.exe -r experiments/dbos_native/requirements-poc.txt
```

The socket/dotenv/tracing guard loads before project imports. The only accepted
PostgreSQL destination is this POC's `127.0.0.1:15433/native_poc`. Inference is
SDK ScriptedModel and existing callback-emitting fixture services. No real API
key, paid model, search API or remote MCP is needed. Memory writes are forced off.

Start **new** uniquely labelled containers, never reuse existing user databases:

```powershell
docker run -d --name por-dbos-native-postgres --label por-evaluation=dbos-native-architecture-poc -e POSTGRES_PASSWORD=native-poc-password -e POSTGRES_DB=native_poc -p 127.0.0.1:15433:5432 --health-cmd 'pg_isready -U postgres -d native_poc' --health-interval 2s --health-retries 30 postgres:17.6
docker run -d --name por-dbos-native-neo4j --label por-evaluation=dbos-native-architecture-poc -e NEO4J_AUTH=neo4j/native-poc-password -p 127.0.0.1:17688:7687 --memory=1g neo4j:5.26.31
```

Wait for PostgreSQL health and Neo4j Bolt readiness. Passwords above are synthetic
container-only fixtures, not operator credentials. No host directories are mounted.
If a name already exists, verify the label and ownership before taking action.

## Run

```powershell
$env:DBOS_NATIVE_LIVE = '1'
.venv/Scripts/python.exe -m pytest experiments/dbos_native/test_native.py experiments/dbos_native/test_extensions.py -q --tb=short --disable-warnings --basetemp .tmp/native-reproduction --junitxml .tmp/native-reproduction.xml
.venv/Scripts/python.exe experiments/dbos_native/inventory.py
```

The live suite is explicit and requires the disposable services. Without the flag
its service tests are skipped; this is not a recorded validation run. Unit clone
and serializer tests require no DB service. Default project CI still runs the
original tests, without silently adding Docker services.

The tests use real process kills, an executor that remains alive while ownership
moves, public `resume_workflow(queue_name=...)`, queue listen filters, native
Streams offsets, transactional enqueue/send, and an actual PostgreSQL stop/start.
They do not fake framework persistence or recovery. All spawned executor processes
are owned by each test and killed on cleanup. Native global/owner limits are
measured across two executor processes. SDK role parallelism is within the
research executor; cross-host distributed Producer execution is not claimed.

Manual normal flow (choose new IDs/directories on each fresh submission):

```powershell
.venv/Scripts/python.exe experiments/dbos_native/executor.py run .tmp/native-manual-a --run-id native-manual-a --route A
.venv/Scripts/python.exe experiments/dbos_native/executor.py run .tmp/native-manual-b --run-id native-manual-b --route B
```

`recover` uses the same executor identity/version for genuine startup recovery.
`--probes` explicitly registers the optional fine-grained SDK replay probe; route B
loads its integration before DBOS launch. The default A entry point loads neither
`runtime_ext` nor `probe`. `serve` launches a DBOS process and waits for a stop marker; there is no custom
claim loop, lease, heartbeat or scheduler. Concurrent live executors must have
different IDs. Public operator recovery can be routed to a queue that the old
executor is not listening to. This demonstrates manual recovery, not Conductor
failure detection or production multi-host HA.

## Structure and guarantees

- `engine.py`: native control/planning/research/role workflows and business steps.
  Planning/research share one limited queue; approval waits do not occupy that
  queue. DBOS is the execution authority. No cursor or duplicate Run state machine.
- `business.py`: explicit model/tool dependencies, original AgentSpecs, SDK Runtime,
  Graph Workspace, domain patch construction/reducers and original report compiler.
  Planning/review/report inference is deliberately controlled; full intake, provider
  lifecycle and production report feature parity are listed as audit gaps.
- `domain.py`: tool operation identity/status, immutable role-result artifacts,
  SDK settlement facts and a per-role fencing capability. No timed lease, attempt
  history, SQL task queue or public event outbox. Unconfirmed opaque SDK roles
  cannot be rerun just because a workflow can recover. A dedicated idempotent
  ownership probe explicitly opts into safe incomplete-step replay.
- `api.py`: minimal token auth fixtures, native status, input fingerprint conflict,
  transactional approval decision + DBOS send, cancellation and Streams-backed SSE.
  Paths are derived server-side. These routes are a POC, not a replacement for the
  complete existing `/api/runs` or its frontend protocol.
- `runtime_ext.py`: public runner-injection seam and scoped clone admission. The
  SDKAgentRuntime.run method is a source-comparable copy with one Harness factory
  expression changed. It is not a new Agent Loop. The original context/run/role/task
  checks and the canonical input filter remain. Runtime policy snapshots are
  immutable; forged names/instructions/schemas/contexts/handoffs are rejected.
- `probe.py`: standard-context per-model/tool DBOSRunner replay. ModelResponse is
  encoded by an explicit whitelist Serializer. It does **not** prove native replay
  of Graph input-filter I/O, rolling-summary state or every SDK/MCP turn.
- `codec.py`: public DBOS Serializer implementation; known messages, domain models,
  ModelResponse/Usage and error kinds. No pickle or dynamic import from persisted data.

The synthetic external authority is a separate committed transaction in its own
schema and explicitly supports an idempotency key. A committed external effect is
not atomically committed with the DBOS journal. Invoker-attempt witnesses and
unknown-effect guards are checked separately, so deduplication cannot mask an
unwanted replay. This is not a claim of cross-service exactly-once execution.

Domain role-result memoization handles the small window between a fully settled
business result and its engine acknowledgement. It stores no Run lifecycle or
cursor. Old execution permits are invalidated before new model/tool starts. The
fence cannot revoke an external request already issued before ownership changed.

Native execution status and report/archive facts are separate. The archive-failure
case intentionally has a readable report and native ERROR; full legacy
completed/archive_pending behavior and background compensation remain parity work.
Session/unknown-effect capacity and full error/usage projection are also gaps;
see the audit before treating the POC as a production-equivalent replacement.

## Checks and cleanup

```powershell
.venv/Scripts/python.exe -m pytest -q --tb=short --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe -m ruff check --select E,F,I --ignore E501 experiments/dbos_native
.venv/Scripts/python.exe -m ruff format --check experiments/dbos_native
.venv/Scripts/python.exe -m compileall -q experiments/dbos_native
```

Do not cancel arbitrary workflows in a shared database. All validation IDs are
unique. To clean up, first close only the test-owned processes, inspect the exact
container identities/labels, then remove **only these created containers**:

```powershell
foreach ($pocContainer in @('por-dbos-native-postgres','por-dbos-native-neo4j')) {
  $pocLabel = docker inspect --format '{{index .Config.Labels "por-evaluation"}}' $pocContainer
  if ($pocLabel -ne 'dbos-native-architecture-poc') { throw 'POC ownership mismatch' }
  docker rm -f -v $pocContainer
}
```

Do not stop existing MySQL/Neo4j, prune volumes, run git clean, change .env, or
remove the prior evaluation worktree. Runtime databases/logs/transcripts/caches
stay ignored under `.tmp`/`.venv`. Only source, tests, counts methodology and audit
are committed.
