"""Optional raw transcript sink kept outside model context."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ResearchTranscript:
    """Append-only debug transcript for raw tool results and compaction events."""

    def __init__(self, directory: str | None, run_id: str) -> None:
        """Configure an append-only transcript path for one research run."""
        self.directory = Path(directory or ".tmp/runtime/transcripts")
        self.run_id = _safe_path_part(run_id)
        self.path = self.directory / f"{self.run_id}.jsonl"

    def append(self, event_type: str, payload: dict[str, Any]) -> None:
        """Append a JSON event without ever returning it to model context."""
        self.directory.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "run_id": self.run_id,
            **payload,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def _safe_path_part(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in str(value))
    return safe or "research-run"


__all__ = ["ResearchTranscript"]
