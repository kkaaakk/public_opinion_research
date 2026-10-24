"""Expose durable Run controls and cursor-only SSE subscriptions on FastAPI."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.configuration import (
    DEFAULT_PUBLIC_OPINION_AGENTS,
    Configuration,
)
from open_deep_research.web.durable import get_run_client
from open_deep_research.web.run_data import (
    TERMINAL,
    RunConflict,
    RunNotFound,
    public_run,
)

router = APIRouter(prefix="/api/runs")


def trusted_owner(request: Request) -> str:
    """Authenticate the existing single-operator token, ignoring client owner IDs."""
    from open_deep_research.web import server

    if not server.WEB_API_TOKEN:
        raise HTTPException(
            503, "Configure PUBLIC_OPINION_API_TOKEN before enabling durable Runs."
        )
    server._require_authorization(request)
    return "operator"


def _store():
    try:
        return get_run_client()
    except (ValueError, SQLAlchemyError):
        raise HTTPException(503, "Durable Run storage is unavailable.") from None


def _call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except RunNotFound:
        raise HTTPException(404, "Run or Session not found.") from None
    except RunConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except SQLAlchemyError:
        raise HTTPException(503, "Durable Run storage is unavailable.") from None


def _key(raw: Request) -> str:
    key = raw.headers.get("idempotency-key", "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
        raise HTTPException(400, "A bounded Idempotency-Key header is required.")
    return key


def accepted_config(body: dict) -> dict:
    """Build only admitted business settings; URLs, credentials and owners are trusted."""
    mode = {
        "fast": {"max_react_tool_calls": 2, "max_content_length": 8000},
        "normal": {"max_react_tool_calls": 4, "max_content_length": 20000},
        "deep": {},
    }[body["mode"]]
    return {
        "configurable": dict(
            research_model=body["model"],
            compression_model=body["model"],
            final_report_model=body["model"],
            summarization_model=body["model"],
            search_api=body["search_api"],
            allow_clarification=False,
            allow_plan_feedback=body.get("require_approval", False),
            business_scenario="public_opinion_risk",
            organization_context=body["org_context"] or None,
            rag_enabled=body["rag_enabled"],
            retrieval_mode="hybrid" if body["rag_enabled"] else "web_only",
            enabled_business_agents=[
                role
                for role in DEFAULT_PUBLIC_OPINION_AGENTS
                if body["rag_enabled"] or role != "internal_knowledge"
            ],
            **mode,
        )
    }


def create_run(body: dict, raw: Request) -> tuple[dict, bool]:
    """Validate a durable create, then atomically persist the queue delivery."""
    from open_deep_research.web.server import ResearchRequest

    owner = trusted_owner(raw)
    key = _key(raw)
    permitted = {
        "topic",
        "model",
        "search_api",
        "mode",
        "org_context",
        "rag_enabled",
        "workflow_backend",
        "business_agent_runtime",
        "require_approval",
        "research_session_id",
    }
    if set(body) - permitted:
        raise HTTPException(400, "Unsupported research parameter.")
    try:
        request = ResearchRequest.model_validate(body)
    except ValidationError:
        raise HTTPException(400, "Invalid research request.") from None
    # The SDK provider adapter currently supports these Web model families.
    if (
        not request.model.startswith(("openai:", "deepseek:"))
        or request.search_api != "tavily"
    ):
        raise HTTPException(
            400, "This SDK Web path requires a supported model and local Tavily tools."
        )
    session_id = body.get("research_session_id")
    if session_id is not None and not re.fullmatch(r"[a-f0-9]{32}", str(session_id)):
        raise HTTPException(400, "Invalid research Session.")
    data = request.model_dump()
    config = accepted_config(data)
    try:
        Configuration.from_runnable_config(config)
    except ValueError:
        raise HTTPException(
            503,
            "Research configuration is unavailable; migrate retired runtime settings to DBOS/SDK.",
        ) from None
    return _call(
        _store().create,
        owner,
        key,
        data,
        config,
        {"messages": [HumanMessage(content=request.topic)]},
        session_id=session_id,
    )


@router.post("")
def create(body: dict, raw: Request) -> JSONResponse:
    """Create one background Run; a repeated identical key returns the same Run."""
    run, created = create_run(body, raw)
    return JSONResponse(
        public_run(run),
        status_code=202 if created else 200,
        headers={"Location": f"/api/runs/{run['run_id']}", "Cache-Control": "no-store"},
    )


@router.get("/{run_id}")
def read(run_id: str, raw: Request) -> JSONResponse:
    """Read current progress and retained reports within the trusted owner scope."""
    owner = trusted_owner(raw)
    run = _call(_store().get, run_id, owner)
    return JSONResponse(public_run(run), headers={"Cache-Control": "no-store"})


@router.post("/{run_id}/cancel")
def cancel(run_id: str, raw: Request) -> dict:
    """Request cancellation without treating an HTTP disconnect as a stop."""
    owner = trusted_owner(raw)
    return public_run(_call(_store().cancel, run_id, owner))


class Approval(BaseModel):
    """Bind one owner decision to the exact immutable plan shown to the user."""

    model_config = ConfigDict(extra="forbid")
    plan_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approve", "reject"]


@router.post("/{run_id}/approval")
def approve(run_id: str, body: Approval, raw: Request) -> dict:
    """Consume an approval once and persist the continuation delivery atomically."""
    owner = trusted_owner(raw)
    return public_run(
        _call(
            _store().approve,
            run_id,
            owner,
            body.plan_version,
            body.decision,
        )
    )


class Retry(BaseModel):
    """Require an observed state version as well as a retry request identity."""

    model_config = ConfigDict(extra="forbid")
    state_version: int = Field(ge=1)


@router.post("/{run_id}/retry")
@router.post("/{run_id}/resume")
def retry(run_id: str, body: Retry, raw: Request) -> dict:
    """Continue only a verified safe checkpoint or a settled side-effect-free step."""
    owner = trusted_owner(raw)
    return public_run(
        _call(_store().retry, run_id, owner, _key(raw), body.state_version)
    )


async def event_stream(store, run_id: str, owner: str, after: int, raw: Request):
    """Replay the ordered DBOS Stream; subscribing never claims or executes work."""
    while True:
        if await raw.is_disconnected():
            return
        try:
            events = await asyncio.to_thread(store.read_events, run_id, owner, after)
            for item in events:
                after = item["seq"]
                yield f"id: {after}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
            run = await asyncio.to_thread(store.get, run_id, owner)
            terminal_delivered = (
                run["status"] in TERMINAL
                and run["settlement"] != "pending"
                and run["events_complete"]
            )
            approval_delivered = (
                run["status"] == "awaiting_approval" and run["events_paused"]
            )
            if (terminal_delivered or approval_delivered) and after >= run["seq"]:
                return
            if not events:
                yield ": keep-alive\n\n"
                await asyncio.sleep(0.5)
        except (SQLAlchemyError, RunConflict, RunNotFound):
            # No fake completion or error event with an invented durable ID.
            # EOF means reconnect/read state; persisted cursor remains valid.
            return


def subscription(raw: Request, run_id: str, after: int = 0) -> StreamingResponse:
    """Authorize and validate the replay cursor before sending response headers."""
    owner = trusted_owner(raw)
    last_id = raw.headers.get("last-event-id")
    if last_id:
        if not last_id.isascii() or not last_id.isdigit() or len(last_id) > 18:
            raise HTTPException(400, "Invalid event cursor.")
        after = int(last_id)
    store = _store()
    _call(store.read_events, run_id, owner, after, 1)
    return StreamingResponse(
        event_stream(store, run_id, owner, after, raw),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Run-ID": run_id,
        },
    )


@router.get("/{run_id}/events")
def events(
    run_id: str, raw: Request, after: int = Query(default=0, ge=0)
) -> StreamingResponse:
    """Reconnect with Last-Event-ID or after without repeating an Agent invocation."""
    return subscription(raw, run_id, after)
