"""Observability integrations for Public Opinion Research.

One boundary with a deliberate scope:

* **LangSmith** (``observability.langsmith``) owns execution observability:
  Trace/Span hierarchy, LangGraph nodes, Agents, LLM, Tool, MCP,
  Retriever/RAG, latency, and errors.  LangChain/LangGraph auto-tracing is
  preferred; only high-value RAG stages add a thin fail-open span.

Token, cache, budget, and cost accounting itself lives in
``open_deep_research.budget`` on LangChain's official callbacks and is
independent of LangSmith.
"""

from open_deep_research.observability.langsmith import (
    DEFAULT_PROJECT,
    WORKFLOW_NAME,
    agent_metadata,
    correlation_metadata,
    ensure_langsmith_configuration,
    invocation_metadata,
    langsmith_enabled,
    langsmith_project,
    model_metadata,
    node_metadata,
    record_current_metadata,
    trace_span,
)

# Normalize LangSmith env vars at import time so tracing is opt-in, key-safe,
# and never a hard dependency.  Failures here must not affect business import.
try:  # pragma: no cover - import-time environment guard
    ensure_langsmith_configuration()
except Exception:  # noqa: BLE001 - observability must never break import
    pass

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
