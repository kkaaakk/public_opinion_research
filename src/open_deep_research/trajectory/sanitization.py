"""Redact before storage or publication; bound large payloads explicitly."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import BaseMessage

_SECRET_KEY = re.compile(
    r"api[_-]?key|authorization|cookie|credential|secret|password|"
    r"access[_-]?token|refresh[_-]?token|dsn|private[_-]?key", re.I
)
_INLINE = re.compile(
    r"(?i)((?:[\"']?)(?:api[_-]?key|authorization|cookie|password|secret|"
    r"access[_-]?token|refresh[_-]?token|dsn)(?:[\"']?)\s*[:=]\s*)"
    r"(?!\[REDACTED\])(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/-]+")
_URL_PASSWORD = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s/@:]+:)[^\s/@]+(@)", re.I)
_KEY_VALUE = re.compile(r"\b(?:sk|lsv2)[-_][a-zA-Z0-9_-]{12,}\b")


def sanitize(value: Any, depth: int = 0) -> Any:
    """Detach JSON data and remove credentials without silently clipping history."""
    if depth > 24:
        return {"truncated": True, "reason": "depth_limit"}
    if isinstance(value, BaseMessage):
        return sanitize({
            "role": value.type, "content": value.content,
            **({"tool_calls": value.tool_calls} if hasattr(value, "tool_calls") else {}),
        }, depth + 1)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "[non-finite]"
    if isinstance(value, str):
        if value.lstrip()[:1] in {"{", "["}:
            try:
                return json.dumps(sanitize(json.loads(value), depth + 1), ensure_ascii=False)
            except (ValueError, TypeError):
                pass
        text = _URL_PASSWORD.sub(r"\1[REDACTED]\2", value)
        text = _BEARER.sub("Bearer [REDACTED]", text)
        text = _KEY_VALUE.sub("[REDACTED]", text)
        for key, secret in os.environ.items():
            if _SECRET_KEY.search(key) and len(secret) >= 8 and secret not in {"[REDACTED]", "Bearer [REDACTED]"}:
                text = text.replace(secret, "[REDACTED]")
        return _INLINE.sub(r"\1[REDACTED]", text)
    if isinstance(value, Mapping):
        return {str(k): "[REDACTED]" if _SECRET_KEY.search(str(k)) else sanitize(v, depth + 1)
                for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [sanitize(v, depth + 1) for v in value]
    if hasattr(value, "model_dump"):
        return sanitize(value.model_dump(), depth + 1)
    raise TypeError(f"Unsupported trajectory value: {type(value).__name__}")


def bounded(value: Any, limit_bytes: int = 65_536) -> Any:
    """Retain sanitized data or a labeled UTF-8 preview with original size."""
    clean = sanitize(value)
    encoded = json.dumps(clean, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) <= limit_bytes:
        return clean
    return {"preview": encoded[:limit_bytes].decode("utf-8", errors="ignore"),
            "truncated": True, "original_size": len(encoded), "limit_bytes": limit_bytes}


def error_data(error: BaseException, kind: str) -> dict[str, Any]:
    """Keep an error's category and safe identity without serializing its internals."""
    message = "Workflow failed; inspect the correlated execution trace." if kind == "workflow_error" else sanitize(str(error))
    return {"kind": kind, "name": type(error).__name__, "message": message,
            **({"code": sanitize(error.code)} if hasattr(error, "code") else {})}
