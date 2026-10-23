# 提供仪表盘、SSE 实时研究接口和历史归档 API，并校验请求与可选令牌。
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
from contextlib import asynccontextmanager
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

from open_deep_research.configuration import DEFAULT_PUBLIC_OPINION_AGENTS  # noqa: E402
from open_deep_research.deep_researcher import (  # noqa: E402, I001
    deep_researcher as _deep_researcher_factory,
)
from open_deep_research.web.history import (  # noqa: E402, I001
    HistoryUnavailable,
    close_history_store,
    get_history_store,
    history_record,
    import_langsmith_history,
    stage_reports_from_state,
    utc_timestamp,
)

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
    os.environ.get("PUBLIC_OPINION_API_TOKEN") or os.environ.get("WEB_API_TOKEN") or ""
).strip()
_RESEARCH_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_RESEARCH_REQUESTS)


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

    topic: str = Field(min_length=1, max_length=MAX_TOPIC_LENGTH)
    model: str = "deepseek:deepseek-flash"
    search_api: Literal["tavily", "openai", "anthropic"] = "tavily"
    mode: Literal["fast", "normal", "deep"] = "normal"
    org_context: str = Field(default="", max_length=MAX_ORG_CONTEXT_LENGTH)
    rag_enabled: bool = False
    workflow_backend: Literal["langgraph", "python"] = "langgraph"
    business_agent_runtime: Literal["legacy", "openai_agents"] = "legacy"
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


# 把事件字典编码为 SSE 数据帧。
def _event(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── Node → human label ─────────────────────────────────────────────
_NODE_LABEL = {
    "write_research_brief": "Planning research…",
    "research_phase": "Analyzing public opinion…",
    "final_report_generation": "Writing report…",
    "compress_research": "Summarizing…",
}


# 从不同形态的 LangChain 消息和内容块提取可读文本。
def _extract_text(value) -> str:
    """Pull readable text out of various LangChain object shapes."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value[-3:]:  # last 3 items
            if hasattr(item, "content"):
                parts.append(str(item.content)[:300])
            elif isinstance(item, str):
                parts.append(item[:300])
        return "\n".join(parts)
    if isinstance(value, dict):
        for k in ("content", "text", "message"):
            if k in value:
                return str(value[k])[:500]
        return str(value)[:500]
    return str(value)[:300]


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
    if request.workflow_backend == "python":
        from open_deep_research.web.runs import create_run, subscription

        run, _ = await asyncio.to_thread(create_run, request.model_dump(), raw)
        return subscription(raw, run["run_id"])

    # 消费研究图流事件，累计状态与角色报告，并在结束或异常时归档。
    async def _research_event_stream():
        trace_id = uuid.uuid4().hex[:12]
        thread_id = uuid.uuid4().hex
        started_at = utc_timestamp()
        final_report = None
        budget = {}
        stage_reports = []

        # 根据当前收集的输出保存完成、失败或取消状态的运行记录。
        async def archive_run(status="completed", error_message=""):
            record = history_record(
                run_id=thread_id,
                request=request.model_dump(),
                report=final_report or "",
                usage={
                    key: budget[key]
                    for key in (
                        "model_calls",
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                    )
                    if key in budget
                },
                started_at=started_at,
                status=status,
                error_message=error_message,
                role_reports=stage_reports,
            )
            try:
                await asyncio.to_thread(lambda: get_history_store().save([record]))
                return record
            except Exception:
                LOGGER.exception("Research archive failed; trace_id=%s", trace_id)
                return None

        # 生成前端可识别的历史保存成功事件。
        def saved_event(record):
            return _event(
                {
                    "type": "history_saved",
                    "id": record.id,
                    "completed_at": record.completed_at,
                    "status": record.status,
                }
            )

        try:
            yield _event({"type": "status", "message": "Starting research…"})

            mode_configs = {
                "fast": {
                    "max_react_tool_calls": 2,
                    "max_content_length": 8000,
                },
                "normal": {
                    "max_react_tool_calls": 4,
                    "max_content_length": 20000,
                },
                "deep": {},
            }
            mode = mode_configs.get(request.mode, mode_configs["normal"])

            config = {
                "configurable": {
                    "thread_id": thread_id,
                    "research_model": request.model,
                    "compression_model": request.model,
                    "final_report_model": request.model,
                    "summarization_model": request.model,
                    "search_api": request.search_api,
                    "allow_clarification": False,
                    "business_scenario": "public_opinion_risk",
                    "organization_context": request.org_context or None,
                    "rag_enabled": request.rag_enabled,
                    "retrieval_mode": "hybrid" if request.rag_enabled else "web_only",
                    "enabled_business_agents": [
                        role
                        for role in DEFAULT_PUBLIC_OPINION_AGENTS
                        if request.rag_enabled or role != "internal_knowledge"
                    ],
                    **mode,
                },
                # Bounded correlation facts only; LangSmith reads them without
                # copying state, prompt, or user content.
                "metadata": correlation_metadata(
                    {"configurable": {"thread_id": thread_id}}
                ),
            }
            config["metadata"]["web_research_mode"] = request.mode

            initial_state = {
                "messages": [HumanMessage(content=request.topic)],
            }

            # Stream graph execution with node-level updates
            async for chunk in _deep_researcher_factory(config).astream(
                initial_state, config, stream_mode="updates", subgraphs=True
            ):
                namespace, updates = chunk if isinstance(chunk, tuple) else ((), chunk)
                chunk = updates
                for node_name, node_output in chunk.items():
                    if isinstance(node_output, dict):
                        # Capture original role outputs as they finish inside
                        # the research subgraph, before a later role can fail.
                        if (
                            node_name.removesuffix("_agent")
                            in DEFAULT_PUBLIC_OPINION_AGENTS
                            or not stage_reports
                        ):
                            stages = stage_reports_from_state(
                                node_output,
                                stage_id=f"{namespace}:{node_name}",
                                completed_at=utc_timestamp(),
                            )
                            known = {
                                (stage.role, stage.content) for stage in stage_reports
                            }
                            stage_reports.extend(
                                stage
                                for stage in stages
                                if (stage.role, stage.content) not in known
                            )
                    if namespace:
                        # Nested state is retained for history, not broadcast
                        # as extra copies of the regular frontend stream.
                        continue
                    # ── Status update ──────────────────────────
                    label = _NODE_LABEL.get(node_name)
                    if label:
                        yield _event({"type": "status", "message": label})

                    # ── Content streaming ─────────────────────
                    content = _extract_text(node_output)
                    if content and node_name not in (
                        "enrich_query_images",
                        "identify_skill",
                        "load_skill",
                    ):
                        yield _event(
                            {
                                "type": "stream",
                                "node": node_name,
                                "content": content,
                            }
                        )

                    # ── Capture final report ──────────────────
                    if isinstance(node_output, dict):
                        report_update = node_output.get("report", {}) or {}
                        runtime_update = node_output.get("runtime", {}) or {}
                        if isinstance(report_update, dict) and report_update.get(
                            "final"
                        ):
                            final_report = report_update["final"]
                        if isinstance(runtime_update, dict) and runtime_update.get(
                            "budget"
                        ):
                            budget = runtime_update["budget"]

            # ── Final result ────────────────────────────────────────
            usage = {
                "model_calls": budget.get("model_calls", 0),
                "input_tokens": budget.get("input_tokens", 0),
                "output_tokens": budget.get("output_tokens", 0),
                "total_tokens": budget.get("total_tokens", 0),
            }

            if final_report:
                record = await archive_run()
                if record is None:
                    yield _event(
                        {
                            "type": "warning",
                            "message": "报告已生成，但历史记录保存失败。请下载报告，并检查 Neo4j 服务。",
                        }
                    )
                yield _event(
                    {
                        "type": "report",
                        "content": final_report,
                    }
                )
                yield _event(
                    {
                        "type": "usage",
                        **usage,
                    }
                )
                if record is not None:
                    yield saved_event(record)
            else:
                record = await archive_run(
                    "incomplete", "研究结束但尚未生成最终报告，已保留阶段输出。"
                )
                yield _event(
                    {
                        "type": "error",
                        "message": "Research completed but no report was generated.",
                    }
                )
                if record is not None:
                    yield saved_event(record)

            yield _event({"type": "done"})

        except asyncio.CancelledError:
            record = await archive_run(
                "cancelled", "研究已停止，已保留停止前完成的阶段输出。"
            )
            yield _event({"type": "error", "message": "Research cancelled."})
            if record is not None:
                yield saved_event(record)
        except Exception:
            LOGGER.exception("Research request failed; trace_id=%s", trace_id)
            record = await archive_run(
                "failed", "研究运行失败，已保留原始主题、参数及已完成的阶段内容。"
            )
            yield _event(
                {
                    "type": "error",
                    "message": f"Research request failed. Trace ID: {trace_id}.",
                }
            )
            if record is not None:
                yield saved_event(record)

    # 包装事件生成器，统一处理流式响应的异常及清理。
    async def event_stream():
        async with _RESEARCH_SEMAPHORE:
            async for event in _research_event_stream():
                yield event

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
