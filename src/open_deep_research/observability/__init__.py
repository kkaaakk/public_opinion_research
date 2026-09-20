"""Optional observability integrations for Public Opinion Research.

Two responsibilities with a deliberate boundary:

* **LangSmith** (``observability.langsmith``) owns execution observability:
  Trace/Span hierarchy, LangGraph nodes, Agents, LLM, Tool, MCP,
  Retriever/RAG, latency, and errors.  LangChain/LangGraph auto-tracing is
  preferred; only high-value RAG stages add a thin fail-open span.
* **Agent Observer** (``observability.agent_observer``) stays an optional
  fail-open sidecar for provider usage/tool telemetry.  Token, cache, budget,
  and cost accounting itself lives in ``open_deep_research.budget`` on
  LangChain's official callbacks and is unaffected by LangSmith.
"""

from open_deep_research.observability.agent_observer import (
    ObservedGraph,
    ObserverRunLifecycle,
    observe_graph_node,
    observe_model_ainvoke,
    observe_model_invoke,
    observe_tool_ainvoke,
    observe_tool_invoke,
    observer_available,
    record_tool_call,
    record_tool_result,
    register_graph_topology,
)
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
    "ObservedGraph",
    "ObserverRunLifecycle",
    "WORKFLOW_NAME",
    "agent_metadata",
    "correlation_metadata",
    "ensure_langsmith_configuration",
    "invocation_metadata",
    "langsmith_enabled",
    "langsmith_project",
    "model_metadata",
    "node_metadata",
    "observe_graph_node",
    "observe_model_ainvoke",
    "observe_model_invoke",
    "observe_tool_ainvoke",
    "observe_tool_invoke",
    "observer_available",
    "record_current_metadata",
    "record_tool_call",
    "record_tool_result",
    "register_graph_topology",
    "trace_span",
]
