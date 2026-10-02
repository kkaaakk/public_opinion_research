"""Central append boundary, session/turn lifecycle and fail-open publication."""

from __future__ import annotations

import logging
import threading
from typing import Any

from open_deep_research.trajectory.events import (
    EVENT_TYPES,
    SessionEvent,
    now_ms,
    validate_session_id,
)
from open_deep_research.trajectory.live import LiveBus
from open_deep_research.trajectory.ownership import WriterOwnedError
from open_deep_research.trajectory.persistence import TrajectorySessionPersistence
from open_deep_research.trajectory.projection import TrajectoryProjection
from open_deep_research.trajectory.sanitization import sanitize

LOGGER = logging.getLogger(__name__)


class TrajectorySession:
    """Own sequence numbers and conversation facts, never business state."""

    def __init__(self, session_id: str, *, storage: TrajectorySessionPersistence | None = None,
                 correlation: dict | None = None):
        """Open a writer once; persistence failure degrades only trajectory."""
        self.id = validate_session_id(session_id)
        self.correlation = sanitize(correlation or {})
        self.bus = LiveBus()
        self._lock = threading.RLock()
        self._events: list[SessionEvent] = []
        self._revision = 0
        self.projection = TrajectoryProjection()
        self.writer = None
        self.degraded = False
        self.finished = False
        self.turn = 0
        if storage is not None:
            try:
                self.writer = storage.create(self.id)
                prior = storage.read(self.id)
                for event in prior.events:
                    self._events.append(event)
                    self.projection.apply(event)
                    self.turn = max(self.turn, event.data.get("turn") or 0)
            except WriterOwnedError:
                # Ownership contention is an admission failure, never a fallback second writer.
                raise
            except Exception:
                self.mark_degraded("persistence_open")

    @property
    def events(self) -> list[SessionEvent]:
        """Return owned copies so neither observers nor tests can edit committed facts."""
        with self._lock:
            return [SessionEvent.from_dict(e.to_dict()) for e in self._events]

    def mark_degraded(self, reason: str) -> None:
        """Log only the failure category and notify readers without leaking raw values."""
        self.degraded = True
        LOGGER.warning("Trajectory degraded: session=%s reason=%s", self.id, reason)
        self.bus.publish({"type": "trajectory_degraded", "session_id": self.id, "reason": reason})

    def append(self, kind: str, data: dict[str, Any], **context: Any) -> SessionEvent | None:
        """Sanitize, append/flush, then publish; never retry the business execution."""
        with self._lock:
            try:
                if kind not in EVENT_TYPES:
                    raise ValueError("Unknown trajectory event")
                event = SessionEvent.from_dict(SessionEvent(self.id, len(self._events), now_ms(), kind,
                    sanitize(data), **context).to_dict())
                if self.writer is not None and not self.degraded:
                    try:
                        self.writer.append([event])
                        if kind in {"turn/end", "session/end"}:
                            self.writer.flush()
                    except Exception:
                        self.mark_degraded("persistence_append")
                self._events.append(event)
                self.projection.apply(event)
                self.publish(kind)
                return event
            except Exception:
                self.mark_degraded("serialization_or_projection")
                return None

    def publish(self, kind: str = "snapshot") -> None:
        """Send a compact presentation snapshot, excluding timed streams and raw history."""
        if not self.bus.subscribers:
            return
        try:
            self.bus.publish(self.packet(kind))
        except Exception:
            self.mark_degraded("projection")

    def packet(self, kind: str = "snapshot") -> dict:
        """Return the authoritative session identity, watermark and derived snapshot."""
        with self._lock:
            self._revision += 1
            return {"type": "trajectory", "session_id": self.id,
                    "seq": len(self._events) - 1, "revision": self._revision, "event_type": kind, "degraded": self.degraded,
                    "snapshot": self.projection.snapshot(active=not self.finished)}

    def update_partial(self, attempt_id: str, turn: int, step: int, content: Any) -> None:
        """Admit transient output without allocating a durable sequence number."""
        with self._lock:
            self.projection.live(attempt_id, turn, step, content)

    def start_turn(self, user_content: str, context: dict) -> None:
        """Record one actual user interaction, independent of agent switching."""
        if not self._events:
            self.append("session/start", {"correlation": self.correlation})
        self.turn += 1
        self.append("turn/start", {"turn": self.turn})
        self.append("user/message", {"turn": self.turn, "content": user_content})
        self.append("request/context", {"turn": self.turn, **context})

    def finish(self, status: str, budget_usage: dict | None = None) -> None:
        """Close the turn once, flush the log and release ownership even after failure."""
        if self.finished:
            return
        self.append("turn/end", {"turn": self.turn, "reason": status})
        self.append("session/end", {"status": status, "budget_usage": budget_usage, "degraded": self.degraded})
        self.finished = True
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                self.mark_degraded("persistence_close")
        self.publish("session/end")
