# DBOS production execution — Phase 5

> 2026-10-31 当前研究流程：统一 Plan → 可恢复需求澄清 → Markdown Brief 预览/编辑/版本审批 → 四角色与 Review/Gap → 默认单次综合报告。应用版本为 `dbos-brief-spec-v1`；旧 `dbos-phase5-studio-v1` journals 必须按原发行版排空。章节写作仅由显式 `report_mode="sections"` 启用。合同、前后流程及升级规则见 [Markdown Research Brief](research-brief.md)。本页其他 Phase 5/Studio 版本记录保留为历史说明。

All new research uses PostgreSQL + DBOS. Web, `/api/runs`, `/api/research` and
`deep-research` CLI share the durable Run API. No LangGraph Server is started.
The former `python`/`openai_agents` request values are validated input aliases
only; Configuration no longer contains runtime/backend selection fields.
`Runner.run()` and the Context Harness remain the single SDK execution path.

Optional Langfuse/OTel tracing defaults off and is configured in API and Executor
environments. It adds no execution database or journal commands; the existing
matching-release drain rules remain; current version is `dbos-brief-spec-v1`. See
[setup, privacy, recovery and rollback](observability.md).

Final acceptance of PR #32 uses an official isolated Langfuse 4.70.1 server and
the guarded `tests/langfuse_acceptance_fixture.py` runner with synthetic inference.
It does not connect test fixtures to an operator's services. See the
[final review and UI evidence](../audits/langfuse-otel-final-review.md) for ports,
identity/usage checks, recovery regressions and the precise-head merge gate.

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
never send a DBOS Run to the old Worker. The old Worker and Graph Server are absent from Phase 5.

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
isolated test services. `TEST_DBOS_POSTGRES_CONTAINER` and
`TEST_DBOS_NEO4J_CONTAINER` identify the labeled test containers for physical
outage contracts; only those exact containers are stopped/restarted.
Contract tests create uniquely named `por_test_<uuid>`
databases and drop only those exact databases. They must not target production.
Process tests exercise real kill/restart and journal recovery, plus actual isolated PostgreSQL and Neo4j container outages. CI runs the same
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

## Phase 5 cutover and compatibility

The original Phase 5 application version was `dbos-phase5-v1`; SDK runtime
version remains `sdk-phase5-v1`. Studio uses application version
`dbos-phase5-studio-v1` because its event relay gates terminal receipts on the
delivery manifest. DBOS native messages with tied millisecond timestamps can
arrive out of send order; archive confirmation must not close a Stream before
that generation's usage/report/error/status facts are delivered. This only
changes event delivery, not research scheduling or retry authorization.
Drain pending `dbos-phase5-v1` journals with the original release and Executor
before switching; never replay an old relay journal on the new release. The
same matching-release rule applies to a rollback. Existing history remains
readable; do not copy or resubmit old opaque work as a new Studio Run.
The role runtime no longer inherits or contains a handwritten Agent loop.
The removed files include `runtime/agent_runtime.py`, `workflow/graph.py`,
`langgraph.json` and the Supabase Auth hooks that only served Graph Server.
The FastAPI operator Token, owner checks and immutable approval rules remain.

Before switching an installation:

1. Stop admission on the old API. Inventory queued, executing, paused, unknown and
   archive-pending Runs with its original APIs/native DBOS catalog. Keep the
   matching old API and Executor identity/version available to finish approvals,
   cancel, settle and compensate archives. Do this for old SQL and Phase 4 DBOS
   deployments separately; none of these are new Phase 5 tasks.
2. Resolve unknown external operations using authoritative receipts. Do not copy
   a SQL row, approval, old retry grant or opaque SDK unit into a new workflow.
   Preserve all old databases/journals and historical archives.
3. Deploy the root lock, run `--initialize-only`, configure PostgreSQL and the
   existing trusted business services, then start uniquely identified Phase 5
   Executors and FastAPI. No LangGraph/Studio service, Redis or old SQL Worker is
   needed. Pending old-version DBOS journals require their matching old release;
   the new release does not silently adopt them.
4. Verify `/api/health`, Executor `--health`, a bounded admitted request, its
   `X-Run-ID`/Run status, SSE cursor and eventual History. A report may be delivered
   before independent History compensation completes; only `archive_status=saved`
   acknowledges successful archive of the current generation.

`PUBLIC_OPINION_API_TOKEN` (or existing `WEB_API_TOKEN`) is mandatory for admission
and all Run controls. It identifies one trusted operator, not multiple tenants.
History-only local reads keep the original optional-token policy; when a Token is
configured, owner filtering remains enforced. Legacy unowned records are not
silently assigned to the operator.

Remove old runtime-selector settings from deployment configuration yourself;
this migration does not edit `.env`. Supported input aliases are
`workflow_backend=python/dbos` and `business_agent_runtime=openai_agents`; they
are discarded before creating new requests. `langgraph`, `legacy` and unknown
values fail closed, including old environment values. Old request fingerprints
may conflict with normalized Phase 5 payloads: reconcile through the old release,
never mint a replacement key to disguise an existing request.

The SDK model adapter still supports OpenAI/DeepSeek and local Tavily tools.
Other Web model/search selections explicitly fail validation; they cannot enter
an old runtime. LangChain provider adapters remain available for single-call
RAG/report/intake services. MCP OAuth token exchange remains supported through
trusted configuration, with no global cache or retired Graph Server token store.

CLI examples (the API and Executor must already be running):

```powershell
.venv/Scripts/python.exe -m open_deep_research.web.cli create 'brand recall' --key research-001 --approval
.venv/Scripts/python.exe -m open_deep_research.web.cli get RUN_ID
.venv/Scripts/python.exe -m open_deep_research.web.cli approve RUN_ID --plan-version SHA256 --decision approve
.venv/Scripts/python.exe -m open_deep_research.web.cli events RUN_ID --after 20
.venv/Scripts/python.exe -m open_deep_research.web.cli cancel RUN_ID
.venv/Scripts/python.exe -m open_deep_research.web.cli retry RUN_ID --key retry-002 --state-version 42
```

A lost create response is retried with exactly the same body/key. An old retry key
only acknowledges its original authorization; another safe failure needs a new
observed state version/key. An SSE disconnect does not cancel execution. The CLI
prints no credentials, and its events command subscribes without claiming work.
Rollback stops new admission and preserves Executors for already accepted Phase 5
Runs; it does not redeliver those Runs into the previous engine or version.
