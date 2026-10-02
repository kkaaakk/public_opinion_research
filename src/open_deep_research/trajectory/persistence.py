"""Abstract storage seam and UTF-8 JSONL storage with contiguous-prefix validation."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from open_deep_research.trajectory.events import SessionEvent, validate_session_id
from open_deep_research.trajectory.ownership import WriterLease, writer_active


@dataclass
class ReadResult:
    """Return validated facts and an explicit torn-tail warning."""

    events: list[SessionEvent]
    warning: str | None = None


class TrajectorySessionWriter(Protocol):
    """Define ownership and checkpoint operations on one admitted writer."""

    def append(self, events: list[SessionEvent]) -> None:
        """Append a contiguous batch of sanitized session facts."""
        ...

    def flush(self) -> None:
        """Checkpoint all admitted records."""
        ...

    def close(self) -> None:
        """Flush and release writer ownership."""
        ...


class TrajectorySessionPersistence(Protocol):
    """Define the small replaceable append/read storage contract."""

    def create(self, session_id: str) -> TrajectorySessionWriter:
        """Acquire one writer channel, refusing a second owner."""
        ...

    def read(self, session_id: str, offset: int = 0, limit: int | None = None) -> ReadResult:
        """Read a contiguous validated slice."""
        ...

    def list(self) -> list[str]:
        """List stored session identities."""
        ...


class JsonlWriter:
    """Append complete records through one leased channel; never rewrite old facts."""

    def __init__(self, storage: JsonlPersistence, session_id: str):
        """Validate any prior log before opening its append handle."""
        self.lease = WriterLease(storage.lock_path(session_id))
        try:
            prior = storage.read(session_id) if storage.path(session_id).exists() else ReadResult([])
            if prior.warning:
                raise ValueError("Refusing to append to a torn trajectory tail")
            self.next_seq = len(prior.events)
            self.session_id = session_id
            self.handle = storage.path(session_id).open("ab")
        except BaseException:
            self.lease.close()
            raise

    def append(self, events: list[SessionEvent]) -> None:
        """Validate the whole batch before writing; redaction precedes serialization."""
        rows = []
        for offset, event in enumerate(events):
            if event.session_id != self.session_id or event.seq != self.next_seq + offset:
                raise ValueError("Trajectory append is not contiguous")
            row = event.to_dict()
            SessionEvent.from_dict(row)
            rows.append(json.dumps(row, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")
        self.handle.write(b"".join(rows))
        self.handle.flush()
        self.next_seq += len(rows)

    def flush(self) -> None:
        """Checkpoint admitted records to the OS at lifecycle settlement."""
        self.handle.flush()
        os.fsync(self.handle.fileno())

    def close(self) -> None:
        """Flush before releasing writer ownership, even when flushing fails."""
        try:
            self.flush()
        finally:
            self.handle.close()
            self.lease.close()


class JsonlPersistence:
    """Store one event log per session without adding a database service."""

    def __init__(self, directory: str | Path, retention_days: int = 30):
        """Configure local private storage; creation is deferred until first use."""
        self.directory = Path(directory).resolve()
        self.retention_days = retention_days

    def path(self, session_id: str) -> Path:
        """Resolve an identity below the configured storage root."""
        return self.directory / f"{validate_session_id(session_id)}.jsonl"

    def lock_path(self, session_id: str) -> Path:
        """Resolve the writer's independent OS lease file."""
        return self.directory / f"{validate_session_id(session_id)}.lock"

    def create(self, session_id: str) -> JsonlWriter:
        """Acquire a writer and keep retention separate from append behavior."""
        return JsonlWriter(self, session_id)

    def active(self, session_id: str) -> bool:
        """Return whether any process currently owns this session's writer."""
        return writer_active(self.lock_path(session_id))

    def read(self, session_id: str, offset: int = 0, limit: int | None = None) -> ReadResult:
        """Validate every record; discard only an incomplete physical final line."""
        events: list[SessionEvent] = []
        warning = None
        with self.path(session_id).open("rb") as handle:
            for line in handle:
                if not line.endswith(b"\n"):
                    warning = "torn_tail"
                    break
                try:
                    event = SessionEvent.from_dict(json.loads(line))
                except (ValueError, TypeError, KeyError) as exc:
                    raise ValueError(f"Corrupt trajectory record at seq {len(events)}") from exc
                if event.session_id != session_id or event.seq != len(events):
                    raise ValueError(f"Noncontiguous trajectory record at seq {len(events)}")
                events.append(event)
        return ReadResult(events[offset:None if limit is None else offset + limit], warning)

    def list(self) -> list[str]:
        """Return newest logs first; retention cleanup never removes a live writer."""
        self.prune()
        return [p.stem for p in sorted(self.directory.glob("*.jsonl"),
                                      key=lambda p: p.stat().st_mtime, reverse=True)]

    def prune(self) -> None:
        """Remove expired idle logs; zero retention disables automatic deletion."""
        if self.retention_days <= 0:
            return
        cutoff = time.time() - self.retention_days * 86400
        for path in self.directory.glob("*.jsonl"):
            if path.stat().st_mtime < cutoff:
                try:
                    lease = WriterLease(self.lock_path(path.stem))
                except (OSError, RuntimeError):
                    continue
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                finally:
                    lease.close()
