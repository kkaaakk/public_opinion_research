# Native views and bounded telemetry delivery (PR B)

2026-10-28, Asia/Shanghai. Independent base: main
`9b479fa4fd4ee412305d81ddba3bf2ecb62b364c`; branch
`codex/observability-reliability`. No model/workflow migration, production deploy,
new trace UI, trace store, persistent queue or retry authority is added.

## Views instead of destructive Span filtering

Official Langfuse **4.70.1 OSS**, Web and Worker, was started in the dedicated
`por-obs-langfuse` Docker project at loopback :33080. PostgreSQL, ClickHouse,
Redis and MinIO have dedicated volumes/network. Random test initialization/API
credentials stay in ignored local fixtures; no operator credential or `.env` was
read. The fixed Python SDK remains **4.18.0**; rolling website examples are not
assumed to describe that SDK. Official reference:
[native views](https://langfuse.com/faq/all/explore-observations-in-v4).

A real complete DBOS/Runner/Harness/Neo4j research with a second Producer round
and Review/Gap reached completed/settled/saved. Its current candidate trace
`225badb17dcdb32257dbc1835c621cb2`, Run/session
`200c0b9f179446dcafe3bd6477c164bc`, contains **894** observations with **zero missing
parents**. This trace includes PR A's extra LC requests and is evidence for the
native view recipe; it does not imply this independent B branch contains A.

| Same complete trace | Count |
| --- | ---: |
| All retained observations | 894 |
| Business observation subset | 119 |
| Framework/helper observations | 775 (86.69%) |
| Dominant helpers | set_event 187; write_stream 178; public_event 178; recv 177 |
| Generations / tools / Agent boundaries | 30 / 4 / 12 |

The business subset means Agent/Generation/Tool types plus `run_submit`, `por.run`,
`por.plan`, `por.research`, `por.role`, `por.finalize`, `business_step`,
`sdk_invocation`, `context_build`, `context_retrieval`, `persist_tool_results`,
`rolling_compaction`, `research_graph_write`, `research_review`, `report_generation`.
It is a positive view definition, not an exporter name blacklist. Different
semantic boundaries may still overlap, e.g. SDK trace group and Agent.

For daily inspection, open **Tracing**, filter **Session ID** to the Run ID,
then use **Name** to select business names or **Type** for Generation/Agent/Tool.
Save the selection in **My Views**. Keep a separate full-trace diagnostics view;
`por.finalize` and native business Steps are essential for authorization/retry/
recovery investigation. Open any observation's original Trace to inspect its
whole hierarchy. In Tree use **Collapse all** and expand the `por.run` branch;
on a narrow trace panel **More options** contains Collapse all and view options.
Search for `por.` to locate native business workflows or for a model/span name;
use Timeline to inspect Producer overlap. Search/filter visibility is not evidence
that an operation did not execute. The original DBOS journal and SSE are unchanged.

The exporter adds a fixed `por.span_layer=business|framework` classification using
the positive business names and native OI Agent/LLM/Tool kinds. It adds no span,
usage or execution fact and cannot be set by arbitrary incoming metadata. Dynamic
tool/model names therefore remain visible without enumerating all tool names.
Historical observations without this tag remain accessible in the full view.

The actual native query is `metadata.attributes.por.span_layer:business`; add
`metadata.run_id:RUN_ID` to inspect one Run. The local project's saved view is
**POR Business path**. A second complete normal research in the A+B composition,
Run `e4687d697ae94fbd918aa8ac5c19b682`, Trace
`08664f56f2043cf8e195cfa15783e9e5`, was compared in the actual UI: **612 → 82
visible observations**, excluding **530 / 86.60%** framework rows. Complete-field
API inspection still returned all **612**, 21 Generations / 234 tokens and zero
missing parents. Both screenshots and the saved-view UI action are preserved.

No Span is deleted, renamed for noise filtering, sampled by a name blacklist, or
reparented. The server still holds all 894 observations after changing the view;
the 119-row business subset simply excludes 775 helpers from the chosen view.
This preserves full parent/child integrity and retry evidence. Actual screenshots,
native menu/control evidence and view results are kept in the local visualization
directory; final acceptance and any remaining UI limitations are in the final
hardening report.

## Delivery and exit ownership

Existing LangfuseSpanProcessor inherits OTel's bounded BatchSpanProcessor. Its
defaults in fixed OTel 1.45.1 are queue 2048 and export batch 512. Model/tool paths
never upload synchronously. OTLP has a 2s request deadline; failed batches leave
the SDK queue, and only subsequent data is exported after endpoint recovery.
There is no retained-failure replay, infinite retry, custom spool or second channel.

`SafeExporter` now counts attempted/acknowledged/failed spans and batches, including
a returned FAILURE (not only raised errors). `delivery_health()` exposes only
bounded process-local numeric facts. ACK means the HTTP exporter accepted a batch,
not a guarantee of Langfuse persistence. It cannot infer open spans or process-kill
losses. Failure logs remain generic; exceptions, response bodies and credentials
are not copied into telemetry diagnostics. Provider/exporter close/flush errors
are isolated. SafeExporter shutdown is idempotent even under concurrent calls.

One application atexit hook now covers normal interpreter exit and unhandled
exceptions in addition to the existing FastAPI/Executor finally paths. Explicit
shutdown unregisters it and flips the existing active guard once. The same daemon
close worker invokes client shutdown/flush and provider shutdown; its entire wait
is bounded by `POR_TRACE_SHUTDOWN_SECONDS` (default 3s). A stuck exporter may
continue in that daemon until process exit, but cannot extend application waiting
indefinitely. There is no synchronous remote upload on Finalizer or model paths.

OTel 1.45.1's **force_flush ignores its timeout argument** internally; this was
confirmed in fixed installed source. Passing `timeout_millis` alone is not a
deadline. Application shutdown therefore retains the outer bounded worker. Do
not call provider/client flush on request or Finalizer hot paths.

## Native queue metrics and fault evidence

Use official `OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED=true` with a configured
standard MeterProvider/MetricReader to observe `otel.sdk.processor.span.queue.size`,
`queue.capacity` and `otel.sdk.processor.span.processed` with
`error.type=queue_full` / `already_shutdown`. The benchmark uses OTel's own
InMemoryMetricReader. No private queue is read and no metrics upload channel is
installed by this PR. An operator's monitoring reader/export remains a deployment
integration, not a claimed new production metrics endpoint.

The deterministic overload contract holds an exporter batch of 4, then enqueues
20 more spans into capacity 8: **12 exported + 12 dropped = 24 ended**. Official
metrics report queue size 8 and exactly 12 queue-full drops. This proves bounded
backpressure and observable loss, not zero loss. Failure/recovery tests preserve
the failed count, export new spans once, and never replay the old batch.

Added local matrix: **11 passed / 2 skipped** on Windows, including real subprocess
normal exit/unhandled exception, one shutdown drain, stuck/throwing close, returned
and raised exporter failure/recovery, queue overflow, and abrupt process-tree kill.
The kill probe uses the existing native fault helper so Windows' venv redirector
cannot leave an exporter alive. Earlier probe code killed only the redirector;
it was corrected and its task-owned orphan probe was terminated. Its earlier
result is not proof of abrupt interpreter death.

SIGINT/SIGTERM tests require POSIX and are explicitly skipped on Windows; their
actual Linux CI results must be reported separately. Existing native DBOS tests
kill/restart workers at completed-checkpoint and unknown-receipt boundaries,
retain durable facts and assert no repeated external effect. Full native, UI,
Studio and precise-head CI outcomes are recorded in the final hardening report;
pending gates are not predeclared successful.

Final full offline: **925 passed / 2 skipped / 107 deselected**, 222.28s. The local
native matrix reached **100 passed / 1 failed**: Studio's read-only test waited for
History saved but read its event snapshot before the independent terminal relay
manifest completed (3 invocation IDs instead of the required 4). The test now
waits for **both** `archive_status=saved` and `events_complete`; no production
behavior or assertion changed. That case and the native trace hierarchy contract
then passed together (**2 passed**). The final remote CI matrix must still be
checked on the publication tree. Studio TS/lint/42 unit/build, 6 real Chrome E2E,
7 Classic protocol checks, Ruff/format/compile/32 imports and lock checks passed.
The pre-existing markitdown-mcp/mcp pip-check incompatibility remains explicit.

## Persistence decision and honest boundary

An ended and acknowledged Span, an ended Span still buffered in this process,
and a DBOS durable execution fact are different things. SIGKILL/TerminateProcess,
`os._exit`, power loss or a process crash can lose buffered/open spans and skip
atexit. No missing LLM observation is reconstructed; no model/tool is replayed to
repair a trace. Use the Run/session ID to compare Langfuse segments with native
DBOS workflow/Step history, domain receipts, settlement and SSE delivery.

The official Collector's sending queue with `file_storage` can retain batches
**after the Collector accepts them** across Collector restart/backend downtime.
It does not preserve spans never sent by the application, ensure disk survival,
or eliminate retry/queue limits. See [OTel resiliency](https://opentelemetry.io/docs/collector/resiliency/).
Adding a mandatory Collector would add another service, volume, disk capacity,
credentials, monitoring and backup duty while leaving the application's pre-send
kill window. This change retains the bounded existing pipeline and documents an
optional future Collector deployment for operators with that durability need;
it does not claim to have deployed or validated a persistent Collector.

The only authoritative recovery path remains DBOS. Telemetry never changes
Finalizer, retry grants, cancellation, reservation release or business usage.
