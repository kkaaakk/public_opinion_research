"""Concentrated hooks for real compaction, independent of agent/tool implementations."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from langchain_core.runnables.config import ensure_config

from open_deep_research.trajectory.sanitization import bounded, error_data

CURRENT_RECORDER: ContextVar = ContextVar("trajectory_recorder", default=None)


class CompactionTransaction:
    """Record only a transformation that actually started (a no-op emits nothing)."""

    def __init__(self, kind: str):
        """Bind the active session and real inherited node context, if enabled."""
        self.recorder = CURRENT_RECORDER.get()
        self.kind = kind
        self.id = uuid.uuid4().hex
        self.started = False

    def emit(self, kind: str, data: dict) -> None:
        """Append at the shared compaction boundary, never at per-agent/tool sites."""
        if self.recorder is None:
            return
        metadata = ensure_config().get("metadata", {})
        self.recorder.session.append(kind, {"turn": self.recorder.session.turn,
            "compaction_id": self.id, "kind": self.kind, **data},
            agent_name=metadata.get("agent_name"), langgraph_node=metadata.get("langgraph_node"))

    def start(self) -> None:
        """Open a bracket once the implementation is about to replace real context."""
        self.started = True
        self.emit("compaction/start", {})

    def summary(self, content, **facts) -> None:
        """Record the actual replacement/summary with bounded content and real facts."""
        self.emit("compaction/summary", {"summary": bounded(content, 262_144), **facts})


@contextmanager
def compaction_transaction(kind: str):
    """Balance a real compaction on success, error or cancellation; no-op stays silent."""
    transaction = CompactionTransaction(kind)
    try:
        yield transaction
    except BaseException as exc:
        if transaction.started:
            transaction.emit("compaction/end", {"error": error_data(exc, "compaction_error")})
        raise
    else:
        if transaction.started:
            transaction.emit("compaction/end", {})
