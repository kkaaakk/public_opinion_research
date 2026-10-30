"""Delivery diagnostics and shutdown use the existing OTel exporter only."""

import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace.export import SpanExportResult

from open_deep_research.observability import telemetry


@pytest.mark.parametrize("raises", [False, True])
def test_export_failure_is_counted_and_new_exports_recover(trace_capture, raises):
    target = trace_capture.memory
    original = target.export

    def fail(spans):
        if raises:
            raise OSError("PRIVATE_ERROR_SENTINEL")
        return SpanExportResult.FAILURE

    target.export = fail
    with telemetry.business_span("outage"):
        assert 6 * 7 == 42
    trace_capture.spans()
    first = telemetry.delivery_health()
    assert first["attempted_spans"] == first["failed_spans"] == 1
    assert first["failed_batches"] == 1 and first["acknowledged_spans"] == 0
    target.export = original
    with telemetry.business_span("recovered"):
        pass
    spans = trace_capture.spans()
    final = telemetry.delivery_health()
    assert final["attempted_spans"] == 2 and final["acknowledged_spans"] == 1
    assert len(spans) == 1 and spans[0].name == "recovered"
    assert "PRIVATE_" not in str(final)


def test_shutdown_is_once_even_when_exporter_close_raises(caplog):
    calls = []

    def shutdown():
        calls.append(1)
        raise OSError("PRIVATE_ERROR_SENTINEL")

    exporter = telemetry.SafeExporter(SimpleNamespace(shutdown=shutdown))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: exporter.shutdown(), range(20)))
    assert calls == [1]
    assert exporter.snapshot()["closed"]
    assert "PRIVATE_" not in caplog.text


def test_shutdown_drains_pending_spans_once(trace_capture):
    with telemetry.business_span("pending_at_exit"):
        pass
    telemetry.shutdown_telemetry()
    telemetry.shutdown_telemetry()
    assert [s.name for s in trace_capture.memory.get_finished_spans()] == [
        "pending_at_exit"
    ]
    assert telemetry.delivery_health()["acknowledged_spans"] == 1


def test_provider_close_exception_and_stuck_client_cannot_block_exit(
    monkeypatch, caplog
):
    release = threading.Event()
    closed = threading.Event()

    def close_provider():
        closed.set()
        raise RuntimeError("PRIVATE_SHUTDOWN_SENTINEL")

    monkeypatch.setattr(telemetry, "_active", True)
    monkeypatch.setattr(
        telemetry, "_client", SimpleNamespace(shutdown=lambda: release.wait(2))
    )
    monkeypatch.setattr(
        telemetry, "_provider", SimpleNamespace(shutdown=close_provider)
    )
    monkeypatch.setattr(telemetry, "_settings", SimpleNamespace(shutdown_seconds=0.03))
    started = time.monotonic()
    try:
        telemetry.shutdown_telemetry()
        assert time.monotonic() - started < 0.3
    finally:
        release.set()
        assert closed.wait(1)
    assert "PRIVATE_" not in caplog.text


def test_exporter_flush_error_is_fail_open(caplog):
    def fail(timeout):
        raise RuntimeError("PRIVATE_FLUSH_SENTINEL")

    exporter = telemetry.SafeExporter(SimpleNamespace(force_flush=fail))
    assert exporter.force_flush(10) is False
    assert "PRIVATE_" not in caplog.text


def test_business_filter_is_metadata_only_and_preserves_all_parents(trace_capture):

    tracer = trace_capture.provider.get_tracer("dbos-tracer")
    with telemetry.business_span("por.run", run_id="layer-run"):
        with tracer.start_as_current_span("send"):
            with telemetry.business_span("sdk_invocation"):
                pass
    spans = trace_capture.spans()
    assert len(spans) == 3
    by_name = {s.name: s for s in spans}
    assert by_name["send"].attributes["por.span_layer"] == "framework"
    assert by_name["por.run"].attributes["por.span_layer"] == "business"
    assert by_name["sdk_invocation"].attributes["por.span_layer"] == "business"
    assert by_name["send"].parent.span_id == by_name["por.run"].context.span_id
    assert by_name["sdk_invocation"].parent.span_id == by_name["send"].context.span_id


def test_official_batch_queue_metrics_expose_bounded_drops(monkeypatch):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

    monkeypatch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
    entered, release = threading.Event(), threading.Event()
    received = []

    class Exporter(SpanExporter):
        def export(self, spans):
            entered.set()
            assert release.wait(3)
            received.extend(spans)
            return SpanExportResult.SUCCESS

    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(
        BatchSpanProcessor(
            Exporter(),
            max_queue_size=8,
            max_export_batch_size=4,
            schedule_delay_millis=60000,
            meter_provider=meters,
        )
    )
    tracer = provider.get_tracer("delivery-contract")
    try:
        for _ in range(4):
            with tracer.start_as_current_span("first_batch"):
                pass
        assert entered.wait(2)
        for _ in range(20):
            with tracer.start_as_current_span("overload"):
                pass
        metrics = {
            m.name: m.data.data_points
            for r in reader.get_metrics_data().resource_metrics
            for s in r.scope_metrics
            for m in s.metrics
        }
        assert sum(p.value for p in metrics["otel.sdk.processor.span.queue.size"]) == 8
        dropped = sum(
            p.value
            for p in metrics["otel.sdk.processor.span.processed"]
            if p.attributes.get("error.type") == "queue_full"
        )
        assert dropped == 12
        release.set()
        assert provider.force_flush(2000)
        assert len(received) == 12
        assert len(received) + dropped == 24
    finally:
        release.set()
        provider.shutdown()
        meters.shutdown()


def exit_process(receiver, mode, ready=None):
    root = Path(__file__).resolve().parents[1]
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}
    }
    env.update(
        PYTHONPATH=str(root / "src"),
        PYTHON_DOTENV_DISABLED="1",
        LANGSMITH_TRACING="false",
        OPENAI_AGENTS_DISABLE_TRACING="1",
        POR_TRACING_ENABLED="true",
        LANGFUSE_BASE_URL=receiver.url,
        LANGFUSE_PUBLIC_KEY="pk-lf-exit-test",
        LANGFUSE_SECRET_KEY="exit-fixture-only",
        LANGFUSE_FLUSH_INTERVAL="60",
        POR_TRACE_TIMEOUT_SECONDS="0.2",
        POR_TRACE_SHUTDOWN_SECONDS="1",
    )
    return subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("telemetry_exit_fixture.py")),
            mode,
            str(ready or "unused"),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def kill_exit_process(child):
    # Windows' venv redirector owns a second interpreter. Reuse the native
    # fault fixture's tree-scoped kill; killing only the redirector is not a
    # process-crash experiment and can leave an exporter alive.
    from test_dbos_process import kill_owned

    kill_owned(child)


@pytest.mark.parametrize("mode", ["normal", "unhandled"])
def test_real_interpreter_exit_flushes_without_explicit_application_close(
    otlp_receiver, mode
):
    child = exit_process(otlp_receiver, mode)
    try:
        child.communicate(timeout=25)
        assert child.returncode == (0 if mode == "normal" else 1)
        assert [s.name for s in otlp_receiver.spans] == ["exit_probe"]
    finally:
        if child.poll() is None:
            kill_exit_process(child)


def test_abrupt_process_kill_loses_only_unexported_memory(otlp_receiver, tmp_path):
    ready = tmp_path / "exit-ready"
    child = exit_process(otlp_receiver, "wait", ready)
    try:
        deadline = time.monotonic() + 25
        while (
            not ready.exists() and child.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert ready.exists()
        assert otlp_receiver.spans == []
        kill_exit_process(child)
        assert otlp_receiver.spans == []
    finally:
        if child.poll() is None:
            kill_exit_process(child)


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX signals require the Linux CI process, not TerminateProcess",
)
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_posix_signals_flush_ended_spans(otlp_receiver, tmp_path, signum):
    ready = tmp_path / "signal-ready"
    child = exit_process(otlp_receiver, "wait", ready)
    try:
        deadline = time.monotonic() + 25
        while (
            not ready.exists() and child.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert ready.exists()
        child.send_signal(signum)
        child.communicate(timeout=10)
        assert [s.name for s in otlp_receiver.spans] == ["exit_probe"]
    finally:
        if child.poll() is None:
            kill_exit_process(child)
