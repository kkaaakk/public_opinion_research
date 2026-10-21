"""Own durable Runs, the SQL task queue, fenced attempts and the event outbox.

All control writes lock one small scheduler row. No lock is held while running
business code. MySQL/InnoDB supports shared-host workers; file SQLite provides
the same contract on one machine. Evidence and archives remain in their stores.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from langchain_core.messages import BaseMessage, message_to_dict, messages_from_dict
from pydantic import BaseModel
from sqlalchemy import (
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.exc import IntegrityError

from open_deep_research.configuration import Configuration
from open_deep_research.runtime.sdk_runtime import SDK_RUNTIME_VERSION
from open_deep_research.state import ResearchReview, ResearchTask, Section
from open_deep_research.workflow.python_workflow import WORKFLOW_VERSION

SDK_VERSION = "0.24.0"
TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
TRANSITIONS = {
    "queued": {"running", "cancelled"},
    "running": {"awaiting_approval", "cancelling", *TERMINAL},
    "awaiting_approval": {"queued", "cancelled"},
    "cancelling": {"cancelled", "interrupted"},
    "failed": {"queued"},
    "interrupted": {"queued"},
    "completed": set(),
    "cancelled": set(),
}
# These calls read inputs/models and cannot replay a business tool or memory write.
SAFE_RETRY_STEPS = frozenset(
    {
        "enrich_query_images",
        "clarify_with_user",
        "write_research_brief",
        "plan_report_sections",
        "research_review",
        "section_writer",
        "write_final_sections",
    }
)
_MODELS = {c.__name__: c for c in (Section, ResearchReview, ResearchTask)}


class RunConflict(ValueError):
    """Reject a stale, illegal or unsafe control operation."""

    configuration_changed: list[str]


class RunNotFound(LookupError):
    """Hide both absent and foreign-owned resources."""


class LeaseLost(RunConflict):
    """Fence an expired attempt before further work or commits."""


def _json_default(value: Any) -> Any:
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


class RunStore:
    """Commit queue ownership, state, checkpoints and public events atomically."""

    def __init__(
        self,
        url: str,
        *,
        max_active: int = 4,
        owner_active: int = 2,
        queue_capacity: int = 1000,
        lease_seconds: float = 60,
        approval_seconds: float = 86400,
    ) -> None:
        """Create additive Phase 4 tables; persist and verify shared queue policy."""
        if (
            min(
                max_active,
                owner_active,
                queue_capacity,
                lease_seconds,
                approval_seconds,
            )
            <= 0
        ):
            raise ValueError("Queue limits must be positive")
        self.policy = dict(
            schema_version=1,
            max_active=max_active,
            owner_active=owner_active,
            queue_capacity=queue_capacity,
            lease_seconds=lease_seconds,
            approval_seconds=approval_seconds,
        )
        self.engine = create_engine(
            url,
            pool_pre_ping=True,
            **(
                {"connect_args": {"check_same_thread": False, "timeout": 30}}
                if url.startswith("sqlite:")
                else {}
            ),
        )
        if self.engine.dialect.name not in {"sqlite", "mysql"}:
            self.engine.dispose()
            raise ValueError("Run database must be file SQLite or MySQL/InnoDB")
        if self.engine.dialect.name == "sqlite":
            if self.engine.url.database in {None, "", ":memory:"}:
                self.engine.dispose()
                raise ValueError("Durable Runs require a file-backed SQLite database")

            @event.listens_for(self.engine, "connect")
            def configure(connection, _record):
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")

        metadata = MetaData()
        text_type = Text().with_variant(LONGTEXT(), "mysql")
        self.scheduler = Table(
            "por_run_scheduler",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("policy", text_type, nullable=False),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_bin",
        )
        self.runs = Table(
            "por_runs",
            metadata,
            Column("run_id", String(64), primary_key=True),
            Column("owner", String(128), nullable=False),
            Column("request_key", String(128), nullable=False),
            Column("session_id", String(64), nullable=False),
            Column("status", String(32), nullable=False),
            Column("version", Integer, nullable=False),
            Column("seq", Integer, nullable=False),
            Column("attempt_id", String(64), nullable=False),
            Column("lease_until", Float(precision=53), nullable=False),
            Column("quarantined", Integer, nullable=False),
            Column("created_at", Float(precision=53), nullable=False),
            Column("updated_at", Float(precision=53), nullable=False),
            Column("data", text_type, nullable=False),
            UniqueConstraint("owner", "request_key", name="por_request_identity"),
            Index("por_queue_scan", "status", "created_at"),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_bin",
        )
        self.events = Table(
            "por_run_events",
            metadata,
            Column("run_id", String(64), primary_key=True),
            Column("seq", Integer, primary_key=True),
            Column("data", text_type, nullable=False),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_bin",
        )
        self.attempts = Table(
            "por_run_attempts",
            metadata,
            Column("attempt_id", String(64), primary_key=True),
            Column("run_id", String(64), nullable=False),
            Column("data", text_type, nullable=False),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_bin",
        )
        self.operations = Table(
            "por_run_operations",
            metadata,
            Column("operation_id", String(64), primary_key=True),
            Column("run_id", String(64), nullable=False),
            Column("attempt_id", String(64), nullable=False),
            Column("status", String(32), nullable=False),
            Column("data", text_type, nullable=False),
            Index("por_operation_run", "run_id", "status"),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_bin",
        )
        metadata.create_all(self.engine)
        try:
            with self.engine.begin() as db:
                db.execute(
                    insert(self.scheduler).values(id=1, policy=dumps(self.policy))
                )
        except IntegrityError:
            pass
        with self._write() as db:
            policy = loads(db.execute(select(self.scheduler.c.policy)).scalar_one())
            if policy != self.policy:
                self.engine.dispose()
                raise RunConflict("Workers must share the persisted queue policy")

    @contextmanager
    def _write(self):
        with self.engine.connect() as db:
            try:
                if self.engine.dialect.name == "sqlite":
                    db.exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    db.begin()
                db.execute(
                    select(self.scheduler.c.id)
                    .where(self.scheduler.c.id == 1)
                    .with_for_update()
                ).scalar_one()
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _now(self, db) -> float:
        query = (
            select((func.julianday("now") - 2440587.5) * 86400)
            if self.engine.dialect.name == "sqlite"
            else select(func.unix_timestamp(func.current_timestamp(6)))
        )
        return float(db.execute(query).scalar_one())

    def _get(self, db, run_id: str, owner: str | None = None) -> dict:
        row = (
            db.execute(select(self.runs).where(self.runs.c.run_id == run_id))
            .mappings()
            .first()
        )
        if row is None or (owner is not None and row["owner"] != owner):
            raise RunNotFound("Run not found")
        return {**row, "data": loads(row["data"])}

    def _save(self, db, run: dict) -> None:
        run["version"] += 1
        run["updated_at"] = self._now(db)
        db.execute(
            update(self.runs)
            .where(self.runs.c.run_id == run["run_id"])
            .values(**{k: dumps(v) if k == "data" else v for k, v in run.items()})
        )

    def _event(self, db, run: dict, kind: str, **payload) -> None:
        run["seq"] += 1
        data = dict(
            protocol_version=1,
            type=kind,
            run_id=run["run_id"],
            attempt_id=run["attempt_id"],
            seq=run["seq"],
            timestamp=timestamp(self._now(db)),
            **payload,
        )
        db.execute(
            insert(self.events).values(
                run_id=run["run_id"], seq=run["seq"], data=dumps(public_payload(data))
            )
        )

    def _transition(self, db, run: dict, status: str, **payload) -> None:
        if status not in TRANSITIONS[run["status"]]:
            raise RunConflict(f"Illegal Run transition: {run['status']} -> {status}")
        run["status"] = status
        if status in TERMINAL:
            run["data"]["terminated_at"] = timestamp(self._now(db))
            run["data"]["archive_status"] = "pending"
        self._event(db, run, "run_status", status=status, **payload)
        if status in {"failed", "cancelled", "interrupted"}:
            self._event(db, run, "error", message=f"Research {status}.", status=status)

    def _owned(self, db, run_id: str, attempt_id: str) -> dict:
        run = self._get(db, run_id)
        if (
            run["attempt_id"] != attempt_id
            or run["lease_until"] <= self._now(db)
            or run["status"] not in {"running", "cancelling"}
        ):
            raise LeaseLost("Attempt ownership expired")
        return run

    def create(
        self,
        owner: str,
        key: str,
        request: dict,
        config: dict,
        state: dict,
        *,
        session_id: str | None = None,
    ) -> tuple[dict, bool]:
        """Atomically accept one request or return its exact previous identity."""
        if not owner or len(owner) > 128 or not key or len(key) > 128:
            raise RunConflict("Owner and idempotency key are required")
        fingerprint = digest({"request": request, "session_id": session_id})
        with self._write() as db:
            row = db.execute(
                select(self.runs.c.run_id).where(
                    self.runs.c.owner == owner, self.runs.c.request_key == key
                )
            ).first()
            if row:
                run = self._get(db, row[0], owner)
                if run["data"]["fingerprint"] != fingerprint:
                    raise RunConflict("Idempotency key was used for another request")
                return run, False
            if session_id:
                session_owners = (
                    db.execute(
                        select(self.runs.c.owner).where(
                            self.runs.c.session_id == session_id
                        )
                    )
                    .scalars()
                    .all()
                )
                if not session_owners or any(
                    value != owner for value in session_owners
                ):
                    raise RunNotFound("Session not found")
            count = db.execute(
                select(func.count())
                .select_from(self.runs)
                .where(self.runs.c.status == "queued")
            ).scalar_one()
            if count >= self.policy["queue_capacity"]:
                raise RunConflict("Research queue is full")
            effective = Configuration.from_runnable_config(config).model_dump(
                mode="json"
            )
            if (
                effective["workflow_backend"] != "python"
                or effective["business_agent_runtime"] != "openai_agents"
            ):
                raise RunConflict(
                    "Operator configuration must enable python/openai_agents"
                )
            now = self._now(db)
            run_id = uuid4().hex
            state = loads(dumps(state))
            state.setdefault("research", {})["run_id"] = run_id
            config = loads(dumps(config))
            config.setdefault("configurable", {}).update(
                research_run_id=run_id,
                thread_id=session_id or uuid4().hex,
                user_id=owner,
            )
            run = dict(
                run_id=run_id,
                owner=owner,
                request_key=key,
                session_id=config["configurable"]["thread_id"],
                status="queued",
                version=0,
                seq=0,
                attempt_id="",
                lease_until=0.0,
                quarantined=0,
                created_at=now,
                updated_at=now,
                data=dict(
                    fingerprint=fingerprint,
                    request=request,
                    config=config,
                    workflow_version=WORKFLOW_VERSION,
                    sdk_version=SDK_VERSION,
                    runtime_version=SDK_RUNTIME_VERSION,
                    environment_fingerprint=digest(effective),
                    configuration_fingerprints={
                        name: digest(value) for name, value in effective.items()
                    },
                    approved_environment_fingerprint=digest(
                        dict(effective, allow_plan_feedback=False)
                    ),
                    state=state,
                    cursor="enrich_query_images",
                    active_step="",
                    completed_steps=[],
                    stages=[],
                    usage=state.get("runtime", {}).get("budget", {}),
                    error={},
                    settlement="settled",
                    plan=None,
                    approval=None,
                    archive_status="none",
                    history_id=None,
                ),
            )
            db.execute(insert(self.runs).values(**{**run, "data": dumps(run["data"])}))
            self._event(db, run, "run_status", status="queued")
            self._save(db, run)
            return run, True

    def get(self, run_id: str, owner: str) -> dict:
        """Read a Run only in its trusted owner scope."""
        with self.engine.connect() as db:
            return self._get(db, run_id, owner)

    def read_events(
        self, run_id: str, owner: str, after: int, limit: int = 100
    ) -> list[dict]:
        """Replay persisted events without scheduling any work."""
        with self.engine.connect() as db:
            run = self._get(db, run_id, owner)
            if after < 0 or after > run["seq"]:
                raise RunConflict("Invalid event cursor")
            values = db.execute(
                select(self.events.c.data)
                .where(self.events.c.run_id == run_id, self.events.c.seq > after)
                .order_by(self.events.c.seq)
                .limit(limit)
            ).scalars()
            return [loads(value) for value in values]

    def claim(self, worker_id: str) -> dict | None:
        """Reserve a global/owner/session slot before delivering a fenced attempt."""
        with self._write() as db:
            busy = (
                db.execute(
                    select(self.runs).where(
                        (self.runs.c.status.in_(["running", "cancelling"]))
                        | (self.runs.c.quarantined == 1)
                    )
                )
                .mappings()
                .all()
            )
            if len(busy) >= self.policy["max_active"]:
                return None
            queued = (
                db.execute(
                    select(self.runs.c.run_id)
                    .where(self.runs.c.status == "queued")
                    .order_by(self.runs.c.created_at)
                    .limit(1000)
                )
                .scalars()
                .all()
            )
            for run_id in queued:
                run = self._get(db, run_id)
                if any(r["session_id"] == run["session_id"] for r in busy):
                    continue
                if (
                    sum(r["owner"] == run["owner"] for r in busy)
                    >= self.policy["owner_active"]
                ):
                    continue
                run["attempt_id"] = uuid4().hex
                run["lease_until"] = self._now(db) + self.policy["lease_seconds"]
                self._transition(db, run, "running")
                db.execute(
                    insert(self.attempts).values(
                        attempt_id=run["attempt_id"],
                        run_id=run_id,
                        data=dumps(
                            dict(
                                worker_id=worker_id,
                                status="running",
                                started_at=timestamp(self._now(db)),
                                initial_usage=run["data"]["usage"],
                                workflow_version=WORKFLOW_VERSION,
                            )
                        ),
                    )
                )
                self._save(db, run)
                return run
            return None

    def heartbeat(self, run_id: str, attempt_id: str) -> str:
        """Extend only an unexpired lease and return durable cancellation state."""
        with self._write() as db:
            run = self._owned(db, run_id, attempt_id)
            run["lease_until"] = self._now(db) + self.policy["lease_seconds"]
            self._save(db, run)
            return run["status"]

    def progress(self, run_id: str, attempt_id: str, kind: str, **facts) -> None:
        """Persist a boundary or an allowlisted SDK lifecycle fact."""
        with self._write() as db:
            run = self._owned(db, run_id, attempt_id)
            data = run["data"]
            if kind == "step_started":
                if run["status"] == "cancelling":
                    raise LeaseLost("Run is cancelling")
                data["active_step"] = facts["step"]
                data["settlement"] = "pending"
                self._event(db, run, kind, step=facts["step"])
                self._event(
                    db,
                    run,
                    "status",
                    message=f"Running {facts['step']}",
                    granularity="stage",
                )
            elif kind == "checkpoint":
                from open_deep_research.web.history import stage_reports_from_state

                data.update(
                    state=facts["state"],
                    cursor=facts["cursor"],
                    active_step="",
                    usage=facts["state"].get("runtime", {}).get("budget", {}),
                    settlement="settled",
                )
                step_id = f"{facts['step']}:{facts['state'].get('workflow', {}).get('round', 0)}"
                data["completed_steps"].append(step_id)
                known = {(s["role"], s["content"]) for s in data["stages"]}
                for stage in stage_reports_from_state(
                    facts["state"],
                    stage_id=step_id,
                    completed_at=timestamp(self._now(db)),
                ):
                    if (stage.role, stage.content) not in known:
                        data["stages"].append(stage.model_dump())
                        self._event(
                            db,
                            run,
                            "agent_report",
                            role=stage.role,
                            content=stage.content,
                        )
                        self._event(
                            db,
                            run,
                            "stream",
                            node=f"{stage.role}_agent",
                            content=stage.content,
                            granularity="stage",
                        )
                self._event(db, run, "step_completed", step=facts["step"])
                if facts["step"] == "research_review":
                    review = facts["state"].get("workflow", {}).get("review")
                    self._event(
                        db,
                        run,
                        "research_review",
                        review=(
                            review.model_dump()
                            if isinstance(review, BaseModel)
                            else review
                        ),
                    )
                if facts["step"] == "followup" and facts["cursor"] == "research_agents":
                    self._event(
                        db,
                        run,
                        "research_followup",
                        round=facts["state"].get("workflow", {}).get("round", 1) + 1,
                    )
            elif kind == "approval_required":
                # The previous plan checkpoint already includes all planner usage.
                plan = dict(
                    brief=facts["state"].get("workflow", {}).get("brief", ""),
                    sections=facts["state"].get("report", {}).get("sections", []),
                )
                if data["plan"] is not None:
                    raise RunConflict("Plan is immutable")
                data["plan"] = dict(
                    version=digest(plan),
                    plan=plan,
                    usage=data["usage"],
                    expires_at=self._now(db) + self.policy["approval_seconds"],
                )
                self._transition(
                    db, run, "awaiting_approval", plan_version=digest(plan)
                )
                self._end_attempt(db, run, "awaiting_approval")
            else:
                # No prompt, model input, raw tool argument/output or arbitrary
                # exception text can enter the public event log through this seam.
                if (
                    kind in {"model_started", "agent_started"}
                    and run["status"] == "cancelling"
                ):
                    raise LeaseLost("Run is cancelling")
                allowed = {
                    k: facts[k]
                    for k in (
                        "role",
                        "name",
                        "operation_id",
                        "status",
                        "model_calls",
                        "invocation_id",
                    )
                    if k in facts
                }
                self._event(db, run, "sdk_event", event=kind, **allowed)
            self._save(db, run)

    def operation(
        self, run_id: str, attempt_id: str, identity: dict, *, status: str = "started"
    ) -> str:
        """Write intent before a tool and a receipt after it; never replay uncertainty."""
        operation_id = digest(
            dict(
                run_id=run_id,
                attempt_id=attempt_id,
                **{k: v for k, v in identity.items() if k != "arguments"},
            )
        )
        with self._write() as db:
            run = self._owned(db, run_id, attempt_id)
            if status == "started" and run["status"] == "cancelling":
                raise LeaseLost("Run is cancelling")
            row = (
                db.execute(
                    select(self.operations).where(
                        self.operations.c.operation_id == operation_id
                    )
                )
                .mappings()
                .first()
            )
            fingerprint = digest(identity)
            if status == "started":
                if row:
                    raise RunConflict(
                        "Tool operation already exists; reconcile instead of replaying"
                    )
                db.execute(
                    insert(self.operations).values(
                        operation_id=operation_id,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        status=status,
                        data=dumps(
                            dict(
                                fingerprint=fingerprint,
                                role=identity["role"],
                                name=identity["name"],
                                native_call_id=identity.get("tool_call_id"),
                                sdk_invocation_id=identity.get("invocation_id"),
                                task_id=identity.get("task_id"),
                                research_round=identity.get("research_round"),
                                started_at=timestamp(self._now(db)),
                            )
                        ),
                    )
                )
            else:
                if row is None or loads(row["data"])["fingerprint"] != fingerprint:
                    raise RunConflict("Tool receipt identity mismatch")
                if row["status"] != "started":
                    raise RunConflict("Tool receipt already committed")
                db.execute(
                    update(self.operations)
                    .where(self.operations.c.operation_id == operation_id)
                    .values(status=status)
                )
            self._event(
                db,
                run,
                "sdk_event",
                event="tool",
                operation_id=operation_id,
                role=identity["role"],
                name=identity["name"],
                status=status,
            )
            self._save(db, run)
        return operation_id

    def _end_attempt(self, db, run: dict, status: str) -> None:
        row = db.execute(
            select(self.attempts.c.data).where(
                self.attempts.c.attempt_id == run["attempt_id"]
            )
        ).scalar_one()
        data = loads(row)
        data.update(
            status=status,
            ended_at=timestamp(self._now(db)),
            usage=run["data"]["usage"],
            error=run["data"]["error"],
            settlement=run["data"]["settlement"],
        )
        db.execute(
            update(self.attempts)
            .where(self.attempts.c.attempt_id == run["attempt_id"])
            .values(data=dumps(data))
        )
        run["lease_until"] = 0.0

    def _retain_stages(self, db, run: dict, state: dict) -> None:
        from open_deep_research.web.history import stage_reports_from_state

        data = run["data"]
        known = {(s["role"], s["content"]) for s in data["stages"]}
        for stage in stage_reports_from_state(
            state,
            stage_id=f"{data['active_step']}:{run['attempt_id']}",
            completed_at=timestamp(self._now(db)),
        ):
            if (stage.role, stage.content) not in known:
                data["stages"].append(stage.model_dump())
                self._event(
                    db, run, "agent_report", role=stage.role, content=stage.content
                )

    def finish(
        self,
        run_id: str,
        attempt_id: str,
        status: str,
        state: dict,
        *,
        error: dict | None = None,
        settled: bool = True,
    ) -> None:
        """Commit one terminal result, archive intent and events in one transaction."""
        with self._write() as db:
            run = self._owned(db, run_id, attempt_id)
            data = run["data"]
            if run["status"] == "cancelling":
                status = "cancelled"
            if not settled:
                status = "interrupted"
            data.update(
                state=state,
                usage=state.get("runtime", {}).get("budget", {}),
                error=error or {},
                settlement="settled" if settled else "unresolved",
            )
            # Failed pure reads can retry from their previous persisted boundary.
            data["retry_safe"] = settled and (
                data["active_step"] in SAFE_RETRY_STEPS or not data["active_step"]
            )
            run["quarantined"] = int(not settled)
            self._retain_stages(db, run, state)
            self._transition(db, run, status)
            self._event(
                db,
                run,
                "settlement",
                status=data["settlement"],
                usage=data["usage"],
                error=data["error"],
            )
            if state.get("report", {}).get("final"):
                self._event(db, run, "report", content=state["report"]["final"])
            self._event(
                db,
                run,
                "usage",
                **{
                    k: data["usage"][k]
                    for k in (
                        "model_calls",
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                    )
                    if k in data["usage"]
                },
            )
            self._end_attempt(db, run, status)
            self._event(db, run, "done", status=status, archive_status="pending")
            self._save(db, run)

    def cancel(self, run_id: str, owner: str) -> dict:
        """Signal an owned worker; queued/paused cancellation needs no worker."""
        with self._write() as db:
            run = self._get(db, run_id, owner)
            if run["status"] in TERMINAL or run["status"] == "cancelling":
                return run
            self._transition(
                db, run, "cancelling" if run["status"] == "running" else "cancelled"
            )
            self._save(db, run)
            return run

    def approve(
        self, run_id: str, owner: str, plan_version: str, decision: str
    ) -> dict:
        """Consume a versioned plan once; acceptance and enqueue are atomic."""
        with self._write() as db:
            run = self._get(db, run_id, owner)
            data = run["data"]
            if run["status"] != "awaiting_approval" or data["approval"] is not None:
                raise RunConflict("Approval is no longer pending")
            if data["plan"]["version"] != plan_version or decision not in {
                "approve",
                "reject",
            }:
                raise RunConflict("Invalid plan version or decision")
            expired = data["plan"]["expires_at"] <= self._now(db)
            data["approval"] = dict(
                owner=owner,
                decision="expired" if expired else decision,
                plan_version=plan_version,
                at=timestamp(self._now(db)),
            )
            data["cursor"] = "research_init"
            data["config"]["configurable"]["allow_plan_feedback"] = False
            data["environment_fingerprint"] = data["approved_environment_fingerprint"]
            data["configuration_fingerprints"]["allow_plan_feedback"] = digest(False)
            self._transition(
                db, run, "cancelled" if expired or decision == "reject" else "queued"
            )
            self._save(db, run)
            return run

    def retry(self, run_id: str, owner: str, key: str, expected_version: int) -> dict:
        """Enqueue only a proven safe boundary/read; never restart an Agent role."""
        with self._write() as db:
            run = self._get(db, run_id, owner)
            data = run["data"]
            requests = data.setdefault("retry_requests", {})
            if key in requests:
                if requests[key] != expected_version:
                    raise RunConflict("Retry key was used with another state version")
                return run
            if (
                not key
                or run["version"] != expected_version
                or run["quarantined"]
                or run["status"] not in {"failed", "interrupted"}
                or not data.get("retry_safe")
            ):
                raise RunConflict(
                    "Run requires reconciliation; no safe automatic retry"
                )
            data.update(
                retry_key=key,
                error={},
                active_step="",
                terminated_at=None,
                archive_status="none",
                archive_after=0,
                archive_attempts=0,
            )
            requests[key] = expected_version
            self._transition(db, run, "queued")
            self._save(db, run)
            return run

    def reconcile(self) -> int:
        """Expire ownership, quarantine in-flight work and identify safe checkpoints."""
        changed = 0
        with self._write() as db:
            now = self._now(db)
            ids = (
                db.execute(
                    select(self.runs.c.run_id).where(
                        self.runs.c.status.in_(
                            ["running", "cancelling", "awaiting_approval"]
                        )
                    )
                )
                .scalars()
                .all()
            )
            for run_id in ids:
                run = self._get(db, run_id)
                data = run["data"]
                if run["status"] == "awaiting_approval":
                    if data["plan"]["expires_at"] > now:
                        continue
                    data["approval"] = dict(decision="expired", at=timestamp(now))
                    self._transition(db, run, "cancelled")
                elif run["lease_until"] <= now:
                    unsafe = bool(data["active_step"])
                    data.update(
                        retry_safe=not unsafe and run["status"] != "cancelling",
                        settlement="unresolved" if unsafe else "settled",
                        error=dict(
                            kind="worker_lost",
                            step=data["active_step"],
                            usage_complete=not unsafe,
                        ),
                    )
                    run["quarantined"] = int(unsafe)
                    status = (
                        "cancelled"
                        if not unsafe and run["status"] == "cancelling"
                        else "interrupted"
                    )
                    self._transition(db, run, status)
                    self._end_attempt(db, run, status)
                else:
                    continue
                self._save(db, run)
                changed += 1
        return changed

    def release_quarantine(
        self,
        run_id: str,
        owner: str,
        expected_version: int,
        *,
        worker_stopped: bool,
        note: str,
    ) -> None:
        """Release capacity only after an operator verifies process death and effects.

        This does not make the interrupted business step resumable. A new
        research decision must account for the previously observed effects.
        """
        if not worker_stopped or not note.strip() or len(note) > 2000:
            raise RunConflict(
                "Confirm the worker stopped and record the reconciliation evidence"
            )
        with self._write() as db:
            run = self._get(db, run_id, owner)
            if (
                run["version"] != expected_version
                or not run["quarantined"]
                or run["status"] != "interrupted"
            ):
                raise RunConflict(
                    "Only the observed quarantined terminal can be reconciled"
                )
            run["quarantined"] = 0
            run["data"].update(
                retry_safe=False,
                reconciliation=dict(
                    owner=owner,
                    note=note,
                    worker_stopped=True,
                    at=timestamp(self._now(db)),
                ),
            )
            self._event(db, run, "reconciled", retry_safe=False)
            self._save(db, run)

    def archive_candidates(self, limit: int = 20) -> list[dict]:
        """Find retryable terminal archive projections independently of subscribers."""
        with self.engine.connect() as db:
            ids = db.execute(
                select(self.runs.c.run_id)
                .where(self.runs.c.status.in_(TERMINAL))
                .order_by(self.runs.c.updated_at)
            ).scalars()
            result = []
            for run_id in ids:
                run = self._get(db, run_id)
                if run["data"]["archive_status"] == "pending" and run["data"].get(
                    "archive_after", 0
                ) <= self._now(db):
                    result.append(run)
                if len(result) >= limit:
                    break
            return result

    def archive_result(self, run_id: str, version: int, history_id: str | None) -> None:
        """Commit an idempotent projection receipt only for the observed terminal."""
        with self._write() as db:
            run = self._get(db, run_id)
            if run["version"] != version or run["status"] not in TERMINAL:
                return
            if history_id:
                run["data"].update(archive_status="saved", history_id=history_id)
                self._event(
                    db, run, "history_saved", id=history_id, status=run["status"]
                )
                self._event(db, run, "done", status=run["status"])
            else:
                attempts = run["data"].get("archive_attempts", 0) + 1
                run["data"].update(
                    archive_attempts=attempts,
                    archive_after=self._now(db) + min(300, 2 ** min(8, attempts)),
                )
                self._event(
                    db,
                    run,
                    "archive_pending",
                    message="Report archive is pending; the Run retains its result.",
                )
            self._save(db, run)

    def close(self) -> None:
        """Release owned database connections."""
        self.engine.dispose()


def public_run(run: dict) -> dict:
    """Project owner-safe status without credentials, prompts or private context."""
    data = run["data"]
    plan = data["plan"]
    return public_payload(
        dict(
            run_id=run["run_id"],
            research_session_id=run["session_id"],
            attempt_id=run["attempt_id"],
            status=run["status"],
            state_version=run["version"],
            event_cursor=run["seq"],
            current_step=data["active_step"],
            completed_steps=data["completed_steps"],
            workflow_backend="python",
            business_agent_runtime="openai_agents",
            workflow_version=data["workflow_version"],
            sdk_version=data["sdk_version"],
            runtime_version=data["runtime_version"],
            usage=data["usage"],
            settlement=data["settlement"],
            error=data["error"],
            report=data["state"].get("report", {}).get("final", ""),
            role_reports=data["stages"],
            archive_status=data["archive_status"],
            history_id=data["history_id"],
            plan=(
                dict(
                    version=plan["version"],
                    brief=plan["plan"]["brief"],
                    sections=[
                        s.model_dump() if isinstance(s, BaseModel) else s
                        for s in plan["plan"]["sections"]
                    ],
                    expires_at=timestamp(plan["expires_at"]),
                )
                if plan
                else None
            ),
            retry_safe=bool(data.get("retry_safe")) and not bool(run["quarantined"]),
            created_at=timestamp(run["created_at"]),
            updated_at=timestamp(run["updated_at"]),
            terminated_at=data.get("terminated_at"),
        )
    )
