"""Opt-in, fail-open OTel/Langfuse export; never an execution authority.

Only metadata crosses this boundary. Native DBOS and OpenInference own their
spans; the exporter also removes sensitive data produced by either library.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import logging
import math
import os
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from urllib.parse import urlsplit

from agents.tracing import TracingProcessor
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import Link, SpanContext, Status, StatusCode

_logger = logging.getLogger(__name__)
_identity = ContextVar("por_trace_identity", default={})
_harness = ContextVar("por_trace_harness", default=None)
_lock = threading.Lock()
_attempted = False
_provider = None
_client = None
_settings = None
_active = False
_exporter = None

# Explicit fields only: no arbitrary metadata, content, exception text or auth.
_IDENTITY = frozenset(
    {
        "run_id",
        "workflow_id",
        "unit_id",
        "execution_id",
        "invocation_id",
        "role",
        "task_id",
        "research_round",
        "generation",
        "tool_call_id",
        "graph_call_id",
        "operation_id",
        "model_turn",
        "step",
        "status",
        "error_type",
        "failure_kind",
        "effect_status",
    }
)
_COUNTS = frozenset(
    {
        "batch_size",
        "nodes",
        "edges",
        "evidence_count",
        "compacted_calls",
        "context_chars",
        "estimated_input_tokens",
        "estimated_tokens_before",
        "estimated_tokens_after",
        "removed_turns",
        "model_calls",
        "dbos_step_id",
        "input_tokens",
        "output_tokens",
        "total_tokens",
    }
)
_NATIVE = frozenset(
    {
        "openinference.span.kind",
        "llm.model_name",
        "llm.system",
        "llm.provider",
        "tool.name",
        "agent.name",
        "dbos.operation.workflow_id",
        "dbos.operation.type",
        "dbos.application.version",
        "dbos.executor.id",
        "dbos.queue.name",
    }
)
_TOKEN_KEYS = frozenset(
    {
        "llm.token_count.prompt",
        "llm.token_count.completion",
        "llm.token_count.total",
        "llm.token_count.prompt_details.cache_read",
        "llm.token_count.prompt_details.cache_write",
        "llm.token_count.completion_details.reasoning",
    }
)
# Exact key sets and this immutable content-free Resource do not vary by Span.
_NUMERIC_KEYS = _TOKEN_KEYS | {
    "por." + field for field in _COUNTS | {"research_round", "generation", "model_turn"}
}
_STRING_KEYS = _NATIVE | {"por." + field for field in _IDENTITY}
_SAFE_RESOURCE = Resource({"service.name": "public-opinion-research"})
_ALLOWED_SCOPES = frozenset(
    {
        "dbos-tracer",
        __name__,
        "openinference.instrumentation.openai_agents",
        "openinference.instrumentation.langchain",
    }
)
_LABEL = re.compile(r"[\w.:/\-]{1,200}\Z", re.UNICODE)
# Display classification only. Every native diagnostic span is still exported.
_BUSINESS_SPANS = frozenset(
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
    }
)


@dataclass(frozen=True)
class TelemetrySettings:
    """Operator-only settings, excluded from Configuration/workflow inputs."""

    public_key: str
    secret_key: str
    base_url: str
    sample_rate: float = 1.0
    timeout: float = 2.0
    shutdown_seconds: float = 3.0

    @classmethod
    def environment(cls):
        value = cls(
            os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip(),
            os.environ.get("LANGFUSE_SECRET_KEY", "").strip(),
            os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").rstrip(
                "/"
            ),
            float(os.environ.get("POR_TRACE_SAMPLE_RATE", "1")),
            float(os.environ.get("POR_TRACE_TIMEOUT_SECONDS", "2")),
            float(os.environ.get("POR_TRACE_SHUTDOWN_SECONDS", "3")),
        )
        url = urlsplit(value.base_url)
        if (
            not value.public_key
            or not value.secret_key
            or url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not all(
                math.isfinite(x)
                for x in (value.sample_rate, value.timeout, value.shutdown_seconds)
            )
            or not 0 <= value.sample_rate <= 1
            or not 0 < value.timeout <= 30
            or not 0 < value.shutdown_seconds <= 30
        ):
            raise ValueError("Invalid telemetry configuration")
        return value


def _attributes(values):
    result = {}
    for key, value in values.items():
        if key in _NUMERIC_KEYS:
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value >= 0
            ):
                result[key] = value
        elif key in _STRING_KEYS and isinstance(value, str) and _LABEL.fullmatch(value):
            result[key] = value
    return result


def _allowed_span(span):
    scope = span.instrumentation_scope
    return scope is not None and scope.name in _ALLOWED_SCOPES


def _export_context(context):
    """Keep correlation IDs/flags while removing arbitrary vendor tracestate."""
    if context is None:
        return None
    return SpanContext(
        trace_id=context.trace_id,
        span_id=context.span_id,
        is_remote=context.is_remote,
        trace_flags=context.trace_flags,
    )


def safe_span(span):
    """Copy an export span using an allowlist, including status/resource/links.

    DBOS 3.3 embeds exception text in status.description. OI may emit tool
    schemas even with sensitive SDK data disabled. Neither is safe to export.
    """
    attributes = _attributes(span.attributes or {})
    code = (
        StatusCode.ERROR
        if attributes.get("por.status")
        in {"failed", "cancelled", "unresolved", "exception"}
        else span.status.status_code
    )
    if code == StatusCode.ERROR:
        attributes.setdefault("por.failure_kind", "execution_error")
    workflow = attributes.get("dbos.operation.workflow_id")
    if workflow:
        attributes["por.workflow_id"] = workflow
        attributes.setdefault("por.run_id", workflow.split(":", 1)[0])
    if run_id := attributes.get("por.run_id"):
        attributes["session.id"] = run_id
        attributes["langfuse.trace.metadata.run_id"] = run_id
    kind = attributes.get("openinference.span.kind")
    attributes["por.span_layer"] = (
        "business"
        if kind in {"LLM", "AGENT", "TOOL"} or span.name in _BUSINESS_SPANS
        else "framework"
    )
    attributes["langfuse.observation.type"] = {
        "LLM": "generation",
        "AGENT": "agent",
        "TOOL": "tool",
    }.get(kind, "span")
    # SDK-generated names can originate from tool/model output; bound them too.
    name = (
        "research_agent"
        if span.name == "Agent workflow"
        else span.name
        if _LABEL.fullmatch(span.name)
        else "execution"
    )
    if name == "execution" and kind == "AGENT" and (role := attributes.get("por.role")):
        # Display the existing safe identity instead of exporting a free-form
        # Agent name or making all four business roles look identical in the UI.
        name = f"{role[:180]}.agent"
    # Preserve only DBOS's native retry number, never its raw error attribute.
    events = []
    if span.instrumentation_scope and span.instrumentation_scope.name == "dbos-tracer":
        for event in span.events:
            if match := re.fullmatch(r"Step attempt (\d{1,6}) failed", event.name):
                events.append(
                    Event(
                        "dbos.step.retry",
                        {"dbos.step.attempt": int(match[1])},
                        event.timestamp,
                    )
                )
    return ReadableSpan(
        name=name,
        context=_export_context(span.context),
        parent=_export_context(span.parent),
        kind=span.kind,
        resource=_SAFE_RESOURCE,
        attributes=attributes,
        events=events,
        links=[Link(_export_context(link.context)) for link in span.links],
        status=Status(code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=InstrumentationScope(
            span.instrumentation_scope.name, span.instrumentation_scope.version
        )
        if span.instrumentation_scope
        else None,
    )


class SafeExporter(SpanExporter):
    """Sanitize every egress path, with no remote work on the calling thread."""

    def __init__(self, exporter):
        self.exporter = exporter
        self._lock = threading.Lock()
        self._closed = False
        self._delivery = {
            "attempted_spans": 0,
            "acknowledged_spans": 0,
            "failed_spans": 0,
            "export_batches": 0,
            "failed_batches": 0,
        }

    def snapshot(self):
        """Process-local delivery counters; an ACK is not backend persistence."""
        with self._lock:
            return {**self._delivery, "closed": self._closed}

    def export(self, spans):
        safe = []
        try:
            safe = [safe_span(s) for s in spans if _allowed_span(s)]
            result = self.exporter.export(safe)
        except Exception:
            result = SpanExportResult.FAILURE
        with self._lock:
            self._delivery["attempted_spans"] += len(safe)
            self._delivery["export_batches"] += 1
            success = result == SpanExportResult.SUCCESS
            self._delivery["acknowledged_spans" if success else "failed_spans"] += len(
                safe
            )
            self._delivery["failed_batches"] += int(not success)
        if not success:
            _logger.warning("Trace export failed; research continues")
        return result

    def shutdown(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.exporter.shutdown()
        except Exception:
            _logger.warning("Trace exporter shutdown incomplete")

    def force_flush(self, timeout_millis=30000):
        try:
            return self.exporter.force_flush(timeout_millis)
        except Exception:
            _logger.warning("Trace exporter flush incomplete")
            return False


class CorrelationProcessor(SpanProcessor):
    """Attach existing run-local identity to native spans without retaining it."""

    def on_start(self, span, parent_context=None):
        try:
            values = dict(_identity.get())
            harness = _harness.get()
            if harness is not None:
                values["model_turn"] = harness.model_calls
                values["estimated_input_tokens"] = getattr(
                    harness, "estimated_input_tokens", None
                )
            from dbos import DBOS

            if DBOS.step_id is not None:
                values["dbos_step_id"] = DBOS.step_id
            span.set_attributes(_attributes({"por." + k: v for k, v in values.items()}))
        except Exception:
            pass


class SDKUsageEnrichment(TracingProcessor):
    """Bridge SDK 0.24's content-free usage to OI 2.6's existing LLM span.

    OI ignores ResponseSpanData.usage when response content is withheld. This
    processor runs before OI ends the span; it creates no spans or usage ledger.
    """

    def on_span_end(self, span):
        try:
            usage = getattr(span.span_data, "usage", None)
            current = trace.get_current_span()
            if (
                not isinstance(usage, dict)
                or getattr(current, "attributes", {}).get("openinference.span.kind")
                != "LLM"
            ):
                return
            if not any(
                isinstance(usage.get(k), (int, float)) and usage[k] > 0
                for k in ("input_tokens", "output_tokens", "total_tokens")
            ):
                return  # SDK zero defaults do not prove provider token usage.
            values = {}
            harness = _harness.get()
            model = getattr(
                getattr(getattr(harness, "_agent", None), "model", None), "model", None
            )
            if isinstance(model, str):
                values["llm.model_name"] = model
            for source, target in (
                ("input_tokens", "prompt"),
                ("output_tokens", "completion"),
                ("total_tokens", "total"),
            ):
                if source in usage:
                    values["llm.token_count." + target] = usage[source]
            for source, target, fields in (
                (
                    "input_tokens_details",
                    "prompt_details",
                    {
                        "cached_tokens": "cache_read",
                        "cache_write_tokens": "cache_write",
                    },
                ),
                (
                    "output_tokens_details",
                    "completion_details",
                    {"reasoning_tokens": "reasoning"},
                ),
            ):
                details = usage.get(source)
                if isinstance(details, dict):
                    for key, field in fields.items():
                        if key in details:
                            values[f"llm.token_count.{target}.{field}"] = details[key]
            current.set_attributes(_attributes(values))
        except Exception:
            pass

    def on_trace_start(self, trace):
        pass

    def on_trace_end(self, trace):
        pass

    def on_span_start(self, span):
        pass

    def force_flush(self):
        pass

    def shutdown(self):
        pass


def initialize_telemetry():
    """Initialize once per process; no auth probe and no network on startup."""
    global _attempted, _provider, _client, _settings, _active, _exporter
    with _lock:
        if _attempted:
            return _active
        _attempted = True
        if os.environ.get("POR_TRACING_ENABLED", "").lower() not in {
            "1",
            "true",
            "yes",
        }:
            return False
        provider = client = instrumentor = None
        try:
            from agents import set_trace_processors, set_tracing_disabled
            from langfuse import Langfuse
            from openinference.instrumentation import TraceConfig
            from openinference.instrumentation.openai_agents import (
                OpenAIAgentsInstrumentor,
            )
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            cfg = TelemetrySettings.environment()
            # A preconfigured global exporter could bypass our egress policy.
            # Fail open for business, closed for telemetry in that situation.
            if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
                raise ValueError("OTel provider already configured")
            instrumentor = OpenAIAgentsInstrumentor()
            if instrumentor.is_instrumented_by_opentelemetry:
                raise ValueError("Agents instrumentation already configured")
            provider = TracerProvider(
                resource=_SAFE_RESOURCE,
                sampler=ParentBased(TraceIdRatioBased(cfg.sample_rate)),
                shutdown_on_exit=False,
            )
            provider.add_span_processor(CorrelationProcessor())
            auth = base64.b64encode(
                f"{cfg.public_key}:{cfg.secret_key}".encode()
            ).decode()
            exporter = SafeExporter(
                OTLPSpanExporter(
                    endpoint=cfg.base_url + "/api/public/otel/v1/traces",
                    headers={
                        "Authorization": "Basic " + auth,
                        "x-langfuse-ingestion-version": "4",
                    },
                    timeout=cfg.timeout,
                )
            )
            client = Langfuse(
                public_key=cfg.public_key,
                secret_key=cfg.secret_key,
                base_url=cfg.base_url,
                tracer_provider=provider,
                tracing_enabled=True,
                span_exporter=exporter,
                timeout=math.ceil(cfg.timeout),
                should_export_span=_allowed_span,
                debug=False,
            )
            # Replace the default OpenAI uploader before enabling native SDK spans.
            set_trace_processors([SDKUsageEnrichment()])
            privacy = TraceConfig(
                hide_inputs=True,
                hide_outputs=True,
                hide_input_messages=True,
                hide_output_messages=True,
                hide_llm_invocation_parameters=True,
                hide_llm_tools=True,
                hide_prompts=True,
                hide_retrieval_documents=True,
            )
            from .langchain import configure_model_tracing

            configure_model_tracing(provider, privacy)
            instrumentor.instrument(
                tracer_provider=provider,
                exclusive_processor=False,
                config=privacy,
            )
            if not instrumentor.is_instrumented_by_opentelemetry:
                raise ValueError("Agents instrumentation failed")
            trace.set_tracer_provider(provider)
            if trace.get_tracer_provider() is not provider:
                raise ValueError("OTel provider registration did not take effect")
            set_tracing_disabled(False)
            _provider, _client, _settings, _active = provider, client, cfg, True
            _exporter = exporter
            atexit.register(shutdown_telemetry)
        except Exception:
            _logger.warning(
                "Tracing unavailable; research continues (configuration/initialization)"
            )
            if provider is not None:
                # A failed initialization must never enable the default SDK uploader.
                from agents import set_trace_processors, set_tracing_disabled

                set_trace_processors([])
                set_tracing_disabled(True)
                if (
                    instrumentor is not None
                    and instrumentor.is_instrumented_by_opentelemetry
                ):
                    instrumentor.uninstrument()
                _close_in_background(client, provider, 0)
        return _active


def telemetry_enabled():
    return _active


def delivery_health():
    """Read bounded numeric export facts without querying or replaying a Run.

    Queue occupancy/drops belong to OTel's native SDK metrics. Missing or open
    spans and SIGKILL losses cannot be inferred from these delivery counters.
    """
    return {"enabled": _active, **(_exporter.snapshot() if _exporter else {})}


def _close_in_background(client, provider, seconds):
    def close():
        try:
            if client is not None:
                client.shutdown()
        except Exception:
            _logger.warning("Trace shutdown incomplete")
        finally:
            if provider is not None:
                try:
                    provider.shutdown()
                except Exception:
                    _logger.warning("Trace provider shutdown incomplete")

    worker = threading.Thread(target=close, name="por-trace-shutdown", daemon=True)
    worker.start()
    worker.join(seconds)
    if seconds and worker.is_alive():
        _logger.warning("Trace shutdown deadline reached")


def shutdown_telemetry():
    """Bound the entire SDK/provider shutdown, including unavailable exporters."""
    global _active
    with _lock:
        if not _active:
            return
        _active = False
    atexit.unregister(shutdown_telemetry)
    _close_in_background(_client, _provider, _settings.shutdown_seconds)


@contextmanager
def correlation(*, harness=None, **values):
    """Bind identity with contextvars so parallel roles stay independent."""
    token = _identity.set(
        {**_identity.get(), **{k: v for k, v in values.items() if k in _IDENTITY}}
    )
    harness_token = _harness.set(harness if harness is not None else _harness.get())
    try:
        annotate(**values)
        yield
    finally:
        _harness.reset(harness_token)
        _identity.reset(token)


def annotate(**values):
    """Enrich an existing native span; never start another model/tool span."""
    if _active:
        try:
            span = trace.get_current_span()
            span.set_attributes(_attributes({"por." + k: v for k, v in values.items()}))
            if values.get("status") in {
                "failed",
                "cancelled",
                "unresolved",
                "exception",
            }:
                span.set_status(StatusCode.ERROR)
        except Exception:
            pass


@contextmanager
def business_span(name, **values):
    """Observe one business seam without recording raw exceptions."""
    span = None
    manager = None
    if _active:
        try:
            manager = trace.get_tracer(__name__).start_as_current_span(
                name,
                record_exception=False,
                set_status_on_exception=False,
            )
            span = manager.__enter__()
            annotate(**values)
        except Exception:
            manager = None
    try:
        yield span
    except BaseException as exc:
        annotate(
            status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
            error_type=type(exc).__name__,
        )
        if span is not None:
            try:
                span.set_status(StatusCode.ERROR)
            except Exception:
                pass
        raise
    finally:
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass


def observed(name):
    """Decorate an async business boundary, leaving Runner/tool loops untouched."""

    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            with business_span(name):
                return await function(*args, **kwargs)

        return wrapped

    return decorate


def dbos_scope(function):
    """Use DBOS's persisted propagation, without adding journal commands."""

    @wraps(function)
    async def wrapped(run_id, *args, **kwargs):
        if not _active:
            return await function(run_id, *args, **kwargs)
        from dbos import DBOS, PropagateOtelContext

        with (
            correlation(run_id=run_id, workflow_id=DBOS.workflow_id),
            PropagateOtelContext(),
        ):
            return await function(run_id, *args, **kwargs)

    return wrapped


def enqueue_context():
    """DBOSClient's official propagation field; excludes incoming HTTP baggage."""
    if _active:
        from opentelemetry.context import get_current

        return {"otel_context": get_current()}
    return {}
