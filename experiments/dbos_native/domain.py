"""Domain effects, report artifacts and stale-execution permits; no queue/lease."""

from __future__ import annotations

import hashlib
from uuid import uuid4

from codec import DomainFailure, dumps, loads
from sqlalchemy import create_engine, text


class Domain:
    def __init__(self, url):
        self.engine = create_engine(
            url, pool_pre_ping=True, connect_args={"connect_timeout": 2}
        )
        with self.engine.begin() as db:
            db.execute(text("CREATE SCHEMA IF NOT EXISTS native_domain"))
            db.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS native_domain.roles (id text PRIMARY KEY, permit text NOT NULL, artifact text)"
                )
            )
            db.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS native_domain.operations (id text PRIMARY KEY, role_id text NOT NULL, fingerprint text NOT NULL, status text NOT NULL)"
                )
            )
            db.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS native_domain.facts (seq bigserial PRIMARY KEY, role_id text NOT NULL, kind text NOT NULL, value text NOT NULL)"
                )
            )
            db.execute(text("CREATE SCHEMA IF NOT EXISTS external_authority"))
            db.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS external_authority.effects (id text PRIMARY KEY, role_id text NOT NULL, value text NOT NULL)"
                )
            )

    def fact(self, role, kind, value):
        with self.engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO native_domain.facts(role_id,kind,value) VALUES (:r,:k,:v)"
                ),
                {"r": role, "k": kind, "v": dumps(value)},
            )

    def facts(self, role, kind):
        with self.engine.connect() as db:
            return [
                loads(v)
                for v in db.execute(
                    text(
                        "SELECT value FROM native_domain.facts WHERE role_id LIKE :r AND kind=:k ORDER BY seq"
                    ),
                    {"r": role + "%", "k": kind},
                ).scalars()
            ]

    def acquire(self, role, *, allow_incomplete=False):
        permit = uuid4().hex
        with self.engine.begin() as db:
            created = (
                db.execute(
                    text(
                        "INSERT INTO native_domain.roles VALUES (:r,:p,NULL) ON CONFLICT DO NOTHING"
                    ),
                    {"r": role, "p": permit},
                ).rowcount
                == 1
            )
            row = (
                db.execute(
                    text("SELECT * FROM native_domain.roles WHERE id=:r FOR UPDATE"),
                    {"r": role},
                )
                .mappings()
                .one()
            )
            if row["artifact"]:
                return row["permit"], loads(row["artifact"])
            unresolved = db.execute(
                text(
                    "SELECT count(*) FROM native_domain.operations WHERE role_id=:r AND status IN ('started','uncertain')"
                ),
                {"r": role},
            ).scalar_one()
            db.execute(
                text("UPDATE native_domain.roles SET permit=:p WHERE id=:r"),
                {"r": role, "p": permit},
            )
        if unresolved or (not created and not allow_incomplete):
            self.fact(
                role,
                "interrupted",
                {"unknown_operations": unresolved, "usage_complete": False},
            )
            raise DomainFailure(
                "Unknown external effects or unconfirmed role outcome require reconciliation",
                "UnknownToolEffect",
            )
        return permit, None

    def check(self, role, permit):
        with self.engine.connect() as db:
            current = db.execute(
                text("SELECT permit FROM native_domain.roles WHERE id=:r"), {"r": role}
            ).scalar_one()
        if current != permit:
            raise DomainFailure(
                "Stale execution cannot start model/tool or publish artifact",
                "StaleExecution",
            )

    def intent(self, role, permit, identity):
        self.check(role, permit)
        op = hashlib.sha256(
            f"{role}:{identity['name']}:{identity['tool_call_id']}".encode()
        ).hexdigest()
        fingerprint = hashlib.sha256(identity["arguments"].encode()).hexdigest()
        with self.engine.begin() as db:
            current = db.execute(
                text("SELECT permit FROM native_domain.roles WHERE id=:r FOR UPDATE"),
                {"r": role},
            ).scalar_one()
            if current != permit:
                raise DomainFailure("Stale tool admission rejected", "StaleExecution")
            db.execute(
                text(
                    "INSERT INTO native_domain.operations VALUES (:id,:r,:f,'started') ON CONFLICT DO NOTHING"
                ),
                {"id": op, "r": role, "f": fingerprint},
            )
            old = db.execute(
                text(
                    "SELECT fingerprint,status FROM native_domain.operations WHERE id=:id"
                ),
                {"id": op},
            ).one()
        if old[0] != fingerprint or old[1] in {"uncertain"}:
            raise DomainFailure(
                "Operation identity conflict or unknown effect", "UnknownToolEffect"
            )
        return op

    def receipt(self, role, permit, identity, status):
        self.check(role, permit)
        op = hashlib.sha256(
            f"{role}:{identity['name']}:{identity['tool_call_id']}".encode()
        ).hexdigest()
        with self.engine.begin() as db:
            changed = db.execute(
                text(
                    "UPDATE native_domain.operations SET status=:s WHERE id=:id AND EXISTS (SELECT 1 FROM native_domain.roles WHERE id=:r AND permit=:p)"
                ),
                {"id": op, "s": status, "r": role, "p": permit},
            ).rowcount
            if changed != 1:
                raise DomainFailure("Stale receipt rejected", "StaleExecution")

    def effect(self, role, operation_id, value):
        # Separate transaction from application intent/receipt. Idempotency is
        # explicitly provided by this synthetic external service, not DBOS.
        with self.engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO external_authority.effects VALUES (:id,:r,:v) ON CONFLICT DO NOTHING"
                ),
                {"id": operation_id, "r": role, "v": value},
            )
            return db.execute(
                text("SELECT value FROM external_authority.effects WHERE id=:id"),
                {"id": operation_id},
            ).scalar_one()

    def artifact(self, role, permit, value):
        with self.engine.begin() as db:
            count = db.execute(
                text(
                    "UPDATE native_domain.roles SET artifact=:v WHERE id=:r AND permit=:p"
                ),
                {"r": role, "p": permit, "v": dumps(value)},
            ).rowcount
            if count != 1:
                raise DomainFailure("Stale report rejected", "StaleExecution")

    def count_effects(self, prefix):
        with self.engine.connect() as db:
            return db.execute(
                text(
                    "SELECT count(*) FROM external_authority.effects WHERE role_id LIKE :r"
                ),
                {"r": prefix + "%"},
            ).scalar_one()
