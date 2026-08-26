"""FastAPI web server for Open Deep Research.

Provides a clean web frontend and SSE streaming API for the deep research agent.
Launch with: uv run python -m open_deep_research.web.server
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env before anything else — API keys live there
load_dotenv()

from fastapi import FastAPI, Request  # noqa: E402, I001
from fastapi.responses import HTMLResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from open_deep_research.deep_researcher import deep_researcher as _deep_researcher_factory  # noqa: E402

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="Public Opinion Research",
    description="Enterprise public-opinion and brand-risk monitoring system",
    version="0.2.0",
)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class ResearchRequest(BaseModel):
    topic: str
    model: str = "deepseek:deepseek-chat"
    search_api: str = "tavily"
    mode: str = "normal"
    org_context: str = ""
    rag_enabled: bool = False


def _event(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── Node → human label ─────────────────────────────────────────────
_NODE_LABEL = {
    "write_research_brief": "Planning research…",
    "research_supervisor": "Analyzing public opinion…",
    "final_report_generation": "Writing report…",
    "compress_research": "Summarizing…",
}


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


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    index_path = STATIC_DIR / "index.html"
    if index_path.is_file():
        return index_path.read_text(encoding="utf-8")
    return "<h1>Open Deep Research</h1><p>Frontend not found.</p>"


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/research")
async def research(request: ResearchRequest, raw: Request) -> StreamingResponse:
    """Run deep research with real-time streaming via SSE."""

    async def event_stream():
        try:
            yield _event({"type": "status", "message": "Starting research…"})

            mode_configs = {
                "fast": {
                    "max_researcher_iterations": 1,
                    "max_react_tool_calls": 2,
                    "max_concurrent_research_units": 1,
                    "max_content_length": 8000,
                },
                "normal": {
                    "max_researcher_iterations": 3,
                    "max_react_tool_calls": 4,
                    "max_concurrent_research_units": 2,
                    "max_content_length": 20000,
                },
                "deep": {},
            }
            mode = mode_configs.get(request.mode, mode_configs["normal"])

            config = {
                "configurable": {
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
                    **mode,
                }
            }

            initial_state = {
                "messages": [HumanMessage(content=request.topic)],
            }

            final_report = None
            budget = {}

            # Stream graph execution with node-level updates
            async for chunk in _deep_researcher_factory(config).astream(
                initial_state, config, stream_mode="updates"
            ):
                for node_name, node_output in chunk.items():
                    # ── Status update ──────────────────────────
                    label = _NODE_LABEL.get(node_name)
                    if label:
                        yield _event({"type": "status", "message": label})

                    # ── Content streaming ─────────────────────
                    content = _extract_text(node_output)
                    if content and node_name not in ("enrich_query_images", "identify_skill", "load_skill"):
                        yield _event({
                            "type": "stream",
                            "node": node_name,
                            "content": content,
                        })

                    # ── Capture final report ──────────────────
                    if isinstance(node_output, dict):
                        if "final_report" in node_output:
                            final_report = node_output["final_report"]
                        if "budget_usage" in node_output:
                            budget = node_output["budget_usage"]

            # ── Final result ────────────────────────────────────────
            usage = {
                "model_calls": budget.get("model_calls", 0),
                "input_tokens": budget.get("input_tokens", 0),
                "output_tokens": budget.get("output_tokens", 0),
                "total_tokens": budget.get("total_tokens", 0),
            }

            if final_report:
                yield _event({
                    "type": "report",
                    "content": final_report,
                })
                yield _event({
                    "type": "usage",
                    **usage,
                })
            else:
                yield _event({
                    "type": "error",
                    "message": "Research completed but no report was generated.",
                })

            yield _event({"type": "done"})

        except asyncio.CancelledError:
            yield _event({"type": "error", "message": "Research cancelled."})
        except Exception as exc:
            yield _event({
                "type": "error",
                "message": f"Research failed: {exc}",
            })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def main():
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))

    print("\n  Open Deep Research web UI")
    print("  ─────────────────────────")
    print(f"  http://{host}:{port}\n")

    uvicorn.run(
        "open_deep_research.web.server:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
