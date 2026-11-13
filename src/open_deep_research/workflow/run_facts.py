"""Business admission, settled artifacts and external operation facts in PostgreSQL.

No execution queue, lease, attempt table, workflow cursor or transition graph.
DBOS owns execution; these facts prevent unsafe external replay and overbooking.
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from importlib.metadata import version
from uuid import uuid4

from dbos import DBOSClient
from sqlalchemy import text

from open_deep_research.configuration import Configuration
from open_deep_research.runtime.sdk_runtime import SDK_RUNTIME_VERSION
from open_deep_research.web.run_data import (
    ExecutionRevoked,
    RunConflict,
    RunJSON,
    RunNotFound,
    digest,
    dumps,
    loads,
    public_payload,
    timestamp,
)
from open_deep_research.workflow.dbos_config import APP, SDK_VERSION, VERSION, settings


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
                "CREATE TABLE IF NOT EXISTS por_domain.approvals (run_id text PRIMARY KEY REFERENCES por_domain.requests(id), version text NOT NULL, decision text NOT NULL)",
                "CREATE TABLE IF NOT EXISTS por_domain.brief_revision_receipts (run_id text NOT NULL REFERENCES por_domain.requests(id), key_hash text NOT NULL, fingerprint text NOT NULL, PRIMARY KEY(run_id,key_hash))",
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

    def publish(self, db, run_id, kind, **payload):
        # Native communication transaction: no application outbox/relay table.
        payload.setdefault("observed_at", timestamp(self.now(db)))
        self.client.send_in_transaction(
            db,
            run_id + ":events",
            public_payload(dict(type=kind, **payload)),
            topic="events",
        )

    def validate(self, request):
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

    @staticmethod
    def approval_receipt(db, run_id):
        return db.execute(
            text("SELECT version,decision FROM por_domain.approvals WHERE run_id=:id"),
            {"id": run_id},
        ).first()

    def expire_approval(self, run_id, plan):
        from datetime import datetime

        with self.engine.begin() as db:
            self.request(run_id, db=db, lock=True)
            if self.now(db) < datetime.fromisoformat(plan["expires_at"]).timestamp():
                return False
            latest = self.client.get_event(run_id, "plan", timeout_seconds=0)
            if not latest or latest["version"] != plan["version"]:
                return False
            inserted = db.execute(
                text(
                    "INSERT INTO por_domain.approvals (run_id,version,decision) VALUES (:id,:v,'expired') "
                    "ON CONFLICT (run_id) DO UPDATE SET version=EXCLUDED.version,decision='expired' "
                    "WHERE por_domain.approvals.version<>EXCLUDED.version "
                    "OR por_domain.approvals.decision LIKE 'revise:%'"
                ),
                {"id": run_id, "v": plan["version"]},
            ).rowcount
            decision = db.execute(
                text("SELECT decision FROM por_domain.approvals WHERE run_id=:id"),
                {"id": run_id},
            ).scalar_one()
            if inserted:
                self.publish(db, run_id, "approval_ended", decision="expired")
            return decision == "expired"

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
            settled, usage = db.execute(
                text("SELECT settled,usage FROM por_domain.units WHERE id=:id FOR UPDATE"),
                {"id": unit_id},
            ).one()
            # Native recovery is checked at each external boundary. Standalone
            # callers must still fail closed on an opaque interrupted unit.
            deny = not native_replay and (not safe or settled is not True)
            db.execute(
                text(
                    "UPDATE por_domain.units SET permit=:p,settled=:s,error=:e WHERE id=:id"
                ),
                {
                    "id": unit_id,
                    "p": permit,
                    "s": False if deny else None,
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
                            if identity["name"] in {"llm", "llm_sync", "sdk_model"}
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

    def physical_usage(self, unit_id):
        from open_deep_research.budget import merge_budget_usage

        with self.engine.connect() as db:
            rows = list(
                db.execute(
                    text("SELECT name,usage FROM por_domain.operations WHERE unit=:id"),
                    {"id": unit_id},
                )
            )
        actual = {}
        for _, value in rows:
            actual = merge_budget_usage(actual, loads(value))
        return actual, any(name in {"llm", "llm_sync", "sdk_model"} for name, _ in rows)

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
                    step=name,
                    unit_id=unit_id,
                    execution_id=digest({"unit": unit_id, "permit": permit}),
                    error_kind=error.get("kind", "execution_error"),
                    settled=settled,
                )

    def units(self, run_id):
        with self.engine.connect() as db:
            return [
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

    def close(self):
        self.client.destroy()
        self.engine.dispose()


@lru_cache(maxsize=1)
def facts():
    return RunFacts()
