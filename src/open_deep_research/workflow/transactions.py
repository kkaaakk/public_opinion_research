"""Business database transactions owned by a DBOS Datasource.

Every function here is one native transaction step: the business rows it writes
and the datasource checkpoint that records its result commit in the same
database transaction. A recovered execution therefore replays the committed
outcome instead of writing a second time, and a crash before the commit leaves
no partial stage transition behind.

These transactions never perform network I/O. Model calls, tool calls and
provider receipts stay in their own ledgers.
"""

from __future__ import annotations

from functools import lru_cache

from dbos import SQLAlchemyDatasource

from open_deep_research.state import ResearchReview
from open_deep_research.web.run_data import (
    RunJSON,
    digest,
    publish_event,
    role_deliverables,
    stage_budget,
)
from open_deep_research.workflow.dbos_config import (
    SYSTEM_SCHEMA,
    domain_engine,
    settings,
)

# Stage events are inserted with a deterministic message identity.
ISOLATION = "READ COMMITTED"
STAGES = ("running", "completed", "failed")


@lru_cache(maxsize=1)
def business_datasource() -> SQLAlchemyDatasource:
    """Own the schema, engine and checkpoint table of every business transaction.

    The engine is the shared business engine, so these transactions write the
    same PostgreSQL database as the repositories they call.
    """
    return SQLAlchemyDatasource.create(
        settings().url,
        engine=domain_engine(),
        schema=SYSTEM_SCHEMA,
        serializer=RunJSON(),
    )


def prepare():
    """Register the business datasource before ``DBOS.launch()``.

    DBOS owns each datasource's checkpoint lifecycle, so it only accepts a
    datasource before launch. Rebuilding the instance binds it to the registry of
    the process epoch that is starting.
    """
    business_datasource.cache_clear()
    return business_datasource()


def run_transaction(name, call, *args):
    """Execute one named transaction step on the live workflow."""
    return business_datasource().run_tx_step(
        {"name": name, "isolation_level": ISOLATION}, call, *args
    )


def stage_event(db, scope, kind, **payload):
    """Publish one business event inside the caller's transaction."""
    publish_event(
        db,
        scope.run_id,
        kind,
        step=scope.name,
        unit_id=scope.stage_id,
        execution_id=scope.execution_id,
        generation=scope.generation,
        idempotency_key=digest((scope.execution_id, kind, payload.get("role"))),
        **payload,
    )


def _commit_stage(scope, status, result, error):
    """Write one stage transition and its progress events atomically."""
    if status not in STAGES:
        raise ValueError(f"Unknown stage status: {status}")
    db = business_datasource().sql_session()
    if status == "running":
        stage_event(
            db,
            scope,
            "step_started",
            research_round=scope.research_round,
        )
        stage_event(
            db, scope, "status", message=f"Running {scope.name}", granularity="stage"
        )
    elif status == "failed":
        if not error or not error.get("kind"):
            raise ValueError("A failed stage transaction requires an error")
        stage_event(db, scope, "step_failed", error_kind=error["kind"], error=error)
    else:
        patch = result.get("patch", {})
        if result.get("observation", {}).get("skipped"):
            stage_event(db, scope, "step_skipped", reason="role_disabled")
        else:
            stage_event(db, scope, "step_completed", **stage_budget(result))
        for role, report in role_deliverables(result).items():
            stage_event(db, scope, "agent_report", role=role, content=report)
            stage_event(
                db,
                scope,
                "stream",
                role=role,
                node=role + "_agent",
                content=report,
                granularity="stage",
            )
        review = patch.get("workflow", {}).get("review")
        if review is not None:
            stage_event(
                db,
                scope,
                "research_review",
                review=ResearchReview.model_validate(review).model_dump(mode="json"),
            )
        if report := patch.get("report", {}).get("final"):
            stage_event(db, scope, "report", content=report)
    return {"stage": scope.name, "status": status}


def commit_stage(scope, status, *, result=None, error=None):
    """Commit one stage transition: ``running``, ``completed`` or ``failed``.

    A skipped stage keeps its explicit ``observation.skipped`` receipt, so the
    business distinction between "disabled" and "completed with no output"
    survives the unified entry point.
    """
    return run_transaction("por.commit_stage", _commit_stage, scope, status, result, error)


__all__ = [
    "business_datasource",
    "commit_stage",
    "prepare",
    "run_transaction",
    "stage_event",
]
