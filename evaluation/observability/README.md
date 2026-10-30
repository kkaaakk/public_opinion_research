# Local observability benchmark

This is a test driver, not a production entry point. It runs the existing native
Run API, DBOS workflows, SDK Runner, Harness, Neo4j Graph and History with the
repository's controlled business fixtures. No paid provider is called; dotenv,
LangSmith and non-loopback Python sockets are disabled. Use the root lock/venv.

Start dedicated `compose.dbos.yaml` services and an official local Langfuse project.
Pass **test-only** keys through the environment, never CLI arguments or Git:
`TEST_DBOS_DATABASE_URL`, `TEST_DBOS_NEO4J_URI`, `TEST_DBOS_NEO4J_PASSWORD`,
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`. Service URLs must be loopback and
must not point at business databases. The driver creates/drops only its own
random test databases. PostgreSQL URLs require explicit test user/password/database
and no query parameters; host/service/passfile overrides are rejected. Operator
business environment and SDK session paths are not inherited. Local PG disables
SSL/GSS and requires password authentication; Requests sessions ignore netrc/proxies.
`--source-root` applies to single-case execution as well as the full matrix.
Only random `por_test_UUID` databases are created/dropped. Evidence/History remain in the isolated Neo4j
volume until that test project's normal cleanup.

```powershell
.venv/Scripts/python.exe evaluation/observability/run_benchmark.py --enable-local-services --healthy-url http://127.0.0.1:33080 --output .tmp/observability/benchmark.json --repetitions 3 --runs 4 --warmup 1
```

Each repetition/configuration starts a fresh interpreter and warms up a full Run.
The seeded order randomizes balanced blocks to reduce service-cache/time drift.
The five configurations are serial tracing off/on, four concurrent tracing off/on,
and tracing with a reserved non-listening loopback port. Four matches the project's
default active capacity. All use the same deterministic responses, workflows and
service resources. Every measured Run must finish/settle/archive with 28 model
calls / 311 tokens, including the fixture's initial 7 calls / 77 tokens. Real
requests are 6 SDK + 15 Core and tools execute twice. No failure grants are issued.

Agent/LLM/tool clocks are present in every configuration, including tracing off.
Their small timing cost is part of the harness, not a provider performance claim.
The SDK uses official OpenAIResponsesModel over httpx2.MockTransport, with a fixed
2ms reply delay and reported 10 input / 4 output tokens per request. Core uses its
normal fake-model lifecycle, reporting 7 input / 3 output tokens. These are not
provider latency or billing measurements. A metadata-only validation processor and exporter result probe measure
Span counts, critical-path ACK completeness and failures. They add measurement
overhead in traced cases; no second export channel exists. Sampling is 1, OTLP
timeout 2s, shutdown deadline 3s and the SDK's bounded queue defaults are retained.

RSS is sampled every 25ms with the operating system; CPU is process CPU seconds
and one-core utilization. This includes DBOS/Core/SDK/fixtures and tracing; record
machine, VM/container limits and other workloads separately. RSS/CPU for Langfuse
and database services are separate from the Python process. OTel's official SDK
internal metrics measure queue occupancy, capacity and `queue_full` drops; the
driver never reads private queue fields or implements a queue. Run elapsed time
ends after terminal delivery and archive; telemetry shutdown/drain is separate.

The numeric `LIMITS` in the script are frozen **before running**. The local gate
allows 25% p95 overhead plus 100ms, at most 256MiB additional peak RSS, zero healthy
queue drops, all critical stages acknowledged, and shutdown ≤3.5s. Concurrent p95
is compared with the healthy sequential baseline using a 2× local capacity gate.
Report failed gates; do not adjust limits after seeing favorable measurements.
No production optimization is justified solely by this synthetic test.

The integration revision waits using public `DBOSClient.get_event` for the
relay's archive receipt, then reads and validates the complete public Run
projection once. Both submission and that final projection remain inside Run
latency/CPU timing. The relay publishes this receipt only after saved History
and the terminal manifest. There are still exactly 21 model requests and two
tools per measured Run. The old fixture polled the full projection every 50ms;
it adds a substantial SQL workload. Keep its historical measurements separately.
Compare OFF/original ON/optimized ON with the same observer; a measurement-method
correction is not a production optimization. Regular native tests retain the
original polling helper, so this does not weaken their correctness contracts.

For a same-environment comparison, freeze the original production `src` tree in
a separate checkout or snapshot, then add `--baseline-source-root ORIGINAL` and
`--source-root CANDIDATE`. Each repetition includes all five cases for both
versions, shuffled using `--seed` (default 20261029). The output retains the exact
schedule, normalized production Python SHA-256, raw per-case samples, and
separate `summaries.original` / `summaries.candidate`. `summary` remains the
candidate alias. Do not compare a mixture of different source fingerprints.
Batch timing includes sanitization and OTLP; OTLP-only timing is also retained.
Flush is observed inside bounded shutdown, not added to the request hot path.
Completed Runs/second is reported separately from acknowledged spans/second.
The same 256MiB incremental RSS limit also applies to concurrent ON versus OFF.

Aggregate raw Run/agent/LLM/tool samples across repetitions, not the mean of their
percentiles. Retain each repetition's range and counters. Twelve Runs/config are
a small local regression sample, especially for p99; they do not establish a
production tail-latency SLO. OTLP ACK completeness is transport evidence and must
be distinguished from server persistence and UI validation. Healthy/concurrent
cases additionally fetch complete v2 observation fields after the timing window,
wait for all acknowledged span IDs, traverse cursors, and check Generations,
tokens, parent closure and content sentinels against the actual Langfuse server.
Validation is per Run: trace sets cannot overlap, every observation/parent must
belong to its Run/Trace, and Agent/Tool/critical-stage counts must match. Prompt,
Memory, SDK response, tool arguments and real tool-body sentinel families are checked.
An unavailable
exporter discards failed batches after its bounded attempt; recovery does not
replay them. In-memory queued/open spans can be lost on a process kill.

`--source-root` can test a clearly identified local composition of independent
candidate PRs. Use an interpreter containing that source's locked dependencies,
record its commits/tree, and keep all PRs based independently on main. This does
not merge their GitHub PRs or make the benchmark branch depend on unmerged code.
The report must distinguish measurements of main from that composed candidate.

`--case concurrent_off --runs 4` remains available for a single diagnostic, but
the full matrix now measures it alongside tracing ON in every randomized block.
Do not replace the historical four-case results or change their frozen limits.

## SQL and event diagnostics

`run_sql_diagnostics.py` is a separate, explicit diagnostic entry point. Use the
same isolated environment variables above. It requires PostgreSQL 17 with
`pg_stat_statements` preloaded and installed in the explicit test observer database.
Planning, I/O and WAL I/O timers may be enabled on that dedicated test container
for diagnosis; record and restore them before formal timing. Keep fsync,
synchronous_commit and full_page_writes enabled. Never run this against an
operator or production database.

```powershell
.venv/Scripts/python.exe evaluation/observability/run_sql_diagnostics.py --enable-local-services --case concurrent --healthy-url http://127.0.0.1:33080 --output .tmp/sql-event/diagnostic.json --runs 4 --warmup 1
```

The four diagnostic cases are `off`, `healthy`, `concurrent_off`, `concurrent`.
Run balanced, seeded, repeated blocks in separate processes; do not run another
load concurrently. Diagnostic SQL/public-method timing, PG activity/locks and
container/host samplers add cost. Their benchmark sibling file is diagnostic
evidence, never a formal gate sample. Formal measurement continues to use
`run_benchmark.py` without installing these probes.

The SQL probe keeps templates and timing/row counts, not bind parameters,
exception messages or query payloads. Unknown syntax is opaque. It excludes
startup/warmup, marks measurement/drain, and freezes before test DB cleanup.
PG deltas require stable full statement keys, database identity and reset/eviction
metadata. Server totals do not provide statement percentiles. Client method
timers are inclusive; pool acquisition includes ping, and commit includes the
server acknowledgement. Do not sum nested timers or label the remainder network
latency. Resource samples have their own observation interval. Per-Run attribution
is the caller context; background and bulk operations remain unassigned.

`--executor-no-ping` applies one **experiment-only** public engine option to a
benchmark executor. Domain/API engines retain their existing connection checks.
It changes when a dead connection is detected and is not a production recommendation.
Healthy speedups alone do not establish equivalent failure/recovery behavior.
Keep every failed or slower cell, the exact order, source/method hashes, raw
samples and service configuration. See the [SQL/event audit](../../docs/audits/dbos-sql-event-performance.md)
for the frozen baseline, measurement limits and promotion decision.
