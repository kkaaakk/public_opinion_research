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
                "CREATE TABLE IF NOT EXISTS por_domain.retries (run_id text NOT NULL REFERENCES por_domain.requests(id), key text NOT NULL, version bigint NOT NULL, generation bigint NOT NULL, PRIMARY KEY(run_id,key))",
                "CREATE TABLE IF NOT EXISTS por_domain.approvals (run_id text PRIMARY KEY REFERENCES por_domain.requests(id), version text NOT NULL, decision text NOT NULL)",
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
        with self.engine.begin() as db:
            self.lock(db)
            req = self.request(run_id, db=db, lock=True)
            if req["cancel_requested"]:
                raise ExecutionRevoked("Cancellation requested")
            if req["occupied"]:
                return True
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
                return False
            db.execute(
                text("UPDATE por_domain.requests SET occupied=true WHERE id=:id"),
                {"id": run_id},
            )
            return True

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

    def expire_approval(self, run_id, plan):
        from datetime import datetime

        with self.engine.begin() as db:
            self.request(run_id, db=db, lock=True)
            if self.now(db) < datetime.fromisoformat(plan["expires_at"]).timestamp():
                return False
            inserted = db.execute(
                text(
                    "INSERT INTO por_domain.approvals VALUES (:id,:v,'expired') ON CONFLICT DO NOTHING"
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

    def acquire(self, run_id, unit_id, name, *, safe=False):
        permit = uuid4().hex
        with self.engine.begin() as db:
            req = self.request(run_id, db=db)
            if req["cancel_requested"]:
                raise ExecutionRevoked("Cancellation requested")
            created = (
                db.execute(
                    text(
                        "INSERT INTO por_domain.units(id,run_id,name,permit) VALUES (:id,:r,:n,:p) ON CONFLICT DO NOTHING"
                    ),
                    {"id": unit_id, "r": run_id, "n": name, "p": permit},
                ).rowcount
                == 1
            )
            row = (
                db.execute(
                    text("SELECT * FROM por_domain.units WHERE id=:id FOR UPDATE"),
                    {"id": unit_id},
                )
                .mappings()
                .one()
            )
            if row["artifact"]:
                return row["permit"], loads(row["artifact"]), loads(row["usage"])
            # Rotate before rejecting recovery, fencing an old still-alive executor.
            db.execute(
                text(
                    "UPDATE por_domain.units SET permit=:p,settled=NULL,error='{}' WHERE id=:id"
                ),
                {"id": unit_id, "p": permit},
            )
            deny = not created and (not safe or row["settled"] is not True)
            if deny:
                db.execute(
                    text(
                        "UPDATE por_domain.units SET settled=false,error=:e WHERE id=:id"
                    ),
                    {
                        "id": unit_id,
                        "e": dumps({"kind": "unknown_effect", "usage_complete": False}),
                    },
                )
            old_usage = loads(row["usage"])
        if deny:
            raise ExecutionRevoked(
                "Unconfirmed business step requires authoritative reconciliation"
            )
        return permit, None, old_usage

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

    @contextmanager
    def effect_guard(self, unit_id, permit):
        # Revocation waits for a currently authorized Graph/Memory commit to drain.
        # No claim of distributed atomicity: a failed/unknown I/O stays quarantined.
        with self.engine.begin() as db:
            self.check(unit_id, permit, db=db, locked=True, allow_cancel=True)
            yield

    def operation(self, unit_id, permit, identity, status="started"):
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
                unit_id, permit, db=db, locked=True, allow_cancel=status != "started"
            )
            if status == "started":
                inserted = db.execute(
                    text(
                        "INSERT INTO por_domain.operations VALUES (:id,:u,:f,'started',:n) ON CONFLICT DO NOTHING"
                    ),
                    {
                        "id": operation_id,
                        "u": unit_id,
                        "f": fingerprint,
                        "n": identity["name"],
                    },
                ).rowcount
                if inserted != 1:
                    raise RunConflict(
                        "Operation already admitted; reconcile instead of replaying"
                    )
            else:
                changed = db.execute(
                    text(
                        "UPDATE por_domain.operations SET status=:s WHERE id=:id AND fingerprint=:f AND status='started'"
                    ),
                    {"id": operation_id, "f": fingerprint, "s": status},
                ).rowcount
                if changed != 1:
                    raise RunConflict("Tool receipt identity or state mismatch")
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
            )
        return operation_id

    def finish_unit(
        self, unit_id, permit, *, artifact=None, usage=None, settled=True, error=None
    ):
        with self.engine.begin() as db:
            self.check(unit_id, permit, db=db, locked=True, allow_cancel=True)
            confirmed = db.execute(
                text("SELECT artifact FROM por_domain.units WHERE id=:id"),
                {"id": unit_id},
            ).scalar_one()
            if confirmed is not None:
                if artifact is not None and confirmed != dumps(artifact):
                    raise RunConflict("Confirmed business result identity changed")
                return
            db.execute(
                text(
                    "UPDATE por_domain.units SET artifact=:a,usage=:u,settled=:s,error=:e WHERE id=:id"
                ),
                {
                    "id": unit_id,
                    "a": dumps(artifact) if artifact is not None else None,
                    "u": dumps(usage or {}),
                    "s": settled,
                    "e": dumps(error or {}),
                },
            )
            if artifact is not None:
                row = db.execute(
                    text("SELECT run_id,name FROM por_domain.units WHERE id=:id"),
                    {"id": unit_id},
                ).one()
                run_id, name = row
                self.publish(db, run_id, "step_completed", step=name)
                patch = artifact.get("patch", {})
                for role, role_data in patch.get("agents", {}).items():
                    if isinstance(role_data, dict) and role_data.get("report"):
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
