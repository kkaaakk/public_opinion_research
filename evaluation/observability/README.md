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
random `por_test_UUID` databases. Evidence/History remain in the isolated Neo4j
volume until that test project's normal cleanup.

```powershell
.venv/Scripts/python.exe evaluation/observability/run_benchmark.py --enable-local-services --healthy-url http://127.0.0.1:33080 --output .tmp/observability/benchmark.json --repetitions 3 --runs 4 --warmup 1
```

Each repetition/configuration starts a fresh interpreter and warms up a full Run.
Order rotates to expose service-cache drift. The four configurations are tracing
off, tracing with real healthy Langfuse, tracing with a reserved non-listening
loopback port, and four concurrent traced Runs. The last matches the project's
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

Aggregate raw Run/agent/LLM/tool samples across repetitions, not the mean of their
percentiles. Retain each repetition's range and counters. Twelve Runs/config are
a small local regression sample, especially for p99; they do not establish a
production tail-latency SLO. OTLP ACK completeness is transport evidence and must
be distinguished from server persistence and UI validation. Healthy/concurrent
cases additionally fetch complete v2 observation fields after the timing window,
wait for all acknowledged span IDs, traverse cursors, and check Generations,
tokens, parent closure and content sentinels against the actual Langfuse server.
An unavailable
exporter discards failed batches after its bounded attempt; recovery does not
replay them. In-memory queued/open spans can be lost on a process kill.

`--source-root` can test a clearly identified local composition of independent
candidate PRs. Use an interpreter containing that source's locked dependencies,
record its commits/tree, and keep all PRs based independently on main. This does
not merge their GitHub PRs or make the benchmark branch depend on unmerged code.
The report must distinguish measurements of main from that composed candidate.

If the four-concurrent capacity gate fails, `--case concurrent_off --runs 4`
provides the same-concurrency tracing-off diagnostic. Repeat it independently;
do not replace the original four-case results or change their frozen limits.
