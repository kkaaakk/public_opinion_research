# DBOS production execution — Phase 5

> 当前应用版本为 `dbosrunner-brief-idempotency-v1`。Brief AI 修改的子 Workflow ID
> 绑定 HTTP 请求 Key。见[切换步骤](#brief-请求键版本切换)及 [Brief API 合同](research-brief.md#ai-revision-during-approval)。
>
> `dbosrunner-stage-settlement-v1` 将阶段费用随 `por.stage_result` Step 持久化。
> 见[阶段清算版本切换](#阶段清算版本切换)和[职责审查](../audits/run-execution-reliability.md)。
>
> 2026-11-10 的 `dbosrunner-brief-revision-v1`。Brief AI 修改使用
> 既有 `por.run` 审批循环和 `por.plan` 业务子 Workflow，命令序列已变更。
> 交互合同见 [Markdown Research Brief](research-brief.md)，上线和回滚见[版本切换](#brief-ai-修改版本切换)。

> 2026-11-05 的 `dbosrunner-unified-v1` 引入官方 DBOSRunner、八个阶段 Workflow 和动态模型 Steps。见[统一改造](dbosrunner-unification.md)及[验收记录](../audits/dbosrunner-unification.md)。

> 2026-10-31 当前研究流程：统一 Plan → 可恢复需求澄清 → Markdown Brief 预览/编辑/版本审批 → 四角色与 Review/Gap → 默认单次综合报告。应用版本为 `dbos-brief-spec-v1`；旧 `dbos-phase5-studio-v1` journals 必须按原发行版排空。章节写作仅由显式 `report_mode="sections"` 启用。合同、前后流程及升级规则见 [Markdown Research Brief](research-brief.md)。本页其他 Phase 5/Studio 版本记录保留为历史说明。

All new research uses PostgreSQL + DBOS. Web, `/api/runs`, `/api/research` and
`deep-research` CLI share the durable Run API. No LangGraph Server is started.
The former `python`/`openai_agents` request values are validated input aliases
only; Configuration no longer contains runtime/backend selection fields.
`Runner.run()` and the Context Harness remain the single SDK execution path.

Optional Langfuse/OTel tracing defaults off and is configured in API and Executor
environments. It adds no execution database or journal commands. See
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

## Brief 请求键版本切换

1. 停止旧 API 的新研究准入。保留原 API、前端和匹配的 Executor，
   完成或取消并清算 `dbosrunner-stage-settlement-v1` 及更早版本的任务。
   清点等待审批的 Brief、已提交但尚未接收的审批消息、运行中的修改子 Workflow、
   重试、待归档及未确认外部效果。按原版本核对外部回执，并备份两个 PostgreSQL schema
   及关联业务存储。
2. 用新发行版运行 `python -m open_deep_research.web.executor --migration-check`。
   该命令打印原生 journal 和占位清单，确认输出为 `[]` 后再切换。旧版本未完成的
   `por.run`、`por.plan` 及未清算占位会阻止新版 Executor 启动和新 Run 准入。
3. 用迁移角色运行 `--initialize-only`，增量创建 `por_domain.brief_revision_receipts`，
   为运行角色补齐该表的 DML 权限，再一起部署新版 API、Studio、Classic 与 Executor。
   旧 `approvals`、Brief 快照和子 Workflow ID 原样保留。旧回执没有 HTTP Key，
   无法回填历史 Key，旧审批消息由原发行版消费。
4. 回滚时先停新版准入，由新版完成或取消并清算已接纳的任务，再恢复旧 API 与
   Executor。保留新增回执表及各版本原生 journal，避免失去延迟重发的请求身份。
   [DBOS 版本匹配规则](https://docs.dbos.dev/python/tutorials/upgrading-workflows#versioning)

## 阶段清算版本切换

1. 停止旧 API 的新研究准入。用匹配的旧发行版和 Executor 完成或取消并清算
   `dbosrunner-brief-revision-v1` 及更早版本的工作，包括等待审批、重试、待归档和未决外部效果。
   备份 PostgreSQL 两个 schema 及相关业务存储。
2. 运行新发行版的 `python -m open_deep_research.web.executor --migration-check`。
   费用查询进入 `por.stage_result` 后，查询异常也可能出现在该 Step 的 journal 中。
   新版准入和 Executor 要求旧版本未完成的 journal 与占位先完成清算。
3. 现有六张业务表及其索引、外键、历史投影和回执可直接读取。新写入的
   `units.artifact` 省略 JSON 的 `completed` 键，API 继续按投影是否存在展示阶段完成。
   新数据库沿用 `--initialize-only`。在新版本中验证报告、清算、费用和 SSE 回放。
4. 回滚时先停新版准入，由新版完成或清算其已接受工作，然后恢复旧版服务。
   两版各自恢复匹配的 journal，原有历史记录随库保留。
   [DBOS 版本匹配规则](https://docs.dbos.dev/python/tutorials/upgrading-workflows#versioning)

## Brief AI 修改版本切换

1. 停止旧 API 接收新研究，保留旧 API 的澄清、审批和取消入口。使用原发行版、
   匹配的应用版本和 Executor 身份，完成或明确取消并清算
   `dbosrunner-clarification-v1` 及更早版本的排队、执行、等待、待重试、未决效果
   和待归档任务。保留 PostgreSQL 两个 schema 和外部回执。
2. 部署根锁后运行 `python -m open_deep_research.web.executor --migration-check`
   检查旧 journal 和未决资源。
   未完成旧版本 journal 会阻止新 Executor 启动和新 Run 准入。使用现有
   `--initialize-only` 初始化新环境即可，本次无需数据改写或新增业务表。
3. 用 `dbosrunner-brief-revision-v1` 启动 API 和 Executor，核对澄清、Brief
   版本审批、AI 修改与审批截止时间，以及等待期间的资源释放。回滚时先停新准入，再由本版完成
   或取消并清算本版任务，之后恢复旧版。每个版本只恢复自己的 journal。
   [DBOS 版本匹配规则](https://docs.dbos.dev/python/tutorials/upgrading-workflows#versioning)

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


## Research role result envelope

Research producers return a Python `ResearchRoleResult`; the existing parallel
role workflows and native command sequence remain unchanged. Steps now store a
plain JSON `research_result` instead of a producer State Patch. The research join
also reads the exact prior `dbos-brief-spec-v1` Patch contract, including already
committed role outputs and the domain-Artifact/native-ACK window, without running
models or tools again. Unsupported saved fields fail closed and retain their data.
See the [contract, recovery evidence and cost audit](../audits/research-role-results-assembly.md).

Stop old admission and old Executors before switching this release. Do not mix
an old Executor that only understands Patch results with this release. Forward
reading of the preceding contract does not make backward reading safe: on rollback,
stop new admission and keep this release to finish its accepted Runs first. Do not
rewrite journals, copy opaque units or mint new Runs to migrate external effects.
Earlier application versions still drain on their matching releases.
