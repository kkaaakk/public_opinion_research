# 导出可选 LangSmith 追踪能力，供各层统一附加关联元数据。
"""Observability integrations for Public Opinion Research.

One boundary with a deliberate scope:

* **Langfuse / OTel** (``observability.telemetry``) observes native DBOS,
  OpenInference SDK spans and selected Harness/Graph business boundaries.
* **LangSmith** (``observability.langsmith``) retains its independent opt-in
  LangChain business-service/RAG integration and historical report imports.

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
