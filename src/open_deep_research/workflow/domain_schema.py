"""Business schema installation and explicit, offline cutover from RunFacts."""

from sqlalchemy import text

from open_deep_research.web.run_data import RunConflict, dumps, loads
from open_deep_research.workflow.dbos_config import (
    ACTIVE,
    APP,
    LEGACY_SCHEMA,
    VERSION,
    domain_engine,
    native_client,
    settings,
)


def admission_lock(db):
    db.execute(text("SELECT pg_advisory_xact_lock(7240431)"))


def _policy():
    cfg = settings()
    return dumps(
        {
            key: getattr(cfg, key)
            for key in (
                "max_active",
                "owner_active",
                "queue_capacity",
                "approval_seconds",
                "run_seconds",
            )
        }
    )


def verify_policy():
    with domain_engine().connect() as db:
        if _exists(db, "por_domain.units"):
            raise RunConflict(
                "Legacy business schema requires the explicit domain migration"
            )
        if (
            db.scalar(text("SELECT value FROM por_domain.policy WHERE id=1"))
            != _policy()
        ):
            raise RunConflict("Executors must use the initialized admission policy")


def _exists(db, name):
    return db.scalar(text("SELECT to_regclass(:name)"), {"name": name}) is not None


def _columns(db, table):
    return {
        row[0]
        for row in db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='por_domain' AND table_name=:t"
            ),
            {"t": table},
        )
    }


def _constraints(db, table):
    return {
        row[0]
        for row in db.execute(
            text(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE table_schema='por_domain' AND table_name=:t"
            ),
            {"t": table},
        )
    }


def _upgrade_call_ledger(db):
    """Add call-ledger columns and the Operation link to an existing schema.

    ``CREATE TABLE IF NOT EXISTS`` cannot add a column or a foreign key to a
    table it skips, so each addition is checked against the catalog first. The
    statements stay inside the installer's single transaction and are safe to
    replay. Operations admitted before this release keep a ``NULL`` tool-call
    reference; every new one is linked and validated by the foreign key.
    """
    columns = _columns(db, "tool_calls")
    if "name" not in columns:
        db.exec_driver_sql(
            "ALTER TABLE por_domain.tool_calls ADD COLUMN name text NOT NULL DEFAULT ''"
        )
    if "search" not in columns:
        db.exec_driver_sql(
            "ALTER TABLE por_domain.tool_calls "
            "ADD COLUMN search boolean NOT NULL DEFAULT false"
        )
        if "search_calls" in columns:
            # The retired smallint carried the same fact: 1 for a retrieval tool.
            db.exec_driver_sql(
                "UPDATE por_domain.tool_calls SET search = search_calls > 0"
            )
            db.exec_driver_sql(
                "ALTER TABLE por_domain.tool_calls DROP COLUMN search_calls"
            )
    if "status" not in columns:
        db.exec_driver_sql(
            "ALTER TABLE por_domain.tool_calls "
            "ADD COLUMN status text NOT NULL DEFAULT 'admitted'"
        )
    if "tool_calls_status_check" not in _constraints(db, "tool_calls"):
        db.exec_driver_sql(
            "ALTER TABLE por_domain.tool_calls ADD CONSTRAINT "
            "tool_calls_status_check CHECK "
            "(status IN ('admitted','returned','failed','cancelled'))"
        )
    if "tool_call_id" not in _columns(db, "operations"):
        db.exec_driver_sql(
            "ALTER TABLE por_domain.operations ADD COLUMN tool_call_id text"
        )
    if "operations_tool_call_fkey" not in _constraints(db, "operations"):
        db.exec_driver_sql(
            "ALTER TABLE por_domain.operations ADD CONSTRAINT "
            "operations_tool_call_fkey FOREIGN KEY(run_id, tool_call_id) "
            "REFERENCES por_domain.tool_calls(run_id, id)"
        )


def migration_inventory():
    """Report old native work and unreconciled legacy effects without modifying either."""
    rows = []
    for offset in range(0, 2**31, 500):
        page = native_client().list_workflows(
            status=[*ACTIVE, "MAX_RECOVERY_ATTEMPTS_EXCEEDED"],
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
    with domain_engine().connect() as db:
        if _exists(db, "por_domain.units"):
            for run_id, data in db.execute(
                text("""
                SELECT r.id, r.value FROM por_domain.requests r
                WHERE r.released IS NULL AND (
                    r.occupied OR EXISTS (SELECT 1 FROM por_domain.units u
                        WHERE u.run_id=r.id AND u.settled IS NOT TRUE)
                    OR EXISTS (SELECT 1 FROM por_domain.operations o
                        JOIN por_domain.units u ON u.id=o.unit
                        WHERE u.run_id=r.id AND o.status IN ('started', 'uncertain'))
                )
                """)
            ):
                rows.append(
                    dict(
                        workflow_id=run_id,
                        name="external_reconciliation",
                        version=loads(data).get("version"),
                        status="UNRESOLVED",
                    )
                )
    return rows


def require_matching_journals():
    if migration_inventory():
        raise RunConflict(
            "Incompatible unfinished DBOS journals or effects: drain/cancel/reconcile "
            "using the original release; run --migration-check"
        )


def initialize_domain(*, migrate_legacy=False):
    """Fresh installs create the current schema. Legacy cutover is operator-invoked."""
    with domain_engine().begin() as db:
        admission_lock(db)
        db.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS por_domain")
        if _exists(db, "por_domain.units"):
            if not migrate_legacy:
                raise RunConflict(
                    "Legacy business schema requires --migrate-domain after drain"
                )
            require_matching_journals()
            # Keep the original receipts, artifacts, constraints and indexes for
            # readback/audit. This moves tables inside PostgreSQL without copying
            # or deleting their data. The operator stops old writers first.
            db.exec_driver_sql(f"CREATE SCHEMA {LEGACY_SCHEMA}")
            db.exec_driver_sql(
                f"ALTER TABLE por_domain.units SET SCHEMA {LEGACY_SCHEMA}"
            )
            db.exec_driver_sql(
                f"ALTER TABLE por_domain.operations SET SCHEMA {LEGACY_SCHEMA}"
            )
            db.exec_driver_sql("ALTER TABLE por_domain.requests DROP COLUMN occupied")
            db.exec_driver_sql(
                "ALTER TABLE por_domain.requests RENAME COLUMN released TO reconciliation"
            )
        for ddl in (
            """CREATE TABLE IF NOT EXISTS por_domain.policy
               (id int PRIMARY KEY, value text NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS por_domain.requests
               (id text PRIMARY KEY, owner text NOT NULL, request_key text NOT NULL,
                session text NOT NULL, value text NOT NULL, cancel_requested boolean NOT NULL DEFAULT false,
                reconciliation text, UNIQUE(owner, request_key))""",
            "CREATE INDEX IF NOT EXISTS por_request_session ON por_domain.requests(session)",
            """CREATE TABLE IF NOT EXISTS por_domain.request_images
               (run_id text REFERENCES por_domain.requests(id) ON DELETE CASCADE,
                position int NOT NULL CHECK (position >= 0), media_type text NOT NULL,
                content bytea NOT NULL, PRIMARY KEY(run_id, position))""",
            """CREATE TABLE IF NOT EXISTS por_domain.model_attempts
               (run_id text REFERENCES por_domain.requests(id), id text,
                stage_id text NOT NULL, usage text, PRIMARY KEY(run_id, id))""",
            """CREATE TABLE IF NOT EXISTS por_domain.tool_calls
               (run_id text REFERENCES por_domain.requests(id), id text,
                name text NOT NULL, search boolean NOT NULL DEFAULT false,
                status text NOT NULL DEFAULT 'admitted',
                PRIMARY KEY(run_id, id),
                CONSTRAINT tool_calls_status_check
                    CHECK (status IN ('admitted','returned','failed','cancelled')))""",
            """CREATE TABLE IF NOT EXISTS por_domain.operations
               (run_id text REFERENCES por_domain.requests(id), id text,
                tool_call_id text,
                name text NOT NULL, fingerprint text NOT NULL, executor_id text NOT NULL,
                outcome text CHECK (outcome IN ('returned', 'unknown', 'not_executed')),
                PRIMARY KEY(run_id, id),
                CONSTRAINT operations_tool_call_fkey FOREIGN KEY(run_id, tool_call_id)
                    REFERENCES por_domain.tool_calls(run_id, id))""",
            """CREATE TABLE IF NOT EXISTS por_domain.retries
               (run_id text REFERENCES por_domain.requests(id), key text,
                version bigint NOT NULL, generation bigint NOT NULL, outcome text,
                PRIMARY KEY(run_id, key))""",
            "CREATE UNIQUE INDEX IF NOT EXISTS por_retry_generation ON por_domain.retries(run_id,generation)",
        ):
            db.exec_driver_sql(ddl)
        _upgrade_call_ledger(db)
        db.exec_driver_sql(
            "ALTER TABLE por_domain.model_attempts ADD COLUMN IF NOT EXISTS reservation jsonb"
        )
        db.exec_driver_sql("""
            ALTER TABLE por_domain.operations
            ADD COLUMN IF NOT EXISTS workflow_id text,
            ADD COLUMN IF NOT EXISTS step_id integer,
            ADD COLUMN IF NOT EXISTS apify_run_id text
            """)
        db.execute(
            text("INSERT INTO por_domain.policy VALUES (1,:v) ON CONFLICT DO NOTHING"),
            {"v": _policy()},
        )
        if (
            db.scalar(text("SELECT value FROM por_domain.policy WHERE id=1"))
            != _policy()
        ):
            raise RunConflict("Executors must use the initialized admission policy")
