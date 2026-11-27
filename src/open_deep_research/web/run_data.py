"""Typed JSON and redacted public Run protocol; no execution state machine."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Any

from agents.items import ModelResponse
from dbos import Serializer
from langchain_core.messages import BaseMessage, message_to_dict, messages_from_dict
from pydantic import BaseModel, TypeAdapter
from sqlalchemy import text

from open_deep_research.llm.errors import ModelInvocationError
from open_deep_research.state import (
    ClarifyWithUser,
    ResearchQuestion,
    ResearchReview,
    ResearchTask,
    Summary,
)
from open_deep_research.workflow.brief import BriefRevision, PlanDecision

IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
_MODELS = {
    c.__name__: c
    for c in (
        ResearchReview,
        ResearchTask,
        ClarifyWithUser,
        ResearchQuestion,
        Summary,
        PlanDecision,
        BriefRevision,
    )
}
_SDK_RESPONSE = TypeAdapter(ModelResponse)


class RunConflict(ValueError):
    """Reject a stale, illegal or unsafe control operation."""

    configuration_changed: list[str]


class RunNotFound(LookupError):
    """Hide both absent and foreign-owned resources."""


def _json_default(value: Any) -> Any:
    if isinstance(value, ModelResponse):
        return {
            "__por_sdk_response__": _SDK_RESPONSE.dump_python(
                value, mode="json", exclude_unset=True
            )
        }
    if isinstance(value, BaseException):
        from open_deep_research.runtime.sdk_context import (
            SDKContextLimitExceeded,
            _ReasoningStopped,
        )

        if isinstance(value, _ReasoningStopped):
            return {
                "__por_reasoning_stop__": str(value),
                "budget_usage": getattr(value, "budget_usage", {}),
            }
        if isinstance(value, SDKContextLimitExceeded):
            return {
                "__por_context_limit__": str(value),
                "budget_usage": getattr(value, "budget_usage", {}),
            }
        if isinstance(value, ModelInvocationError):
            return {
                "__por_model_error__": {
                    "error_type": value.error_type,
                    "context_limit": value.context_limit,
                    "budget_usage": value.budget_usage,
                }
            }
        from open_deep_research.budget import ModelBudgetExceeded
        from open_deep_research.workflow.execution import ExecutionControlError

        if isinstance(value, (ModelBudgetExceeded, ExecutionControlError)):
            return {
                "__por_control_error__": type(value).__name__,
                "budget_usage": getattr(value, "budget_usage", {}),
                "unknown_outcome": getattr(value, "unknown_outcome", False),
            }
        return {
            "__dbos_error__": type(value).__name__,
            "budget_usage": getattr(value, "budget_usage", {}),
        }
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
        if "__por_reasoning_stop__" in item:
            from open_deep_research.runtime.sdk_context import _ReasoningStopped

            error = _ReasoningStopped(item["__por_reasoning_stop__"])
            error.budget_usage = item.get("budget_usage", {})
            return error
        if "__por_context_limit__" in item:
            from open_deep_research.runtime.sdk_context import SDKContextLimitExceeded

            error = SDKContextLimitExceeded(item["__por_context_limit__"])
            error.budget_usage = item["budget_usage"]
            return error
        if "__por_model_error__" in item:
            return ModelInvocationError(**item["__por_model_error__"])
        if "__por_control_error__" in item:
            from open_deep_research.budget import ModelBudgetExceeded
            from open_deep_research.workflow.execution import ExecutionControlError

            cls = {
                "ModelBudgetExceeded": ModelBudgetExceeded,
                "ExecutionControlError": ExecutionControlError,
            }[item["__por_control_error__"]]
            error = cls(item["__por_control_error__"])
            error.budget_usage = item["budget_usage"]
            error.unknown_outcome = item.get("unknown_outcome", False)
            return error
        if "__por_sdk_response__" in item:
            return _SDK_RESPONSE.validate_python(item["__por_sdk_response__"])
        if "__dbos_error__" in item:
            error = RuntimeError(item["__dbos_error__"])
            error.budget_usage = item.get("budget_usage", {})
            return error
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
    return public_payload(
        {
            key: value
            for key, value in run.items()
            if key not in {"owner", "seq", "configuration_source"}
        }
    )


def sdk_observation(payload: dict) -> dict:
    """Allow only bounded metadata, never arbitrary hook payloads or model content."""
    strings = {
        "role",
        "name",
        "invocation_id",
        "task_id",
        "tool_call_id",
        "operation_id",
        "stage",
        "error_type",
        "reason",
        "status",
        "model",
    }
    numbers = {
        "model_calls",
        "tool_calls",
        "search_calls",
        "model_turn",
        "research_round",
        "duration_ms",
        "batch_size",
        "context_chars",
        "compacted_calls",
        "nodes",
        "edges",
        "pending_calls",
        "in_flight_calls",
        "unexecuted_calls",
        "removed_turns",
    }
    result = {
        key: value[:160]
        for key, value in payload.items()
        if key in strings and isinstance(value, str)
    }
    result.update(
        {
            key: value
            for key, value in payload.items()
            if key in numbers
            and type(value) in {int, float}
            and math.isfinite(value)
            and value >= 0
        }
    )
    if isinstance(payload.get("usage"), dict):
        result["usage"] = {
            key: value
            for key, value in payload["usage"].items()
            if key in {"input_tokens", "output_tokens", "total_tokens"}
            and type(value) is int
            and value >= 0
        }
    return result


def db_time(db):
    return float(db.scalar(text("SELECT extract(epoch from clock_timestamp())")))


def read_request(run_id, owner=None, *, db=None, lock=False):
    from open_deep_research.workflow.dbos_config import domain_engine

    if db is None:
        with domain_engine().connect() as connection:
            return read_request(run_id, owner, db=connection, lock=lock)
    query = "SELECT * FROM por_domain.requests WHERE id=:id" + (
        " FOR UPDATE" if lock else ""
    )
    row = db.execute(text(query), {"id": run_id}).mappings().first()
    if row is None or (owner is not None and row["owner"] != owner):
        raise RunNotFound("Run or Session not found")
    return dict(row) | {"data": loads(row["value"])}


def read_request_images(run_id, position=None):
    """Load immutable image bytes only on demand, outside normal Run/status reads."""
    from open_deep_research.workflow.dbos_config import domain_engine

    with domain_engine().connect() as db:
        return db.execute(
            text("""
                SELECT media_type, content FROM por_domain.request_images
                WHERE run_id = :run_id AND (CAST(:position AS int) IS NULL OR position = :position)
                ORDER BY position
                """),
            {"run_id": run_id, "position": position},
        ).all()


def publish_event(db, run_id, kind, *, idempotency_key=None, **payload):
    from open_deep_research.workflow.dbos_config import native_client

    payload.setdefault("observed_at", timestamp(db_time(db)))
    if "generation" not in payload:
        payload["generation"] = retry_generation(db, run_id)
    native_client().send_in_transaction(
        db,
        run_id + ":events",
        public_payload(dict(type=kind, **payload)),
        topic="events",
        idempotency_key=idempotency_key,
    )


def retry_generation(db, run_id):
    return db.scalar(
        text(
            "SELECT coalesce(max(generation),0) FROM por_domain.retries WHERE run_id=:id"
        ),
        {"id": run_id},
    )


def finalizer_id(run_id, generation=0):
    return f"{run_id}:finalize:{generation}" if generation else run_id + ":finalize"


def result_budget(result):
    if role := result.get("research_result"):
        return role["budget_delta"]
    return result.get("patch", {}).get("runtime", {}).get("budget", {})


def stage_budget(result):
    budget = result_budget(result)
    return {
        key: budget[key]
        for key in ("budget_warnings", "degradation_reasons")
        if budget.get(key)
    }


def role_deliverables(result):
    """Extract public role text from a native result without its private context."""
    raw = result.get("patch", {}).get("agents", {})
    if producer := result.get("research_result"):
        raw = {producer["role"]: {"report": producer["report"]}}
    reports = {}
    for role, data in raw.items():
        report = data.get("report")
        if isinstance(report, dict):
            report = report.get("value")
        if report:
            reports[role] = report
    return reports


def legacy_projection(db, run_id):
    from open_deep_research.budget import diff_budget_usage, merge_budget_usage
    from open_deep_research.workflow.dbos_config import LEGACY_SCHEMA

    if not db.scalar(
        text("SELECT to_regclass(:name)"), {"name": f"{LEGACY_SCHEMA}.units"}
    ):
        return {}, {}, False
    view = {}
    usage = {}
    unresolved = False
    for stage in db.execute(
        text(f"""
        SELECT u.id,u.name,u.artifact,u.usage,u.error,u.settled,
            ARRAY(SELECT o.usage FROM {LEGACY_SCHEMA}.operations o WHERE o.unit=u.id) AS costs,
            EXISTS(SELECT 1 FROM {LEGACY_SCHEMA}.operations o
                   WHERE o.unit=u.id AND o.status IN ('started','uncertain')) AS unknown_effect
        FROM {LEGACY_SCHEMA}.units u WHERE u.run_id=:id ORDER BY u.id
        """),
        {"id": run_id},
    ).mappings():
        billed = loads(stage["usage"])
        if not stage["settled"]:
            lower_bound = {}
            for cost in stage["costs"]:
                lower_bound = merge_budget_usage(lower_bound, loads(cost))
            billed = merge_budget_usage(billed, diff_budget_usage(lower_bound, billed))
        usage = merge_budget_usage(usage, billed)
        unresolved |= not stage["settled"] or stage["unknown_effect"]
        patch = loads(stage["artifact"])["patch"] if stage["artifact"] else {}
        if stage["artifact"]:
            view.setdefault("completed_steps", []).append(stage["name"])
        else:
            view["current_step"] = stage["name"]
        if error := loads(stage["error"]):
            view["error"] = dict(error, retry_safe=False)
        if report := patch.get("report", {}).get("final"):
            view["legacy_report"] = report
        for role, content in role_deliverables({"patch": patch}).items():
            view.setdefault("legacy_roles", []).append(
                dict(role=role, content=content, stage_id=stage["id"], completed_at="")
            )
    return view, usage, unresolved
