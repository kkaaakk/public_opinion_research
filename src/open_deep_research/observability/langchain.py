"""Model-only adapter over the official OpenInference LangChain callback.

OI's public instrument/uninstrument and Core's public callback manager/hook
provide the configured handler. Only model lifecycle events are forwarded;
OI still owns span creation, timing and provider usage. No private tracer,
callback-manager monkeypatch, chain/tool recorder or second Provider is used.
"""

import asyncio
import logging
from contextvars import ContextVar

from langchain_core.callbacks import BaseCallbackHandler, CallbackManager
from langchain_core.tracers.context import register_configure_hook

_logger = logging.getLogger(__name__)
_delegate = None


class ModelOnlyHandler(BaseCallbackHandler):
    """Forward Core model callbacks inline, preserving the current OTel parent."""

    run_inline = True
    ignore_chain = True
    ignore_retriever = True
    ignore_agent = True
    ignore_custom_event = True

    def _start(self, event, serialized, inputs, *, run_id, **kwargs):
        from .telemetry import telemetry_enabled

        if not telemetry_enabled() or _delegate is None:
            return
        try:
            # LC chain/tool parents are intentionally not recorded. Let OI use
            # the native DBOS/SDK/business OTel context for each real request.
            kwargs.pop("parent_run_id", None)
            kwargs["name"] = "langchain_generation"
            getattr(_delegate, event)(serialized, inputs, run_id=run_id, **kwargs)
            try:
                owner = asyncio.current_task()
            except RuntimeError:
                owner = None
            if owner is not None:
                delegate = _delegate

                def close_unfinished(task):
                    # Core's outer agenerate cancellation can bypass its error
                    # callback. End only this already-started observation when
                    # its actual inline-callback owner is done. Never cancel,
                    # wait for, retry or reconstruct a model request.
                    try:
                        span = delegate.get_span(run_id)
                        if span is None:
                            return
                        cancelled = task.cancelled()
                        error = (
                            asyncio.CancelledError() if cancelled else task.exception()
                        )
                        span.set_attribute("por.effect_status", "unknown")
                        span.set_attribute("por.failure_kind", "model_callback_missing")
                        self._finish(
                            run_id,
                            error=error or RuntimeError("Model lifecycle incomplete"),
                            delegate=delegate,
                            status="cancelled"
                            if cancelled
                            else "failed"
                            if error
                            else "unresolved",
                        )
                    except Exception:
                        _logger.warning("Model tracing incomplete; research continues")

                owner.add_done_callback(close_unfinished)
        except Exception:
            _logger.warning("Model tracing unavailable; research continues")

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self._start(
            "on_chat_model_start", serialized, messages, run_id=run_id, **kwargs
        )

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs):
        self._start("on_llm_start", serialized, prompts, run_id=run_id, **kwargs)

    def on_llm_end(self, response, *, run_id, **kwargs):
        self._finish(run_id, response=response)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._finish(run_id, error=error, response=kwargs.get("response"))

    def _finish(self, run_id, *, response=None, error=None, delegate=None, status=None):
        from .telemetry import _attributes

        delegate = delegate if delegate is not None else _delegate
        if delegate is None:
            return
        try:
            span = delegate.get_span(run_id)
            if span is None:
                return
            groups = getattr(response, "generations", [])
            message = (
                getattr(groups[0][0], "message", None) if groups and groups[0] else None
            )
            # OI reads invocation metadata/llm_output, but some Core providers
            # report the actual model only on the returned AIMessage.
            metadata = getattr(message, "response_metadata", None) or {}
            span.set_attributes(
                _attributes(
                    {
                        "llm.model_name": metadata.get("model_name")
                        or metadata.get("model")
                    }
                )
            )
            span.set_attribute(
                "por.status",
                status
                if status is not None
                else "succeeded"
                if error is None
                else "cancelled"
                if isinstance(error, asyncio.CancelledError)
                else "failed",
            )
            if error is None:
                delegate.on_llm_end(response, run_id=run_id)
            else:
                # OI 0.1.80's error callback ignores Core's partial LLMResult.
                # Copy only its canonical reported numbers onto the same span.
                usage = getattr(message, "usage_metadata", None) or {}
                values = {"por.error_type": type(error).__name__}
                for source, target in (
                    ("input_tokens", "prompt"),
                    ("output_tokens", "completion"),
                    ("total_tokens", "total"),
                ):
                    if source in usage:
                        values["llm.token_count." + target] = usage[source]
                for source, target, fields in (
                    (
                        "input_token_details",
                        "prompt_details",
                        {"cache_read": "cache_read", "cache_creation": "cache_write"},
                    ),
                    (
                        "output_token_details",
                        "completion_details",
                        {"reasoning": "reasoning"},
                    ),
                ):
                    for key, field in fields.items():
                        if key in usage.get(source, {}):
                            values[f"llm.token_count.{target}.{field}"] = usage[source][
                                key
                            ]
                span.set_attributes(_attributes(values))
                delegate.on_llm_error(error, run_id=run_id)
        except Exception:
            _logger.warning("Model tracing incomplete; research continues")


_handler = ContextVar("por_model_trace_handler", default=ModelOnlyHandler())
register_configure_hook(_handler, inheritable=True, handle_class=ModelOnlyHandler)


def configure_model_tracing(provider, config):
    """Acquire OI's callback through public APIs; disable its all-event hook."""
    global _delegate
    from openinference.instrumentation.langchain import LangChainInstrumentor

    instrumentor = LangChainInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        raise ValueError("LangChain instrumentation already configured")
    try:
        instrumentor.instrument(tracer_provider=provider, config=config)
        candidates = [
            handler
            for handler in CallbackManager.configure().handlers
            if type(handler).__module__.startswith(
                "openinference.instrumentation.langchain."
            )
        ]
        if len(candidates) != 1:
            raise ValueError("Model instrumentation unavailable")
        _delegate = candidates[0]
    finally:
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
