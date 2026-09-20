"""Thin LangSmith integration for execution observability.

LangSmith is the project's tracing system: it owns Trace/Span hierarchy, graph
node, Agent, LLM, Tool, MCP, Retriever/RAG, latency, and error visibility.
Token / usage / cache / budget / cost accounting deliberately stays in
``open_deep_research.budget`` on LangChain's official callbacks.

This module only:

* normalizes the official LangSmith environment variables (fail-open);
* builds bounded correlation metadata;
* exposes a fail-open ``trace_span`` for the few high-value RAG stages that
  LangChain cannot auto-capture.

It intentionally does not define a trace data model, storage, UI, waterfall,
or search.  Anything LangChain/LangGraph already auto-traces must not be wrapped
again, so no LLM/Tool/Node span is created here.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from collections.abc import Mapping
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_PROJECT = "public-opinion-research"
WORKFLOW_NAME = "public_opinion_research"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_CORRELATION_KEYS = ("thread_id", "checkpoint_id", "task_id", "research_run_id")

try:  # Optional at runtime: an absent/broken langsmith degrades to a no-op.
    from langsmith import trace as _ls_trace
    from langsmith.run_helpers import get_current_run_tree as _get_current_run_tree
except Exception:  # pragma: no cover - optional dependency guard
    _ls_trace = None  # type: ignore[assignment,misc]
    _get_current_run_tree = None  # type: ignore[assignment,misc]


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in _TRUE_VALUES


def _config_value(config: Mapping[str, Any] | None, key: str) -> Any:
    if not isinstance(config, Mapping):
        return None
    configurable = config.get("configurable", {})
    if isinstance(configurable, Mapping) and key in configurable:
        return configurable.get(key)
    metadata = config.get("metadata", {})
    if isinstance(metadata, Mapping) and key in metadata:
        return metadata.get(key)
    return None


def _clear_langsmith_env_cache() -> None:
    """Clear LangSmith's env cache so normalized values take effect."""
    try:
        from langsmith import utils as ls_utils

        cache_clear = getattr(ls_utils.get_env_var, "cache_clear", None)
        if callable(cache_clear):
            cache_clear()
    except Exception:  # pragma: no cover - version/compat guard
        pass


def ensure_langsmith_configuration() -> None:
    """Normalize LangSmith env vars without making tracing a hard dependency.

    * An empty ``LANGSMITH_PROJECT`` falls back to the project default.
    * Tracing that is enabled without any API key or custom endpoint is turned
      off so a missing key cannot spam a run with failed uploads.  Business
      behavior is never affected either way.

    Idempotent and safe to call again after a late ``.env`` load.
    """
    changed = False
    project = os.environ.get("LANGSMITH_PROJECT")
    if project is not None and not project.strip():
        os.environ["LANGSMITH_PROJECT"] = DEFAULT_PROJECT
        changed = True
    if not os.environ.get("LANGSMITH_PROJECT") and not os.environ.get("LANGCHAIN_PROJECT"):
        os.environ["LANGSMITH_PROJECT"] = DEFAULT_PROJECT
        changed = True

    tracing = os.environ.get("LANGSMITH_TRACING") or os.environ.get("LANGCHAIN_TRACING")
    if _truthy(tracing):
        has_key = bool((os.environ.get("LANGSMITH_API_KEY") or "").strip())
        has_endpoint = bool((os.environ.get("LANGSMITH_ENDPOINT") or "").strip())
        if not has_key and not has_endpoint:
            LOGGER.warning(
                "LANGSMITH_TRACING is enabled but no LANGSMITH_API_KEY is configured; "
                "disabling LangSmith tracing for this process (business behavior is unchanged)."
            )
            for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING"):
                if name in os.environ:
                    os.environ[name] = "false"
            changed = True

    if changed:
        _clear_langsmith_env_cache()


def langsmith_enabled() -> bool:
    """Return whether LangSmith tracing should be active for this process."""
    if not _truthy(os.environ.get("LANGSMITH_TRACING") or os.environ.get("LANGCHAIN_TRACING")):
        return False
    return bool((os.environ.get("LANGSMITH_API_KEY") or "").strip()) or bool(
        (os.environ.get("LANGSMITH_ENDPOINT") or "").strip()
    )


def langsmith_project() -> str:
    """Return the configured LangSmith project name."""
    value = (os.environ.get("LANGSMITH_PROJECT") or "").strip()
    return value or DEFAULT_PROJECT


def _bounded(value: Any, *, limit: int = 160) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.lower() == "none":
        return None
    return text[:limit]


def logical_run_id(config: Mapping[str, Any] | None = None) -> str:
    """Derive a stable, serializable logical run id without persisting state.

    Deterministic per ``thread_id`` so interrupt/resume segments of one logical
    run correlate; an explicit id always wins.
    """
    explicit = _bounded(_config_value(config, "logical_run_id"))
    if explicit is not None:
        return explicit
    thread_id = _bounded(_config_value(config, "thread_id"))
    if thread_id:
        seed = f"public-opinion:{thread_id}"
        return f"workflow_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]}"
    checkpoint_id = _bounded(_config_value(config, "checkpoint_id"))
    if checkpoint_id:
        seed = f"public-opinion:checkpoint:{checkpoint_id}"
        return f"workflow_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]}"
    return f"workflow_{uuid.uuid4().hex}"


def correlation_metadata(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build bounded, serializable correlation facts for LangSmith runs.

    Only stable identifiers are copied; no graph state, document body, prompt,
    credential, or user content is included.  Values are emitted only when set.
    """
    metadata: dict[str, Any] = {"workflow": WORKFLOW_NAME}
    for key in _CORRELATION_KEYS:
        value = _bounded(_config_value(config, key))
        if value is not None:
            metadata[key] = value
    metadata["logical_run_id"] = logical_run_id(config)
    environment = _bounded(os.environ.get("APP_ENV") or os.environ.get("ENVIRONMENT"), limit=64)
    if environment is not None:
        metadata["environment"] = environment
    git_commit = _bounded(os.environ.get("GIT_COMMIT"), limit=128)
    if git_commit is not None:
        metadata["git_commit"] = git_commit
    return metadata


def invocation_metadata(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return metadata to attach to one graph invocation's LangSmith run."""
    metadata = correlation_metadata(config)
    metadata["ls_provider"] = "langsmith"
    return metadata


def node_metadata(node_name: str, *, kind: str = "graph_node") -> dict[str, Any]:
    """Return bounded metadata for one LangGraph node run."""
    metadata: dict[str, Any] = {
        "workflow": WORKFLOW_NAME,
        "node_name": node_name,
        "node_kind": kind,
    }
    if kind == "agent":
        metadata["agent_name"] = node_name
        metadata["agent_role"] = node_name.removesuffix("_agent")
    return metadata


def agent_metadata(agent_name: str, agent_role: str | None = None) -> dict[str, Any]:
    """Return bounded metadata for one public-opinion business agent."""
    return {
        "workflow": WORKFLOW_NAME,
        "agent_name": agent_name,
        "agent_role": agent_role or agent_name.removesuffix("_agent"),
    }


def model_metadata(
    *,
    component: str | None = None,
    structured_output: bool | None = None,
) -> dict[str, Any]:
    """Return metadata LangChain attaches to one auto-traced LLM run."""
    metadata: dict[str, Any] = {}
    if component:
        metadata["component"] = str(component)[:160]
    if structured_output:
        metadata["structured_output"] = True
    return metadata


def record_current_metadata(metadata: Mapping[str, Any]) -> None:
    """Attach bounded metadata to the current run tree; never raises."""
    if not metadata or _get_current_run_tree is None:
        return
    try:
        run_tree = _get_current_run_tree()
        if run_tree is None:
            return
        run_tree.metadata.update(dict(metadata))
    except Exception:  # pragma: no cover - observability must never break business
        LOGGER.debug("LangSmith metadata update failed", exc_info=True)


class _NoOpSpan:
    """A context manager used when tracing is disabled or setup failed."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: Any) -> None:
        return None

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class trace_span:
    """Fail-open LangSmith span for a high-value logical stage.

    Use only for stages LangChain cannot auto-capture (RAG internals).  It works
    as a sync and async context manager and yields the LangSmith ``RunTree`` or
    ``None`` when tracing is disabled.  Setup and teardown failures are swallowed
    so observability can never fail a research run.
    """

    def __init__(
        self,
        name: str,
        run_type: str = "chain",
        *,
        inputs: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Configure one logical stage span (bounded metadata only)."""
        self.name = name
        self.run_type = run_type
        self.inputs = dict(inputs) if inputs else None
        self.metadata = dict(metadata) if metadata else None
        self.tags = tags
        self._cm: Any = None

    def _start(self) -> Any:
        if not langsmith_enabled() or _ls_trace is None:
            return _NoOpSpan()
        try:
            from typing import cast

            from langsmith.client import RUN_TYPE_T

            return _ls_trace(
                self.name,
                run_type=cast(RUN_TYPE_T, self.run_type),
                inputs=self.inputs,
                metadata=self.metadata,
                tags=self.tags,
            )
        except Exception:  # pragma: no cover - setup guard
            LOGGER.debug("LangSmith span setup failed", exc_info=True)
            return _NoOpSpan()

    def __enter__(self) -> Any:
        """Start the span synchronously; degrade to a no-op on failure."""
        self._cm = self._start()
        try:
            return self._cm.__enter__()
        except Exception:  # pragma: no cover - enter guard
            LOGGER.debug("LangSmith span enter failed", exc_info=True)
            self._cm = _NoOpSpan()
            return None

    def __exit__(self, *exc: Any) -> bool:
        """End the span without ever raising."""
        try:
            return bool(self._cm.__exit__(*exc))
        except Exception:  # pragma: no cover - exit guard
            LOGGER.debug("LangSmith span exit failed", exc_info=True)
            return False

    async def __aenter__(self) -> Any:
        """Start the span asynchronously; degrade to a no-op on failure."""
        self._cm = self._start()
        try:
            return await self._cm.__aenter__()
        except Exception:  # pragma: no cover - enter guard
            LOGGER.debug("LangSmith span aenter failed", exc_info=True)
            self._cm = _NoOpSpan()
            return None

    async def __aexit__(self, *exc: Any) -> bool:
        """End the span without ever raising."""
        try:
            return bool(await self._cm.__aexit__(*exc))
        except Exception:  # pragma: no cover - exit guard
            LOGGER.debug("LangSmith span aexit failed", exc_info=True)
            return False


__all__ = [
    "DEFAULT_PROJECT",
    "WORKFLOW_NAME",
    "agent_metadata",
    "correlation_metadata",
    "ensure_langsmith_configuration",
    "invocation_metadata",
    "langsmith_enabled",
    "langsmith_project",
    "model_metadata",
    "node_metadata",
    "record_current_metadata",
    "trace_span",
]
