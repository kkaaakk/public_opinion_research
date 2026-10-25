"""Actual PostgreSQL contract fixtures; inference only is scripted."""

from __future__ import annotations

import os
import time
from pathlib import Path
from uuid import uuid4

import pytest
from dbos import DBOS
from neo4j import GraphDatabase
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from open_deep_research.web.durable import RunClient, close_run_client
from open_deep_research.web.history import Neo4jHistoryStore
from open_deep_research.workflow.dbos_config import settings
from open_deep_research.workflow.run_facts import facts

BASE_URL = os.environ.get(
    "TEST_DBOS_DATABASE_URL",
    "postgresql+psycopg://por_execution:local-dbos-development-only@127.0.0.1:15434/por_execution",
)
NEO4J = os.environ.get("TEST_DBOS_NEO4J_URI", "bolt://127.0.0.1:17689")
NEO4J_PASSWORD = os.environ.get("TEST_DBOS_NEO4J_PASSWORD", "local-dbos-test-only")


@pytest.fixture
def pg_database(monkeypatch, request, tmp_path):
    url = make_url(BASE_URL)
    if url.host not in {"127.0.0.1", "localhost", "postgres"} or not url.database:
        raise AssertionError("Tests require an explicitly isolated local PostgreSQL")
    name = "por_test_" + uuid4().hex
    admin = create_engine(
        url,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )
    with admin.connect() as db:
        db.execute(text("CREATE DATABASE " + name))
    isolated = url.set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("DBOS_DATABASE_URL", isolated)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
    for name_ in (
        "BUSINESS_AGENT_RUNTIME",
        "WORKFLOW_BACKEND",
        "ALLOW_PLAN_FEEDBACK",
        "RESEARCH_GRAPH_ENABLED",
    ):
        monkeypatch.delenv(name_, raising=False)
    options = getattr(request, "param", {})
    for policy_name, value in options.items():
        if policy_name == "RUN_SDK_SESSION_PATH":
            # Inspectable test sessions must never append to an operator file.
            value = tmp_path / "sdk-sessions" / Path(value).name
            value.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(policy_name, str(value))
    settings.cache_clear()
    DBOS.migrate(isolated, schema="por_dbos")
    facts().initialize()
    try:
        yield isolated
    finally:
        close_run_client()
        settings.cache_clear()
        assert (
            name.startswith("por_test_")
            and len(name) == 41
            and all(c in "0123456789abcdef" for c in name[9:])
        )
        with admin.connect() as db:
            db.execute(text("DROP DATABASE " + name + " WITH (FORCE)"))
        admin.dispose()


class NativeCase:
    def __init__(self, patch, directory, mode):
        self.mode = mode
        from test_python_workflow_sdk import install_sdk

        from open_deep_research.web import history

        self.state, self.cfg, self.runtimes, self.models, self.calls, self.blocked = (
            install_sdk(patch, directory, mode)
        )
        self.client = RunClient()
        self.owner = "alice"
        self.history = Neo4jHistoryStore(
            GraphDatabase.driver(NEO4J, auth=("neo4j", NEO4J_PASSWORD))
        )
        patch.setattr(history, "get_history_store", lambda: self.history)

    def create(self, *, owner="alice", key=None, approval=False, session=None):
        from copy import deepcopy

        cfg = deepcopy(self.cfg)
        cfg["configurable"]["allow_plan_feedback"] = approval
        return self.client.create(
            owner,
            key or uuid4().hex,
            {"topic": "brand recall"},
            cfg,
            self.state,
            session_id=session,
        )[0]

    def wait(self, run, predicate=None, timeout=50):
        predicate = predicate or (
            lambda r: r["status"] in {"completed", "failed", "cancelled", "interrupted"}
            and r["settlement"] != "pending"
        )
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = self.client.get(run["run_id"], run["owner"])
            if predicate(value):
                return value
            time.sleep(0.05)
        raise AssertionError(
            "Native workflow timed out: "
            + str(
                {k: value[k] for k in ("status", "error", "current_step", "settlement")}
            )
        )


@pytest.fixture
def native_case(pg_database, monkeypatch, tmp_path, request):
    from open_deep_research.web.executor import launch

    mode = getattr(request, "param", "normal")
    case = NativeCase(monkeypatch, tmp_path, mode)
    launch("test-" + uuid4().hex)
    try:
        yield case
    finally:
        # This database was created by this exact fixture, never a shared catalog.
        repo = facts()
        with repo.engine.connect() as db:
            owned_ids = list(
                db.execute(text("SELECT id FROM por_domain.requests")).scalars()
            )
        for root in owned_ids:
            targets = repo.client.list_workflows(
                workflow_id_prefix=root,
                status=["PENDING", "ENQUEUED"],
                load_input=False,
                load_output=False,
            )
            for target in targets:
                repo.client.cancel_workflow(target.workflow_id, cancel_children=True)
        time.sleep(0.3)
        DBOS.destroy(workflow_completion_timeout_sec=2)
        case.history.close()
