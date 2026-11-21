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
            """CREATE TABLE IF NOT EXISTS por_domain.operations
               (run_id text REFERENCES por_domain.requests(id), id text,
                name text NOT NULL, fingerprint text NOT NULL, executor_id text NOT NULL,
                outcome text CHECK (outcome IN ('returned', 'unknown', 'not_executed')),
                PRIMARY KEY(run_id, id))""",
            """CREATE TABLE IF NOT EXISTS por_domain.model_attempts
               (run_id text REFERENCES por_domain.requests(id), id text,
                stage_id text NOT NULL, usage text, PRIMARY KEY(run_id, id))""",
            """CREATE TABLE IF NOT EXISTS por_domain.tool_calls
               (run_id text REFERENCES por_domain.requests(id), id text,
                search_calls smallint NOT NULL DEFAULT 0,
                PRIMARY KEY(run_id, id))""",
            """CREATE TABLE IF NOT EXISTS por_domain.retries
               (run_id text REFERENCES por_domain.requests(id), key text,
                version bigint NOT NULL, generation bigint NOT NULL, outcome text,
                PRIMARY KEY(run_id, key))""",
            "CREATE UNIQUE INDEX IF NOT EXISTS por_retry_generation ON por_domain.retries(run_id,generation)",
            """CREATE TABLE IF NOT EXISTS por_domain.history
               (id text PRIMARY KEY, run_id text NOT NULL, topic text NOT NULL,
                report text NOT NULL DEFAULT '', started_at text NOT NULL,
                completed_at text NOT NULL, request_json text NOT NULL DEFAULT '{}',
                usage_json text NOT NULL DEFAULT '{}', source text NOT NULL DEFAULT 'dbos',
                langsmith_run_id text, status text NOT NULL DEFAULT 'completed',
                role_reports_json text NOT NULL DEFAULT '[]',
                stage_count integer NOT NULL DEFAULT 0,
                error_message text NOT NULL DEFAULT '', failed_stage text NOT NULL DEFAULT '',
                owner text NOT NULL DEFAULT '', research_session_id text NOT NULL DEFAULT '',
                run_state_version bigint NOT NULL DEFAULT 0,
                preview text NOT NULL DEFAULT '', search_text text NOT NULL DEFAULT '')""",
            "CREATE INDEX IF NOT EXISTS por_history_completed ON por_domain.history(completed_at DESC)",
            "CREATE INDEX IF NOT EXISTS por_history_owner ON por_domain.history(owner)",
        ):
            db.exec_driver_sql(ddl)
        db.execute(
            text("INSERT INTO por_domain.policy VALUES (1,:v) ON CONFLICT DO NOTHING"),
            {"v": _policy()},
        )
        if (
            db.scalar(text("SELECT value FROM por_domain.policy WHERE id=1"))
            != _policy()
        ):
            raise RunConflict("Executors must use the initialized admission policy")
