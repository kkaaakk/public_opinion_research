"""Fail-open projection of LangChain v2 events for the local developer UI.

This is a transport DTO, not a trace store or a token accountant.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import BaseMessage

_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|authorization|cookie|credential|secret|password|"
    r"access[_-]?token|refresh[_-]?token|dsn|private[_-]?key)", re.I
)
_INLINE_SECRET = re.compile(
    r"(?i)((?:[\"']?)(?:api[_-]?key|authorization|cookie|password|secret|"
    r"access[_-]?token|refresh[_-]?token)(?:[\"']?)\s*[:=]\s*(?:[\"']?))"
    r"([^\"'\s,;}\]]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/-]+")
_URL_PASSWORD = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s/@:]+:)[^\s/@]+(@)", re.I)
MAX_DEPTH = 8
MAX_ITEMS = 80
MAX_TEXT = 30_000


def sanitize(value: Any, depth: int = 0) -> Any:
    """Serialize bounded presentation data while redacting credentials centrally."""
    if depth >= MAX_DEPTH:
        return "[depth limit]"
    if isinstance(value, BaseMessage):
        return {
            "role": value.type,
            "content": sanitize(value.content, depth + 1),
        }
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        text = value[:MAX_TEXT]
        if text[:1] in {"{", "["}:
            try:
                return json.dumps(sanitize(json.loads(text), depth + 1), ensure_ascii=False)
            except (TypeError, ValueError):
                pass
        text = _URL_PASSWORD.sub(r"\1[REDACTED]\2", text)
        text = _BEARER.sub("Bearer [REDACTED]", text)
        return _INLINE_SECRET.sub(r"\1[REDACTED]", text)
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _SECRET_KEY.search(str(key))
            else sanitize(item, depth + 1)
            for key, item in list(value.items())[:MAX_ITEMS]
        }
    if isinstance(value, list | tuple):
        return [sanitize(item, depth + 1) for item in value[:MAX_ITEMS]]
    if hasattr(value, "model_dump"):
        return sanitize(value.model_dump(), depth + 1)
    return sanitize(str(value), depth + 1)


def _usage(output: Any) -> dict[str, Any] | None:
    """Read provider reported usage only; absent fields stay absent."""
    candidate = output
    generations = candidate.get("generations") if isinstance(candidate, Mapping) else getattr(candidate, "generations", None)
    if generations and generations[0]:
        generation = generations[0][0]
        candidate = generation.get("message") if isinstance(generation, Mapping) else getattr(generation, "message", None)
    usage = getattr(candidate, "usage_metadata", None)
    if usage is None and isinstance(candidate, Mapping):
        usage = candidate.get("usage_metadata")
    return sanitize(usage) if usage is not None else None


def _has_token(chunk: Any) -> bool:
    content = chunk.get("content") if isinstance(chunk, Mapping) else getattr(chunk, "content", None)
    tool_chunks = chunk.get("tool_call_chunks") if isinstance(chunk, Mapping) else getattr(chunk, "tool_call_chunks", None)
    return bool(content or tool_chunks or (isinstance(chunk, str) and chunk))


class TrajectoryProjector:
    """Per-request sequence and timing state for browser display only."""

    def __init__(self, thread_id: str):
        """Initialize a fresh per-request sequence and timing map."""
        self.thread_id = thread_id
        self.seq = 0
        self.started: dict[str, int] = {}
        self.first_token: dict[str, int] = {}

    def project(self, source: Mapping[str, Any]) -> dict[str, Any] | None:
        """Map one native v2 event to a bounded browser event when relevant."""
        event_type = str(source.get("event", ""))
        name = str(source.get("name", ""))
        run_id = str(source.get("run_id", ""))
        metadata = source.get("metadata") or {}
        data = source.get("data") or {}
        if not isinstance(metadata, Mapping) or not isinstance(data, Mapping):
            return None
        kind = None
        if event_type.startswith("on_chat_model_") or event_type.startswith("on_llm_"):
            kind = "model_" + event_type.rsplit("_", 1)[-1]
        elif event_type.startswith("on_tool_"):
            kind = "tool_" + event_type.rsplit("_", 1)[-1]
        elif event_type.endswith("_retry") or event_type == "on_retry":
            kind = "retry"
        elif event_type.startswith("on_chain_"):
            node_name = metadata.get("langgraph_node") or metadata.get("node_name")
            if event_type.endswith(("start", "end", "error")) and (
                metadata.get("agent_name") or node_name == name or name.endswith("_agent")
            ):
                kind = "agent_" + event_type.rsplit("_", 1)[-1]
        if kind is None:
            return None
        now = int(time.time() * 1000)
        if kind.endswith("start"):
            self.started[run_id] = now
        if kind == "model_stream":
            # The first real model chunk is the only valid TTFT source.
            if run_id in self.first_token or not _has_token(data.get("chunk")):
                return None
            self.first_token[run_id] = now
            kind = "model_first_token"
        self.seq += 1
        parent_ids = [str(item) for item in source.get("parent_ids") or []]
        safe_metadata = sanitize({
            key: metadata[key] for key in (
                "agent_name", "agent_role", "node_name", "langgraph_node",
                "business_scenario", "retrieval_mode", "rag_enabled",
            ) if key in metadata
        })
        payload = {
            "seq": self.seq,
            "timestamp": now,
            "kind": kind,
            "run_id": run_id,
            "parent_ids": parent_ids,
            "thread_id": self.thread_id,
            "name": name,
            "agent_name": safe_metadata.get("agent_name") or safe_metadata.get("langgraph_node"),
            "metadata": safe_metadata,
            "status": "running" if kind.endswith("start") else "error" if kind.endswith("error") else "completed",
        }
        if kind.startswith("model_"):
            provider = metadata.get("ls_provider") or metadata.get("provider")
            model = metadata.get("ls_model_name") or metadata.get("model_name")
            if provider is not None:
                payload["provider"] = sanitize(str(provider))
            if model is not None:
                payload["model"] = sanitize(str(model))
        if kind.endswith("start") and "input" in data:
            payload["input"] = sanitize(data["input"])
        if kind.endswith("end") and "output" in data:
            payload["output"] = sanitize(data["output"])
            usage = _usage(data["output"])
            if usage is not None:
                payload["usage"] = usage
        if kind.endswith("error"):
            payload["error"] = sanitize(str(data.get("error", "Unknown error")))
        if kind == "retry":
            payload["input"] = sanitize(data)
        if kind == "model_first_token":
            payload["ttft_ms"] = now - self.started[run_id] if run_id in self.started else None
        if kind.endswith(("end", "error")):
            started = self.started.pop(run_id, None)
            payload["duration_ms"] = now - started if started is not None else None
            first = self.first_token.pop(run_id, None)
            if first is not None and started is not None:
                payload["ttft_ms"] = first - started
        return payload

    def terminal(self, status: str) -> dict[str, Any]:
        """Close a research request without storing trace state."""
        self.seq += 1
        return {
            "seq": self.seq,
            "timestamp": int(time.time() * 1000),
            "kind": "run_end",
            "run_id": self.thread_id,
            "parent_ids": [],
            "thread_id": self.thread_id,
            "name": "research",
            "status": status,
        }
