# DBOS Phase 4 execution

New durable Runs use PostgreSQL + DBOS. The existing `python/openai_agents`
request selector remains an API compatibility alias; it now selects DBOS.
`Runner.run()` and the Context Harness remain the single SDK execution path.

## Local setup

Use a new database and do not point these commands at existing MySQL/Neo4j data.
The Compose PostgreSQL port is loopback-only; its default password is for local
isolated development. Set `DBOS_POSTGRES_PASSWORD` for other deployments.

```powershell
docker compose -p por-dbos-phase4 -f compose.dbos.yaml up -d postgres --wait
uv sync --extra dev --locked --python 3.11
$env:DBOS_DATABASE_URL = 'postgresql+psycopg://por_execution:local-dbos-development-only@127.0.0.1:15434/por_execution'
.venv/Scripts/python.exe -m open_deep_research.web.executor --initialize-only
# Independent terminal, same trusted business settings and resource policy:
.venv/Scripts/python.exe -m open_deep_research.web.executor --executor-id workstation-1
# Web terminal: existing PUBLIC_OPINION_API_TOKEN and business provider/Graph settings.
.venv/Scripts/python.exe -m open_deep_research.web.server
.venv/Scripts/python.exe -m open_deep_research.web.executor --health
```

No commands edit `.env`. Run `--initialize-only` once with a migration-capable
role before starting processes. It uses public `DBOS.migrate(schema='por_dbos')`
and creates `por_domain` application tables. The runtime role needs USAGE on both
schemas, DML on their tables and USAGE/SELECT on sequences. Database ownership,
CREATE/ALTER and schema migration privileges can stay with a separate migration
role; configure the runtime URL through the service environment/secret store.
The dev Compose user is the database owner and is not a least-privilege template.

DBOS owns `por_dbos`; business identity, configuration, permissions, operations
and settled artifacts are in `por_domain`. Neo4j evidence/History and MySQL
long-term memory remain in their existing stores. DBOS journal is not Memory.
Back up both PostgreSQL schemas consistently, plus the existing business stores.
Retain journal/streams for the public cursor lifetime; no automatic truncation
or silent expired-cursor remapping is performed in this release.

## Process ownership and deployment

The FastAPI process submits/reads using DBOSClient and never claims work.
The Executor initializes/registers native workflows and queues; it does not
contain a task pool, application claim, lease or heartbeat scheduler.
`RUN_MAX_ACTIVE` (default4), `RUN_OWNER_ACTIVE` (2), `RUN_QUEUE_CAPACITY` (1000),
`RUN_APPROVAL_SECONDS` (86400), `RUN_TIMEOUT_SECONDS` (3600 per executing planning/research segment), and optional `RUN_SDK_SESSION_PATH` are centralized
in `workflow/dbos_config.py`. All processes must use the same persisted policy.
Create the parent directory of RUN_SDK_SESSION_PATH explicitly and grant the
Executor file permissions before enabling that optional SQLite transcript store.
Session means the existing trusted research session ID; SDK sessions are private
per durable role invocation, useful for inspection, not workflow checkpoints.

Assign a unique `--executor-id` to every simultaneously live Executor. Restart a
dead process with its original identity and matching application version to
recover its pending workflows. Never share one ID between live processes.
Automatic detection/recovery of a dead executor on another host requires DBOS
Conductor or trusted external orchestration; this release does not claim that
unconfigured multihost automatic failover exists. Route an explicitly authorized
manual recovery to `por-recovery` and revoke/check domain permissions before
allowing new external work. Do not resume an unconfirmed Agent/tool unit.

For planned shutdown stop new admission, cancel/drain executing Runs through the
Run API and wait for their settlement, then use SIGTERM/Ctrl-C to stop native DBOS.
Stopping the process is not itself a business cancel acknowledgement. Give SDK/tool cleanup at least its
existing bounded drain window before forcibly killing a process. A database
outage fails admission/control writes closed. API storage failures return503;
EOF from SSE means reconnect/read status, not success. `--health` checks the
PostgreSQL native catalog. Process liveness is separately supervised by the
operator; there is no claimed application heartbeat endpoint for Executors.

## Old Runs and cutover

Stop admission on the old release first. Keep the original Phase4 release in an
isolated drain deployment until its paused, quarantined and archive-pending
Runs have been settled/exported. This new release never reads/claims the old SQL
queue. Do not copy an old Run into a new DBOS workflow or replay its tools.
Legacy archives keep their stable IDs and owner policy. Rollback stops new DBOS
admission and retains the matching Executor version for its existing Runs;
never send a DBOS Run to the old Worker. No Phase5 retirement is included.

Unknown business outcomes retain global/owner/session reservations. Releasing a
reservation requires confirming the old process is stopped, checking external
records and recording evidence; it never makes an opaque unit replayable:

```powershell
.venv/Scripts/python.exe -m open_deep_research.web.executor --reconcile-run RUN_ID --state-version VERSION --worker-stopped --note 'verified external receipt ...'
```

## Verification

Offline regression and lint use the root lock. Native tests require PostgreSQL
and isolated Neo4j and only use controlled inference:

```powershell
docker compose -p por-dbos-phase4 -f compose.dbos.yaml up -d postgres neo4j-test --wait
.venv/Scripts/python.exe -m pytest
.venv/Scripts/python.exe -m pytest -m integration tests/test_dbos_contracts.py tests/test_dbos_process.py tests/test_dbos_retry.py
```

`TEST_DBOS_DATABASE_URL`, `TEST_DBOS_NEO4J_URI`, `TEST_DBOS_NEO4J_PASSWORD` select
isolated test services. Contract tests create uniquely named `por_test_<uuid>`
databases and drop only those exact databases. They must not target production.
Process tests exercise real kill/restart and journal recovery. CI runs the same
native contract suite with service containers. See the audit for actual results,
remaining limitations and the old-to-new acceptance mapping.

Control/event/finalization workflows can wait durably and therefore do not use
finite worker concurrency as a business quota. Actual research execution is
bounded by the business queue and domain reservations; queue capacity bounds
accepted backlog. Large-backlog metadata throughput/storage need deployment
sizing. API state_version is the delivered event cursor; the Neo4j archive
run_state_version is retry generation + 1, preventing late older projections.


## Retry authorization and execution versions

The final-review fix uses application version `dbos-phase4-v2`. Run initialization
adds the nullable `por_domain.retries.outcome` authorization payload and a unique
index on `(run_id, generation)` through the existing `--initialize-only` command.
This is additive domain control data, not another execution queue or checkpoint.
Do not run v2 code against a pending v1 Finalizer journal: drain/reconcile v1 with
its matching release and Executor identity first, as for other version changes.

The HTTP retry endpoint only atomically records a grant and its native message.
Repeating a key/version acknowledges that original request and never rewinds.
A new grant requires a settled safe failure and delivery of that generation's
terminal event, binding the observed cursor to a new failure. Each grant records
the source generation, application version, and exact failed native target/journal
versions. Only the durable Finalizer applies it, with native rewind APIs.

The Finalizer ignores stale, duplicate and unauthorized messages. A lost Step ACK
is recovered by checking the original native execution versions; an already
advanced target is never rewound again merely because it has failed again.
`retry_applied` is a native command receipt containing `generation` and an outcome
(`applied`, `superseded`, `cancelled`, or `blocked`); it does not declare research
success. Run status remains native, and unsettled external effects remain held.
Old History/SSE receipts never establish archive success for a new generation.
Archive compensation continues after an invalid/old retry message and never
restarts research. No HTTP-side recovery path is retained.

Transient recovery storage failures are journaled as retryable Step result data,
so replay does not depend on retaining a Python SQLAlchemy exception type. No
application receipt acknowledges the grant until its native targets are checked.
