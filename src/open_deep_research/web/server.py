"""FastAPI web server for Open Deep Research.

Provides a clean web frontend and SSE streaming API for the deep research agent.
Launch with: uv run python -m open_deep_research.web.server
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import uuid
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

# Load .env before anything else — API keys live there
load_dotenv()

from open_deep_research.observability import (  # noqa: E402, I001
    correlation_metadata,
    ensure_langsmith_configuration,
)

ensure_langsmith_configuration()

from fastapi import FastAPI, HTTPException, Query, Request  # noqa: E402, I001
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402
from pydantic import BaseModel, Field, field_validator  # noqa: E402

from open_deep_research.deep_researcher import deep_researcher as _deep_researcher_factory  # noqa: E402, I001
from open_deep_research.trajectory.service import TrajectoryService  # noqa: E402
from open_deep_research.trajectory.events import validate_session_id  # noqa: E402
from open_deep_research.web.research import ResearchExecution  # noqa: E402

STATIC_DIR = Path(__file__).parent / "static"
LOGGER = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 64 * 1024
MAX_TOPIC_LENGTH = 12_000
MAX_ORG_CONTEXT_LENGTH = 8_000
MAX_CONCURRENT_RESEARCH_REQUESTS = 2
ALLOWED_RESEARCH_MODELS = frozenset(
    {
        "deepseek:deepseek-chat",
        "deepseek:deepseek-v4-flash",
        "deepseek:deepseek-flash",
        "openai:gpt-4.1",
        "openai:gpt-4o",
        "anthropic:claude-sonnet-4-20250514",
        "anthropic:claude-opus-4-20250514",
        "google:gemini-2.5-pro",
    }
)
WEB_API_TOKEN = (
    os.environ.get("PUBLIC_OPINION_API_TOKEN")
    or os.environ.get("WEB_API_TOKEN")
    or ""
).strip()
_RESEARCH_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_RESEARCH_REQUESTS)

app = FastAPI(
    title="Public Opinion Research",
    description="Enterprise public-opinion and brand-risk monitoring system",
    version="0.2.0",
)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class ResearchRequest(BaseModel):
    """Validate the existing public research request and local trajectory opt-in."""
    topic: str = Field(min_length=1, max_length=MAX_TOPIC_LENGTH)
    model: str = "deepseek:deepseek-flash"
    search_api: Literal["tavily", "openai", "anthropic"] = "tavily"
    mode: Literal["fast", "normal", "deep"] = "normal"
    org_context: str = Field(default="", max_length=MAX_ORG_CONTEXT_LENGTH)
    rag_enabled: bool = False
    trajectory_enabled: bool = True

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, value: str) -> str:
        """Reject blank topics after trimming whitespace."""
        if not value.strip():
            raise ValueError("topic must not be blank")
        return value

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        """Allow only models selected by the server-side policy."""
        if value not in ALLOWED_RESEARCH_MODELS:
            raise ValueError("model is not allowed")
        return value


@app.middleware("http")
async def enforce_request_size(request: Request, call_next):
    """Reject oversized HTTP bodies before they reach request validation."""
    content_length = request.headers.get("content-length")
    try:
        request_size = int(content_length) if content_length else 0
    except ValueError:
        request_size = MAX_REQUEST_BYTES + 1
    if request_size > MAX_REQUEST_BYTES:
        return JSONResponse(
            status_code=413,
            content={"detail": "Request payload exceeds the allowed size."},
        )
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Return stable, non-sensitive validation errors to API clients."""
    oversized_types = {"string_too_long", "bytes_too_long", "value_error.any_str.max_length"}
    status_code = 413 if any(error.get("type") in oversized_types for error in exc.errors()) else 400
    message = (
        "Request payload exceeds the allowed size."
        if status_code == 413
        else "Invalid research request."
    )
    return JSONResponse(status_code=status_code, content={"detail": message})


def _request_is_authorized(request: Request) -> bool:
    """Validate the optional local API token without exposing it in errors/logs."""
    if not WEB_API_TOKEN:
        return True
    authorization = request.headers.get("authorization", "").split()
    if len(authorization) == 2 and authorization[0].lower() == "bearer":
        provided_token = authorization[1]
    else:
        provided_token = request.headers.get("x-api-token", "")
    return secrets.compare_digest(provided_token, WEB_API_TOKEN)


def _event(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    """Serve the packaged React entry point."""
    index_path = STATIC_DIR / "dist" / "index.html"
    if index_path.is_file():
        return index_path.read_text(encoding="utf-8")
    return "<h1>Public Opinion Research</h1><p>Build the frontend with npm run build.</p>"


@app.get("/api/health")
async def health() -> dict:
    """Report process health without calling any provider."""
    return {"status": "ok"}


_trajectory_service: TrajectoryService | None = None
_executions: dict[str, ResearchExecution] = {}


def trajectory_service() -> TrajectoryService:
    """Initialize storage lazily; tests can replace the service in isolation."""
    global _trajectory_service
    if _trajectory_service is None:
        _trajectory_service = TrajectoryService()
    return _trajectory_service


def _authorize(raw: Request) -> None:
    if not _request_is_authorized(raw):
        raise HTTPException(status_code=401, detail="Authentication required.")


def _history(session_id: str, before_seq: int | None, limit: int) -> dict:
    try:
        validate_session_id(session_id)
        return trajectory_service().history(session_id, before_seq=before_seq, limit=limit)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Trajectory session not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid or corrupt trajectory history.") from exc
    except Exception as exc:
        LOGGER.warning("Trajectory history unavailable: category=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Trajectory history unavailable.") from exc


@app.get("/api/trajectory/sessions")
async def trajectory_sessions(raw: Request) -> dict:
    """List local history under the existing API authorization policy."""
    _authorize(raw)
    try:
        return {"sessions": trajectory_service().list()[:100]}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Trajectory history unavailable.") from exc


@app.get("/api/trajectory/sessions/{session_id}")
async def trajectory_session(session_id: str, raw: Request) -> dict:
    """Read status and the latest replay snapshot without executing a graph."""
    _authorize(raw)
    return _history(session_id, None, 100)


@app.get("/api/trajectory/sessions/{session_id}/events")
async def trajectory_events(session_id: str, raw: Request,
                            before_seq: int | None = Query(default=None, ge=0),
                            limit: int = Query(default=100, ge=1, le=500)) -> dict:
    """Read a bounded older event page with a cumulative deterministic projection."""
    _authorize(raw)
    return _history(session_id, before_seq, limit)


def _stream_response(execution: ResearchExecution, session_id: str) -> StreamingResponse:
    async def stream():
        async for packet in execution.stream():
            yield _event(packet)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        "X-Research-Session-Id": session_id,
    })


@app.get("/api/trajectory/sessions/{session_id}/stream")
async def trajectory_stream(session_id: str, raw: Request) -> StreamingResponse:
    """Reconnect to the original live writer, never reinvoke research for replay."""
    _authorize(raw)
    execution = _executions.get(session_id)
    if execution is None:
        raise HTTPException(status_code=409, detail="Live writer unavailable here; read history instead.")
    return _stream_response(execution, session_id)


@app.post("/api/research/{session_id}/cancel")
async def cancel_research(session_id: str, raw: Request) -> dict:
    """Await backend cancellation and preserve every committed history record."""
    _authorize(raw)
    execution = _executions.get(session_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Research execution not found.")
    await execution.cancel()
    return {"session_id": session_id, "status": execution.session.projection.status if execution.session else "cancelled"}


@app.post("/api/research")
async def research(request: ResearchRequest, raw: Request) -> StreamingResponse:
    """Admit one workflow with independent tracing, ledger and SSE subscribers."""
    _authorize(raw)
    thread_id = uuid.uuid4().hex
    mode = {"fast": {"max_react_tool_calls": 2, "max_content_length": 8000},
            "normal": {"max_react_tool_calls": 4, "max_content_length": 20000}, "deep": {}}[request.mode]
    config = {"configurable": {
        "thread_id": thread_id, "research_run_id": thread_id,
        "research_model": request.model, "compression_model": request.model,
        "final_report_model": request.model, "summarization_model": request.model,
        "search_api": request.search_api, "allow_clarification": False,
        "business_scenario": "public_opinion_risk", "organization_context": request.org_context or None,
        "rag_enabled": request.rag_enabled, "retrieval_mode": "hybrid" if request.rag_enabled else "web_only", **mode}}
    config["metadata"] = {**correlation_metadata(config), "trajectory_session_id": thread_id}
    session = None
    if request.trajectory_enabled:
        session = trajectory_service().create(thread_id, config["metadata"])
        session.start_turn(request.topic, {"business_scenario": "public_opinion_risk",
            "retrieval_mode": config["configurable"]["retrieval_mode"], "rag_enabled": request.rag_enabled,
            "search_api": request.search_api, "mode": request.mode})
    execution = ResearchExecution(_deep_researcher_factory,
        {"messages": [HumanMessage(content=request.topic)]}, config, _RESEARCH_SEMAPHORE, session)
    for key, previous in list(_executions.items()):
        if len(_executions) < 8:
            break
        if previous.task is not None and previous.task.done():
            del _executions[key]
    _executions[thread_id] = execution
    execution.start()
    return _stream_response(execution, thread_id)


def main():
    """Launch the local web server with the configured host and port."""
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))

    if host in {"0.0.0.0", "::"} and not WEB_API_TOKEN:
        LOGGER.warning(
            "Web UI is listening on %s without PUBLIC_OPINION_API_TOKEN; "
            "use a reverse proxy and authentication before exposing it publicly.",
            host,
        )

    LOGGER.info("Open Deep Research web UI: http://%s:%s", host, port)

    uvicorn.run(
        "open_deep_research.web.server:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
