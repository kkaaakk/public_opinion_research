"""Expose operator-safe business configuration and independent service probes."""

from typing import Literal

from dbos import error as dbos_error
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.config_file import config_summary
from open_deep_research.web.contracts import BudgetLimits
from open_deep_research.web.images import ImageLimits, image_limits
from open_deep_research.web.run_data import public_payload
from open_deep_research.web.runs import (
    ResearchRequest,
    bearer,
    trusted_owner,
    web_configuration,
)
from open_deep_research.workflow.dbos_config import APP, domain_engine, native_client

router = APIRouter(prefix="/api", dependencies=[bearer])


class Settings(BaseModel):
    models: dict[str, str]
    budget: BudgetLimits
    search_api: str
    retrieval_mode: str
    rag_enabled: bool
    enabled_roles: list[str]
    enabled_plugins: list[str]
    allow_clarification: bool
    require_approval: bool
    tracing_enabled: bool
    max_research_rounds: int
    max_react_tool_calls: int
    workflow_step_timeout_seconds: float
    new_run_rag_default: bool
    image_limits: ImageLimits


class SystemStatus(BaseModel):
    web: Literal["ok"] = "ok"
    database: Literal["reachable", "unconfigured", "unavailable"]
    catalog: Literal["readable", "unavailable", "unknown"] = "unknown"
    executor: Literal["unknown"] = "unknown"
    registered_queues: list[str] | None = None


@router.get("/settings", response_model=Settings)
def settings_view(raw: Request) -> JSONResponse:
    """Read an explicit allowlist of effective business settings."""
    trusted_owner(raw)
    config = web_configuration()
    summary = config_summary(config)
    view = Settings(
        models={
            key: value
            for key, value in summary.items()
            if key.endswith("_model") and value
        },
        budget=BudgetLimits.model_validate(summary),
        search_api=summary["search_api"],
        retrieval_mode=summary["retrieval_mode"],
        rag_enabled=summary["rag_enabled"],
        enabled_roles=summary["enabled_business_agents"],
        enabled_plugins=summary["enabled_plugins"],
        allow_clarification=summary["allow_clarification"],
        require_approval=summary["allow_plan_feedback"],
        tracing_enabled=summary["tracing_enabled"],
        max_research_rounds=summary["max_research_rounds"],
        max_react_tool_calls=summary["max_react_tool_calls"],
        workflow_step_timeout_seconds=summary["workflow_step_timeout_seconds"],
        new_run_rag_default=ResearchRequest.model_fields["rag_enabled"].default,
        image_limits=image_limits(config),
    )
    return JSONResponse(
        public_payload(view.model_dump()), headers={"Cache-Control": "no-store"}
    )


@router.get("/system/status", response_model=SystemStatus)
def system_status(raw: Request) -> JSONResponse:
    """Check connectivity and readable DBOS metadata without inferring worker liveness."""
    trusted_owner(raw)
    result = SystemStatus(database="reachable")
    try:
        with domain_engine().connect() as db:
            db.execute(text("SELECT 1"))
    except ValueError:
        result.database = "unconfigured"
    except SQLAlchemyError:
        result.database = "unavailable"
    else:
        try:
            client = native_client()
            client.list_workflows(limit=1, load_input=False, load_output=False)
            result.registered_queues = sorted(
                queue.name for queue in client.list_queues(application_name=APP)
            )
            result.catalog = "readable"
        except (SQLAlchemyError, dbos_error.DBOSQueryTimeoutError):
            result.catalog = "unavailable"
    return JSONResponse(result.model_dump(), headers={"Cache-Control": "no-store"})
