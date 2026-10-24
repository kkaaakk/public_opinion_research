# 提供仪表盘、SSE 实时研究接口和历史归档 API，并校验请求与可选令牌。
"""FastAPI web server for Open Deep Research.

Provides a clean web frontend and SSE streaming API for the deep research agent.
Launch with: uv run python -m open_deep_research.web.server
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

# Load .env before anything else — API keys live there
load_dotenv()

from open_deep_research.observability import (  # noqa: E402, I001
    ensure_langsmith_configuration,
)

ensure_langsmith_configuration()

from fastapi import FastAPI, HTTPException, Query, Request  # noqa: E402, I001
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator  # noqa: E402

from open_deep_research.config_values import without_retired_execution_selectors  # noqa: E402
from open_deep_research.web.history import (  # noqa: E402, I001
    HistoryUnavailable,
    close_history_store,
    get_history_store,
    import_langsmith_history,
)

STATIC_DIR = Path(__file__).parent / "static"
LOGGER = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 64 * 1024
MAX_TOPIC_LENGTH = 12_000
MAX_ORG_CONTEXT_LENGTH = 8_000
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
    os.environ.get("PUBLIC_OPINION_API_TOKEN") or os.environ.get("WEB_API_TOKEN") or ""
).strip()


# 管理 Web 服务生命周期，在关闭时释放历史数据库连接。
@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Release history database connections when the web server stops."""
    try:
        yield
    finally:
        await asyncio.to_thread(close_history_store)
        from open_deep_research.web.durable import close_run_client

        await asyncio.to_thread(close_run_client)


app = FastAPI(
    title="Public Opinion Research",
    description="Enterprise public-opinion and brand-risk monitoring system",
    version="0.2.0",
    lifespan=lifespan,
)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# 校验前端研究主题、模型及数量等受限输入。
class ResearchRequest(BaseModel):
    """Validate bounded research inputs from the web frontend."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def migration_aliases(cls, values):
        """Discard old SDK/DBOS input aliases; reject retired engine selections."""
        return (
            without_retired_execution_selectors(values)
            if isinstance(values, dict)
            else values
        )

    topic: str = Field(min_length=1, max_length=MAX_TOPIC_LENGTH)
    model: str = "deepseek:deepseek-flash"
    search_api: Literal["tavily", "openai", "anthropic"] = "tavily"
    mode: Literal["fast", "normal", "deep"] = "normal"
    org_context: str = Field(default="", max_length=MAX_ORG_CONTEXT_LENGTH)
    rag_enabled: bool = False
    require_approval: bool = False
    research_session_id: str | None = Field(default=None, max_length=64)

    # 去除主题两端空白，并拒绝空主题。
    @field_validator("topic")
    @classmethod
    def validate_topic(cls, value: str) -> str:
        """Reject blank topics after trimming whitespace."""
        if not value.strip():
            raise ValueError("topic must not be blank")
        return value

    # 只接受服务端策略允许的模型标识。
    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        """Allow only models selected by the server-side policy."""
        if value not in ALLOWED_RESEARCH_MODELS:
            raise ValueError("model is not allowed")
        return value


# 在模型校验前拒绝过大的 HTTP 请求体。
@app.middleware("http")
async def enforce_request_size(request: Request, call_next):
    """Reject oversized HTTP bodies before they reach request validation."""
    content_length = request.headers.get("content-length")
    try:
        request_size = int(content_length) if content_length else 0
    except ValueError:
        request_size = MAX_REQUEST_BYTES + 1
    if request_size > MAX_REQUEST_BYTES or request_size < 0:
        return JSONResponse(
            status_code=413,
            content={"detail": "Request payload exceeds the allowed size."},
        )
    chunks = []
    actual_bytes = 0
    async for chunk in request.stream():
        actual_bytes += len(chunk)
        if actual_bytes > MAX_REQUEST_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": "Request payload exceeds the allowed size."},
            )
        chunks.append(chunk)
    # Starlette's cached Request replays this bounded body to validation, even
    # when Content-Length was absent, false, or the request used chunked framing.
    request._body = b"".join(chunks)
    return await call_next(request)


# 将校验错误整理为稳定响应，避免回传敏感输入内容。
@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Return stable, non-sensitive validation errors to API clients."""
    oversized_types = {
        "string_too_long",
        "bytes_too_long",
        "value_error.any_str.max_length",
    }
    status_code = (
        413
        if any(error.get("type") in oversized_types for error in exc.errors())
        else 400
    )
    message = (
        "Request payload exceeds the allowed size."
        if status_code == 413
        else "Invalid research request."
    )
    return JSONResponse(status_code=status_code, content={"detail": message})


# 校验可选本地 API 令牌，避免把令牌写入日志或错误消息。
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


# 返回研究仪表盘 HTML 页面。
@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    """Serve the research dashboard."""
    index_path = STATIC_DIR / "index.html"
    if index_path.is_file():
        return index_path.read_text(encoding="utf-8")
    return "<h1>Open Deep Research</h1><p>Frontend not found.</p>"


# 返回服务存活状态，供本地健康检查使用。
@app.get("/api/health")
async def health() -> dict:
    """Return the web service's liveness status."""
    return {"status": "ok"}


# 对受保护接口执行统一授权检查，不通过时抛出 HTTP 错误。
def _require_authorization(request: Request) -> None:
    if not _request_is_authorized(request):
        raise HTTPException(status_code=401, detail="Authentication required.")


# 返回历史服务错误，隐藏连接串与数据库凭据。
@app.exception_handler(HistoryUnavailable)
async def handle_history_unavailable(
    _request: Request, exc: HistoryUnavailable
) -> JSONResponse:
    """Report history failures without leaking connection strings or credentials."""
    LOGGER.warning("History service unavailable", exc_info=exc)
    return JSONResponse(status_code=503, content={"detail": str(exc)})


# 在授权后按搜索条件和分页查询研究历史。
@app.get("/api/history")
def list_history(
    raw: Request,
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> JSONResponse:
    """List and search completed reports from the existing Neo4j database."""
    _require_authorization(raw)
    from open_deep_research.web.run_data import public_payload

    data = get_history_store().list(
        query=q,
        limit=limit,
        offset=offset,
        **({"owner": "operator"} if WEB_API_TOKEN else {}),
    )
    return JSONResponse(
        content=public_payload(data), headers={"Cache-Control": "no-store"}
    )


# 将已有 LangSmith 研究输出导入历史库，避免追踪过期后丢失。
@app.post("/api/history/import/langsmith")
def import_history(raw: Request) -> dict:
    """Preserve existing LangSmith reports before their traces expire."""
    _require_authorization(raw)
    return import_langsmith_history(get_history_store())


# 读取指定历史报告及当时研究参数，不存在时返回相应 HTTP 错误。
@app.get("/api/history/{history_id}")
def read_history(history_id: str, raw: Request) -> JSONResponse:
    """Return a saved report and the research parameters used to produce it."""
    _require_authorization(raw)
    record = get_history_store().get(
        history_id, **({"owner": "operator"} if WEB_API_TOKEN else {})
    )
    if record is None:
        raise HTTPException(status_code=404, detail="研究历史不存在。")
    from open_deep_research.web.run_data import public_payload

    return JSONResponse(
        content=public_payload(record), headers={"Cache-Control": "no-store"}
    )


# 启动研究图，并以 SSE 流推送阶段进度、报告及归档状态。
@app.post("/api/research")
async def research(request: ResearchRequest, raw: Request) -> StreamingResponse:
    """Run deep research with real-time streaming via SSE."""
    if not _request_is_authorized(raw):
        raise HTTPException(status_code=401, detail="Authentication required.")
    from open_deep_research.web.runs import create_run, subscription

    run, _ = await asyncio.to_thread(create_run, request.model_dump(), raw)
    return subscription(raw, run["run_id"])


# 读取服务启动配置并运行本地 Uvicorn 服务。
def main():
    """Launch the local research web server."""
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


from open_deep_research.web.runs import router as _run_router  # noqa: E402

app.include_router(_run_router)


if __name__ == "__main__":
    main()
