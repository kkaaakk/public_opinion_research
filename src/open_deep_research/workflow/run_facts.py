"""Business admission, settled artifacts and external operation facts in PostgreSQL.

No execution queue, lease, attempt table, workflow cursor or transition graph.
DBOS owns execution; these facts prevent unsafe external replay and overbooking.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.metadata import version
from uuid import uuid4

from dbos import DBOS, DBOSClient
from dbos import error as dbos_error
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.configuration import Configuration
from open_deep_research.llm.errors import ModelInvocationError
from open_deep_research.web.run_data import (
    ExecutionRevoked,
    RunConflict,
    RunJSON,
    RunNotFound,
    digest,
    dumps,
    loads,
    public_payload,
    sdk_observation,
    timestamp,
)
from open_deep_research.workflow.dbos_config import (
    APP,
    FACT_WRITE_GRACE_SECONDS,
    SDK_VERSION,
    VERSION,
    settings,
)
from open_deep_research.workflow.execution import (
    ExecutionControlError,
    error_chain,
    finish_owned,
    native_step_available,
    owned_cleanup,
    owned_io,
)

MODEL_OPERATIONS = {"llm", "llm_sync", "sdk_model"}


class RunFacts:
    def __init__(self, config=None):
        self.settings = config or settings()
        self.engine = self.settings.engine()
        self.client = DBOSClient(
            system_database_engine=self.engine,
            application_name=APP,
            dbos_system_schema="por_dbos",
            serializer=RunJSON(),
            retry_connection_errors=False,
            observability_query_timeout_sec=3,
        )

    def initialize(self):
        with self.engine.begin() as db:
            db.execute(text("CREATE SCHEMA IF NOT EXISTS por_domain"))
            for ddl in (
                "CREATE TABLE IF NOT EXISTS por_domain.policy (id int PRIMARY KEY, value text NOT NULL)",
                "CREATE TABLE IF NOT EXISTS por_domain.requests (id text PRIMARY KEY, owner text NOT NULL, request_key text NOT NULL, session text NOT NULL, value text NOT NULL, occupied boolean NOT NULL DEFAULT false, cancel_requested boolean NOT NULL DEFAULT false, released text, UNIQUE(owner,request_key))",
                "CREATE INDEX IF NOT EXISTS por_request_session ON por_domain.requests(session)",
                "CREATE TABLE IF NOT EXISTS por_domain.units (id text PRIMARY KEY, run_id text NOT NULL REFERENCES por_domain.requests(id), name text NOT NULL, permit text NOT NULL, artifact text, usage text NOT NULL DEFAULT '{}', settled boolean, error text NOT NULL DEFAULT '{}')",
                "CREATE INDEX IF NOT EXISTS por_unit_run ON por_domain.units(run_id)",
                "CREATE TABLE IF NOT EXISTS por_domain.operations (id text PRIMARY KEY, unit text NOT NULL REFERENCES por_domain.units(id), fingerprint text NOT NULL, status text NOT NULL, name text NOT NULL)",
                "ALTER TABLE por_domain.operations ADD COLUMN IF NOT EXISTS usage text NOT NULL DEFAULT '{}'",
                "ALTER TABLE por_domain.operations ADD COLUMN IF NOT EXISTS admission_permit text",
                "CREATE INDEX IF NOT EXISTS por_operation_unit ON por_domain.operations(unit)",
                "CREATE TABLE IF NOT EXISTS por_domain.retries (run_id text NOT NULL REFERENCES por_domain.requests(id), key text NOT NULL, version bigint NOT NULL, generation bigint NOT NULL, PRIMARY KEY(run_id,key))",
                "ALTER TABLE por_domain.retries ADD COLUMN IF NOT EXISTS outcome text",
                "CREATE UNIQUE INDEX IF NOT EXISTS por_retry_generation ON por_domain.retries(run_id,generation)",
            ):
                db.execute(text(ddl))
            policy = self.policy()
            db.execute(
                text(
                    "INSERT INTO por_domain.policy VALUES (1,:v) ON CONFLICT DO NOTHING"
                ),
                {"v": policy},
            )
            if (
                db.execute(
                    text("SELECT value FROM por_domain.policy WHERE id=1")
                ).scalar_one()
                != policy
            ):
                raise RunConflict("Executors must use the initialized resource policy")

    def policy(self):
        return dumps(
            {
                name: getattr(self.settings, name)
                for name in (
                    "max_active",
                    "owner_active",
                    "queue_capacity",
                    "approval_seconds",
                    "run_seconds",
                )
            }
        )

    def verify_policy(self):
        with self.engine.connect() as db:
            if (
                db.execute(
                    text("SELECT value FROM por_domain.policy WHERE id=1")
                ).scalar_one()
                != self.policy()
            ):
                raise RunConflict("Executors must use the initialized resource policy")

    def migration_inventory(self):
        """Read native catalog and business quarantine; never change old work."""
        rows = []
        offset = 0
        while True:
            page = self.client.list_workflows(
                status=["PENDING", "ENQUEUED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"],
                application_name=APP,
                load_input=False,
                load_output=False,
                limit=500,
                offset=offset,
            )
            rows.extend(
                dict(
                    workflow_id=w.workflow_id,
                    name=w.name,
                    version=w.app_version,
                    status=w.status,
                )
                for w in page
                if w.name.startswith("por.") and w.app_version != VERSION
            )
            if len(page) < 500:
                break
            offset += len(page)
        with self.engine.connect() as db:
            quarantined = db.execute(
                text(
                    "SELECT r.id,r.value FROM por_domain.requests r WHERE r.occupied OR EXISTS(SELECT 1 FROM por_domain.units u WHERE u.run_id=r.id AND u.settled IS NOT TRUE)"
                )
            )
            for run_id, value in quarantined:
                version_ = loads(value).get("version")
                if version_ != VERSION:
                    rows.append(
                        dict(
                            workflow_id=run_id,
                            name="domain_reconciliation",
                            version=version_,
                            status="UNRESOLVED",
                        )
                    )
        return rows

    def require_matching_journals(self):
        if self.migration_inventory():
            raise RunConflict(
                "Incompatible unfinished DBOS journals: stop admission and drain/cancel/settle with the original release; run --migration-check"
            )

    def quarantine_pending(self, run_id):
        with self.engine.begin() as db:
            db.execute(
                text(
                    "UPDATE por_domain.units SET permit=:p,settled=false,error=:e WHERE run_id=:id AND settled IS NULL"
                ),
                {
                    "id": run_id,
                    "p": uuid4().hex,
                    "e": dumps({"kind": "unsettled_cleanup", "usage_complete": False}),
                },
            )

    @staticmethod
    def now(db):
        return float(
            db.execute(
                text("SELECT extract(epoch from clock_timestamp())")
            ).scalar_one()
        )

    @staticmethod
    def lock(db):
        # Admission/release only, never task claiming, polling or a scheduler lease.
        db.execute(text("SELECT pg_advisory_xact_lock(7240431)"))

    def request(self, run_id, owner=None, *, db=None, lock=False):
        if db is None:
            with self.engine.connect() as conn:
                return self.request(run_id, owner, db=conn)
        row = (
            db.execute(
                text(
                    "SELECT * FROM por_domain.requests WHERE id=:id"
                    + (" FOR UPDATE" if lock else "")
                ),
                {"id": run_id},
            )
            .mappings()
            .first()
        )
        if row is None or (owner is not None and owner != row["owner"]):
            raise RunNotFound("Run or Session not found")
        return dict(row) | {"data": loads(row["value"])}

    def publish(self, db, run_id, kind, *, idempotency_key=None, **payload):
        # Native communication transaction: no application outbox/relay table.
        payload.setdefault("observed_at", timestamp(self.now(db)))
        self.client.send_in_transaction(
            db,
            run_id + ":events",
            public_payload(dict(type=kind, **payload)),
            topic="events",
            idempotency_key=idempotency_key,
        )

    def validate(self, request):
        from open_deep_research.runtime.sdk_runtime import SDK_RUNTIME_VERSION

        data = request["data"]
        cfg = data["config"]
        effective = Configuration.from_runnable_config(cfg).model_dump(mode="json")
        if (
            data["version"] != VERSION
            or data["sdk"] != SDK_VERSION
            or version("openai-agents") != SDK_VERSION
            or data["runtime"] != SDK_RUNTIME_VERSION
            or digest(effective) != data["configuration"]
        ):
            raise RunConflict(
                "Accepted configuration/version changed; drain using the original executor"
            )

    def reserve(self, run_id):
        """Return the accepted request once capacity is held for the Run."""
        with self.engine.begin() as db:
            self.lock(db)
            req = self.request(run_id, db=db, lock=True)
            self.validate(req)
            if req["cancel_requested"]:
                raise ExecutionRevoked("Cancellation requested")
            if req["occupied"]:
                return req
            active = (
                db.execute(
                    text(
                        "SELECT owner,session FROM por_domain.requests WHERE occupied=true"
                    )
                )
                .mappings()
                .all()
            )
            if (
                len(active) >= self.settings.max_active
                or sum(r["owner"] == req["owner"] for r in active)
                >= self.settings.owner_active
                or any(r["session"] == req["session"] for r in active)
            ):
                return None
            db.execute(
                text("UPDATE por_domain.requests SET occupied=true WHERE id=:id"),
                {"id": run_id},
            )
            return req

    def pending_settlement(self, run_id, *, db=None):
        if db is None:
            with self.engine.connect() as conn:
                return self.pending_settlement(run_id, db=conn)
        return db.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM por_domain.units "
                "WHERE run_id=:id AND settled IS NULL)"
            ),
            {"id": run_id},
        ).scalar_one()

    def unresolved(self, run_id, db=None):
        if db is None:
            with self.engine.connect() as conn:
                return self.unresolved(run_id, conn)
        return bool(
            db.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM por_domain.units u WHERE u.run_id=:id AND u.settled IS NOT TRUE) OR EXISTS(SELECT 1 FROM por_domain.operations o JOIN por_domain.units u ON u.id=o.unit WHERE u.run_id=:id AND o.status IN ('started','uncertain'))"
                ),
                {"id": run_id},
            ).scalar_one()
        )

    def release(self, run_id):
        with self.engine.begin() as db:
            self.lock(db)
            if not self.unresolved(run_id, db):
                db.execute(
                    text("UPDATE por_domain.requests SET occupied=false WHERE id=:id"),
                    {"id": run_id},
                )

    def acquire(self, run_id, unit_id, name, *, safe=False, native_replay=False):
        permit = uuid4().hex
        with self.engine.begin() as db:
            req = self.request(run_id, db=db)
            if req["cancel_requested"]:
                raise ExecutionRevoked("Cancellation requested")
            usage = db.execute(
                text(
                    "INSERT INTO por_domain.units(id,run_id,name,permit) "
                    "VALUES (:id,:r,:n,:p) ON CONFLICT DO NOTHING RETURNING usage"
                ),
                {"id": unit_id, "r": run_id, "n": name, "p": permit},
            ).scalar_one_or_none()
            if usage is not None:
                # The insert already holds the new permit and pending settlement.
                return permit, None, loads(usage)
            settled, usage, artifact, error = db.execute(
                text(
                    "SELECT settled,usage,artifact,error FROM por_domain.units WHERE id=:id FOR UPDATE"
                ),
                {"id": unit_id},
            ).one()
            # Native recovery is checked at each external boundary. Standalone
            # callers must still fail closed on an opaque interrupted unit.
            deny = not native_replay and (not safe or settled is not True)
            # A native save may already be journaled. Rotating its fence on
            # recovery must not put a committed, settled projection back in limbo.
            completed = settled is True and artifact is not None and not loads(error)
            db.execute(
                text(
                    "UPDATE por_domain.units SET permit=:p,settled=:s,error=:e WHERE id=:id"
                ),
                {
                    "id": unit_id,
                    "p": permit,
                    "s": False if deny else True if completed else None,
                    "e": dumps({"kind": "unknown_effect", "usage_complete": False})
                    if deny
                    else "{}",
                },
            )
            prior_usage = loads(usage)
        # Commit the revocation before reporting the denial.
        if deny:
            raise ExecutionRevoked(
                "Unconfirmed business step requires authoritative reconciliation"
            )
        return permit, None, prior_usage

    def failed_usage(self, unit_id, fallback):
        from open_deep_research.budget import diff_budget_usage

        # Only costs without a physical receipt are carried. A replayed failed
        # native Step already supplies its own attempted usage again in memory.
        actual = self.physical_usage(unit_id)[0] if fallback else {}
        return diff_budget_usage(fallback, actual)

    def check(self, unit_id, permit, *, db=None, locked=False, allow_cancel=False):
        if db is None:
            with self.engine.connect() as conn:
                return self.check(unit_id, permit, db=conn, allow_cancel=allow_cancel)
        row = db.execute(
            text(
                "SELECT u.permit,r.cancel_requested FROM por_domain.units u JOIN por_domain.requests r ON r.id=u.run_id WHERE u.id=:id"
                + (" FOR UPDATE OF u" if locked else "")
            ),
            {"id": unit_id},
        ).first()
        if not row or row[0] != permit or (row[1] and not allow_cancel):
            raise ExecutionRevoked("Business execution permission was revoked")
        return bool(row[1])

    def cancellation_requested(self, run_id, unit_id, permit, workflow_id):
        if self.check(unit_id, permit, allow_cancel=True):
            self.client.cancel_workflow(run_id, cancel_children=True)
            return True
        return bool(
            self.client.list_workflows(
                workflow_ids=[workflow_id],
                status="CANCELLED",
                load_input=False,
                load_output=False,
            )
        )

    @contextmanager
    def effect_guard(self, unit_id, permit):
        # Revocation waits for a currently authorized Graph/Memory commit to drain.
        # No claim of distributed atomicity: a failed/unknown I/O stays quarantined.
        with self.engine.begin() as db:
            self.check(unit_id, permit, db=db, locked=True, allow_cancel=True)
            yield

    def operation(self, unit_id, permit, identity, status="started", usage=None):
        operation_id = digest(
            {
                "unit": unit_id,
                **{
                    k: identity.get(k)
                    for k in (
                        "role",
                        "task_id",
                        "research_round",
                        "tool_call_id",
                        "name",
                    )
                },
            }
        )
        fingerprint = digest(identity.get("arguments", ""))
        with self.engine.begin() as db:
            self.check(
                unit_id,
                permit,
                db=db,
                locked=True,
                allow_cancel=status != "started" or identity.get("settlement", False),
            )
            if status == "started":
                if db.execute(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM por_domain.operations WHERE unit=:u AND status IN ('started','uncertain') AND admission_permit IS DISTINCT FROM :p)"
                    ),
                    {"u": unit_id, "p": permit},
                ).scalar_one():
                    raise RunConflict(
                        "Unconfirmed work from an earlier executor requires authoritative reconciliation"
                    )
                unit = current_unit.get()
                if identity["name"] in MODEL_OPERATIONS and unit is not None:
                    from open_deep_research.budget import (
                        merge_budget_usage,
                        require_physical_model_budget,
                    )

                    # Budget check and the new intent share the unit lock. Parallel
                    # tool models can see any attempts already admitted here.
                    require_physical_model_budget(
                        unit.config,
                        merge_budget_usage(unit.baseline, unit.carry),
                        self.physical_usage(unit_id, db=db)[0],
                        reserve_report=unit.reserve_report,
                    )
                    if unit.expires_at is not None and self.now(db) >= unit.expires_at:
                        raise ModelInvocationError("BriefApprovalExpired")
                inserted = db.execute(
                    text(
                        "INSERT INTO por_domain.operations(id,unit,fingerprint,status,name,usage,admission_permit) VALUES (:id,:u,:f,'started',:n,:usage,:p) ON CONFLICT DO NOTHING"
                    ),
                    {
                        "id": operation_id,
                        "u": unit_id,
                        "f": fingerprint,
                        "n": identity["name"],
                        "p": permit,
                        "usage": dumps(
                            {"model_calls": 1}
                            if identity["name"] in MODEL_OPERATIONS
                            else {}
                        ),
                    },
                ).rowcount
                if inserted != 1:
                    raise RunConflict(
                        "Operation already admitted; reconcile instead of replaying"
                    )
            else:
                changed = db.execute(
                    text(
                        "UPDATE por_domain.operations SET status=:s,usage=:usage WHERE id=:id AND fingerprint=:f AND status='started'"
                    ),
                    {
                        "id": operation_id,
                        "f": fingerprint,
                        "s": status,
                        "usage": dumps(usage or {}),
                    },
                ).rowcount
                if changed != 1:
                    row = db.execute(
                        text(
                            "SELECT fingerprint,status,usage FROM por_domain.operations WHERE id=:id"
                        ),
                        {"id": operation_id},
                    ).first()
                    if (
                        row
                        and row[0] == fingerprint
                        and row[1] == status
                        and row[2] == dumps(usage or {})
                    ):
                        # A lost ACK may repeat only the exact already committed
                        # receipt, after the current permit check above. No event
                        # is published a second time and started is never reused.
                        return operation_id
                    raise RunConflict("Tool receipt identity or state mismatch")
            if identity["role"] == "service":
                return operation_id
            run_id = db.execute(
                text("SELECT run_id FROM por_domain.units WHERE id=:id"),
                {"id": unit_id},
            ).scalar_one()
            self.publish(
                db,
                run_id,
                "sdk_event",
                event="tool",
                operation_id=operation_id,
                role=identity["role"],
                name=identity["name"],
                status=status,
                unit_id=unit_id,
                execution_id=digest({"unit": unit_id, "permit": permit}),
                **{
                    key: identity[key]
                    for key in (
                        "invocation_id",
                        "task_id",
                        "tool_call_id",
                        "research_round",
                    )
                    if key in identity
                },
            )
        return operation_id

    def physical_usage(self, unit_id, *, db=None):
        from open_deep_research.budget import merge_budget_usage

        if db is None:
            with self.engine.connect() as conn:
                return self.physical_usage(unit_id, db=conn)
        actual = {}
        has_models = False
        for name, value in db.execute(
            text("SELECT name,usage FROM por_domain.operations WHERE unit=:id"),
            {"id": unit_id},
        ):
            actual = merge_budget_usage(actual, loads(value))
            has_models |= name in MODEL_OPERATIONS
        return actual, has_models

    def unconfirmed_operations(self, unit_id):
        with self.engine.connect() as db:
            return db.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM por_domain.operations "
                    "WHERE unit=:id AND status IN ('started','uncertain'))"
                ),
                {"id": unit_id},
            ).scalar_one()

    def retry_generation(self, run_id):
        with self.engine.connect() as db:
            return db.execute(
                text(
                    "SELECT coalesce(max(generation),0) FROM por_domain.retries WHERE run_id=:id"
                ),
                {"id": run_id},
            ).scalar_one()

    def accounted_usage(self, unit_id, observed, carry):
        """Receipts account physical attempts; replayed hooks rebuild local state.

        Receipt totals are a lower bound when a provider withholds token usage.
        They are never added to the same costs already in a business result.
        """
        from open_deep_research.budget import merge_budget_usage

        actual, has_models = self.physical_usage(unit_id)
        billed = merge_budget_usage(actual, carry)
        value = dict(observed)
        if has_models:
            for key in (
                "model_calls",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "input_token_details",
                "output_token_details",
            ):
                value[key] = billed.get(key, {} if key.endswith("_details") else 0)
        for key in ("tool_calls", "search_calls"):
            value[key] = max(value.get(key, 0), billed.get(key, 0))
        return value

    def finish_unit(
        self, unit_id, permit, *, usage, artifact=None, settled=True, error=None
    ):
        """Commit resource settlement, billed usage and public deliverables."""
        projection = None
        if artifact is not None:
            patch = artifact.get("patch", {})
            reports = {
                role: {"report": value["report"]}
                for role, value in patch.get("agents", {}).items()
                if isinstance(value, dict) and "report" in value
            }
            if result := artifact.get("research_result"):
                reports = {result["role"]: {"report": result["report"]}}
            projected_patch = {}
            if "report" in patch:
                projected_patch["report"] = patch["report"]
            if reports or "agents" in patch:
                projected_patch["agents"] = reports
            projection = dumps(
                {
                    "delivery_ids": artifact.get("delivery_ids", []),
                    "patch": projected_patch,
                }
            )
        with self.engine.begin() as db:
            self.check(unit_id, permit, db=db, locked=True, allow_cancel=True)
            run_id, name, previous_projection = db.execute(
                text("SELECT run_id,name,artifact FROM por_domain.units WHERE id=:id"),
                {"id": unit_id},
            ).one()
            db.execute(
                text(
                    "UPDATE por_domain.units SET artifact=coalesce(:a,artifact),usage=:u,settled=:s,error=:e WHERE id=:id"
                ),
                {
                    "id": unit_id,
                    "a": projection,
                    "u": dumps(usage),
                    "s": settled,
                    "e": dumps(error or {}),
                },
            )
            if projection is not None and projection != previous_projection:
                skipped = artifact.get("observation", {}).get("skipped") is True
                self.publish(
                    db,
                    run_id,
                    "step_skipped" if skipped else "step_completed",
                    step=name,
                    unit_id=unit_id,
                    execution_id=digest({"unit": unit_id, "permit": permit}),
                    **({"reason": "role_disabled"} if skipped else {}),
                )
                for role, role_data in reports.items():
                    if role_data["report"]:
                        report = role_data["report"]
                        report = (
                            report.get("value", "")
                            if isinstance(report, dict)
                            else report
                        )
                        self.publish(
                            db, run_id, "agent_report", role=role, content=report
                        )
                        self.publish(
                            db,
                            run_id,
                            "stream",
                            node=role + "_agent",
                            content=report,
                            granularity="stage",
                        )
                review = patch.get("workflow", {}).get("review")
                if review is not None:
                    self.publish(
                        db,
                        run_id,
                        "research_review",
                        review=review.model_dump(mode="json")
                        if hasattr(review, "model_dump")
                        else review,
                    )
            elif error:
                self.publish(
                    db,
                    run_id,
                    "step_failed",
                    idempotency_key=digest([unit_id, permit, "step-failed"]),
                    step=name,
                    unit_id=unit_id,
                    execution_id=digest({"unit": unit_id, "permit": permit}),
                    error_kind=error.get("kind", "execution_error"),
                    settled=settled,
                )

    def units(self, run_id):
        with self.engine.connect() as db:
            rows = [
                dict(r)
                | {
                    "artifact": loads(r["artifact"]) if r["artifact"] else None,
                    "usage": loads(r["usage"]),
                    "error": loads(r["error"]),
                }
                for r in db.execute(
                    text("SELECT * FROM por_domain.units WHERE run_id=:id ORDER BY id"),
                    {"id": run_id},
                ).mappings()
            ]

            # A dead executor may not have had a chance to publish its final
            # bill. Keep the lower bound from operation intents and receipts.
            unfinished = {row["id"]: row for row in rows if row["settled"] is not True}
            if unfinished:
                from open_deep_research.budget import (
                    diff_budget_usage,
                    merge_budget_usage,
                )

                actual = {}
                for unit_id, receipt in db.execute(
                    text(
                        "SELECT unit,usage FROM por_domain.operations WHERE unit=ANY(:units)"
                    ),
                    {"units": list(unfinished)},
                ):
                    actual[unit_id] = merge_budget_usage(
                        actual.get(unit_id, {}), loads(receipt)
                    )
                for unit_id, usage in actual.items():
                    row = unfinished[unit_id]
                    row["usage"] = merge_budget_usage(
                        row["usage"], diff_budget_usage(usage, row["usage"])
                    )
            return rows

    def close(self):
        self.client.destroy()
        self.engine.dispose()


@lru_cache(maxsize=1)
def facts():
    return RunFacts()


@dataclass
class UnitReceipts:
    """The live fence and accounting for one unit's external I/O."""

    repo: RunFacts
    run_id: str
    id: str
    name: str
    permit: str
    config: Configuration
    baseline: dict
    generation: int
    carry: dict
    reserve_report: bool = True
    expires_at: float | None = None
    control_error: ExecutionControlError | None = None
    delivery_ids: set[str] = field(default_factory=set)
    # Only identify physical calls sharing one Step (e.g. per-URL model calls
    # inside a tool). The native result alone determines whether a Step runs.
    _ordinals: dict = field(default_factory=lambda: defaultdict(int))

    @property
    def execution_id(self):
        return digest({"unit": self.id, "permit": self.permit})

    def operation_key(self, category):
        key = (DBOS.workflow_id, DBOS.step_id, category)
        self._ordinals[key] += 1
        return f"{key[0]}:{key[1]}:{self._ordinals[key]}:{self.generation}"

    def remember_control_error(self, exc):
        for error in error_chain(exc):
            if isinstance(error, ExecutionControlError):
                self.control_error = self.control_error or error
                if getattr(error, "unknown_outcome", False):
                    self.control_error.unknown_outcome = True
        return self.control_error

    async def write(self, operation, *args, **kwargs):
        """Retry an idempotent business commit, keeping its actual outcome owned."""

        async def write_receipt():
            deadline = time.monotonic() + FACT_WRITE_GRACE_SECONDS
            while True:
                try:
                    return await asyncio.to_thread(
                        operation, self.id, self.permit, *args, **kwargs
                    )
                except SQLAlchemyError:
                    if time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(0.5)

        return await finish_owned(asyncio.create_task(write_receipt()))

    async def operation(self, identity, *, status, usage=None):
        from open_deep_research.observability.telemetry import annotate

        try:
            if status == "started":
                if self.control_error and (
                    getattr(self.control_error, "unknown_outcome", False)
                    or not owned_cleanup.get()
                ):
                    raise self.control_error
                result, cancelled = await owned_io(
                    self.repo.operation, self.id, self.permit, identity
                )
                if cancelled is not None:
                    await self.write(
                        self.repo.operation, identity, status="not_executed"
                    )
                    raise cancelled
            else:
                result, _ = await self.write(
                    self.repo.operation, identity, status=status, usage=usage
                )
        except (RunConflict, SQLAlchemyError) as exc:
            if isinstance(exc, ExecutionRevoked) and await confirm_cancel(
                self.repo, self.run_id
            ):
                raise dbos_error.DBOSWorkflowCancelledError(
                    "Cancellation requested"
                ) from exc
            error = ExecutionControlError(
                "Durable execution permission/storage unavailable"
            )
            error.unknown_outcome = isinstance(exc, RunConflict) and not isinstance(
                exc, ExecutionRevoked
            )
            self.remember_control_error(error)
            raise error from exc
        annotate(operation_id=result, effect_status=status)
        return result

    async def observe(self, kind, payload, *, call_id=None):
        """Deliver sanitized SDK observations and retain replayable manifest IDs."""
        allowed = sdk_observation(payload)

        def publish(event_id):
            with self.repo.engine.begin() as db:
                self.repo.check(
                    self.id,
                    self.permit,
                    db=db,
                    locked=True,
                    allow_cancel=owned_cleanup.get() or kind == "tool_result",
                )
                self.repo.publish(
                    db,
                    self.run_id,
                    "sdk_event",
                    idempotency_key=event_id,
                    event_id=event_id,
                    event=kind,
                    step=self.name,
                    unit_id=self.id,
                    execution_id=self.execution_id,
                    **allowed,
                )
            return event_id

        async def send():
            try:
                event_id = digest(
                    [self.id, self.generation, kind, call_id]
                    if call_id is not None
                    else [self.id, self.operation_key("event")]
                )
                _, cancelled = await owned_io(publish, event_id)
            except (RunConflict, SQLAlchemyError) as exc:
                error = ExecutionControlError("Durable SDK observation unavailable")
                self.remember_control_error(error)
                raise error from exc
            if cancelled is not None:
                raise cancelled
            return event_id

        event_id = (
            await DBOS.run_step_async({"name": "por.sdk_observation"}, send)
            if call_id is None and native_step_available()
            else await send()
        )
        self.delivery_ids.add(event_id)


current_unit: ContextVar[UnitReceipts | None] = ContextVar("current_unit", default=None)


async def confirm_cancel(repo, run_id):
    """Deliver an already authorized cancellation before a phase can fail fast."""
    if (await asyncio.to_thread(repo.request, run_id))["cancel_requested"]:
        await owned_io(repo.client.cancel_workflow, run_id, cancel_children=True)
        return True
    return False


async def begin_call(name):
    unit = current_unit.get()
    if unit is None:
        return None
    identity = {
        "role": "service",
        "name": name,
        "task_id": unit.id,
        "research_round": 1,
        "tool_call_id": unit.operation_key("service"),
        "arguments": "",
        "settlement": owned_cleanup.get(),
    }
    await unit.operation(identity, status="started")
    return identity


async def end_call(identity, *, usage=None, status="returned"):
    if identity is not None:
        await current_unit.get().operation(identity, status=status, usage=usage)
