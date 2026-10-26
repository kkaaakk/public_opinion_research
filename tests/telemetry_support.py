"""Real SDK/OTel/Langfuse pipeline, exporting only to local test memory."""

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.fixture
def otlp_receiver():
    """Receive actual OTLP protobuf on loopback, with no Langfuse account."""
    import gzip
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    spans = []
    headers = []
    state = SimpleNamespace(delay=0)

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            request = ExportTraceServiceRequest.FromString(body)
            assert self.path == "/api/public/otel/v1/traces"
            for resource in request.resource_spans:
                for scope in resource.scope_spans:
                    spans.extend(scope.spans)
            headers.append(dict(self.headers))
            if state.delay:
                import time

                time.sleep(state.delay)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        state.url = f"http://127.0.0.1:{server.server_port}"
        state.spans, state.headers = spans, headers
        yield state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


@pytest.fixture
def trace_capture(monkeypatch, request):
    from agents import set_trace_processors, set_tracing_disabled
    from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http import trace_exporter
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from open_deep_research.observability import telemetry

    memory = InMemorySpanExporter()
    current = {"provider": trace.ProxyTracerProvider()}
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: current["provider"])
    monkeypatch.setattr(
        trace, "set_tracer_provider", lambda p: current.update(provider=p)
    )
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", lambda **kw: memory)
    monkeypatch.setenv("POR_TRACING_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test-" + uuid4().hex)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "test-only-secret")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("POR_TRACE_SAMPLE_RATE", str(getattr(request, "param", 1)))
    for key, value in {
        "_attempted": False,
        "_active": False,
        "_provider": None,
        "_client": None,
        "_settings": None,
    }.items():
        monkeypatch.setattr(telemetry, key, value)
    assert telemetry.initialize_telemetry()
    provider = telemetry._provider

    def spans():
        assert provider.force_flush(timeout_millis=2000)
        return memory.get_finished_spans()

    try:
        yield SimpleNamespace(spans=spans, memory=memory, provider=provider)
    finally:
        OpenAIAgentsInstrumentor().uninstrument()
        set_trace_processors([])
        set_tracing_disabled(True)
        telemetry.shutdown_telemetry()
