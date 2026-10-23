"""Typed JSON and redacted public Run protocol; no execution state machine."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from dbos import Serializer
from langchain_core.messages import BaseMessage, message_to_dict, messages_from_dict
from pydantic import BaseModel

from open_deep_research.state import ResearchReview, ResearchTask, Section

TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
_MODELS = {c.__name__: c for c in (Section, ResearchReview, ResearchTask)}


class RunConflict(ValueError):
    """Reject a stale, illegal or unsafe control operation."""

    configuration_changed: list[str]


class RunNotFound(LookupError):
    """Hide both absent and foreign-owned resources."""


class ExecutionRevoked(RunConflict):
    """Reject a revoked business execution permission."""


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseException):
        return {"__dbos_error__": type(value).__name__}
    if isinstance(value, BaseMessage):
        return {"__p4_message__": message_to_dict(value)}
    if isinstance(value, BaseModel) and type(value).__name__ in _MODELS:
        return {
            "__p4_model__": type(value).__name__,
            "value": value.model_dump(mode="json"),
        }
    raise TypeError(f"Unsupported persisted type: {type(value).__name__}")


def dumps(value: Any) -> str:
    """Serialize only JSON and the known Workflow data types, never pickle."""
    return json.dumps(value, default=_json_default, ensure_ascii=False, sort_keys=True)


def loads(value: str) -> Any:
    """Restore known state types without importing names from stored input."""

    def restore(item):
        if "__dbos_error__" in item:
            return RuntimeError(item["__dbos_error__"])
        if "__p4_message__" in item:
            return messages_from_dict([item["__p4_message__"]])[0]
        if item.get("__p4_model__") in _MODELS:
            return _MODELS[item["__p4_model__"]].model_validate(item["value"])
        return item

    return json.loads(value, object_hook=restore)


def digest(value: Any) -> str:
    """Hash canonical request/operation facts without storing sensitive arguments."""
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def timestamp(seconds: float) -> str:
    """Format the database clock in UTC."""
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def public_payload(value: Any) -> Any:
    """Redact known operator credentials and credential-shaped strings at egress."""
    if isinstance(value, dict):
        return {key: public_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_payload(item) for item in value]
    if isinstance(value, str):
        for name, secret in os.environ.items():
            if (
                name.endswith(("_API_KEY", "_TOKEN", "_PASSWORD", "_SECRET"))
                and len(secret) >= 8
            ):
                value = value.replace(secret, "[redacted]")
        return re.sub(
            r"(?i)(bearer\s+|(?:api[_-]?key|password|secret)\s*[=:]\s*)[^\s,;\"']+",
            r"\1[redacted]",
            value,
        )
    return value


class RunJSON(Serializer):
    def name(self):
        return "por_run_json_v1"

    def serialize(self, value):
        return dumps(value)

    def deserialize(self, value):
        return loads(value)


def public_run(run: dict) -> dict:
    return public_payload({k: v for k, v in run.items() if k not in {"owner", "seq"}})
