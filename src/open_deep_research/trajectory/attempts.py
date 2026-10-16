"""Attempt buffering and settlement with explicit size and cross-chunk redaction."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from open_deep_research.trajectory.sanitization import sanitize


@dataclass
class ModelAttempt:
    """Retain one request's timed stream until one durable settlement."""

    run_id: str
    step: int
    attempt: int
    started_at: int
    scope_id: str
    context: dict
    structured_output: bool = False
    first_token_at: int | None = None
    stream: list[dict] = field(default_factory=list)
    stream_bytes: int = 0
    stream_truncated: bool = False
    content: str = ""
    reasoning: str = ""
    settled: bool = False
    pending: set[str] = field(default_factory=set)
    ended: bool = False

    def push(self, time: int, chunk: dict, limit: int) -> None:
        """Keep original chunk boundaries/times within the declared attempt limit."""
        content = chunk.get("content")
        visible = content if isinstance(content, str) else "".join(
            str(c.get("text") or c.get("reasoning") or c.get("thinking") or "")
            for c in content or [] if isinstance(c, dict)
        )
        tool_delta = any(c.get("args") or c.get("name") for c in chunk.get("tool_call_chunks", []))
        nonempty = bool(visible or tool_delta or chunk.get("reasoning_content"))
        if self.first_token_at is None and nonempty:
            self.first_token_at = time
        size = len(json.dumps(chunk, ensure_ascii=False).encode("utf-8"))
        self.stream_bytes += size
        if self.stream_bytes <= limit:
            self.stream.append({"time": time, "chunk": chunk})
            if isinstance(content, str):
                self.content += content
            elif isinstance(content, list):
                self.content += "".join(c.get("text", "") for c in content if isinstance(c, dict))
            self.reasoning += chunk.get("reasoning_content", "")
        else:
            self.stream_truncated = True

    def safe_stream(self) -> list[dict]:
        """Redact credentials split across deltas while retaining their timed frames."""
        records = copy.deepcopy(self.stream)
        # If a joined channel contains a secret, redact that channel's full delta
        # sequence. This avoids inventing transformed delta boundaries or retaining
        # a credential split over two individually innocent-looking chunks.
        for key in ("content", "reasoning_content"):
            combined = "".join(r["chunk"].get(key, "") for r in records if isinstance(r["chunk"].get(key), str))
            if sanitize(combined) != combined:
                for record in records:
                    if isinstance(record["chunk"].get(key), str):
                        record["chunk"][key] = "[REDACTED stream]"
                        record["redacted"] = True
        nested = [block for record in records for block in record["chunk"].get("content", [])
                  if isinstance(record["chunk"].get("content"), list) and isinstance(block, dict)]
        for channel in ("text", "reasoning", "thinking"):
            joined = "".join(b.get(channel, "") for b in nested if isinstance(b.get(channel), str))
            if sanitize(joined) != joined:
                for block in nested:
                    if channel in block:
                        block[channel] = "[REDACTED stream]"
        calls: dict[Any, list[dict]] = {}
        for record in records:
            for call in record["chunk"].get("tool_call_chunks", []):
                calls.setdefault(call.get("index", call.get("id")), []).append(call)
        for chunks in calls.values():
            joined = "".join(c.get("args", "") for c in chunks)
            if sanitize(joined) != joined:
                for call in chunks:
                    call["args"] = "[REDACTED stream]"
        return sanitize(records)
