# Execution observability and usage

DBOS controls execution; Langfuse observes it through OpenTelemetry (OTel).
Research Graph owns evidence. Traces cannot authorize retries, recovery, cancel
or resource release. See the [implementation audit](../audits/langfuse-otel.md).

| Fact | Owner and correlation |
| --- | --- |
| Native execution / retries / elapsed time / errors | DBOS Workflow/Step journal; root Workflow ID equals public Run ID |
| Role invocation | Native child/unit ID plus SDK Harness `invocation_id`, role and round |
| Model usage | Actual SDK response/provider usage, plus captured single-call business-service attempts |
| Tool effects | `por_domain.operations` stable operation ID, role/unit/native call/name and argument fingerprint; no public raw arguments |
| Evidence | Run/role/task/round and graph call/receipt IDs link Source → Evidence → Claim → Finding |
| Public progress | DBOS Durable Stream seq, generation and Run ID; SSE is a read projection |
| Report archive | Stable History ID, owner, Run ID and generation-derived `run_state_version` |
| Trace hierarchy/timing | Native DBOS + OpenInference SDK spans and selected business spans exported to Langfuse |
| LangChain/RAG request tracing | Official OpenInference model callbacks on the same Provider/exporter; LangSmith remains independently opt-in |

Telemetry defaults off. After successful initialization, Harness RunConfig enables
SDK tracing with `trace_include_sensitive_data=False`. Native DBOS and SDK spans
share one OTel Provider and Langfuse BatchSpanProcessor. The default OpenAI SDK
uploader is replaced; only OpenInference creates SDK spans. No trace store, query
API, execution table, scheduler or new frontend waterfall is added.

`budget.py` remains the budget/usage authority. SDK Runtime captures SDK usage and
nested Graph/tool/report model attempts, and business Steps retain failed and
cancelled charges. Unknown provider token usage is not estimated as billed usage.
Estimates for context admission are explicitly separate from actual usage.
Parallel deltas merge once; request redelivery and SSE replay do not add charges.

History intentionally stores the four numeric public counters: model calls,
input tokens, output tokens and total tokens. Run status retains the full budget
including warnings/cache details/tool counts. Compare those four counters across
History, Run and SSE; their schemas are not identical. Unknown opaque effects
project as Run `interrupted`/unresolved and legacy History `incomplete`.

Research terminal delivery and History completion are separate facts. A stream
can end after `done` while History is pending; reconnect/read Run for eventual
`history_saved`, and do not treat an old generation's receipt as current success.
Database errors close admission/control writes; SSE EOF is never proof of success.

LangSmith remains opt-in (`LANGSMITH_TRACING`, `LANGSMITH_API_KEY`,
`LANGSMITH_PROJECT`). Existing RAG stage spans are retained without wrapping a
model/tool twice. It no longer supplies native Graph root/node execution. Trace
failure does not authorize repeating research or suppress a business exception.
Avoid raw prompts, tool payloads, private memory, provider responses and secrets
in public events or correlation metadata; egress redaction and owner checks stay
active. Historical LangSmith reports can still be imported into owner-scoped
History without executing research.

## Trace structure and business boundaries

```text
run_submit                                API; only for a newly accepted Run
├─ por.run                                native root Workflow
│  ├─ por.plan                            native queue and planning Steps
│  └─ por.research                        native queue
│     ├─ por.role(public_signal) ── business_step ── sdk_invocation
│     ├─ por.role(internal_knowledge) ── business_step ── sdk_invocation
│     ├─ business_step ── research_review
│     ├─ additional Producer rounds / review, when actually scheduled
│     ├─ por.role(risk_assessment) ── business_step ── sdk_invocation
│     ├─ por.role(response_strategy) ── business_step ── sdk_invocation
│     └─ reporting Steps ── report_generation
├─ por.events                             native durable event relay
└─ por.finalize                           native settlement/archive/retry
```

`run_submit` is short; `por.run` measures the executing Run. Reporting stays in
the existing research Workflow. Producer workflows are siblings and can overlap.
Within each invocation OpenInference creates SDK trace/Agent/LLM/Tool spans.
`sdk_invocation` also covers exit settlement and failures after Runner exits.
Other business spans are `context_build`, `context_retrieval`,
`persist_tool_results`, `rolling_compaction`, `research_graph_write`,
`research_review` and `report_generation`. Counts reuse Harness facts and graph
receipts. Context/compaction tokens are estimates, not provider usage.

Planning, intake, extraction, compaction, memory and report models remain bounded
LangChain business services. Existing actual stage usage is copied to the
enclosing business Step/span as `por.model_calls` and
`por.{input,output,total}_tokens`. Each actual Core model lifecycle also creates
one `langchain_generation` through OpenInference LangChain **0.1.80**. The
model-only adapter forwards public model callbacks to OI's official handler;
Core's public configure hook inherits it into nested services and threads. OI's
global all-event hook is removed before enabling the pipeline. Chains,
retrievers and LC tools are not recorded again. No private tracer is patched.
SDK Models remain owned by OI Agents. Stage and child usage overlap: do not sum them.

Structured parsing retries produce separate Generations only when Core actually
starts another model lifecycle. Provider SDK HTTP retries inside a lifecycle are
not individually observable here. OI retains canonical usage, cache and reasoning
details; an error's partial Core usage enriches the same existing span. Missing
usage stays absent. Nothing is submitted to the business budget by this adapter.
Cancellation is recorded when Core delivers its model-error callback. If outer
awaiter cancellation omits that callback, the inline callback's original Task
ends the already-started observation with an unknown effect and a
`model_callback_missing` marker. It does not cancel, wait for or retry a request,
or invent response/usage. The existing budget admission callback also runs inline
before model observers, so a rejected request produces no Generation. Admission
rules, charges, owned-I/O settlement and DBOS recovery remain unchanged. A process
crash can still lose an open/buffered span; no missing Generation is reconstructed.

## Configuration and startup

Use the native Langfuse Tracing view with query
`metadata.attributes.por.span_layer:business` and save it in My Views. Add
`metadata.run_id:RUN_ID` for one research. The business classification retains
native Run/plan/role/Finalizer and the SDK Agent/Generation/Tool path; every
framework span remains exported and accessible by clearing the view. Historical
observations without the label require the full view. No hierarchy is reparented.
See [native views and reliability evidence](../audits/observability-reliability.md).

Install the root lock: `uv sync --extra dev --locked --python 3.11`.

| Operator environment variable | Default / behavior |
| --- | --- |
| `POR_TRACING_ENABLED` | `false`; `true`, `1`, `yes` enable initialization |
| `LANGFUSE_BASE_URL` | `https://cloud.langfuse.com`; HTTP(S) base URL without credentials/query/fragment |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Both required, from the intended Langfuse project |
| `POR_TRACE_SAMPLE_RATE` | `1.0`, finite `[0,1]`; parent-based trace sampling |
| `POR_TRACE_TIMEOUT_SECONDS` | `2`, positive and at most 30; OTLP request timeout |
| `POR_TRACE_SHUTDOWN_SECONDS` | `3`, positive and at most 30; entire shutdown deadline |

Supply these to **API and Executor** through their service environment/secret
manager, never Run Configuration, prompts, fingerprints or Git. For Cloud use
the project's region URL (for example `https://us.cloud.langfuse.com` for US).
For self-hosting use your existing Langfuse base URL. The exporter adds
`/api/public/otel/v1/traces`, Basic auth and ingestion-version `4`; a reverse-proxy
path prefix is preserved. Use TLS outside isolated localhost.

Start the normal [API and Executor](dbos-deployment.md#local-setup). Create and
approve a bounded research in the existing UI/CLI. In Langfuse find the session
whose ID is the returned `run_id`, or filter trace metadata `run_id`. Expand
`por.run`, plan/research, both Producer rows, Review and actual followup rounds,
then Risk/Strategy and reporting. Unsampled Runs still have DBOS/Studio facts.

Initialization occurs once per process before DBOS launch or in FastAPI lifespan,
with no remote auth probe. Missing/invalid settings and initialization failures
disable telemetry with a generic warning. An already-configured global OTel
provider or Agents instrumentor also disables this pipeline: other exporters
could bypass the privacy policy. Configure this single initializer first.
The initializer verifies successful global Provider registration before enabling
SDK tracing; losing registration to another initializer disables this pipeline
and its DBOS tracing configuration. Configuration changes require a restart.

## Fixed versions and privacy

The lock uses DBOS **3.3.0**, Agents SDK **0.24.0**, Langfuse **4.18.0**,
OpenInference Agents **2.6.0**, OpenInference LangChain **0.1.80** and OTel
**1.45.1**. No Langfuse v2 API is used.
`tracing_disabled=True` suppresses SDK events needed by OpenInference; it is
disabled only after successful initialization. Sensitive SDK data stays off.

One compatibility processor, `SDKUsageEnrichment`, copies numeric native
`SpanData.usage` to the existing LLM span before OI ends it. SDK 0.24 exposes
content-free Responses usage, but OI 2.6 reads the withheld full Response;
its Chat Completions mapping also omits native total/cache/reasoning counts.
The model label comes from the already-assembled official SDK Model.
This processor creates no spans, model calls, execution IDs or usage ledger.
Missing/all-zero defaults do not prove provider tokens. It must precede OI in
the SDK processor list; official Models over mock HTTP test both APIs.

OpenInference TraceConfig hides inputs/outputs, messages, prompts, invocation
parameters, tool schemas and retrieved content. A final egress allowlist also
sanitizes **all** spans, including DBOS/native SDK errors. It retains bounded
identities/labels, counts, actual usage and minimal operation metadata. It removes
raw prompts/instructions, tool parameters/results, responses, private Memory,
arbitrary metadata, auth/owner fields, resource/scope attributes, exception
descriptions, stack traces and event payloads. Only DBOS native retry numbers
survive as `dbos.step.retry` / `dbos.step.attempt`; the raw error is removed.
Span, parent and Link contexts retain correlation IDs, remote flag and trace
flags; arbitrary vendor `tracestate` and Link attributes are removed at egress.
In-process/native DBOS propagation is unchanged. Agent display names rejected by
the content allowlist use the existing safe `por.role` label, not raw name text.
This is independent of SSE sanitization.
There is no content-capture switch.

## Identity, retries and recovery

Attributes reuse `por.run_id`, `por.workflow_id`, `por.unit_id`, `por.execution_id`,
`por.invocation_id`, `por.role`, `por.task_id`, `por.research_round`,
`por.generation`, `por.model_turn`, `por.tool_call_id`, `por.graph_call_id` and
`por.operation_id`. Only available facts are attached. `por.dbos_step_id` is native
`DBOS.step_id`; `execution_id` is Studio's existing unit/permit digest. Token keys
follow OpenInference. `por.status`, `por.failure_kind`, `por.error_type` and
`por.effect_status` describe known observations without exception strings.

`run_id` is **not** an OTel `trace_id`. Langfuse session ID and trace metadata
`run_id` group segments even if tracing was toggled or an old workflow lacked a
parent. A telemetry-only generation lookup reads RunClient's existing accepted
retry-generation fact; it grants no permission and is omitted if its read fails.
The native workflow/unit/permit and Finalizer still determine actual execution.

API enqueue uses DBOSClient `otel_context`; workflows use `PropagateOtelContext`
for children. DBOS persists W3C traceparent/tracestate in its existing
`dbos.otelContext` workflow attribute and restores it on dequeue/recovery. No HTTP
auth or baggage is propagated. Retries/recovery emit fresh span identities under
the persisted caller. A killed process cannot finish/export its in-flight spans.
Unknown effects remain quarantined; completed tools are never replayed for traces.

There are no new DBOS commands, function IDs, workflow inputs, schema migrations
or retry branches. Application stays `dbos-phase5-studio-v1`, runtime stays
`sdk-phase5-v1`. Matching-version journals without the optional carrier recover
with business-ID correlation; continuity is not fabricated. Older Phase 4 and
pre-Studio journals still require their matching drain Executors.

## Failure, shutdown, rollback and limits

Langfuse BatchSpanProcessor exports off the model/tool thread with a bounded
queue. Slow/unavailable export can drop spans, never replay work or add usage.
OTLP requests honor the timeout. Shutdown drains DBOS, then waits at most
`POR_TRACE_SHUTDOWN_SECONDS` for SDK/provider shutdown on a daemon thread; tail
batches may be lost. Native polling Steps can generate substantial telemetry
for long Runs; sample according to SLOs. Trace absence never proves missing work.

Rollback with `POR_TRACING_ENABLED=false` and restart API/Executor on the same
compatible release. No trace database cleanup or Run migration is needed.
Preserve DBOS journals, operation receipts and Graph data.

Keep `LANGSMITH_TRACING=false` for default metadata-only Langfuse operation.
Explicitly enabling both does not double the budget, but LangSmith retains its
own content-upload policy; this filter does not sanitize another provider.
No Langfuse callback is added to LangChain tools, avoiding duplicate observations
and a retired LangGraph dependency.

Studio receives no project credential or guessed deep link. Operators query
`run_id` in their authorized Langfuse project. Multi-tenant trace-link permissions,
trace query APIs, production SaaS
validation, exactly-once export, guaranteed SIGKILL tail delivery and production
throughput benchmarks remain unsupported.

```powershell
.venv/Scripts/python.exe -m pytest tests/test_telemetry.py --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
# Isolated local services from dbos-deployment.md, never business databases:
.venv/Scripts/python.exe -m pytest -m integration tests/test_telemetry_dbos.py --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
```

Native tests use actual DBOS/PostgreSQL/Neo4j, ScriptedModel, parallel roles,
review/gap, cancel, safe/native Step retries, process kill/restart and a loopback
OTLP protobuf receiver. The receiver verifies transport and hierarchy, **not**
the Langfuse UI. For an existing Cloud/self-hosted installation, supply real
operator keys outside Git, run one bounded approved task and inspect the session
above. Check export failures and sensitive-data absence before wider rollout.
Actual results and unverified conditions are recorded in the
[audit](../audits/langfuse-otel.md).

## Local validation and closeout scope (2026-10-30)

The original A+B production combination passed two new independent balanced local
experiments: seeds 20261030/20261031, three blocks each, two full warmups and four
measured Runs per cell. Both unchanged pooled capacity gates passed: concurrent
ON p95 17.868708s <= 22.626264s and 17.861206s <= 18.564218s. All six descriptive
block ratios, including 2.008198 in B1, and earlier capacity failures remain in the
[final hardening audit](../audits/observability-final-hardening.md#10-2026-10-30-收尾复测及顺序集成).
No sustained severe degradation was observed in these local mock-model cohorts;
shared Windows/Docker memory, storage/cache and small samples still limit this
judgment. The earlier sanitization and pool-pre-ping candidates remain unpromoted.

Actual local Langfuse UI/API verification covered Tree, Timeline, SDK and
LangChain Generations, Tool, Run correlation and full/business views (612/82
observations), with 21 Generations and 234 tokens. Outage and recovery in one
process preserved research completion and did not replay model/tool work.
All parent links and the sensitive-data egress check passed. These are validated
local features and local capacity results. Trace export remains best-effort:
failed OTLP batches and SIGKILL/crash/power-loss memory tails can be lost.
DBOS journals and operation receipts remain the execution authority.

Long-running production load, HA, disaster recovery and real cloud-model quality,
cost and latency remain unverified. The existing markitdown-mcp/mcp pip-check
conflict is retained in the audit. Sequential Merge Commits and final main test
results are recorded in [PR #37](https://github.com/kkaaakk/public_opinion_research/pull/37);
local success does not establish a production SLA or zero-loss guarantee.
