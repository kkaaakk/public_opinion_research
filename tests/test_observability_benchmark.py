"""The benchmark must reject external services and distinguish absent metrics."""

import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

DRIVER = runpy.run_path(
    str(
        Path(__file__).resolve().parents[1]
        / "evaluation/observability/run_benchmark.py"
    )
)


def test_percentiles_do_not_invent_values_for_missing_samples():
    assert DRIVER["distribution"]([]) is None
    assert DRIVER["distribution"]([100])["p99"] == 100
    values = DRIVER["distribution"]([40, 10, 30, 20])
    assert values["p50"] == 25 and values["p95"] == pytest.approx(38.5)
    assert values["min"] == 10 and values["max"] == 40 and values["n"] == 4


def test_benchmark_rejects_external_and_obfuscated_service_addresses():
    for url in (
        "https://cloud.langfuse.com",
        "http://127.0.0.1@external.test",
        "bolt://external.test:7687",
    ):
        with pytest.raises(ValueError, match="isolated loopback"):
            DRIVER["local"](url)
    DRIVER["local"]("postgresql+psycopg://local:fixture@127.0.0.1:15434/por_test")


def test_rss_measurement_is_available_and_not_a_zero_fallback():
    assert DRIVER["rss"]() > 1024 * 1024


def test_summary_uses_raw_quantiles_and_does_not_pass_incomplete_experiments():
    def row(samples):
        return {
            "case": "off",
            "runs": [{"elapsed_ms": n} for n in samples],
            "wall_seconds": 1,
            "cpu_seconds": 0.1,
            "peak_rss_mib": 100,
            "latency_ms": {"run": DRIVER["distribution"](samples)},
            "timing_samples_ms": {},
            "export": {},
            "queue": {},
            "span_count": 0,
            "acknowledged_spans": 0,
            "shutdown_seconds": 0.01,
        }

    result = DRIVER["summarize"]([row([1, 1]), row([1, 100])])
    assert result["cases"]["off"]["run_ms"]["p95"] == pytest.approx(85.15)
    assert result["gates"]["all_configurations"] is False
    assert result["gates"]["repeated_samples"] is False
    assert result["all_gates_passed"] is False


def test_operator_configuration_and_libpq_environment_are_not_inherited():
    supplied = {
        "PATH": "runtime",
        "TEMP": "test-temp",
        "PYTHONPATH": "candidate/src",
        "TEST_DBOS_DATABASE_URL": "postgresql+psycopg://fixture@127.0.0.1/test",
        "LANGFUSE_PUBLIC_KEY": "local-test-only",
        "RESEARCH_GRAPH_URI": "bolt://business.example:7687",
        "RESEARCH_GRAPH_TRANSCRIPT_DIR": "business-output",
        "RUN_SDK_SESSION_PATH": "business-session.sqlite",
        "OPENAI_API_KEY": "operator-key-must-not-be-read",
        "PGSERVICE": "business",
        "PGPASSFILE": "operator-credentials",
    }
    clean = DRIVER["isolated_environment"](supplied)
    assert set(clean) == {
        "PATH",
        "TEMP",
        "PYTHONPATH",
        "TEST_DBOS_DATABASE_URL",
        "LANGFUSE_PUBLIC_KEY",
    }
    assert supplied["RUN_SDK_SESSION_PATH"] == "business-session.sqlite"


def test_environment_allowlist_does_not_read_disallowed_values():
    class Values(dict):
        def items(self):
            raise AssertionError("Do not enumerate credential values")

        def __getitem__(self, name):
            assert name != "OPENAI_API_KEY"
            return super().__getitem__(name)

    assert DRIVER["isolated_environment"](
        Values(PATH="runtime", OPENAI_API_KEY="not-read")
    ) == {"PATH": "runtime"}


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://fixture@127.0.0.1/test?host=external.example",
        "postgresql+psycopg://fixture@localhost/test?service=operator",
        "postgresql+psycopg://fixture@localhost/test?passfile=operator",
        "bolt://localhost:7687/?routing=external.example",
        "https://127.0.0.1/#private",
        "neo4j://localhost:7687",
        "postgresql+psycopg://fixture@127.0.0.1/test",
        "postgresql+psycopg://fixture:password@127.0.0.1/",
        "http://operator:password@localhost:33080",
    ],
)
def test_connection_parameters_cannot_override_loopback_or_read_operator_files(url):
    with pytest.raises(ValueError, match="isolated loopback"):
        DRIVER["local"](url)


def test_case_entrypoint_applies_explicit_source_root_before_worker(
    tmp_path, monkeypatch
):
    source = tmp_path / "src/open_deep_research/observability"
    source.mkdir(parents=True)
    (source / "telemetry.py").write_text("# isolated entrypoint probe\n")
    monkeypatch.setenv("PYTHONPATH", "incorrect-source")
    monkeypatch.setattr(sys, "path", list(sys.path))
    observed = []
    main = DRIVER["main"]
    monkeypatch.setitem(
        main.__globals__,
        "worker",
        lambda args: observed.append((os.environ["PYTHONPATH"], sys.path[0])),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark",
            "--enable-local-services",
            "--healthy-url",
            "http://127.0.0.1:33080",
            "--output",
            str(tmp_path / "output.json"),
            "--source-root",
            str(tmp_path),
            "--case",
            "concurrent_off",
        ],
    )
    main()
    assert observed == [(str(tmp_path / "src"), str(tmp_path / "src"))]


@pytest.mark.parametrize(
    "text",
    [
        "private-public_signal",
        "private-internal_knowledge",
        "private-risk_assessment",
        "private-response_strategy",
        "SDK_MODEL_OK",
        "Official brake investigation content",
        "PRIVATE_MEMORY_SENTINEL",
        "service output",
    ],
)
def test_content_gate_covers_actual_model_tool_and_memory_payloads(text):
    assert DRIVER["contains_content"]([{"input": text}])


def _server_rows(run, trace, start):
    rows = {}
    sequence = start
    root = f"{start:016x}"

    def add(name, kind="SPAN", usage=0):
        nonlocal sequence
        key = f"{sequence:016x}"
        sequence += 1
        rows[key] = {
            "id": key,
            "name": name,
            "type": kind,
            "totalUsage": usage,
            "traceId": trace,
            "sessionId": run,
            "metadata": {"run_id": run, "attributes.por.run_id": run},
            "parentObservationId": None if key == root else root,
        }

    for name, count in DRIVER["EXPECTED_STAGES"].items():
        for _ in range(count):
            add(name)
    for usage in [14] * 6 + [10] * 15:
        add("generation", "GENERATION", usage)
    for _ in range(8):
        add("agent", "AGENT")
    for _ in range(2):
        add("tool", "TOOL")
    return rows


def test_per_run_validation_rejects_shared_traces_wrong_usage_and_cross_run_parents():
    rows = {
        **_server_rows("run-a", "trace-a", 1),
        **_server_rows("run-b", "trace-b", 1000),
    }
    traces = {"run-a": {"trace-a"}, "run-b": {"trace-b"}}
    verify = DRIVER["verify_runs"]
    assert all(value["valid"] for value in verify(rows, traces, 21, 234).values())
    shared = verify(rows, {"run-a": {"trace-a"}, "run-b": {"trace-a"}}, 21, 234)
    assert (
        not shared["run-a"]["valid"] and "trace_ownership" in shared["run-a"]["issues"]
    )
    generation = next(o for o in rows.values() if o["type"] == "GENERATION")
    generation["totalUsage"] -= 1
    other = next(
        o
        for o in rows.values()
        if o["type"] == "GENERATION" and o["sessionId"] == "run-b"
    )
    other["totalUsage"] += 1  # total stays correct; each Run must still fail
    assert all(
        "generation_usage" in value["issues"]
        for value in verify(rows, traces, 21, 234).values()
    )
    generation["parentObservationId"] = f"{1000:016x}"
    assert "parent_ownership" in verify(rows, traces, 21, 234)["run-a"]["issues"]


def test_worker_timeout_terminates_the_owned_process_tree(monkeypatch):
    scope = DRIVER["execute_worker"].__globals__

    class Process:
        pid = 12345
        waits = 0
        killed = False

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return -9

        def poll(self):
            return None

        def kill(self):
            self.killed = True

    process = Process()
    commands = []
    monkeypatch.setattr(scope["subprocess"], "Popen", lambda *a, **k: process)
    monkeypatch.setattr(
        scope["subprocess"], "run", lambda command, **kwargs: commands.append(command)
    )
    with pytest.raises(subprocess.TimeoutExpired):
        DRIVER["execute_worker"](["owned-fixture"], {}, None, timeout=0.01)
    if os.name == "nt":
        assert commands == [["taskkill", "/PID", "12345", "/T", "/F"]]
    else:
        assert process.killed
    assert process.waits == 2
