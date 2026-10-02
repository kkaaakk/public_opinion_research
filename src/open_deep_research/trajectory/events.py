"""Versioned session envelopes and the supported conversation vocabulary."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from open_deep_research.trajectory.sanitization import sanitize

EVENT_TYPES = frozenset({
    "session/start", "session/end", "turn/start", "turn/end",
    "step/start", "step/end", "user/message", "system/message", "request/header",
    "request/context", "assistant/message", "assistant/attempt", "tool/call", "tool/result",
    "agent/context", "workflow/error", "retry/scheduled", "retry/started", "compaction/start",
    "compaction/summary", "compaction/end",
})
_ID = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def validate_session_id(value: str) -> str:
    """Reject unsafe path identities rather than changing the caller's identifier."""
    if not _ID.fullmatch(value) or value.upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10)),
    }:
        raise ValueError("Invalid trajectory session id")
    return value


def now_ms() -> int:
    """Read an epoch timestamp once at the collection boundary."""
    return time.time_ns() // 1_000_000


@dataclass(frozen=True)
class SessionEvent:
    """Represent one detached append-only envelope (seq is allocated by Session)."""

    session_id: str
    seq: int
    time: int
    type: str
    data: dict[str, Any]
    run_id: str | None = None
    parent_run_id: str | None = None
    parent_ids: tuple[str, ...] = ()
    agent_name: str | None = None
    langgraph_node: str | None = None
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        """Return a detached serializable event so readers cannot mutate the ledger."""
        value = {"version": self.version, "session_id": self.session_id,
                 "seq": self.seq, "time": self.time, "type": self.type, "data": self.data}
        for key in ("run_id", "parent_run_id", "agent_name", "langgraph_node"):
            if getattr(self, key) is not None:
                value[key] = getattr(self, key)
        if self.parent_ids:
            value["parent_ids"] = list(self.parent_ids)
        return json.loads(json.dumps(sanitize(value), ensure_ascii=False, allow_nan=False))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SessionEvent:
        """Validate stored envelopes, refusing gaps, unknown formats and malformed data."""
        if value.get("version") != 1 or value.get("type") not in EVENT_TYPES:
            raise ValueError("Unsupported trajectory event format")
        validate_session_id(value["session_id"])
        if any(type(value.get(k)) is not int or value[k] < 0 for k in ("seq", "time")):
            raise ValueError("Invalid trajectory event position")
        if not isinstance(value.get("data"), dict):
            raise ValueError("Invalid trajectory event data")
        for key in ("run_id", "parent_run_id", "agent_name", "langgraph_node"):
            if value.get(key) is not None and not isinstance(value[key], str):
                raise ValueError("Invalid trajectory runtime identity")
        parents = value.get("parent_ids", [])
        if not isinstance(parents, list | tuple) or not all(isinstance(p, str) for p in parents):
            raise ValueError("Invalid trajectory runtime ancestry")
        copied = json.loads(json.dumps(value, allow_nan=False))
        copied["parent_ids"] = tuple(copied.get("parent_ids", ()))
        return cls(**copied)
