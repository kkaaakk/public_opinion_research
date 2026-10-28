"""Repeated local DBOS research against real Langfuse; synthetic inference only."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
CRITICAL = frozenset(
    {
        "run_submit",
        "por.run",
        "por.plan",
        "por.research",
        "por.role",
        "por.finalize",
        "business_step",
        "sdk_invocation",
        "context_build",
        "context_retrieval",
        "persist_tool_results",
        "rolling_compaction",
        "research_graph_write",
        "research_review",
        "report_generation",
        "langchain_generation",
    }
)
# Frozen before measurement; the concurrency target is the default 4 active Runs.
# These gates detect local regressions, not production/provider latency SLOs.
LIMITS = {
    "p95_overhead_ratio": 1.25,
    "p95_slack_ms": 100,
    "concurrent_p95_ratio": 2.0,
    "additional_rss_mib": 256,
    "healthy_queue_drops": 0,
    "healthy_key_ack_fraction": 1.0,
    "healthy_key_server_fraction": 1.0,
    "shutdown_seconds": 3.5,
    "minimum_repetitions": 3,
    "minimum_runs_per_case": 12,
}


def distribution(values):
    values = sorted(values)
    if not values:
        return None

    def q(p):
        i = (len(values) - 1) * p
        a, b = math.floor(i), math.ceil(i)
        return values[a] + (values[b] - values[a]) * (i - a)

    return dict(
        n=len(values),
        min=values[0],
        max=values[-1],
        p50=q(0.5),
        p95=q(0.95),
        p99=q(0.99),
    )


def local(url):
    parsed = urlsplit(url)
    if (
        parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.scheme not in {"http", "https", "bolt", "postgresql+psycopg"}
        or parsed.query
        or parsed.fragment
        or (
            parsed.scheme == "postgresql+psycopg"
            and (not parsed.username or not parsed.password or parsed.path in {"", "/"})
        )
        or (
            parsed.scheme != "postgresql+psycopg"
            and (parsed.username is not None or parsed.password is not None)
        )
    ):
        raise ValueError("Benchmark requires explicitly isolated loopback services")


def isolated_environment(values):
    """Keep OS startup and explicit test inputs, never operator business config."""
    names = {
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "TEST_DBOS_DATABASE_URL",
        "TEST_DBOS_NEO4J_URI",
        "TEST_DBOS_NEO4J_PASSWORD",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    }
    return {key: values[key] for key in values if key.upper() in names}


def apply_source_root(source_root):
    if source_root is not None:
        source = source_root.resolve() / "src"
        if not (source / "open_deep_research/observability/telemetry.py").is_file():
            raise ValueError("Source root is not an application checkout")
        sys.path.insert(0, str(source))
        os.environ["PYTHONPATH"] = str(source)


CONTENT_SENTINELS = (
    "service output",
    "PRIVATE_",
    "SDK_MODEL_OK",
    "Official brake investigation",
    "private-public_signal",
    "private-internal_knowledge",
    "private-risk_assessment",
    "private-response_strategy",
)
EXPECTED_STAGES = {
    "run_submit": 1,
    "por.run": 1,
    "por.plan": 1,
    "por.research": 1,
    "por.role": 4,
    "por.finalize": 1,
    "sdk_invocation": 4,
    "research_review": 1,
    "report_generation": 3,
    "persist_tool_results": 2,
    "research_graph_write": 2,
    "context_build": 6,
    "context_retrieval": 12,
    "business_step": 12,
}


def contains_content(observations):
    return any(
        sentinel in json.dumps(observation)
        for observation in observations
        for sentinel in CONTENT_SENTINELS
    )


def verify_runs(observations, run_traces, generations, tokens):
    """Check each Run independently; aggregate totals cannot prove ownership."""
    owners = defaultdict(set)
    for run, traces in run_traces.items():
        for trace_id in traces:
            owners[trace_id].add(run)
    result = {}
    for run, traces in run_traces.items():
        rows = [o for o in observations.values() if o["traceId"] in traces]
        kinds, names = (
            Counter(o["type"] for o in rows),
            Counter(o["name"] for o in rows),
        )
        issues = []
        if len(traces) != 1 or any(len(owners[t]) != 1 for t in traces):
            issues.append("trace_ownership")
        if not rows or any(
            o.get("sessionId") != run
            or o.get("metadata", {}).get("run_id") != run
            or o.get("metadata", {}).get("attributes.por.run_id") != run
            for o in rows
        ):
            issues.append("run_identity")
        if (
            kinds["GENERATION"] != generations
            or sum(o.get("totalUsage") or 0 for o in rows) != tokens
        ):
            issues.append("generation_usage")
        if kinds["TOOL"] != 2 or kinds["AGENT"] != 8:
            issues.append("agent_tool_count")
        if any(names[name] != count for name, count in EXPECTED_STAGES.items()):
            issues.append("business_stage_count")
        for observation in rows:
            parent = observation.get("parentObservationId")
            if parent and (
                parent not in observations
                or observations[parent]["traceId"] != observation["traceId"]
                or observations[parent].get("sessionId") != run
            ):
                issues.append("parent_ownership")
                break
        if contains_content(rows):
            issues.append("content_leak")
        result[run] = {
            "valid": not issues,
            "issues": issues,
            "generations": kinds["GENERATION"],
            "tokens": sum(o.get("totalUsage") or 0 for o in rows),
        }
    return result


def terminate_owned(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    else:
        process.kill()
    process.wait(timeout=10)


def execute_worker(command, env, log, timeout=600):
    process = subprocess.Popen(
        command,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        code = process.wait(timeout=timeout)
        if code:
            raise subprocess.CalledProcessError(code, command)
    except BaseException:
        terminate_owned(process)
        raise


def summarize(records):
    """Aggregate raw samples, then apply the limits fixed before measurement."""
    groups = defaultdict(list)
    for record in records:
        groups[record["case"]].append(record)
    result = {}
    for name, rows in groups.items():
        n = sum(len(r["runs"]) for r in rows)
        wall = sum(r["wall_seconds"] for r in rows)
        batches = sum(r["export"].get("batches", 0) for r in rows)
        result[name] = {
            "runs": n,
            "run_ms": distribution([v["elapsed_ms"] for r in rows for v in r["runs"]]),
            "stage_ms": {
                key: distribution(
                    [v for r in rows for v in r["timing_samples_ms"].get(key, [])]
                )
                for key in ("agent_ms", "llm_ms", "tool_ms")
            },
            "cpu_seconds_per_run": sum(r["cpu_seconds"] for r in rows) / n,
            "cpu_one_core_percent": 100 * sum(r["cpu_seconds"] for r in rows) / wall,
            "peak_rss_mib": max(r["peak_rss_mib"] for r in rows),
            "spans": sum(r["span_count"] for r in rows),
            "export_spans_per_second": sum(r["acknowledged_spans"] for r in rows)
            / wall,
            "export_failed_batch_fraction": sum(
                r["export"].get("failed_batches", 0) for r in rows
            )
            / batches
            if batches
            else None,
            "queue_peak": max(r["queue"].get("peak", 0) for r in rows),
            "queue_dropped": sum(r["queue"].get("dropped", 0) for r in rows),
            "shutdown_seconds": distribution([r["shutdown_seconds"] for r in rows]),
            "repetition_p95_range": [
                min(r["latency_ms"]["run"]["p95"] for r in rows),
                max(r["latency_ms"]["run"]["p95"] for r in rows),
            ],
        }
    checks = {
        "all_configurations": set(groups)
        == {"off", "healthy", "unavailable", "concurrent"},
        "repeated_samples": all(
            len(rows) >= LIMITS["minimum_repetitions"]
            and result[name]["runs"] >= LIMITS["minimum_runs_per_case"]
            for name, rows in groups.items()
        ),
    }
    if "off" in result:
        baseline = result["off"]
        for name in ("healthy", "unavailable"):
            if name in result:
                checks[name + "_p95"] = (
                    result[name]["run_ms"]["p95"]
                    <= baseline["run_ms"]["p95"] * LIMITS["p95_overhead_ratio"]
                    + LIMITS["p95_slack_ms"]
                )
                checks[name + "_rss"] = (
                    result[name]["peak_rss_mib"]
                    <= baseline["peak_rss_mib"] + LIMITS["additional_rss_mib"]
                )
    if "healthy" in result and "concurrent" in result:
        checks["concurrent_p95_capacity"] = (
            result["concurrent"]["run_ms"]["p95"]
            <= result["healthy"]["run_ms"]["p95"] * LIMITS["concurrent_p95_ratio"]
        )
    for name in ("healthy", "concurrent"):
        if name in result:
            rows = groups[name]
            checks[name + "_queue"] = (
                result[name]["queue_dropped"] == LIMITS["healthy_queue_drops"]
            )
            checks[name + "_transport"] = all(
                r["key_span_ack_fraction"] == LIMITS["healthy_key_ack_fraction"]
                for r in rows
            )
            checks[name + "_server"] = all(
                r["server_verification"]["key_span_fraction"]
                == LIMITS["healthy_key_server_fraction"]
                and r["server_verification"]["acknowledged_span_fraction"] == 1
                and r["server_verification"]["generations"]
                == r["server_verification"]["expected_generations"]
                and r["server_verification"]["tokens"]
                == r["server_verification"]["expected_tokens"]
                and r["server_verification"]["parent_gaps"] == 0
                and not r["server_verification"]["content_leak"]
                and len(r["server_verification"].get("per_run", {})) == len(r["runs"])
                and all(
                    v["valid"]
                    for v in r["server_verification"].get("per_run", {}).values()
                )
                for r in rows
            )
    for name, summary in result.items():
        checks[name + "_shutdown"] = (
            summary["shutdown_seconds"]["max"] <= LIMITS["shutdown_seconds"]
        )
    return {"cases": result, "gates": checks, "all_gates_passed": all(checks.values())}


def rss():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Memory(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (k, ctypes.c_size_t)
                for k in (
                    "peak",
                    "working",
                    "pool_peak",
                    "pool",
                    "nonpaged_peak",
                    "nonpaged",
                    "pagefile",
                    "pagefile_peak",
                )
            ]

        m = Memory()
        m.cb = ctypes.sizeof(m)
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Memory),
            wintypes.DWORD,
        ]
        if not ctypes.windll.psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(m), m.cb
        ):
            raise OSError("Cannot measure RSS")
        return m.working
    if Path("/proc/self/statm").exists():
        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf(
            "SC_PAGE_SIZE"
        )
    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def worker(args):
    traced = args.case not in {"off", "concurrent_off"}
    clean = isolated_environment(os.environ)
    os.environ.clear()
    os.environ.update(clean)
    for key in (
        "TEST_DBOS_DATABASE_URL",
        "TEST_DBOS_NEO4J_URI",
        "TEST_DBOS_NEO4J_PASSWORD",
    ):
        if not os.environ.get(key):
            raise ValueError("Missing isolated setting: " + key)
    local(os.environ["TEST_DBOS_DATABASE_URL"])
    local(os.environ["TEST_DBOS_NEO4J_URI"])
    local(args.healthy_url)
    os.environ.update(
        PYTHON_DOTENV_DISABLED="1",
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
        OPENAI_AGENTS_DISABLE_TRACING="1",
        POR_TRACING_ENABLED="true" if traced else "false",
        POR_TRACE_SAMPLE_RATE="1",
        POR_TRACE_TIMEOUT_SECONDS="2",
        POR_TRACE_SHUTDOWN_SECONDS="3",
        OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED="true",
        RUN_MAX_ACTIVE="4",
        RUN_OWNER_ACTIVE="2",
        LANGFUSE_BASE_URL=args.healthy_url,
        LANGFUSE_FLUSH_AT="512",
        LANGFUSE_FLUSH_INTERVAL="5",
        OTEL_BSP_MAX_QUEUE_SIZE="2048",
        OTEL_BSP_MAX_EXPORT_BATCH_SIZE="512",
        OTEL_BSP_SCHEDULE_DELAY="5000",
        NO_PROXY="127.0.0.1,localhost,::1",
        PGSSLMODE="disable",
        PGGSSENCMODE="disable",
        PGREQUIREAUTH="scram-sha-256,md5,password",
        PGPASSFILE=os.devnull,
    )
    reserved = None
    if args.case == "unavailable":
        reserved = socket.socket()
        reserved.bind(("127.0.0.1", 0))  # reserved, deliberately not listening
        os.environ["LANGFUSE_BASE_URL"] = (
            f"http://127.0.0.1:{reserved.getsockname()[1]}"
        )
    import dotenv

    dotenv.load_dotenv = lambda *a, **k: False
    original_connect = socket.socket.connect

    def connect(self, address):
        if isinstance(address, tuple) and address[0] not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise AssertionError("Benchmark attempted external network")
        return original_connect(self, address)

    socket.socket.connect = connect
    sys.path.insert(0, str(ROOT / "tests"))
    import asyncio
    import importlib.metadata
    from contextlib import asynccontextmanager
    from contextvars import ContextVar

    import httpx2
    import pytest
    import requests
    from agents import OpenAIResponsesModel
    from dbos import DBOS
    from dbos_support import NativeCase
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.tools import BaseTool
    from langchain_core.tracers.context import register_configure_hook
    from openai import AsyncOpenAI
    from opentelemetry import metrics
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import SpanProcessor
    from opentelemetry.sdk.trace.export import SpanExportResult
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from test_sdk_models import _reply

    from open_deep_research.observability import telemetry
    from open_deep_research.runtime import business_agent
    from open_deep_research.runtime.sdk_runtime import SDKAgentRuntime
    from open_deep_research.web.durable import close_run_client
    from open_deep_research.web.executor import launch
    from open_deep_research.workflow.dbos_config import settings
    from open_deep_research.workflow.run_facts import facts

    def version(package):
        try:
            return importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            return None

    lock, times, starts = threading.Lock(), defaultdict(list), {}
    ended, ack, exports, kinds = {}, set(), Counter(), Counter()
    trace_ids = defaultdict(set)
    acknowledged_traces = defaultdict(set)
    http_requests = Counter()
    patch = pytest.MonkeyPatch()
    original_exporter_init = OTLPSpanExporter.__init__

    def local_exporter_init(self, *a, **kwargs):
        session = requests.Session()
        session.trust_env = False  # no operator proxy/netrc credential discovery
        kwargs["session"] = session
        original_exporter_init(self, *a, **kwargs)

    patch.setattr(OTLPSpanExporter, "__init__", local_exporter_init)

    class Clock(BaseCallbackHandler):
        run_inline = True

        def on_chat_model_start(self, *a, run_id, **k):
            starts[run_id] = time.perf_counter()

        def on_llm_end(self, response, *, run_id, **k):
            with lock:
                times["llm_ms"].append(
                    (time.perf_counter() - starts.pop(run_id)) * 1000
                )

    register_configure_hook(
        ContextVar("benchmark_clock", default=Clock()),
        inheritable=True,
        handle_class=Clock,
    )

    def timed(cls, name, bucket, delay=0):
        original = getattr(cls, name)

        async def invoke(self, *a, **k):
            start = time.perf_counter()
            try:
                if delay:
                    await asyncio.sleep(delay)
                return await original(self, *a, **k)
            finally:
                with lock:
                    times[bucket].append((time.perf_counter() - start) * 1000)

        patch.setattr(cls, name, invoke)

    timed(OpenAIResponsesModel, "get_response", "llm_ms")
    timed(SDKAgentRuntime, "run", "agent_ms")
    timed(BaseTool, "ainvoke", "tool_ms")
    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    metrics.set_meter_provider(meters)

    class Probe(SpanProcessor):
        def on_end(self, span):
            attrs = span.attributes
            kind = attrs.get("openinference.span.kind", "SPAN")
            with lock:
                rid = attrs.get("por.run_id")
                if rid:
                    trace_ids[rid].add(f"{span.context.trace_id:032x}")
                ended[span.context.span_id] = (
                    rid,
                    span.name in CRITICAL or kind in {"LLM", "AGENT", "TOOL"},
                    span.name,
                )
                kinds[kind] += 1

    original_export = OTLPSpanExporter.export

    def export(self, spans):
        result = original_export(self, spans)
        with lock:
            exports["attempted_spans"] += len(spans)
            exports["batches"] += 1
            exports["failed_batches"] += result != SpanExportResult.SUCCESS
            if result == SpanExportResult.SUCCESS:
                ack.update(s.context.span_id for s in spans)
                for span in spans:
                    acknowledged_traces[f"{span.context.trace_id:032x}"].add(
                        span.context.span_id
                    )
        return result

    patch.setattr(OTLPSpanExporter, "export", export)
    url, name = (
        make_url(os.environ["TEST_DBOS_DATABASE_URL"]),
        "por_test_" + uuid4().hex,
    )
    admin = create_engine(
        url, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3}
    )
    with admin.connect() as db:
        db.execute(text("CREATE DATABASE " + name))
    os.environ["DBOS_DATABASE_URL"] = url.set(database=name).render_as_string(
        hide_password=False
    )
    settings.cache_clear()
    directory = args.output.parent / (name + "-data")
    directory.mkdir(parents=True, exist_ok=True)
    case, stop, samples, queue = None, threading.Event(), [], Counter()

    def sample():
        while not stop.is_set():
            samples.append(rss())
            data = reader.get_metrics_data()
            for resource in data.resource_metrics if data else []:
                for scope in resource.scope_metrics:
                    for m in scope.metrics:
                        points = m.data.data_points
                        if m.name == "otel.sdk.processor.span.queue.size":
                            queue["peak"] = max(
                                queue["peak"], sum(p.value for p in points)
                            )
                        elif m.name == "otel.sdk.processor.span.queue.capacity":
                            queue["capacity"] = max(
                                queue["capacity"], sum(p.value for p in points)
                            )
                        elif m.name == "otel.sdk.processor.span.processed":
                            queue["dropped"] = max(
                                queue["dropped"],
                                sum(
                                    p.value
                                    for p in points
                                    if p.attributes.get("error.type") == "queue_full"
                                ),
                            )
            stop.wait(0.025)

    def run(index):
        start = time.perf_counter()
        value = case.create(owner="bench-" + str(index))
        final = case.wait(
            value,
            lambda r: r["archive_status"] == "saved" and r["events_complete"],
            timeout=120,
        )
        assert final["status"] == "completed" and final["settlement"] == "settled"
        assert (
            final["usage"]["model_calls"] == 28
            and final["usage"]["total_tokens"] == 311
        )
        return {
            "run_id": value["run_id"],
            "elapsed_ms": (time.perf_counter() - start) * 1000,
        }

    try:
        DBOS.migrate(os.environ["DBOS_DATABASE_URL"], schema="por_dbos")
        facts().initialize()
        case = NativeCase(patch, directory, "normal")
        case.state["messages"][0].content += " PRIVATE_PROMPT_SENTINEL"
        for state in case.state["agents"].values():
            for memory in state.get("memory", []):
                memory["content"] += " PRIVATE_MEMORY_SENTINEL"

        @asynccontextmanager
        async def http_model(*a, **k):
            assert k["max_retries"] == 0
            turn = 0

            async def respond(request):
                nonlocal turn
                turn += 1
                body = json.loads(request.content)
                role = (
                    body["instructions"]
                    .split("<Role>\n", 1)[1]
                    .split("\n</Role>", 1)[0]
                )
                http_requests[role] += 1
                producer = role in {"public_signal", "internal_knowledge"}
                payload = _reply(
                    "responses", turn if producer else 2, "benchmark-model"
                )
                if producer and turn == 1:
                    payload["output"][0].update(
                        name="web_search" if role == "public_signal" else "rag_search",
                        call_id="lookup-" + role,
                        arguments=json.dumps(
                            {"query": role + " PRIVATE_TOOL_ARGUMENT_SENTINEL"}
                        ),
                    )
                payload["usage"].update(
                    input_tokens=10, output_tokens=4, total_tokens=14
                )
                payload["usage"]["input_tokens_details"]["cached_tokens"] = 0
                payload["usage"]["output_tokens_details"]["reasoning_tokens"] = 0
                if not (producer and turn == 1):
                    payload["output"][0]["content"][0]["text"] += (
                        " PRIVATE_SDK_RESPONSE_SENTINEL"
                    )
                await asyncio.sleep(0.002)
                return httpx2.Response(200, json=payload)

            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(respond)
            ) as transport:
                client = AsyncOpenAI(
                    api_key="offline-only",
                    http_client=transport,
                    max_retries=0,
                    base_url="http://127.0.0.1:1/offline",
                )
                model = OpenAIResponsesModel(
                    model="benchmark-model", openai_client=client
                )
                case.models.append(model)
                yield model

        patch.setattr(business_agent, "sdk_model", http_model)
        case.cfg["configurable"].update(
            research_graph_backend="neo4j",
            research_graph_uri=os.environ["TEST_DBOS_NEO4J_URI"],
            research_graph_username="neo4j",
            research_graph_password=os.environ["TEST_DBOS_NEO4J_PASSWORD"],
        )
        launch("benchmark-" + uuid4().hex)
        assert telemetry.telemetry_enabled() == traced
        if telemetry.telemetry_enabled():
            telemetry._provider.add_span_processor(Probe())
        for i in range(args.warmup):
            run(-i - 1)
        time.sleep(0.3)
        if telemetry.telemetry_enabled():
            telemetry._provider.force_flush(3000)  # warmup, outside timing window
        times.clear()
        ended.clear()
        ack.clear()
        acknowledged_traces.clear()
        exports.clear()
        kinds.clear()
        trace_ids.clear()
        before, wall_start, cpu_start = rss(), time.perf_counter(), time.process_time()
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        concurrency = 4 if args.case in {"concurrent", "concurrent_off"} else 1
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(run, range(args.runs)))
        wall, cpu = time.perf_counter() - wall_start, time.process_time() - cpu_start
        assert len(case.models) == 4 * (args.runs + args.warmup)
        assert sum(http_requests.values()) == 6 * (args.runs + args.warmup)
        assert case.calls == {
            "public_signal": args.runs + args.warmup,
            "internal_knowledge": args.runs + args.warmup,
        }
        assert len(times["llm_ms"]) == args.runs * 21
        assert (
            len(times["agent_ms"]) == args.runs * 4
            and len(times["tool_ms"]) == args.runs * 2
        )
        DBOS.destroy(workflow_completion_timeout_sec=3)
        start = time.perf_counter()
        telemetry.shutdown_telemetry()
        shutdown = time.perf_counter() - start
        time.sleep(0.05)
        stop.set()
        sampler.join(2)
        if traced:
            assert queue["capacity"] == 2048, "Official SDK queue metrics unavailable"
        ids = {r["run_id"] for r in results}
        keys = {
            sid for sid, (rid, critical, _) in ended.items() if rid in ids and critical
        }
        required = {
            "por.run",
            "por.plan",
            "por.research",
            "por.finalize",
            "sdk_invocation",
            "research_review",
            "report_generation",
        }
        missing = {
            rid: sorted(
                required
                - {name for current, _, name in ended.values() if current == rid}
            )
            for rid in ids
        }
        if telemetry._provider is not None:
            assert keys and not any(missing.values()), missing
        complete = sum(
            not missing[rid]
            and {
                sid
                for sid, (current, critical, _) in ended.items()
                if current == rid and critical
            }
            <= ack
            for rid in ids
        )
        server = None
        if args.case in {"healthy", "concurrent"}:
            # Verification is outside the CPU/RSS/latency measurement window.
            # An OTLP ACK alone does not prove backend persistence.
            persisted, observations = set(), {}
            target_traces = {t for rid in ids for t in trace_ids[rid]}
            expected_ids = {
                sid for tid in target_traces for sid in acknowledged_traces[tid]
            }
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                for tid in target_traces:
                    cursor = None
                    visited = set()
                    while time.monotonic() < deadline:
                        params = {
                            "traceId": tid,
                            "limit": 1000,
                            "fields": "basic,core,io,metadata,model,usage",
                        }
                        if cursor:
                            params["cursor"] = cursor
                        verification_session = requests.Session()
                        verification_session.trust_env = False
                        response = verification_session.get(
                            args.healthy_url + "/api/public/v2/observations",
                            params=params,
                            auth=(
                                os.environ["LANGFUSE_PUBLIC_KEY"],
                                os.environ["LANGFUSE_SECRET_KEY"],
                            ),
                            timeout=5,
                        )
                        response.raise_for_status()
                        page = response.json()
                        verification_session.close()
                        for item in page["data"]:
                            observations[item["id"]] = item
                            persisted.add(int(item["id"], 16))
                        cursor = page.get("meta", {}).get("cursor") or page.get(
                            "meta", {}
                        ).get("nextCursor")
                        if not cursor:
                            break
                        if cursor in visited:
                            raise ValueError("Observation pagination cursor repeated")
                        visited.add(cursor)
                if expected_ids <= persisted:
                    break
                time.sleep(0.5)
            generations = [
                o for o in observations.values() if o["type"] == "GENERATION"
            ]
            has_lc = Path(telemetry.__file__).with_name("langchain.py").exists()
            expected_generations = args.runs * (21 if has_lc else 6)
            expected_tokens = args.runs * (234 if has_lc else 84)
            parent_gaps = sum(
                bool(o["parentObservationId"])
                and o["parentObservationId"] not in observations
                for o in observations.values()
            )
            server = {
                "key_span_fraction": len(keys & persisted) / len(keys)
                if keys
                else None,
                "acknowledged_span_fraction": len(expected_ids & persisted)
                / len(expected_ids)
                if expected_ids
                else None,
                "observations": len(observations),
                "generations": len(generations),
                "expected_generations": expected_generations,
                "tokens": sum(o.get("totalUsage") or 0 for o in observations.values()),
                "expected_tokens": expected_tokens,
                "parent_gaps": parent_gaps,
                "content_leak": contains_content(observations.values()),
                "per_run": verify_runs(
                    observations,
                    {rid: trace_ids[rid] for rid in ids},
                    21 if has_lc else 6,
                    234 if has_lc else 84,
                ),
            }
        output = {
            "case": args.case,
            "concurrency": concurrency,
            "runs": results,
            "latency_ms": {
                "run": distribution([r["elapsed_ms"] for r in results]),
                **{k: distribution(v) for k, v in times.items()},
            },
            "timing_samples_ms": dict(times),
            "trace_ids": {rid: sorted(trace_ids[rid]) for rid in ids},
            "server_verification": server,
            "wall_seconds": wall,
            "cpu_seconds": cpu,
            "cpu_one_core_percent": 100 * cpu / wall,
            "rss_start_mib": before / 2**20,
            "peak_rss_mib": max(samples or [before]) / 2**20,
            "queue": dict(queue),
            "export": dict(exports),
            "span_count": len(ended),
            "business_span_count": sum(critical for _, critical, _ in ended.values()),
            "missing_stages": missing if traced else None,
            "acknowledged_complete_run_fraction": complete / args.runs
            if traced
            else None,
            "kinds": dict(kinds),
            "acknowledged_spans": len(ack),
            "export_spans_per_second": len(ack) / wall,
            "shutdown_seconds": shutdown,
            "key_span_ack_fraction": len(keys & ack) / len(keys) if keys else None,
            "unacknowledged_keys": len(keys - ack),
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "logical_cpus": os.cpu_count(),
                "warmup_runs": args.warmup,
                "source_root": os.environ.get(
                    "PYTHONPATH", "installed editable package"
                ),
                "versions": {
                    p: version(p)
                    for p in (
                        "dbos",
                        "openai-agents",
                        "langfuse",
                        "opentelemetry-sdk",
                        "langchain-core",
                        "openinference-instrumentation-openai-agents",
                        "openinference-instrumentation-langchain",
                    )
                },
            },
            "limits_frozen_before_run": LIMITS,
            "limitations": [
                "Synthetic local inference, not provider/production performance",
                "OTLP ACK completeness does not prove backend persistence",
                "Process CPU/RSS include fixtures and validation probes; container CPU is separate",
            ],
        }
        args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")
    finally:
        stop.set()
        DBOS.destroy(workflow_completion_timeout_sec=2)
        telemetry.shutdown_telemetry()
        if case is not None:
            case.history.close()
        close_run_client()
        patch.undo()
        meters.shutdown()
        settings.cache_clear()
        assert name.startswith("por_test_") and len(name) == 41
        with admin.connect() as db:
            db.execute(text("DROP DATABASE " + name + " WITH (FORCE)"))
        admin.dispose()
        if reserved:
            reserved.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-local-services", action="store_true")
    parser.add_argument("--healthy-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--runs", type=int, default=4)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument(
        "--case",
        choices=("off", "healthy", "unavailable", "concurrent", "concurrent_off"),
    )
    args = parser.parse_args()
    if (
        not args.enable_local_services
        or min(args.runs, args.warmup, args.repetitions) < 1
    ):
        parser.error("Explicit local services, positive samples and warmup required")
    local(args.healthy_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    apply_source_root(args.source_root)
    if args.case:
        worker(args)
        return
    records, cases = [], ["off", "healthy", "unavailable", "concurrent"]
    env = isolated_environment(os.environ)
    for repeat in range(args.repetitions):
        for case in cases[repeat % 4 :] + cases[: repeat % 4]:
            output = args.output.parent / f"{args.output.stem}-{repeat}-{case}.json"
            command = [
                sys.executable,
                str(Path(__file__)),
                "--enable-local-services",
                "--healthy-url",
                args.healthy_url,
                "--output",
                str(output),
                "--runs",
                str(args.runs),
                "--warmup",
                str(args.warmup),
                "--case",
                case,
            ]
            with output.with_suffix(".log").open("w", encoding="utf-8") as log:
                execute_worker(command, env, log)
            records.append(json.loads(output.read_text(encoding="utf-8")))
            print(
                f"Completed repetition {repeat + 1}/{args.repetitions}: {case}",
                flush=True,
            )
    args.output.write_text(
        json.dumps(
            {
                "limits": LIMITS,
                "repetitions": args.repetitions,
                "records": records,
                "summary": summarize(records),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
