"""Actual PostgreSQL contract fixtures; inference only is scripted."""

from __future__ import annotations

import os
import time
from pathlib import Path
from uuid import uuid4

import pytest
from dbos import DBOS
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from open_deep_research.web.durable import RunClient, close_run_client
from open_deep_research.web.history import HistoryStore
from open_deep_research.web.run_data import RunConflict
from open_deep_research.workflow.dbos_config import (
    domain_engine,
    native_client,
    settings,
)
from open_deep_research.workflow.domain_schema import initialize_domain

BASE_URL = os.environ.get(
    "TEST_DBOS_DATABASE_URL",
    "postgresql+psycopg://por_execution:local-dbos-development-only@127.0.0.1:15434/por_execution",
)


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
    initialize_domain()
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


def stage_observations(run_id):
    """Read public stage events and their physical bills for the test assertions."""
    from open_deep_research.budget import merge_budget_usage
    from open_deep_research.web.run_data import loads

    used = {}
    with domain_engine().connect() as db:
        for stage, usage in db.execute(
            text(
                "SELECT stage_id,usage FROM por_domain.model_attempts WHERE run_id=:r"
            ),
            {"r": run_id},
        ):
            used[stage] = merge_budget_usage(
                used.get(stage),
                loads(usage) if usage is not None else {"model_calls": 1},
            )
    store = RunClient()
    stages = {}
    for event in store.read_events(run_id, None, 0, 10000):
        if event["type"] not in {
            "step_started",
            "step_failed",
            "step_skipped",
            "step_completed",
        }:
            continue
        stage = event["unit_id"]
        view = stages.setdefault(
            stage,
            dict(
                id=stage,
                name=event["step"],
                artifact=None,
                error={},
                settled=None,
                usage=used.get(stage, {}),
            ),
        )
        if event["type"] != "step_started":
            view["settled"] = not event.get("error", {}).get("unknown_outcome")
        if event["type"] in {"step_completed", "step_skipped"}:
            view["artifact"] = True
            view["usage"] = merge_budget_usage(
                view["usage"],
                {
                    key: event[key]
                    for key in ("budget_warnings", "degradation_reasons")
                    if key in event
                },
            )
        if event["type"] == "step_failed":
            view["error"] = event.get("error", {})
    return list(stages.values())


def holds_business_slot(run_id):
    return bool(
        native_client().list_workflows(
            workflow_id_prefix=run_id + ":",
            status="PENDING",
            queue_name="por-business",
            load_input=False,
            load_output=False,
        )
    )


def has_unconfirmed_bills_or_effects(run_id):
    from open_deep_research.budget import model_bill
    from open_deep_research.web.durable import external_effects

    with domain_engine().connect() as db:
        return bool(any(external_effects(db, run_id)) or model_bill(db, run_id)[1])


async def record_scripted_model_attempt():
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import AIMessage, HumanMessage

    from open_deep_research.budget import ainvoke_model_with_budget

    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="controlled failed review",
                usage_metadata={
                    "input_tokens": 2,
                    "output_tokens": 0,
                    "total_tokens": 2,
                },
            )
        ]
    )
    await ainvoke_model_with_budget(
        model, [HumanMessage(content="test review")], model_name="fixture:review"
    )


def retry_observed_failure(case, run, key, observed):
    """Re-observe only a newer receipt cursor for the same safe test failure.

    Late relay packets can invalidate a terminal observation. Never reinterpret
    an accepted request, a different failure generation or an unsafe outcome.
    """
    for attempt in range(3):
        try:
            case.client.retry(
                run["run_id"], run["owner"], key, observed["state_version"]
            )
        except RunConflict:
            if attempt == 2:
                raise
            latest = case.client.get(run["run_id"], run["owner"])
            if (
                latest["state_version"] <= observed["state_version"]
                or latest["generation"] != observed["generation"]
                or latest["status"] != "failed"
                or not latest["retry_safe"]
                or not latest["events_complete"]
                or latest["retry_requested"]
                or latest["settlement"] != "settled"
            ):
                raise
            observed = latest
        else:
            return observed


class NativeCase:
    def __init__(self, patch, directory, mode):
        self.mode = mode
        from test_python_workflow_sdk import install_sdk

        from open_deep_research.web import history

        self.plan_prompts = []
        self.revision_requests = []
        self.revision_response = None

        async def revise(data):
            self.revision_requests.append(data)
            if self.revision_response is not None:
                return await self.revision_response(data)
            return None

        self.state, self.cfg, self.runtimes, self.models, self.calls, self.blocked = (
            install_sdk(
                patch,
                directory,
                mode,
                on_plan=self.plan_prompts.append,
                on_revision=revise,
            )
        )
        self.client = RunClient()
        self.owner = "alice"
        self.history = HistoryStore(domain_engine())
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
        with domain_engine().connect() as db:
            owned_ids = list(
                db.execute(text("SELECT id FROM por_domain.requests")).scalars()
            )
        for root in owned_ids:
            targets = native_client().list_workflows(
                workflow_id_prefix=root,
                status=["PENDING", "ENQUEUED"],
                load_input=False,
                load_output=False,
            )
            for target in targets:
                native_client().cancel_workflow(
                    target.workflow_id, cancel_children=True
                )
        time.sleep(0.3)
        DBOS.destroy(workflow_completion_timeout_sec=2)
