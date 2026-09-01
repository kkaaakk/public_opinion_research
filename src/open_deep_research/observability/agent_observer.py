"""Fail-open Agent Observer v0.2 integration.

The business graph only depends on this small adapter.  Agent Observer remains
an optional sidecar: an absent package, an unavailable server, a full queue, or
an event serialization error must never change research behavior.
"""

from __future__ import annotations

import contextvars
import hashlib
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

_MODEL_BOUNDARY_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "public_opinion_model_boundary_depth",
    default=0,
)
_TOOL_BOUNDARY_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "public_opinion_tool_boundary_depth",
    default=0,
)
_MAX_RESULT_SIZE_PROBE = 64 * 1024

_PUBLIC_OPINION_TOPOLOGY = {
    "edges": [
        ["enrich_query_images", "clarify_with_user"],
        ["clarify_with_user", "write_research_brief"],
        ["write_research_brief", "plan_report_sections"],
        ["plan_report_sections", "research_phase"],
        ["research_phase", "section_writer"],
        ["section_writer", "write_final_sections"],
        ["write_final_sections", "compile_final_report"],
        ["public_signal_agent", "research_review"],
        ["internal_knowledge_agent", "research_review"],
        ["research_review", "public_signal_agent"],
        ["research_review", "internal_knowledge_agent"],
        ["research_review", "risk_assessment_agent"],
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


def _is_graph_interrupt(error: BaseException) -> bool:
    """Recognize LangGraph's resumable interrupt without importing LangGraph."""
    return any(cls.__name__ == "GraphInterrupt" for cls in type(error).__mro__)


def _is_interrupt_result(value: Any) -> bool:
    """Return whether a graph result is the public LangGraph interrupt shape."""
    if isinstance(value, Mapping):
        if "__interrupt__" in value:
            return True
        return any(_is_interrupt_result(nested) for nested in value.values())
    if isinstance(value, (list, tuple)):
        return any(_is_interrupt_result(item) for item in value)
    return False


def _runtime_execution_info(config: Mapping[str, Any] | None) -> Any:
    configurable = _configurable(config)
    runtime = configurable.get("__pregel_runtime")
    return getattr(runtime, "execution_info", None)


def _runtime_identifier(config: Mapping[str, Any] | None, key: str) -> str | None:
    configurable = _configurable(config)
    info = _runtime_execution_info(config)
    value = getattr(info, key, None)
    if value is None:
        value = configurable.get(key)
    if value is None and isinstance((config or {}).get("metadata"), Mapping):
        value = (config or {})["metadata"].get(key)
    if isinstance(value, bool) or value is None:
        return None
    value = str(value)
    if not value or value.lower() == "none":
        return None
    return value[:160]


def _is_runtime_resuming(config: Mapping[str, Any] | None) -> bool:
    return _configurable(config).get("__pregel_resuming") is True


def _logical_run_id(config: Mapping[str, Any] | None) -> str:
    """Derive a stable logical workflow id without persisting a Run object."""
    configurable = _configurable(config)
    for source in (configurable, (config or {}).get("metadata", {})):
        if isinstance(source, Mapping):
            explicit = source.get("observer_logical_run_id")
            if isinstance(explicit, str) and explicit.strip():
                return explicit.strip()[:160]

    thread_id = _runtime_identifier(config, "thread_id")
    checkpoint_id = _runtime_identifier(config, "checkpoint_id")
    if thread_id and checkpoint_id:
        seed = f"public-opinion:{thread_id}:{checkpoint_id}"
        return f"workflow_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]}"
    return f"workflow_{uuid.uuid4().hex}"


def _correlation_metadata(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build bounded, serializable invocation/segment correlation facts."""
    resuming = _is_runtime_resuming(config)
    metadata: dict[str, Any] = {
        "logical_run_id": _logical_run_id(config),
        "execution_segment_id": f"segment_{uuid.uuid4().hex}",
        "invocation_kind": "resume" if resuming else "start",
        "resume": resuming,
    }
    for key in ("thread_id", "checkpoint_id", "task_id"):
        value = _runtime_identifier(config, key)
        if value is not None:
            metadata[key] = value
    configurable = _configurable(config)
    explicit_resume_of = configurable.get("observer_resume_of_run_id")
    if explicit_resume_of is None and isinstance((config or {}).get("metadata"), Mapping):
        explicit_resume_of = (config or {})["metadata"].get("observer_resume_of_run_id")
    if isinstance(explicit_resume_of, (str, int)) and not isinstance(explicit_resume_of, bool):
        metadata["resume_of_run_id"] = str(explicit_resume_of)[:160]
    return metadata


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


def _metadata_for_run(
    config: Mapping[str, Any] | None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
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
    metadata.update(_correlation_metadata(config))
    if extra:
        metadata.update(dict(extra))
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

    def interrupt(self, error: BaseException | None = None) -> None:
        """Close this physical segment as paused, never as failed."""
        interrupt_method = getattr(self.run, "interrupt", None)
        if callable(interrupt_method):
            interrupt_method(
                reason="langgraph_interrupt",
                interrupt_type=type(error).__name__ if error is not None else "GraphInterrupt",
                duration_ms=_elapsed_ms(self.started_at),
                resume_available=True,
            )
        else:  # pragma: no cover - only for an old mixed SDK installation
            self.run.record(
                "run_interrupted",
                reason="langgraph_interrupt",
                interrupt_type=type(error).__name__ if error is not None else "GraphInterrupt",
                duration_ms=_elapsed_ms(self.started_at),
                resume_available=True,
            )

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


def _start_observed_run(
    value: Any,
    config: Mapping[str, Any] | None,
    *,
    correlation: Mapping[str, Any] | None = None,
) -> _ObserverRun | None:
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
                metadata=_metadata_for_run(config, correlation),
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
    """Bind short-lived Observer segments to native graph node executions.

    LangGraph may reuse one compiled graph for many invocations.  Active
    segments are keyed by the stable thread id only while they are running;
    no Observer Run object is persisted in graph state or in a process-global
    registry.  An interrupt removes the active segment, so a later
    ``Command(resume=...)`` creates a linked physical segment with the same
    deterministic logical workflow id.
    """

    def __init__(self) -> None:
        """Create an isolated, bounded lifecycle for one native graph object."""
        self._active: dict[str, _ObserverRun] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _identity(config: Mapping[str, Any] | None) -> tuple[str, dict[str, Any]]:
        thread_id = _runtime_identifier(config, "thread_id")
        key = f"thread:{thread_id}" if thread_id else "thread:__unkeyed__"
        correlation = _correlation_metadata(config)
        return key, correlation

    def _ensure_run(self, value: Any, config: Mapping[str, Any] | None) -> _ObserverRun | None:
        key, correlation = self._identity(config)
        resuming = correlation.get("resume") is True
        with self._lock:
            observed = self._active.get(key)
            if observed is not None and not resuming:
                return observed
            if observed is not None and resuming:
                # A well-formed interrupt removes the previous segment.  If a
                # mixed runtime delivers resume before that cleanup, close the
                # stale segment rather than append events to a terminal Run.
                self._active.pop(key, None)
                observed.interrupt()
                observed.close()
            observed = _start_observed_run(value, config, correlation=correlation)
            if observed is not None:
                self._active[key] = observed
            return observed

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

    def _remove(self, key: str, observed: _ObserverRun) -> None:
        with self._lock:
            if self._active.get(key) is observed:
                self._active.pop(key, None)

    def finish(self, key: str, observed: _ObserverRun, result: Any) -> None:
        """Finish and close one physical segment after terminal output."""
        self._remove(key, observed)
        if _is_interrupt_result(result):
            observed.interrupt()
        else:
            observed.finish(result)
        observed.close()

    def fail(self, key: str, observed: _ObserverRun, error: BaseException) -> None:
        """Fail and close one physical segment after an unrecoverable error."""
        self._remove(key, observed)
        observed.fail(error)
        observed.close()

    def interrupt(self, key: str, observed: _ObserverRun, error: BaseException | None = None) -> None:
        """Close one physical segment as interrupted and release its context."""
        self._remove(key, observed)
        observed.interrupt(error)
        observed.close()

    @staticmethod
    def _node_attempt(config: Mapping[str, Any] | None) -> int:
        info = _runtime_execution_info(config)
        attempt = getattr(info, "node_attempt", None)
        if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 1:
            return attempt
        return 1

    def _retry_pending(
        self,
        config: Mapping[str, Any] | None,
        retry_max_attempts: int | None,
    ) -> bool:
        return retry_max_attempts is not None and self._node_attempt(config) < max(1, retry_max_attempts)

    def wrap_node(
        self,
        name: str,
        function: F,
        *,
        finish: bool = False,
        terminal: bool = False,
        retry_max_attempts: int | None = None,
    ) -> F:
        """Return a native graph node wrapper that binds this lifecycle."""
        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                value, config = self._node_inputs(args, kwargs)
                key, _ = self._identity(config)
                observed = self._ensure_run(value, config)
                if observed is None or _run_context is None:
                    return await function(*args, **kwargs)
                try:
                    with _run_context(observed.run):
                        result = await function(*args, **kwargs)
                    if finish or (terminal and self._is_terminal_command(result)):
                        self.finish(key, observed, result)
                    return result
                except BaseException as error:
                    if _is_graph_interrupt(error):
                        self.interrupt(key, observed, error)
                    elif not self._retry_pending(config, retry_max_attempts):
                        self.fail(key, observed, error)
                    raise

            return async_wrapper  # type: ignore[return-value]

        @wraps(function)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            value, config = self._node_inputs(args, kwargs)
            key, _ = self._identity(config)
            observed = self._ensure_run(value, config)
            if observed is None or _run_context is None:
                return function(*args, **kwargs)
            try:
                with _run_context(observed.run):
                    result = function(*args, **kwargs)
                if finish or (terminal and self._is_terminal_command(result)):
                    self.finish(key, observed, result)
                return result
            except BaseException as error:
                if _is_graph_interrupt(error):
                    self.interrupt(key, observed, error)
                elif not self._retry_pending(config, retry_max_attempts):
                    self.fail(key, observed, error)
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
            if _is_interrupt_result(result):
                observed.interrupt()
            else:
                observed.finish(result)
            return result
        except BaseException as error:
            if observed is not None:
                if _is_graph_interrupt(error):
                    observed.interrupt(error)
                else:
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
            if _is_interrupt_result(result):
                observed.interrupt()
            else:
                observed.finish(result)
            return result
        except BaseException as error:
            if observed is not None:
                if _is_graph_interrupt(error):
                    observed.interrupt(error)
                else:
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
            if _is_interrupt_result(last_item):
                observed.interrupt()
            else:
                observed.finish(last_item)
        except BaseException as error:
            if observed is not None:
                if _is_graph_interrupt(error):
                    observed.interrupt(error)
                else:
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
            if _is_interrupt_result(last_item):
                observed.interrupt()
            else:
                observed.finish(last_item)
        except BaseException as error:
            if observed is not None:
                if _is_graph_interrupt(error):
                    observed.interrupt(error)
                else:
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


def _raw_response(response: Any) -> Any:
    """Unwrap common structured-output envelopes for usage inspection only."""
    candidate = response
    for _ in range(2):
        next_candidate = None
        if isinstance(candidate, Mapping):
            for key in ("raw", "response", "message"):
                value = candidate.get(key)
                if value is not None and value is not candidate:
                    next_candidate = value
                    break
        else:
            for key in ("raw", "response", "message"):
                value = getattr(candidate, key, None)
                if value is not None and value is not candidate:
                    next_candidate = value
                    break
        if next_candidate is None:
            break
        candidate = next_candidate
    return candidate


def _response_usage(response: Any) -> dict[str, int | None]:
    response = _raw_response(response)
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
    response = _raw_response(response)
    metadata = getattr(response, "response_metadata", None)
    if isinstance(metadata, Mapping):
        for key in ("stop_reason", "finish_reason", "stop"):
            value = metadata.get(key)
            if value is not None:
                return str(value)
    if getattr(response, "tool_calls", None):
        return "tool_call"
    return None


def _model_options(runnable: Any, kwargs: dict[str, Any]) -> tuple[str | None, str | None, bool, str | None]:
    model = kwargs.pop("observer_model", None)
    if not isinstance(model, str) or not model:
        for attribute in ("model_name", "model", "model_id"):
            candidate = getattr(runnable, attribute, None)
            if isinstance(candidate, str) and candidate:
                model = candidate
                break
    provider = kwargs.pop("observer_provider", None) or _provider_for_model(model)
    structured_output = bool(kwargs.pop("observer_structured_output", False))
    component = kwargs.pop("observer_component", None)
    return model, provider, structured_output, component


def _record_model_request(
    span: Any,
    request_id: str,
    *,
    model: str | None,
    provider: str | None,
    structured_output: bool,
    component: str | None,
) -> None:
    if span is None:
        return
    facts: dict[str, Any] = {
        "model": model,
        "provider": provider,
        "structured_output": structured_output,
    }
    if component:
        facts["component"] = component
    try:
        span.model_request(request_id=request_id, **facts)
    except Exception:  # pragma: no cover - sidecar guard
        LOGGER.debug("Agent Observer model request failed", exc_info=True)


def _record_model_response(
    span: Any,
    request_id: str,
    *,
    started_at: float,
    response: Any = None,
    error: BaseException | None = None,
    model: str | None,
    provider: str | None,
    structured_output: bool,
    component: str | None,
) -> None:
    if span is None:
        return
    facts: dict[str, Any] = {
        "model": model,
        "provider": provider,
        "structured_output": structured_output,
        "success": error is None,
    }
    if component:
        facts["component"] = component
    if error is not None:
        facts.update({"stop_reason": "error", "error_type": type(error).__name__})
    else:
        facts.update(_response_usage(response))
        facts["stop_reason"] = _stop_reason(response)
    try:
        span.model_response(
            request_id=request_id,
            duration_ms=_elapsed_ms(started_at),
            **facts,
        )
    except Exception:  # pragma: no cover - sidecar guard
        LOGGER.debug("Agent Observer model response failed", exc_info=True)


def observe_model_invoke(runnable: Any, payload: Any, **kwargs: Any) -> Any:
    """Invoke a model synchronously through the single Observer boundary."""
    options = dict(kwargs)
    model, provider, structured_output, component = _model_options(runnable, options)
    if _MODEL_BOUNDARY_DEPTH.get() > 0:
        return runnable.invoke(payload, **options)

    token = _MODEL_BOUNDARY_DEPTH.set(_MODEL_BOUNDARY_DEPTH.get() + 1)
    span = _current_span()
    request_id = f"req_{uuid.uuid4().hex}"
    started_at = time.perf_counter()
    _record_model_request(
        span,
        request_id,
        model=model,
        provider=provider,
        structured_output=structured_output,
        component=component,
    )
    try:
        response = runnable.invoke(payload, **options)
    except BaseException as error:
        _record_model_response(
            span,
            request_id,
            started_at=started_at,
            error=error,
            model=model,
            provider=provider,
            structured_output=structured_output,
            component=component,
        )
        raise
    else:
        _record_model_response(
            span,
            request_id,
            started_at=started_at,
            response=response,
            model=model,
            provider=provider,
            structured_output=structured_output,
            component=component,
        )
        return response
    finally:
        _MODEL_BOUNDARY_DEPTH.reset(token)


async def observe_model_ainvoke(runnable: Any, payload: Any, **kwargs: Any) -> Any:
    """Asynchronously invoke a model through the single Observer boundary."""
    options = dict(kwargs)
    model, provider, structured_output, component = _model_options(runnable, options)
    if _MODEL_BOUNDARY_DEPTH.get() > 0:
        return await runnable.ainvoke(payload, **options)

    token = _MODEL_BOUNDARY_DEPTH.set(_MODEL_BOUNDARY_DEPTH.get() + 1)
    span = _current_span()
    request_id = f"req_{uuid.uuid4().hex}"
    started_at = time.perf_counter()
    _record_model_request(
        span,
        request_id,
        model=model,
        provider=provider,
        structured_output=structured_output,
        component=component,
    )
    try:
        response = await runnable.ainvoke(payload, **options)
    except BaseException as error:
        _record_model_response(
            span,
            request_id,
            started_at=started_at,
            error=error,
            model=model,
            provider=provider,
            structured_output=structured_output,
            component=component,
        )
        raise
    else:
        _record_model_response(
            span,
            request_id,
            started_at=started_at,
            response=response,
            model=model,
            provider=provider,
            structured_output=structured_output,
            component=component,
        )
        return response
    finally:
        _MODEL_BOUNDARY_DEPTH.reset(token)


def _safe_args_summary(args: Any) -> dict[str, Any]:
    """Describe tool arguments without copying business query/result text."""
    if not isinstance(args, Mapping):
        return {"type": type(args).__name__}
    value_lengths: dict[str, int] = {}
    for key, value in args.items():
        if value is None:
            continue
        if isinstance(value, str):
            value_lengths[str(key)] = len(value)
        elif isinstance(value, (bytes, bytearray)):
            value_lengths[str(key)] = len(value)
        else:
            try:
                value_lengths[str(key)] = len(value)
            except TypeError:
                continue
    return {"keys": sorted(str(key) for key in args.keys()), "value_lengths": value_lengths}


def _bounded_result_bytes(result: Any) -> int | None:
    """Measure common results without serializing unbounded tool content."""
    if result is None:
        return 0
    if isinstance(result, str):
        return len(result.encode("utf-8"))
    if isinstance(result, (bytes, bytearray)):
        return len(result)
    try:
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        total = 0
        for chunk in encoder.iterencode(result):
            total += len(chunk.encode("utf-8"))
            if total > _MAX_RESULT_SIZE_PROBE:
                return None
        return total
    except (TypeError, ValueError):
        return None


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
    duration_ms: int | None,
    result: Any = None,
    raw_bytes: int | None = None,
    context_bytes: int | None = None,
    **facts: Any,
) -> None:
    """Record Tool timing and sizes while withholding raw result content."""
    span = _current_span()
    if span is None:
        return
    try:
        if raw_bytes is None:
            raw_bytes = _bounded_result_bytes(result)
        if context_bytes is None:
            context_bytes = raw_bytes
        span.tool_result(
            tool,
            tool_call_id=tool_call_id,
            success=success,
            duration_ms=duration_ms,
            raw_bytes=raw_bytes,
            context_bytes=context_bytes,
            **facts,
        )
    except Exception:  # pragma: no cover - sidecar guard
        LOGGER.debug("Agent Observer tool result failed", exc_info=True)


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", None) or type(tool).__name__)


def _tool_metadata(tool: Any) -> dict[str, Any]:
    metadata = getattr(tool, "metadata", None)
    if not isinstance(metadata, Mapping):
        return {}
    selected: dict[str, Any] = {}
    for key in ("tool_domain", "mcp_server", "source"):
        value = metadata.get(key)
        if isinstance(value, (str, int, bool)):
            selected[key] = str(value) if isinstance(value, int) else value
    return selected


def observe_tool_invoke(
    tool: Any,
    args: Any,
    config: Mapping[str, Any] | None = None,
    *,
    tool_call_id: str | None = None,
    **kwargs: Any,
) -> Any:
    """Execute a tool synchronously through the single Observer boundary."""
    if _TOOL_BOUNDARY_DEPTH.get() > 0:
        return tool.invoke(args, config, **kwargs)
    token = _TOOL_BOUNDARY_DEPTH.set(_TOOL_BOUNDARY_DEPTH.get() + 1)
    tool_name = _tool_name(tool)
    started_at = time.perf_counter()
    record_tool_call(tool_name, tool_call_id=tool_call_id, args=args)
    try:
        result = tool.invoke(args, config, **kwargs)
    except BaseException as error:
        record_tool_result(
            tool_name,
            tool_call_id=tool_call_id,
            success=False,
            duration_ms=_elapsed_ms(started_at),
            result=None,
            error_type=type(error).__name__,
            **_tool_metadata(tool),
        )
        raise
    else:
        record_tool_result(
            tool_name,
            tool_call_id=tool_call_id,
            success=True,
            duration_ms=_elapsed_ms(started_at),
            result=result,
            **_tool_metadata(tool),
        )
        return result
    finally:
        _TOOL_BOUNDARY_DEPTH.reset(token)


async def observe_tool_ainvoke(
    tool: Any,
    args: Any,
    config: Mapping[str, Any] | None = None,
    *,
    tool_call_id: str | None = None,
    **kwargs: Any,
) -> Any:
    """Asynchronously execute a tool through the single Observer boundary."""
    if _TOOL_BOUNDARY_DEPTH.get() > 0:
        return await tool.ainvoke(args, config, **kwargs)
    token = _TOOL_BOUNDARY_DEPTH.set(_TOOL_BOUNDARY_DEPTH.get() + 1)
    tool_name = _tool_name(tool)
    started_at = time.perf_counter()
    record_tool_call(tool_name, tool_call_id=tool_call_id, args=args)
    try:
        result = await tool.ainvoke(args, config, **kwargs)
    except BaseException as error:
        record_tool_result(
            tool_name,
            tool_call_id=tool_call_id,
            success=False,
            duration_ms=_elapsed_ms(started_at),
            result=None,
            error_type=type(error).__name__,
            **_tool_metadata(tool),
        )
        raise
    else:
        record_tool_result(
            tool_name,
            tool_call_id=tool_call_id,
            success=True,
            duration_ms=_elapsed_ms(started_at),
            result=result,
            **_tool_metadata(tool),
        )
        return result
    finally:
        _TOOL_BOUNDARY_DEPTH.reset(token)


__all__ = [
    "ObservedGraph",
    "ObserverRunLifecycle",
    "observe_graph_node",
    "observe_model_invoke",
    "observe_model_ainvoke",
    "observe_tool_invoke",
    "observe_tool_ainvoke",
    "observer_available",
    "record_tool_call",
    "record_tool_result",
    "register_graph_topology",
]
