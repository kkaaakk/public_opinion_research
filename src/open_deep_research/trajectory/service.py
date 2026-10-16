"""Local session history service; business execution remains outside storage."""

from __future__ import annotations

import logging
import os
from collections import OrderedDict

from open_deep_research.trajectory.persistence import JsonlPersistence, ReadResult
from open_deep_research.trajectory.projection import replay
from open_deep_research.trajectory.session import TrajectorySession

LOGGER = logging.getLogger(__name__)


class TrajectoryService:
    """Open writers and serve validated read/replay windows through one storage seam."""

    def __init__(self):
        """Read deployment settings without opening any file or leaking credentials."""
        self.enabled = os.environ.get("TRAJECTORY_PERSISTENCE_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
        try:
            retention = max(0, int(os.environ.get("TRAJECTORY_RETENTION_DAYS", "30")))
        except ValueError:
            LOGGER.warning("Invalid trajectory retention; using 30 days")
            retention = 30
        self.storage = JsonlPersistence(os.environ.get("TRAJECTORY_STORAGE_DIR", ".data/trajectory"), retention)
        self.sessions: OrderedDict[str, TrajectorySession] = OrderedDict()

    def create(self, session_id: str, correlation: dict) -> TrajectorySession:
        """Create a single owner; retain a small fallback cache when persistence fails."""
        if self.enabled:
            try:
                self.storage.prune()
            except Exception:
                LOGGER.warning("Trajectory retention cleanup unavailable")
        session = TrajectorySession(session_id, storage=self.storage if self.enabled else None,
                                    correlation=correlation)
        self.sessions[session_id] = session
        for key in list(self.sessions):
            if len(self.sessions) <= 8:
                break
            if self.sessions[key].finished:
                del self.sessions[key]
        return session

    def history(self, session_id: str, *, before_seq: int | None = None, limit: int = 100) -> dict:
        """Return an older page and its cumulative projection with inherited headers."""
        session = self.sessions.get(session_id)
        if session:
            result = self.storage.read(session_id) if self.enabled and not session.degraded else ReadResult(session.events)
            active = not session.finished
        else:
            result = self.storage.read(session_id)
            active = self.storage.active(session_id)
        events = result.events
        end = min(len(events), before_seq if before_seq is not None else len(events))
        start = max(0, end - limit)
        snapshot = replay(events, active=active, min_seq=start)
        return {"session_id": session_id, "events": [e.to_dict() for e in events[start:end]],
                "snapshot": snapshot, "before_seq": start, "has_more": start > 0,
                "latest_seq": len(events) - 1, "writer_active": active,
                "warning": result.warning, "degraded": session.degraded if session else bool(result.warning),
                "persistence_enabled": self.enabled}

    def list(self) -> list[str]:
        """Include active/nonpersistent sessions alongside stored history."""
        return list(dict.fromkeys([*reversed(self.sessions), *(self.storage.list() if self.enabled else [])]))
