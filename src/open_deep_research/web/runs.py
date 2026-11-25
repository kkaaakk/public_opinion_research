"""Expose durable Run controls and cursor-only SSE subscriptions on FastAPI."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from dbos import error as dbos_error
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.web.durable import get_run_client
from open_deep_research.web.run_data import (
    IDEMPOTENCY_KEY_PATTERN,
    TERMINAL,
    RunConflict,
    RunNotFound,
    public_payload,
    public_run,
)
from open_deep_research.workflow.brief import MAX_BRIEF_FEEDBACK_CHARS

MAX_TOPIC_LENGTH = 12_000
MAX_ORG_CONTEXT_LENGTH = 8_000

router = APIRouter(prefix="/api/runs")
history_router = APIRouter(prefix="/api/history")


class ResearchRequest(BaseModel):
    """Validate the business input of one research Run.

    Models, research depth, search API, budgets, approval and clarification
    policy stay in ``config.yaml``. The request carries the topic, an optional
    organization context, the Run-scoped RAG switch and the research Session to
    continue; see :func:`open_deep_research.config_file.run_configuration`.
    """

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1, max_length=MAX_TOPIC_LENGTH)
    org_context: str = Field(default="", max_length=MAX_ORG_CONTEXT_LENGTH)
    rag_enabled: bool = Field(default=False, strict=True)
    research_session_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")

    # 去除主题两端空白，并拒绝空主题。
    @field_validator("topic")
    @classmethod
    def validate_topic(cls, value: str) -> str:
        """Reject blank topics after trimming whitespace."""
        if not value.strip():
            raise ValueError("topic must not be blank")
        return value


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
    except (SQLAlchemyError, dbos_error.DBOSQueryTimeoutError):
        raise HTTPException(503, "Durable Run storage is unavailable.") from None


def _key(raw: Request) -> str:
    key = raw.headers.get("idempotency-key", "")
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise HTTPException(400, "A bounded Idempotency-Key header is required.")
    return key


def create_run(body: ResearchRequest, raw: Request) -> tuple[dict, bool]:
    """Create one durable Run from an already validated business request.

    The Web request submits business input only: the topic, an optional
    organization context, the per-Run RAG switch and an existing research
    Session. Research models, depth, search API, budgets and approval policy
    stay in ``config.yaml``. The server configuration is copied once so the RAG
    switch cannot mutate the shared object, and that copy is what the Run
    freezes and resumes from.
    """
    from open_deep_research.config_file import (
        DEFAULT_CONFIG_FILE,
        ConfigFileError,
        frozen_config,
        load_configuration,
        request_payload,
        run_configuration,
    )

    owner = trusted_owner(raw)
    key = _key(raw)
    try:
        configuration = run_configuration(
            load_configuration(DEFAULT_CONFIG_FILE), rag_enabled=body.rag_enabled
        )
    except (ConfigFileError, ValueError):
        raise HTTPException(
            503,
            "Research configuration is unavailable; migrate retired runtime settings to DBOS/SDK.",
        ) from None
    data = request_payload(
        body.topic,
        configuration,
        org_context=body.org_context or None,
    )
    return _call(
        _store().create,
        owner,
        key,
        data,
        frozen_config(configuration),
        {"messages": [HumanMessage(content=body.topic)]},
        session_id=body.research_session_id,
        config_source=str(DEFAULT_CONFIG_FILE),
    )


@router.post("")
def create(body: ResearchRequest, raw: Request) -> JSONResponse:
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


@history_router.get("")
def list_history(
    raw: Request,
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> JSONResponse:
    """List the authenticated owner's delivered terminal Runs."""
    owner = trusted_owner(raw)
    page = _call(_store().list_history, owner, limit, offset)
    return JSONResponse(public_payload(page), headers={"Cache-Control": "no-store"})


@history_router.get("/{run_id}")
def read_history(run_id: str, raw: Request) -> JSONResponse:
    """Open a terminal Run with the same read model as the live Run API."""
    owner = trusted_owner(raw)
    run = _call(_store().get, run_id, owner)
    if (
        run["status"] not in TERMINAL
        or run["settlement"] == "pending"
        or not run["events_complete"]
        or run["retry_requested"]
    ):
        raise HTTPException(404, "Finished research Run not found.")
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
