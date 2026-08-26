"""Fail-open Agent Observer v0.2 integration.

The business graph only depends on this small adapter.  Agent Observer remains
an optional sidecar: an absent package, an unavailable server, a full queue, or
an event serialization error must never change research behavior.
"""

from __future__ import annotations

import contextvars
import inspect
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable, TypeVar, overload

from open_deep_research.configuration import Configuration

LOGGER = logging.getLogger(__name__)
F = TypeVar("F", bound=Callable[..., Any])

_PUBLIC_OPINION_TOPOLOGY = {
    "edges": [
        ["enrich_query_images", "clarify_with_user"],
        ["clarify_with_user", "write_research_brief"],
        ["write_research_brief", "plan_report_sections"],
        ["plan_report_sections", "research_supervisor"],
        ["research_supervisor", "section_writer"],
        ["section_writer", "write_final_sections"],
        ["write_final_sections", "compile_final_report"],
        ["public_signal_agent", "risk_assessment_agent"],
        ["internal_knowledge_agent", "risk_assessment_agent"],
        ["risk_assessment_agent", "response_strategy_agent"],
    ]
}

try:  # Optional dependency: the application must work without it installed.
    from agent_observer.sdk import (
        AgentObserver,
        get_current_observed_span,
    )
except Exception:  # pragma: no cover - depends on the local environment
    AgentObserver = None  # type: ignore[assignment,misc]
    get_current_observed_span = None  # type: ignore[assignment]

try:  # Agent Observer v0.2's dependency-free LangGraph adapter.
    from integrations.langgraph import (
        observe_node as _observe_node,
    )
    from integrations.langgraph import (
        register_graph_topology as _register_graph_topology,
    )
    from integrations.langgraph import (
        run_context as _run_context,
    )
except Exception:  # pragma: no cover - package layout varies by installation
    try:
        from agent_observer.integrations.langgraph import (
            observe_node as _observe_node,
        )
        from agent_observer.integrations.langgraph import (
            register_graph_topology as _register_graph_topology,
        )
        from agent_observer.integrations.langgraph import (
            run_context as _run_context,
        )
    except Exception:  # pragma: no cover - optional dependency or old SDK
        _observe_node = None
        _register_graph_topology = None
        _run_context = None


def observer_available() -> bool:
    """Return whether the v0.2 SDK and its LangGraph adapter are importable."""
    return all(
        dependency is not None
        for dependency in (AgentObserver, _observe_node, _register_graph_topology, _run_context)
    )


@overload
def observe_graph_node(
    function: F,
    *,
    name: str | None = None,
    kind: str = "graph_node",
    upstream_nodes: list[str] | tuple[str, ...] | str | None = None,
) -> F: ...


@overload
def observe_graph_node(
    *,
    name: str | None = None,
    kind: str = "graph_node",
    upstream_nodes: list[str] | tuple[str, ...] | str | None = None,
) -> Callable[[F], F]: ...


def observe_graph_node(
    function: F | None = None,
    *,
    name: str | None = None,
    kind: str = "graph_node",
    upstream_nodes: list[str] | tuple[str, ...] | str | None = None,
) -> F | Callable[[F], F]:
    """Decorate a graph node when the v0.2 adapter is available.

    A no-op fallback is intentional.  It lets the project import and run in a
    clean environment without requiring Agent Observer at install time.
    """
    if _observe_node is None:
        if function is not None:
            return function

        def identity(target: F) -> F:
            return target

        return identity
    return _observe_node(  # type: ignore[operator,return-value]
        function,
        name=name,
        kind=kind,
        upstream_nodes=upstream_nodes,
    )


def register_graph_topology(
    topology: Mapping[str, Any] | list[Any] | tuple[Any, ...] | None = None,
    *,
    nodes: list[str] | tuple[str, ...] | None = None,
    edges: list[Any] | tuple[Any, ...] | None = None,
    run: Any = None,
) -> Any:
    """Register stable graph dependencies, or safely do nothing."""
    if _register_graph_topology is None:
        return run
    try:
        return _register_graph_topology(topology, nodes=nodes, edges=edges, run=run)
    except Exception:  # pragma: no cover - sidecar compatibility guard
        LOGGER.debug("Agent Observer topology registration failed", exc_info=True)
        return run


def _config_value(config: Mapping[str, Any] | None, key: str, default: Any) -> Any:
    """Read a config value without exposing the rest of LangGraph config."""
    try:
        configuration = Configuration.from_runnable_config(config or {})
        return getattr(configuration, key)
    except Exception:
        return default


def _configurable(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    value = (config or {}).get("configurable", {})
    return value if isinstance(value, Mapping) else {}


def _extract_prompt(value: Any) -> str:
    """Extract a bounded user-facing prompt without copying graph state."""
    if isinstance(value, str):
        return value[:2_000]
    if isinstance(value, Mapping):
        messages = value.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                content = getattr(message, "content", None)
                if content is None and isinstance(message, Mapping):
                    content = message.get("content")
                if content:
                    return str(content)[:2_000]
        for key in ("topic", "research_brief", "input"):
            if value.get(key):
                return str(value[key])[:2_000]
    return "public opinion research"


def _provider_for_model(model: Any) -> str | None:
    if not isinstance(model, str) or not model:
        return None
    return model.split(":", 1)[0] if ":" in model else None


def _metadata_for_run(config: Mapping[str, Any] | None) -> dict[str, Any]:
    configurable = _configurable(config)
    metadata: dict[str, Any] = {
        "framework": "langgraph",
        "workflow": "public_opinion_research",
    }
    for key in ("thread_id", "checkpoint_id"):
        value = configurable.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            metadata[key] = str(value)
    git_commit = os.getenv("GIT_COMMIT")
    if git_commit:
        metadata["git_commit"] = git_commit[:128]
    return metadata


@dataclass
class _ObserverRun:
    """One sidecar client and Run for one graph invocation."""

    observer: Any
    run: Any
    started_at: float
    timeout: float

    def finish(self, result: Any) -> None:
        final_report = _find_final_report(result)
        answer = "final_report_generated" if final_report is not None else "completed_without_report"
        facts: dict[str, Any] = {"final_report_present": final_report is not None}
        if final_report is not None:
            facts["output_bytes"] = len(final_report.encode("utf-8"))
        self.run.finish(
            answer,
            duration_ms=_elapsed_ms(self.started_at),
            stop_reason="completed",
            **facts,
        )

    def fail(self, error: BaseException) -> None:
        self.run.fail(error, duration_ms=_elapsed_ms(self.started_at))

    def close(self) -> None:
        try:
            self.observer.close(timeout=max(0.05, min(self.timeout, 1.0)))
        except Exception:  # pragma: no cover - sidecar guard
            LOGGER.debug("Agent Observer close failed", exc_info=True)


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - started_at) * 1_000))


def _find_final_report(value: Any) -> str | None:
    if isinstance(value, Mapping):
        final_report = value.get("final_report")
        if isinstance(final_report, str):
            return final_report
        for nested in value.values():
            found = _find_final_report(nested)
            if found is not None:
                return found
    return None


def _start_observed_run(value: Any, config: Mapping[str, Any] | None) -> _ObserverRun | None:
    """Create one Run, with every setup failure converted to a no-op."""
    if not observer_available() or not _config_value(config, "agent_observer_enabled", False):
        return None
    try:
        model = _configurable(config).get("research_model")
        timeout = float(_config_value(config, "agent_observer_timeout", 0.35))
        observer = AgentObserver(
            endpoint=str(_config_value(config, "agent_observer_endpoint", "http://127.0.0.1:8766")),
            project=str(_config_value(config, "agent_observer_project", "public-opinion-research")),
            enabled=True,
            timeout=max(0.01, timeout),
            capture_full_tool_content=False,
        )
        run_holder: dict[str, Any] = {}

        def create_run() -> None:
            run_holder["run"] = observer.start_run(
                prompt=_extract_prompt(value),
                metadata=_metadata_for_run(config),
                model=model if isinstance(model, str) else None,
                provider=_provider_for_model(model),
            )

        # AgentObserver.start_run() binds its Run in a contextvar as a
        # convenience. Create it in an isolated copy so the host task does not
        # retain a stale Run after the native graph finishes.
        contextvars.copy_context().run(create_run)
        run = run_holder["run"]
        register_graph_topology(_PUBLIC_OPINION_TOPOLOGY, run=run)
        return _ObserverRun(observer=observer, run=run, started_at=time.perf_counter(), timeout=timeout)
    except Exception:  # pragma: no cover - optional sidecar guard
        LOGGER.debug("Agent Observer setup failed; continuing without telemetry", exc_info=True)
        return None


class ObserverRunLifecycle:
    """Bind one lazily-created Observer Run to a native graph's node calls."""

    def __init__(self) -> None:
        """Create an isolated lifecycle for one graph factory invocation."""
        self._observed: _ObserverRun | None = None
        self._lock = threading.Lock()
        self._closed = False

    def _ensure_run(self, value: Any, config: Mapping[str, Any] | None) -> _ObserverRun | None:
        with self._lock:
            if self._closed:
                return self._observed
            if self._observed is None:
                self._observed = _start_observed_run(value, config)
            return self._observed

    @staticmethod
    def _node_inputs(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> tuple[Any, Mapping[str, Any] | None]:
        value = args[0] if args else None
        config = kwargs.get("config")
        if config is None and len(args) > 1 and isinstance(args[1], Mapping):
            config = args[1]
        return value, config

    @staticmethod
    def _is_terminal_command(result: Any) -> bool:
        goto = getattr(result, "goto", None)
        if isinstance(goto, str):
            return goto in {"__end__", "END"}
        if isinstance(goto, (list, tuple, set)):
            return any(item in {"__end__", "END"} for item in goto)
        return False

    def finish(self, result: Any) -> None:
        """Finish and close the Run after a successful terminal node."""
        with self._lock:
            observed = self._observed
            self._closed = True
        if observed is not None:
            observed.finish(result)
            observed.close()

    def fail(self, error: BaseException) -> None:
        """Fail and close the Run after an unrecoverable node exception."""
        with self._lock:
            observed = self._observed
            self._closed = True
        if observed is not None:
            observed.fail(error)
            observed.close()

    def wrap_node(
        self,
        name: str,
        function: F,
        *,
        finish: bool = False,
        terminal: bool = False,
    ) -> F:
        """Return a native graph node wrapper that binds this lifecycle."""
        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                value, config = self._node_inputs(args, kwargs)
                observed = self._ensure_run(value, config)
                if observed is None or _run_context is None:
                    return await function(*args, **kwargs)
                try:
                    with _run_context(observed.run):
                        result = await function(*args, **kwargs)
                    if finish or (terminal and self._is_terminal_command(result)):
                        self.finish(result)
                    return result
                except BaseException as error:
                    self.fail(error)
                    raise

            return async_wrapper  # type: ignore[return-value]

        @wraps(function)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            value, config = self._node_inputs(args, kwargs)
            observed = self._ensure_run(value, config)
            if observed is None or _run_context is None:
                return function(*args, **kwargs)
            try:
                with _run_context(observed.run):
                    result = function(*args, **kwargs)
                if finish or (terminal and self._is_terminal_command(result)):
                    self.finish(result)
                return result
            except BaseException as error:
                self.fail(error)
                raise

        return sync_wrapper  # type: ignore[return-value]


class ObservedGraph:
    """Runnable-compatible facade that binds one Observer Run to graph execution."""

    def __init__(self, graph: Any):
        """Wrap a compiled LangGraph without changing its underlying behavior."""
        self._graph = graph

    def __getattr__(self, name: str) -> Any:
        """Delegate unsupported Runnable attributes to the compiled graph."""
        return getattr(self._graph, name)

    def get_graph(self, *args: Any, **kwargs: Any) -> Any:
        """Expose the compiled graph topology for Studio and tests."""
        return self._graph.get_graph(*args, **kwargs)

    def with_config(self, *args: Any, **kwargs: Any) -> ObservedGraph:
        """Return a configured facade while retaining Run instrumentation."""
        return ObservedGraph(self._graph.with_config(*args, **kwargs))

    def invoke(self, value: Any, config: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        """Synchronously execute the graph inside one optional Observer Run."""
        observed = _start_observed_run(value, config)
        try:
            if observed is None or _run_context is None:
                return self._graph.invoke(value, config, **kwargs)
            with _run_context(observed.run):
                result = self._graph.invoke(value, config, **kwargs)
            observed.finish(result)
            return result
        except BaseException as error:
            if observed is not None:
                observed.fail(error)
            raise
        finally:
            if observed is not None:
                observed.close()

    async def ainvoke(self, value: Any, config: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        """Asynchronously execute the graph inside one optional Observer Run."""
        observed = _start_observed_run(value, config)
        try:
            if observed is None or _run_context is None:
                return await self._graph.ainvoke(value, config, **kwargs)
            with _run_context(observed.run):
                result = await self._graph.ainvoke(value, config, **kwargs)
            observed.finish(result)
            return result
        except BaseException as error:
            if observed is not None:
                observed.fail(error)
            raise
        finally:
            if observed is not None:
                observed.close()

    def stream(self, value: Any, config: Mapping[str, Any] | None = None, **kwargs: Any) -> Iterator[Any]:
        """Stream graph updates while keeping the Run context alive."""
        observed = _start_observed_run(value, config)
        last_item: Any = None
        try:
            if observed is None or _run_context is None:
                yield from self._graph.stream(value, config, **kwargs)
                return
            with _run_context(observed.run):
                for last_item in self._graph.stream(value, config, **kwargs):
                    yield last_item
            observed.finish(last_item)
        except BaseException as error:
            if observed is not None:
                observed.fail(error)
            raise
        finally:
            if observed is not None:
                observed.close()

    async def astream(self, value: Any, config: Mapping[str, Any] | None = None, **kwargs: Any) -> AsyncIterator[Any]:
        """Asynchronously stream graph updates with one Run boundary."""
        observed = _start_observed_run(value, config)
        last_item: Any = None
        try:
            if observed is None or _run_context is None:
                async for item in self._graph.astream(value, config, **kwargs):
                    yield item
                return
            with _run_context(observed.run):
                async for item in self._graph.astream(value, config, **kwargs):
                    last_item = item
                    yield item
            observed.finish(last_item)
        except BaseException as error:
            if observed is not None:
                observed.fail(error)
            raise
        finally:
            if observed is not None:
                observed.close()


def _current_span() -> Any:
    if get_current_observed_span is None:
        return None
    try:
        return get_current_observed_span()
    except Exception:  # pragma: no cover - sidecar guard
        return None


def _usage_value(usage: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _response_usage(response: Any) -> dict[str, int | None]:
    usage: Mapping[str, Any] = {}
    candidate = getattr(response, "usage_metadata", None)
    if isinstance(candidate, Mapping):
        usage = candidate
    if not usage:
        metadata = getattr(response, "response_metadata", None)
        if isinstance(metadata, Mapping):
            nested = metadata.get("token_usage") or metadata.get("usage") or metadata.get("usage_metadata")
            if isinstance(nested, Mapping):
                usage = nested
            else:
                usage = metadata
    return {
        "input_tokens": _usage_value(usage, "input_tokens", "prompt_tokens"),
        "output_tokens": _usage_value(usage, "output_tokens", "completion_tokens"),
        "cache_read_tokens": _usage_value(usage, "cache_read_tokens", "cached_tokens", "cache_read_input_tokens"),
        "cache_write_tokens": _usage_value(usage, "cache_write_tokens", "cache_creation_input_tokens"),
    }


def _stop_reason(response: Any) -> str | None:
    metadata = getattr(response, "response_metadata", None)
    if isinstance(metadata, Mapping):
        for key in ("stop_reason", "finish_reason", "stop"):
            value = metadata.get(key)
            if value is not None:
                return str(value)
    if getattr(response, "tool_calls", None):
        return "tool_call"
    return None


async def observe_model_ainvoke(runnable: Any, payload: Any, **kwargs: Any) -> Any:
    """Invoke a model and attach only real usage/timing facts to current Span."""
    span = _current_span()
    request_id = f"req_{uuid.uuid4().hex}"
    model = kwargs.pop("observer_model", None)
    provider = kwargs.pop("observer_provider", None) or _provider_for_model(model)
    started_at = time.perf_counter()
    if span is not None:
        try:
            span.model_request(request_id=request_id, model=model, provider=provider)
        except Exception:  # pragma: no cover - sidecar guard
            LOGGER.debug("Agent Observer model request failed", exc_info=True)
    try:
        response = await runnable.ainvoke(payload, **kwargs)
    except BaseException as error:
        if span is not None:
            try:
                span.model_response(
                    request_id=request_id,
                    duration_ms=_elapsed_ms(started_at),
                    stop_reason="error",
                    model=model,
                    provider=provider,
                    error_type=type(error).__name__,
                )
            except Exception:  # pragma: no cover - sidecar guard
                LOGGER.debug("Agent Observer model error event failed", exc_info=True)
        raise
    if span is not None:
        try:
            usage = _response_usage(response)
            span.model_response(
                request_id=request_id,
                duration_ms=_elapsed_ms(started_at),
                stop_reason=_stop_reason(response),
                model=model,
                provider=provider,
                **usage,
            )
        except Exception:  # pragma: no cover - sidecar guard
            LOGGER.debug("Agent Observer model response failed", exc_info=True)
    return response


def _safe_args_summary(args: Any) -> dict[str, Any]:
    """Describe tool arguments without copying business query/result text."""
    if not isinstance(args, Mapping):
        return {"type": type(args).__name__}
    return {
        "keys": sorted(str(key) for key in args.keys()),
        "value_lengths": {
            str(key): len(str(value))
            for key, value in args.items()
            if value is not None
        },
    }


def record_tool_call(tool: str, *, tool_call_id: str | None = None, args: Any = None) -> None:
    """Record a privacy-preserving Tool Call on the current Agent Span."""
    span = _current_span()
    if span is None:
        return
    try:
        span.tool_call(
            tool,
            tool_call_id=tool_call_id,
            args_summary=_safe_args_summary(args),
        )
    except Exception:  # pragma: no cover - sidecar guard
        LOGGER.debug("Agent Observer tool call failed", exc_info=True)


def record_tool_result(
    tool: str,
    *,
    tool_call_id: str | None = None,
    success: bool | None,
    duration_ms: int,
    result: Any = None,
) -> None:
    """Record Tool timing and sizes while withholding raw result content."""
    span = _current_span()
    if span is None:
        return
    try:
        try:
            raw_bytes = len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))
        except Exception:
            raw_bytes = len(str(result).encode("utf-8"))
        span.tool_result(
            tool,
            tool_call_id=tool_call_id,
            success=success,
            duration_ms=duration_ms,
            raw_bytes=raw_bytes,
            context_bytes=raw_bytes,
        )
    except Exception:  # pragma: no cover - sidecar guard
        LOGGER.debug("Agent Observer tool result failed", exc_info=True)


__all__ = [
    "ObservedGraph",
    "ObserverRunLifecycle",
    "observe_graph_node",
    "observe_model_ainvoke",
    "observer_available",
    "record_tool_call",
    "record_tool_result",
    "register_graph_topology",
]
