"""Authenticated read and maintenance entry points for the configured RAG pipeline."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from time import perf_counter, time
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints
from typing_extensions import Annotated

from open_deep_research.rag.loaders.knowledge import _iter_supported_paths
from open_deep_research.rag.mcp_server import _cached_pipeline
from open_deep_research.rag.service import (
    build_rag_index_id,
    build_rag_pipeline_config,
    get_or_create_rag_pipeline,
)
from open_deep_research.tools.rag_tool import RAGRetrievalError, search_rag_context
from open_deep_research.web.contracts import OffsetPage
from open_deep_research.web.run_data import RunConflict, public_payload, timestamp
from open_deep_research.web.runs import (
    IDEMPOTENT,
    _call,
    _key,
    _store,
    bearer,
    trusted_owner,
    web_configuration,
)

router = APIRouter(prefix="/api/knowledge", dependencies=[bearer])


class IndexJob(BaseModel):
    id: str
    status: Literal["running", "completed", "failed"]
    force: bool
    started_at: str
    finished_at: str | None = None
    error: str | None = None


class KnowledgeStatus(BaseModel):
    enabled: bool
    configured: bool
    retrieval_mode: str
    vector_store: str
    keyword_backend: str
    embedding_provider: str
    embedding_model: str
    reranker_provider: str
    reranker_model: str
    multimodal_enabled: bool
    multimodal_provider: str
    vision_enabled: bool
    vision_model: str
    query_rewrite_enabled: bool
    index_state: Literal["unknown", "ready", "empty", "building"]
    chunk_count: int | None
    document_count: int | None
    job: IndexJob | None


class Source(BaseModel):
    id: str
    name: str
    document_type: str
    category: str
    size_bytes: int
    modified_at: str
    indexed: bool | None


class SourcePage(OffsetPage[Source]):
    total: int


class BuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    force: bool = False


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]


class SearchMatch(BaseModel):
    rank: int
    chunk_id: str
    source: str
    title: str | None
    content: str
    score: float
    vector_score: float | None
    keyword_score: float | None
    structured_score: float | None
    rerank_score: float | None
    metadata: dict[str, JsonValue]


class SearchResult(BaseModel):
    query: str
    elapsed_ms: int
    matches: list[SearchMatch] = Field(default_factory=list)


# Job receipts are local to this Web process. PostgreSQL fences maintenance
# against other Web processes and work admitted through RunClient.
_jobs: OrderedDict[str, tuple[str, IndexJob]] = OrderedDict()
_jobs_lock = Lock()


def index_state(index_id):
    with _jobs_lock:
        recent = next(
            (job for key, job in reversed(_jobs.values()) if key == index_id), None
        )
    pipeline = _cached_pipeline(index_id)
    return (
        recent,
        pipeline.indexer if pipeline is not None and pipeline.indexer._ready else None,
    )


@router.get("", response_model=KnowledgeStatus)
def knowledge_status(raw: Request, response: Response) -> dict:
    trusted_owner(raw)
    config = web_configuration()
    pipeline = build_rag_pipeline_config(config)
    job, indexer = index_state(build_rag_index_id(pipeline))
    state = "unknown"
    if job and job.status == "running":
        state = "building"
        indexer = None
    elif indexer is not None and not (job and job.status == "failed"):
        state = "ready" if indexer.chunks else "empty"
    else:
        indexer = None
    response.headers["Cache-Control"] = "no-store"
    return public_payload(
        dict(
            enabled=config.rag_enabled,
            configured=bool(pipeline.knowledge_base_paths),
            retrieval_mode=config.retrieval_mode.value,
            vector_store=pipeline.vectorstore_provider,
            keyword_backend=pipeline.keyword_backend,
            embedding_provider=pipeline.embedding_provider,
            embedding_model=pipeline.embedding_model,
            reranker_provider=pipeline.reranker_provider,
            reranker_model=pipeline.reranker_model,
            multimodal_enabled=pipeline.multimodal_enabled,
            multimodal_provider=pipeline.multimodal_provider,
            vision_enabled=pipeline.vision_enabled,
            vision_model=pipeline.vision_model,
            query_rewrite_enabled=config.rag_query_rewrite_enabled,
            index_state=state,
            chunk_count=len(indexer.chunks) if indexer is not None else None,
            document_count=len(indexer.documents) if indexer is not None else None,
            job=job.model_dump() if job else None,
        )
    )


@router.get("/sources", response_model=SourcePage)
def knowledge_sources(
    raw: Request,
    response: Response,
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> dict:
    """Catalog configured files without parsing, indexing or calling Vision."""
    from open_deep_research.web.run_data import digest

    trusted_owner(raw)
    pipeline = build_rag_pipeline_config(web_configuration())
    job, indexer = index_state(build_rag_index_id(pipeline))
    indexed = (
        {doc.source for doc in indexer.documents}
        if indexer is not None and (job is None or job.status == "completed")
        else None
    )
    files = {}
    needle = q.strip().casefold()
    for configured in pipeline.knowledge_base_paths:
        root = Path(configured)
        category_root = root.resolve() if root.is_dir() else root.parent.resolve()
        for path in _iter_supported_paths(
            root, include_multimodal=pipeline.multimodal_enabled
        ):
            category = (
                path.parent.name
                if path.is_relative_to(category_root)
                else category_root.name
            )
            if needle in path.name.casefold() or needle in category.casefold():
                files.setdefault(path, category)
    page = sorted(files, key=lambda path: (path.name.casefold(), path.as_posix()))
    items = []
    for path in page[offset : offset + limit]:
        try:
            stat = path.stat()
        except OSError:
            continue  # A configured file was removed during the directory scan.
        items.append(
            dict(
                id=digest(path.as_posix()),
                name=path.name,
                document_type=path.suffix.lstrip(".").lower(),
                category=files[path],
                size_bytes=stat.st_size,
                modified_at=timestamp(stat.st_mtime),
                indexed=None if indexed is None else path.as_posix() in indexed,
            )
        )
    response.headers["Cache-Control"] = "no-store"
    return public_payload(
        dict(
            items=items,
            total=len(page),
            next_offset=offset + min(limit, max(0, len(page) - offset)),
            has_more=offset + limit < len(page),
        )
    )


def require_configured():
    config = web_configuration()
    if not config.rag_enabled or not config.rag_knowledge_base_paths:
        raise HTTPException(
            409, "The configured knowledge base is disabled or has no sources."
        )
    return config


@contextmanager
def idle_index():
    store = _store()
    with store.engine.begin() as db:
        store.check_index_maintenance(db)
        yield


def build_index(key, config, job):
    failure = None
    try:
        with idle_index():
            get_or_create_rag_pipeline(config).indexer.ensure_ready(force=job.force)
    except RunConflict as exc:
        failure = str(exc)
    except Exception:
        failure = "Indexing failed. Check the configured vector, embedding and document backends."
    with _jobs_lock:
        index_id, _ = _jobs[key]
        _jobs[key] = (
            index_id,
            job.model_copy(
                update=dict(
                    status="failed" if failure else "completed",
                    finished_at=timestamp(time()),
                    error=failure,
                )
            ),
        )


@router.post(
    "/index", response_model=IndexJob, status_code=202, openapi_extra=IDEMPOTENT
)
def start_index(
    body: BuildRequest, raw: Request, response: Response, background: BackgroundTasks
):
    trusted_owner(raw)
    key = _key(raw)
    config = build_rag_pipeline_config(require_configured())
    index_id = build_rag_index_id(config)
    response.headers["Cache-Control"] = "no-store"
    with _jobs_lock:
        if receipt := _jobs.get(key):
            configured_id, job = receipt
            if index_id != configured_id or body.force != job.force:
                raise HTTPException(
                    409, "Index request key was used for another operation."
                )
            return job
        if any(job.status == "running" for _, job in _jobs.values()):
            raise HTTPException(409, "An index job is already running.")
        job = IndexJob(
            id=uuid4().hex,
            status="running",
            force=body.force,
            started_at=timestamp(time()),
        )
        _jobs[key] = (index_id, job)
        if len(_jobs) > 32:
            _jobs.popitem(last=False)
    background.add_task(build_index, key, config, job)
    return job


def search_idle(query, config):
    with idle_index():
        return asyncio.run(
            search_rag_context(query, {"configurable": config.model_dump(mode="json")})
        )


@router.post("/search", response_model=SearchResult)
def search(body: SearchRequest, raw: Request, response: Response) -> dict:
    trusted_owner(raw)
    config = require_configured()
    with _jobs_lock:
        if any(job.status == "running" for _, job in _jobs.values()):
            raise HTTPException(409, "An index job is already running.")
    started = perf_counter()
    try:
        result = _call(search_idle, body.query, config)
    except RAGRetrievalError:
        raise HTTPException(
            503, "Retrieval failed. Check the knowledge index and configured backends."
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return public_payload(
        dict(
            query=result.query,
            elapsed_ms=round((perf_counter() - started) * 1000),
            matches=[
                dict(
                    rank=rank,
                    chunk_id=match.chunk.chunk_id,
                    source=Path(match.chunk.source).name,
                    title=match.chunk.title,
                    content=match.chunk.content,
                    score=match.score,
                    vector_score=match.vector_score,
                    keyword_score=match.keyword_score,
                    structured_score=match.structured_score,
                    rerank_score=match.rerank_score,
                    metadata={
                        key: value
                        for key, value in match.chunk.metadata.items()
                        if key
                        in {
                            "page",
                            "page_number",
                            "chunk_index",
                            "extension",
                            "language",
                            "code_language",
                            "title_hierarchy",
                            "authority_level",
                            "authority_score",
                            "source_type",
                            "content_kind",
                            "document_type",
                            "section",
                            "row_index",
                            "json_index",
                        }
                    },
                )
                for rank, match in enumerate(result.matched_chunks, start=1)
            ],
        )
    )
