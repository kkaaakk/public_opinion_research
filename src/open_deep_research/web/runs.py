"""Expose durable Run controls and cursor-only SSE subscriptions on FastAPI."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.web.durable import get_run_client
from open_deep_research.web.run_data import (
    IDEMPOTENCY_KEY_PATTERN,
    RunConflict,
    RunNotFound,
    public_run,
)
from open_deep_research.workflow.brief import MAX_BRIEF_FEEDBACK_CHARS

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
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise HTTPException(400, "A bounded Idempotency-Key header is required.")
    return key


def create_run(body: dict, raw: Request) -> tuple[dict, bool]:
    """Validate a durable create, then atomically persist the queue delivery.

    The Web request submits business input only: the topic, an optional
    organization context and the per-Run RAG switch. Research models, depth,
    search API, budgets and approval policy stay in ``config.yaml``. The server
    configuration is copied once so the RAG switch cannot mutate the shared
    object, and that copy is what the Run freezes and resumes from.
    """
    from open_deep_research.config_file import (
        DEFAULT_CONFIG_FILE,
        ConfigFileError,
        frozen_config,
        load_configuration,
        request_payload,
        run_configuration,
    )
    from open_deep_research.web.server import ResearchRequest

    owner = trusted_owner(raw)
    key = _key(raw)
    permitted = {
        "topic",
        "org_context",
        "rag_enabled",
        "research_session_id",
        # Retired runtime selectors stay accepted and discarded as input
        # aliases; the request model decides that, not this allow-list.
        "workflow_backend",
        "business_agent_runtime",
    }
    if set(body) - permitted:
        raise HTTPException(400, "Unsupported research parameter.")
    try:
        request = ResearchRequest.model_validate(body)
    except ValidationError:
        raise HTTPException(400, "Invalid research request.") from None
    session_id = body.get("research_session_id")
    if session_id is not None and not re.fullmatch(r"[a-f0-9]{32}", str(session_id)):
        raise HTTPException(400, "Invalid research Session.")
    try:
        configuration = run_configuration(
            load_configuration(DEFAULT_CONFIG_FILE), rag_enabled=request.rag_enabled
        )
    except (ConfigFileError, ValueError):
        raise HTTPException(
            503,
            "Research configuration is unavailable; migrate retired runtime settings to DBOS/SDK.",
        ) from None
    data = request_payload(
        request.topic,
        configuration,
        org_context=request.org_context or None,
    )
    return _call(
        _store().create,
        owner,
        key,
        data,
        frozen_config(configuration),
        {"messages": [HumanMessage(content=request.topic)]},
        session_id=session_id,
        config_source=str(DEFAULT_CONFIG_FILE),
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
    version: str = Field(
        pattern=r"^[a-f0-9]{64}$",
        validation_alias=AliasChoices("version", "plan_version"),
    )
    decision: Literal["approve", "reject", "edit", "revise"]
    brief_md: str | None = Field(default=None, max_length=30000)
    feedback: str | None = Field(
        default=None, max_length=MAX_BRIEF_FEEDBACK_CHARS, strict=True
    )


@router.post("/{run_id}/approval")
def approve(run_id: str, body: Approval, raw: Request) -> dict:
    """Submit a version-bound decision and return the current Run snapshot."""
    owner = trusted_owner(raw)
    key = _key(raw) if body.decision == "revise" else raw.headers.get("idempotency-key")
    return public_run(
        _call(
            _store().approve,
            run_id,
            owner,
            body.version,
            body.decision,
            body.brief_md,
            feedback=body.feedback,
            key=key,
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
            if not events:
                if await asyncio.to_thread(store.stream_finished, run_id, owner, after):
                    return
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


@router.get("/{run_id}/event-page")
def event_page(
    run_id: str,
    raw: Request,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
) -> JSONResponse:
    """Read a bounded prefix for cold replay; this never starts research."""
    owner = trusted_owner(raw)
    items = _call(_store().read_events, run_id, owner, after, limit + 1)
    page = items[:limit]
    return JSONResponse(
        {
            "items": page,
            "next_cursor": page[-1]["seq"] if page else after,
            "has_more": len(items) > limit,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{run_id}/execution")
def execution(
    run_id: str,
    raw: Request,
    workflow_id: str = Query(default="", max_length=256),
    offset: int = Query(default=0, ge=0),
    workflow_offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
) -> JSONResponse:
    from open_deep_research.web.observations import execution_view

    owner = trusted_owner(raw)
    data = _call(
        execution_view,
        _store(),
        run_id,
        owner,
        workflow_id,
        offset,
        limit,
        workflow_offset,
    )
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


class ClarificationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: str = Field(pattern=r"^[a-f0-9]{64}$")
    skip: bool = False
    answers: dict[str, str | list[str]] = Field(default_factory=dict, max_length=4)


@router.post("/{run_id}/clarification")
def clarify(run_id: str, body: ClarificationAnswer, raw: Request) -> dict:
    """Continue this same Run using a durable, version-bound answer or explicit skip."""
    owner = trusted_owner(raw)
    return public_run(
        _call(_store().clarify, run_id, owner, body.version, body.answers, body.skip)
    )
