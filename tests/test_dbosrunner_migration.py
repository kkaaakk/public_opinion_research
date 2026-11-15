"""Cutover refuses old journals through public DBOS catalog APIs."""

import pytest

from open_deep_research.web.run_data import RunConflict, read_request
from open_deep_research.workflow.dbos_config import domain_engine, native_client
from open_deep_research.workflow.domain_schema import (
    initialize_domain,
    migration_inventory,
    require_matching_journals,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "version,workflow,args",
    [
        ("dbos-brief-spec-v1", "por.role", ("old", "old:role", "public_signal", {})),
        ("dbosrunner-unified-v1", "por.run", ("old",)),
        ("dbos-native-recovery-v1", "por.run", ("old",)),
        ("dbos-domain-v1", "por.events", ("old",)),
        ("dbosrunner-clarification-v1", "por.run", ("old",)),
        ("dbosrunner-brief-revision-v1", "por.plan", ("old", {}, 1)),
        ("dbosrunner-stage-settlement-v1", "por.run", ("old",)),
        ("dbosrunner-stage-settlement-v1", "por.plan", ("old", {}, 1)),
        ("dbosrunner-brief-idempotency-v1", "por.run", ("old",)),
        ("dbosrunner-brief-idempotency-v1", "por.plan", ("old", {}, 1)),
    ],
)
def test_old_pending_journal_is_inventoried_and_not_deleted(
    pg_database, version, workflow, args
):
    from test_python_workflow import config, initial

    from open_deep_research.web.durable import RunClient
    from open_deep_research.web.executor import launch

    assert migration_inventory() == []
    workflow_id = (
        "old-journal-test:brief-revision:1"
        if workflow == "por.plan"
        else "old-journal-test"
    )
    options = dict(
        workflow_name=workflow,
        workflow_id=workflow_id,
        queue_name="por-business",
        app_version=version,
    )
    revision = (
        {"revision_request": {"feedback": "Add scope.", "expires_at": 1}}
        if workflow == "por.plan"
        else {}
    )
    native_client().enqueue(options, *args, **revision)
    inventory = migration_inventory()
    assert inventory == [
        {
            "workflow_id": workflow_id,
            "name": workflow,
            "version": version,
            "status": "ENQUEUED",
        }
    ]
    with pytest.raises(RunConflict, match="original release"):
        require_matching_journals()
    with pytest.raises(RunConflict, match="original release"):
        launch("new-version-executor")
    with pytest.raises(RunConflict, match="original release"):
        RunClient().create("alice", "new-run", {"topic": "a"}, config(), initial())
    assert (
        native_client().list_workflows(workflow_ids=[workflow_id])[0].status
        == "ENQUEUED"
    )


def test_native_interactions_ignore_optional_legacy_tables(native_case):
    from sqlalchemy import text

    case = native_case
    with domain_engine().begin() as db:
        # Fresh installs have neither application receipt table.
        for name in ("approvals", "brief_revision_receipts"):
            assert (
                db.execute(
                    text("SELECT to_regclass(:name)"), {"name": f"por_domain.{name}"}
                ).scalar_one()
                is None
            )
        db.execute(
            text(
                "CREATE TABLE por_domain.approvals (run_id text PRIMARY KEY, version text, decision text)"
            )
        )
        db.execute(
            text(
                "CREATE TABLE por_domain.brief_revision_receipts (run_id text, key_hash text, fingerprint text, PRIMARY KEY(run_id,key_hash))"
            )
        )
        db.execute(
            text("INSERT INTO por_domain.approvals VALUES ('old','old-slot','expired')")
        )
        db.execute(
            text(
                "INSERT INTO por_domain.brief_revision_receipts VALUES ('old','old-key','old-fingerprint')"
            )
        )

    initialize_domain()
    run = case.create(approval=True)
    run_id = run["run_id"]
    plan = case.wait(run, lambda r: r["status"] == "awaiting_approval")["plan"]
    case.client.approve(
        run_id, "alice", plan["version"], "revise", feedback="Add scope.", key="native"
    )
    revised = case.wait(
        run,
        lambda r: r["status"] == "awaiting_approval"
        and r["plan"]["version"] != plan["version"],
    )["plan"]
    case.client.approve(run_id, "alice", revised["version"], "approve")
    assert (
        case.wait(run, lambda r: r["archive_status"] == "saved")["status"]
        == "completed"
    )
    with domain_engine().connect() as db:
        assert db.execute(text("SELECT * FROM por_domain.approvals")).all() == [
            ("old", "old-slot", "expired")
        ]
        assert db.execute(
            text("SELECT * FROM por_domain.brief_revision_receipts")
        ).all() == [("old", "old-key", "old-fingerprint")]


def seed_legacy(db):
    # Invoked only on a newly created, disposable PostgreSQL fixture database.
    for ddl in (
        "DROP TABLE por_domain.operations",
        "ALTER TABLE por_domain.requests ADD COLUMN occupied boolean NOT NULL DEFAULT false",
        "ALTER TABLE por_domain.requests RENAME COLUMN reconciliation TO released",
        """CREATE TABLE por_domain.units(id text PRIMARY KEY,
               run_id text NOT NULL REFERENCES por_domain.requests(id),
               name text, artifact text, usage text, error text, settled boolean, permit text)""",
        """CREATE TABLE por_domain.operations(id text PRIMARY KEY,
               unit text NOT NULL REFERENCES por_domain.units(id), fingerprint text,
               status text, name text, usage text, admission_permit text)""",
    ):
        db.exec_driver_sql(ddl)


def test_legacy_cutover_preserves_reports_bills_and_unreconciled_operations(
    pg_database,
):
    from sqlalchemy import text
    from test_python_workflow import config, initial

    from open_deep_research.web.durable import RunClient
    from open_deep_research.web.run_data import dumps

    client = RunClient()
    run = client.create("alice", "historical", {"topic": "a"}, config(), initial())[0]
    run_id = run["run_id"]
    for suffix in ("", ":events", ":finalize"):
        client.client.cancel_workflow(run_id + suffix, cancel_children=True)
    unit = run_id + ":compile_final_report"
    with domain_engine().begin() as db:
        request = read_request(run_id, db=db)["data"]
        request["version"] = "dbos-native-recovery-v1"
        seed_legacy(db)
        db.execute(
            text("UPDATE por_domain.requests SET value=:v WHERE id=:id"),
            {"id": run_id, "v": dumps(request)},
        )
        db.execute(
            text("""
            INSERT INTO por_domain.units(id,run_id,name,permit,settled,artifact,usage,error)
            VALUES (:unit,:run,'compile_final_report','old',true,:artifact,:usage,'{}')
            """),
            {
                "unit": unit,
                "run": run_id,
                "artifact": dumps(
                    {"patch": {"report": {"final": "historical report"}}}
                ),
                "usage": dumps({"model_calls": 1, "input_tokens": 19}),
            },
        )
        db.execute(
            text("""
            INSERT INTO por_domain.operations(id,unit,status,name,usage)
            VALUES ('legacy-effect',:u,'started','external-write','{}')
            """),
            {"u": unit},
        )

    with pytest.raises(RunConflict, match="original release"):
        initialize_domain(migrate_legacy=True)
    with domain_engine().begin() as db:
        assert db.scalar(text("SELECT count(*) FROM por_domain.operations")) == 1
        db.execute(
            text("UPDATE por_domain.requests SET released=:note WHERE id=:r"),
            {
                "r": run_id,
                "note": dumps({"note": "Operator reviewed external receipt"}),
            },
        )
    initialize_domain(migrate_legacy=True)
    initialize_domain()
    snapshot = client.get(run_id, "alice")
    assert snapshot["completed_steps"] == ["compile_final_report"]
    assert snapshot["report"] == "historical report"
    assert (
        snapshot["usage"]["input_tokens"]
        == initial()["runtime"]["budget"]["input_tokens"] + 19
    )
    assert not snapshot["retry_safe"] and snapshot["settlement"] == "unresolved"
    with domain_engine().connect() as db:
        assert db.scalar(text("SELECT to_regclass('por_domain.units')")) is None
        assert (
            db.scalar(text("SELECT status FROM por_archive_native_recovery.operations"))
            == "started"
        )
        assert db.scalar(text("SELECT count(*) FROM por_domain.operations")) == 0
        assert db.scalar(text("SELECT count(*) FROM por_domain.model_attempts")) == 0
    replay, created = client.create(
        "alice", "historical", {"topic": "a"}, config(), initial()
    )
    assert not created and replay["run_id"] == run_id


def test_drained_domain_run_keeps_its_model_and_operation_receipts(pg_database):
    from sqlalchemy import text
    from test_python_workflow import config, initial

    from open_deep_research.web.durable import RunClient
    from open_deep_research.web.run_data import dumps

    client = RunClient()
    run, _ = client.create("alice", "old-domain", {"topic": "a"}, config(), initial())
    run_id = run["run_id"]
    for suffix in ("", ":events", ":finalize"):
        client.client.cancel_workflow(run_id + suffix)
    with domain_engine().begin() as db:
        data = read_request(run_id, db=db)["data"] | {"version": "dbos-domain-v1"}
        db.execute(
            text("UPDATE por_domain.requests SET value=:value WHERE id=:run"),
            {"run": run_id, "value": dumps(data)},
        )
        db.execute(
            text("INSERT INTO por_domain.model_attempts VALUES (:run,'m','s',:usage)"),
            {"run": run_id, "usage": dumps({"model_calls": 1, "input_tokens": 19})},
        )
        db.execute(
            text(
                "INSERT INTO por_domain.operations VALUES (:run,'o','external','fingerprint','old','unknown')"
            ),
            {"run": run_id},
        )
    result = client.get(run_id, "alice")
    assert result["workflow_version"] == "dbos-domain-v1"
    assert (
        result["usage"]["input_tokens"]
        == initial()["runtime"]["budget"]["input_tokens"] + 19
    )
    assert result["status"] == "interrupted" and result["settlement"] == "unresolved"
    assert not result["retry_safe"]


def test_fresh_schema_contains_only_request_control_and_effect_billing_tables(
    pg_database,
):
    from sqlalchemy import text

    with domain_engine().connect() as db:
        tables = set(
            db.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname='por_domain'")
            ).scalars()
        )
        assert tables == {
            "requests",
            "retries",
            "policy",
            "operations",
            "model_attempts",
        }
        for table, forbidden in {
            "operations": {"unit", "usage", "admission_permit"},
            "requests": {"occupied", "released"},
            "model_attempts": {"artifact", "permit", "status"},
        }.items():
            names = set(
                db.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema='por_domain' AND table_name=:t"
                    ),
                    {"t": table},
                ).scalars()
            )
            assert not names & forbidden
