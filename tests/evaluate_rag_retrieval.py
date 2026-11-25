"""Evaluate the POR benchmark through the production RAG pipeline or tool."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import sys
from collections import defaultdict
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from typing import Any

import yaml
from pydantic import ValidationError

from open_deep_research.budget import budget_capture
from open_deep_research.config_file import (
    SENSITIVE_CONFIG_FIELDS,
    frozen_config,
    load_configuration,
)
from open_deep_research.configuration import Configuration
from open_deep_research.env import load_environment
from open_deep_research.rag.service import (
    RAGPipeline,
    apply_authority_adjustment,
    build_rag_pipeline_config,
    get_or_create_rag_pipeline,
)
from open_deep_research.tools.rag_tool import search_rag_context

if not __package__:  # Allow both the established file entry point and python -m.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.rag_eval_evidence import (  # noqa: E402
    DocumentCatalog,
    latency_summary,
    read_jsonl,
    score_question,
    summarize,
)
from tests.rag_eval_records import (  # noqa: E402
    BENCHMARK,
    CORPUS,
    FROZEN_DATASET_SHA256,
    HISTORY,
    MANIFEST,
    ROOT,
    RUNS,
    build_fingerprint,
    compare_records,
    dataset_fingerprint,
    find_reusable_record,
    git_commit,
    load_record,
    public_rag_config,
    save_record,
    start_record,
    update_history,
    utc_now,
)

# The pre-POR filename scorer is available only for historical --cases files.
DEFAULT_METRICS = (
    "hit@{k}",
    "recall@{k}",
    "precision@{k}",
    "mrr@{k}",
    "evidence_hit@{k}",
    "evidence_recall@{k}",
    "evidence_precision@{k}",
    "misleading_rate@{k}",
    "authoritative_rate@{k}",
)


# 输出不含终端颜色或控制字符的文本行。
def emit(line: str = "") -> None:
    """Write one plain text line without terminal color/control sequences."""
    sys.stdout.write(f"{line}\n")


# 配置纯文本输出，使 Windows 终端和日志捕获保持可读。
def configure_plain_output() -> None:
    """Keep CLI output readable in Windows terminals and log captures."""
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")


# 从引用来源提取可比较的文件名。
def normalize_source(source: str) -> str:
    """Return the comparable source file name from a citation source."""
    normalized = source.replace("\\", "/")
    return normalized.rsplit("/", 1)[-1].split("#", 1)[0]


# 计算单个可回答案例的来源级召回指标。
def score_retrieval(
    expected_sources: list[str],
    retrieved_sources: list[str],
    k: int,
) -> dict[str, float] | None:
    """Compute retrieval metrics for a single answerable case."""
    if not expected_sources:
        return None

    top_sources = retrieved_sources[:k]
    expected = set(expected_sources)
    matched_positions = [
        index
        for index, source in enumerate(top_sources, start=1)
        if source in expected
    ]
    unique_matches = {source for source in top_sources if source in expected}

    return {
        f"hit@{k}": 1.0 if matched_positions else 0.0,
        f"recall@{k}": len(unique_matches) / len(expected),
        f"precision@{k}": len(matched_positions) / max(1, len(top_sources)),
        f"mrr@{k}": 1.0 / matched_positions[0] if matched_positions else 0.0,
    }


# 计算单案例分块或证据级检索指标。
def score_evidence_retrieval(
    expected_sources: list[str],
    expected_keywords: list[str],
    retrieved_details: list[dict[str, Any]],
    k: int,
) -> dict[str, float] | None:
    """Compute chunk/evidence-level retrieval metrics for one case."""
    if not expected_sources:
        return None

    top_details = retrieved_details[:k]
    expected = set(expected_sources)
    normalized_keywords = [
        keyword.lower().strip()
        for keyword in expected_keywords
        if str(keyword).strip()
    ]
    evidence_chunks = [
        detail
        for detail in top_details
        if detail["source"] in expected
        and _detail_matches_any_keyword(detail, normalized_keywords)
    ]
    matched_keywords = {
        keyword
        for keyword in normalized_keywords
        if any(
            detail["source"] in expected
            and keyword in str(detail.get("page_content", "")).lower()
            for detail in top_details
        )
    }
    denominator = max(1, len(top_details))

    return {
        f"evidence_hit@{k}": 1.0 if evidence_chunks else 0.0,
        f"evidence_recall@{k}": (
            len(matched_keywords) / len(normalized_keywords)
            if normalized_keywords
            else 1.0 if evidence_chunks else 0.0
        ),
        f"evidence_precision@{k}": len(evidence_chunks) / denominator,
        f"misleading_rate@{k}": _source_status_rate(top_details, denominator, misleading=True),
        f"authoritative_rate@{k}": _source_status_rate(
            top_details,
            denominator,
            misleading=False,
        ),
    }


# 判断候选详情是否命中任一目标证据关键词。
def _detail_matches_any_keyword(
    detail: dict[str, Any],
    normalized_keywords: list[str],
) -> bool:
    if not normalized_keywords:
        return True
    content = str(detail.get("page_content", "")).lower()
    return any(keyword in content for keyword in normalized_keywords)


# 统计召回结果中指定来源状态的占比。
def _source_status_rate(
    details: list[dict[str, Any]],
    denominator: int,
    *,
    misleading: bool,
) -> float:
    misleading_statuses = {"deprecated", "misleading", "unanswerable_trap"}
    count = 0
    for detail in details:
        metadata = detail.get("metadata", {}) or {}
        status = str(metadata.get("source_status", "")).lower().strip()
        if not status and detail.get("source") == "misleading_archive.md":
            status = "misleading"
        if not status:
            status = "authoritative"
        if misleading and status in misleading_statuses:
            count += 1
        if not misleading and status == "authoritative":
            count += 1
    return count / denominator


# 对评分字典中的指定有效指标求平均。
def average_metric(scores: list[dict[str, Any]], metric_key: str) -> float:
    """Average one metric over score dictionaries."""
    values = [float(score[metric_key]) for score in scores]
    return statistics.mean(values) if values else 0.0


# 按评估类别聚合检索指标。
def summarize_by_category(
    scores: list[dict[str, Any]],
    metric_keys: list[str],
) -> dict[str, dict[str, float | int]]:
    """Aggregate metrics by evaluation category."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for score in scores:
        grouped[str(score["category"])].append(score)

    return {
        category: {
            "count": len(items),
            **{metric: average_metric(items, metric) for metric in metric_keys},
        }
        for category, items in sorted(grouped.items())
    }


# 读取 JSONL 检索评估案例。
def load_cases(path: Path) -> list[dict[str, Any]]:
    """Read JSONL cases for the POR and historical entry points."""
    return read_jsonl(path)


# 生成适合终端诊断的单行文本预览。
def compact_text(text: str, limit: int = 300) -> str:
    """Return a one-line text preview suitable for terminal diagnostics."""
    return " ".join(text.split())[:limit]


# 格式化可选得分，保留缺失状态并控制精度。
def format_optional_score(value: float | None) -> str:
    """Format optional retrieval scores without adding noisy precision."""
    if value is None:
        return "n/a"
    return f"{float(value):.4f}"


# 将检索结果对象转换为精简调试字典。
def serialize_retrieval_detail(result: Any, rank: int) -> dict[str, Any]:
    """Convert a RetrievalResult-like object to a compact debug dictionary."""
    chunk = result.chunk
    metadata = getattr(chunk, "metadata", {}) or {}
    return {
        "rank": rank,
        "source": normalize_source(getattr(chunk, "source", "")),
        "chunk_id": getattr(chunk, "chunk_id", None) or metadata.get("doc_id") or "n/a",
        "score": getattr(result, "score", None),
        "vector_score": getattr(result, "vector_score", None),
        "keyword_score": getattr(result, "keyword_score", None),
        "structured_score": getattr(result, "structured_score", None),
        "rerank_score": getattr(result, "rerank_score", None),
        "page_content": compact_text(getattr(chunk, "content", "")),
        "metadata": metadata,
    }


# 收集重排候选详情，供漏召分析使用。
def collect_retrieval_details(
    pipeline: RAGPipeline,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Collect reranked retrieval details for miss diagnostics."""
    pipeline.indexer.ensure_ready()

    if not pipeline.indexer.documents or not pipeline.indexer.chunks:
        return []
    if pipeline.indexer.retriever is None:
        return []

    query_vector = pipeline.indexer.embedding_backend.embed_query(query)
    retrieval_results = pipeline.indexer.retriever.retrieve(
        query=query,
        query_vector=query_vector,
        top_k=limit,
        keyword_top_k=max(limit, pipeline.config.keyword_top_k),
    )
    reranked_results = pipeline.reranker.rerank(
        query=query,
        results=retrieval_results,
        top_k=limit,
    )
    if getattr(pipeline.config, "authority_rerank_enabled", True):
        reranked_results = apply_authority_adjustment(reranked_results)
    filtered_results = pipeline._filter_results(reranked_results)  # noqa: SLF001
    return [
        serialize_retrieval_detail(result, rank)
        for rank, result in enumerate(filtered_results[:limit], start=1)
    ]


# 为未命中案例追加候选分块等诊断信息。
def add_miss_diagnostics(
    record: dict[str, Any],
    pipeline: RAGPipeline,
    query: str,
    k: int,
) -> None:
    """Attach extra retrieval diagnostics to one missed scored record."""
    diagnostic_limit = max(k, int(getattr(pipeline.config, "rerank_top_n", k)))
    details = collect_retrieval_details(
        pipeline=pipeline,
        query=query,
        limit=diagnostic_limit,
    )
    expected = set(record["expected"])
    expected_ranks = [
        int(detail["rank"])
        for detail in details
        if detail["source"] in expected
    ]

    record["retrieved_details"] = details
    record["expected_not_retrieved"] = not expected_ranks
    record["expected_retrieved_but_low_rank"] = any(rank > k for rank in expected_ranks)


# 拒绝用非语义嵌入后端产生正式检索质量指标。
def validate_metric_embedding_provider(provider: str) -> None:
    """Reject non-semantic embedding backends for retrieval quality metrics."""
    if provider.lower().strip() == "hash":
        raise ValueError(
            "hash embeddings are not allowed for retrieval metric evaluation; "
            "use the real sentence_transformers stack."
        )


# 创建只评估检索效果的本地 RAG 管线。
def build_pipeline(args: argparse.Namespace) -> RAGPipeline:
    """Build the historical evaluation pipeline using the shared project settings."""
    configuration = resolve_eval_config(args)
    validate_metric_embedding_provider(configuration.rag_embedding_provider)
    return RAGPipeline(build_rag_pipeline_config(configuration))


# 在评估前展示本次实际使用的检索技术栈。
def print_run_config(args: argparse.Namespace) -> None:
    """Print the effective retrieval stack before running evaluation."""
    emit("Config")
    emit(f"knowledge_base: {args.knowledge_base}")
    emit(f"embedding_provider: {args.embedding_provider}")
    emit(f"embedding_model: {args.embedding_model}")
    emit(f"embedding_device: {args.embedding_device or 'auto'}")
    emit(f"vectorstore_provider: {args.vectorstore_provider}")
    emit(f"vectorstore_path: {args.vectorstore_path}")
    emit(f"milvus_uri: {args.milvus_uri or 'auto'}")
    emit(f"milvus_metric_type: {args.milvus_metric_type}")
    emit(f"reranker_provider: {args.reranker_provider}")
    emit(f"reranker_model: {args.reranker_model}")
    emit(f"reranker_device: {args.reranker_device or 'auto'}")
    emit("fusion_method: rrf")
    emit(f"rrf_rank_constant: {getattr(args, 'rrf_rank_constant', 60)}")
    emit(
        "structured_metadata_weight: "
        f"{getattr(args, 'structured_metadata_weight', 0.15)}"
    )
    emit(
        "authority_rerank_enabled: "
        f"{not getattr(args, 'disable_authority_rerank', False)}"
    )
    emit(f"top_k: {args.top_k}")
    emit(f"rerank_top_n: {args.rerank_top_n}")
    emit()


# 逐案例执行检索，返回已评分及被跳过的案例。
def evaluate_cases(
    cases: list[dict[str, Any]],
    pipeline: RAGPipeline,
    k: int,
    time_fn: Callable[[], float] = perf_counter,
    include_miss_details: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run RAG retrieval for each case and return scored and skipped cases."""
    scores = []
    skipped = []
    miss_key = f"hit@{k}"

    for case in cases:
        started_at = time_fn()
        result = pipeline.query(case["query"])
        latency_ms = round((time_fn() - started_at) * 1000, 4)
        retrieved_sources = [normalize_source(citation.source) for citation in result.citations]
        score = score_retrieval(
            expected_sources=case["expected_sources"],
            retrieved_sources=retrieved_sources,
            k=k,
        )
        matched_chunks = getattr(result, "matched_chunks", [])
        retrieved_details = [
            serialize_retrieval_detail(matched_result, rank)
            for rank, matched_result in enumerate(matched_chunks[:k], start=1)
        ]
        evidence_score = score_evidence_retrieval(
            expected_sources=case["expected_sources"],
            expected_keywords=case.get("expected_keywords", []),
            retrieved_details=retrieved_details,
            k=k,
        )
        if score is None:
            skipped.append(
                {
                    "id": case["id"],
                    "category": case["category"],
                    "answer_type": case["answer_type"],
                    "avg_latency_ms": latency_ms,
                    "retrieved": retrieved_sources,
                }
            )
            continue

        record = {
            "id": case["id"],
            "category": case["category"],
            "query": case["query"],
            "expected": case["expected_sources"],
            "retrieved": retrieved_sources,
            "avg_latency_ms": latency_ms,
            **score,
            **(evidence_score or {}),
        }
        if include_miss_details and record[miss_key] == 0.0:
            add_miss_diagnostics(record, pipeline, case["query"], k)
        scores.append(record)

    return scores, skipped


# 读取已就绪索引的文档、分块和向量数量。
def collect_index_stats(pipeline: RAGPipeline) -> dict[str, int]:
    """Return document, chunk, and vector counts from the ready RAG index."""
    indexer = pipeline.indexer
    return {
        "num_documents": len(indexer.documents),
        "num_chunks": len(indexer.chunks),
        "num_vectors": int(getattr(indexer, "last_vector_count", len(indexer.chunks))),
    }


# 输出精简检索质量报告。
def print_report(
    scores: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    k: int,
    show_misses: bool,
    index_stats: dict[str, int] | None = None,
) -> None:
    """Print a compact text report."""
    metric_keys = [metric.format(k=k) for metric in DEFAULT_METRICS]
    latency_key = "avg_latency_ms"
    all_results = [*scores, *skipped]

    emit("Overall")
    emit(f"cases_scored: {len(scores)}")
    emit(f"cases_skipped_unanswerable: {len(skipped)}")
    if index_stats:
        emit(f"num_documents: {index_stats['num_documents']}")
        emit(f"num_chunks: {index_stats['num_chunks']}")
        emit(f"num_vectors: {index_stats['num_vectors']}")
    emit(f"{latency_key}: {round(average_metric(all_results, latency_key), 4)}")
    for metric in metric_keys:
        emit(f"{metric}: {round(average_metric(scores, metric), 4)}")

    emit()
    emit("By category")
    retrieval_summary = summarize_by_category(scores, metric_keys)
    latency_summary = summarize_by_category(all_results, [latency_key])
    for category in sorted(latency_summary):
        summary = {
            "count": latency_summary[category]["count"],
            **retrieval_summary.get(
                category,
                {metric: "n/a" for metric in metric_keys},
            ),
            latency_key: latency_summary[category][latency_key],
        }
        printable = {
            key: round(value, 4) if isinstance(value, float) else value
            for key, value in summary.items()
        }
        fields = " ".join(f"{key}={value}" for key, value in printable.items())
        emit(f"{category}: {fields}")

    if show_misses:
        emit()
        emit("Misses")
        miss_key = f"hit@{k}"
        misses = [score for score in scores if score[miss_key] == 0.0]
        if not misses:
            emit("none")
        for score in misses:
            emit(
                f"{score['id']} expected={score['expected']} "
                f"retrieved={score['retrieved']}"
            )
            print_miss_diagnostics(score)


# 展示单个漏召案例的分块级诊断。
def print_miss_diagnostics(score: dict[str, Any]) -> None:
    """Print chunk-level debug details for one missed case."""
    details = score.get("retrieved_details", [])
    expected = set(score["expected"])
    expected_ranks = [
        int(detail["rank"])
        for detail in details
        if detail["source"] in expected
    ]
    expected_not_retrieved = score.get("expected_not_retrieved", not expected_ranks)
    expected_retrieved_but_low_rank = score.get(
        "expected_retrieved_but_low_rank",
        bool(expected_ranks),
    )
    rank_list = " | ".join(
        f"{detail['rank']}:{detail['source']}#{detail['chunk_id']}"
        for detail in details
    )

    emit(f"  case_id: {score['id']}")
    emit(f"  category: {score['category']}")
    emit(f"  query: {score.get('query', '')}")
    emit(f"  expected_sources: {score['expected']}")
    emit(f"  expected_not_retrieved={expected_not_retrieved}")
    emit(f"  expected_retrieved_but_low_rank={expected_retrieved_but_low_rank}")
    emit(f"  retrieved_rank_list: {rank_list or 'n/a'}")
    for detail in details:
        metadata = json.dumps(
            detail.get("metadata", {}),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        emit(
            "  - "
            f"rank={detail['rank']} "
            f"source={detail['source']} "
            f"chunk_id={detail['chunk_id']} "
            f"score={format_optional_score(detail.get('score'))} "
            f"vector_score={format_optional_score(detail.get('vector_score'))} "
            f"keyword_score={format_optional_score(detail.get('keyword_score'))} "
            f"structured_score={format_optional_score(detail.get('structured_score'))} "
            f"rerank_score={format_optional_score(detail.get('rerank_score'))}"
        )
        emit(f"    page_content: {detail.get('page_content', '')}")
        emit(f"    metadata: {metadata}")

LEGACY_OVERRIDES = {
    "knowledge_base": "rag_knowledge_base_paths",
    **{
        name: f"rag_{name}" for name in (
            "top_k", "chunk_size", "chunk_overlap", "rerank_top_n", "embedding_provider",
            "embedding_model", "embedding_device", "vectorstore_provider", "vectorstore_path",
            "milvus_uri", "milvus_metric_type", "reranker_provider", "reranker_model",
            "reranker_device", "rrf_rank_constant", "structured_metadata_weight",
        )
    },
}


def resolve_eval_config(args: argparse.Namespace) -> Configuration:
    """Use the same validated YAML and RunnableConfig as the RAG tool."""
    config_path = Path(getattr(args, "config", None) or ROOT / "config.yaml").resolve()
    base = load_configuration(config_path)
    overrides = {
        field: value for name, field in LEGACY_OVERRIDES.items()
        if (value := getattr(args, name, None)) is not None
    }
    if getattr(args, "disable_authority_rerank", False):
        overrides["rag_authority_rerank_enabled"] = False
    for entry in getattr(args, "overrides", []):
        field, separator, raw = entry.partition("=")
        if (
            not separator or not field.startswith("rag_")
            or field not in Configuration.model_fields or field in SENSITIVE_CONFIG_FIELDS
        ):
            raise ValueError("--set expects a supported, non-credential rag_FIELD=YAML override")
        try:
            overrides[field] = yaml.safe_load(raw)
        except yaml.YAMLError:
            raise ValueError(f"Invalid YAML value for {field}") from None
    for field in ("rag_knowledge_base_paths", "rag_vectorstore_path", "rag_milvus_uri"):
        value = overrides.get(field)
        if isinstance(value, list) and all(isinstance(path, str) for path in value):
            overrides[field] = [str(Path(path).expanduser().resolve()) for path in value]
        elif isinstance(value, str) and "://" not in value:
            overrides[field] = str(Path(value).expanduser().resolve())
    try:
        configuration = Configuration(**{**base.model_dump(), **overrides})
    except ValidationError as exc:
        fields = ", ".join(str(error["loc"][0]) for error in exc.errors() if error["loc"])
        raise ValueError(f"Invalid RAG overrides for {fields or 'configuration'}") from None
    return Configuration.from_runnable_config(frozen_config(configuration))


def _isolated_config(configuration: Configuration, run_id: str) -> Configuration:
    """Give every execution fresh local storage and remote namespaces."""
    namespace = f"por_eval_{run_id.rsplit('-', 1)[-1]}"
    directory = ROOT / ".tmp/rag-evaluation/indexes" / namespace
    uri = configuration.rag_milvus_uri
    return configuration.model_copy(update={
        "rag_vectorstore_path": str(directory),
        "rag_collection_name": namespace,
        "rag_milvus_uri": uri if uri and "://" in uri else str(directory / "milvus.db"),
        "rag_elasticsearch_index": namespace,
    })


def evaluation_inputs(args: argparse.Namespace) -> tuple[Configuration, list[dict], dict]:
    """Resolve cases, effective configuration and fingerprint for run, find or inspection."""
    configuration = resolve_eval_config(args)
    pipeline = build_rag_pipeline_config(configuration)
    if not configuration.rag_enabled or not pipeline.knowledge_base_paths:
        raise ValueError("RAG must be enabled and have knowledge paths for evaluation")
    if pipeline.top_k < 1 or pipeline.rerank_top_n < 1:
        raise ValueError("RAG Top K and Rerank Top N must be positive")
    if not args.smoke:
        validate_metric_embedding_provider(pipeline.embedding_provider)
    tool_options = {
        key: value for key, value in configuration.model_dump(mode="json").items()
        if key == "rag_enabled" or key.startswith("rag_query_")
    }
    cases = load_cases(ROOT / BENCHMARK / f"{args.split}.jsonl")
    ids = [case["question_id"] for case in cases]
    if len(set(ids)) != len(ids) or not ids:
        raise ValueError("The split must contain unique, nonempty question IDs")
    selection = "subset" if args.question_id or args.limit else "all"
    if args.question_id:
        requested = set(args.question_id)
        if not requested <= set(ids):
            raise ValueError("One or more question IDs are absent from the selected split")
        cases = [case for case in cases if case["question_id"] in requested]
    if args.limit:
        cases = cases[:args.limit]
    scope = {
        "mode": args.scope,
        "split": args.split,
        "purpose": "smoke" if args.smoke else "formal",
        "selection": selection,
        "question_ids": [case["question_id"] for case in cases],
        "top_k": pipeline.top_k,
        "timing_protocol": "fresh_index_include_first_query",
    }
    fingerprint = build_fingerprint(
        scope, pipeline.model_dump(), tool_options, frozen_config(configuration),
    )
    return configuration, cases, fingerprint


class _RAGWarnings(logging.Handler):
    """Collect only stable warning categories, never provider exception text."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.events: list[str] = []

    def emit(self, record):
        if record.name.endswith(".query_rewriter"):
            self.events.append("query_rewrite_fallback")
        else:
            self.events.append("knowledge_loader_warning")


@contextmanager
def _observe_rag_warnings():
    handler = _RAGWarnings()
    loggers = [
        logging.getLogger("open_deep_research.rag." + name)
        for name in ("loaders.knowledge", "query_rewriter")
    ]
    for logger in loggers:
        logger.addHandler(handler)
    try:
        yield handler.events
    finally:
        for logger in loggers:
            logger.removeHandler(handler)


def _usage(observed: dict) -> dict:
    fields = ("model_calls", "input_tokens", "output_tokens", "total_tokens")
    return {**{key: observed[key] for key in fields}, "cost": None}


async def _execute_cases(
    cases: list[dict], pipeline: RAGPipeline, runnable: dict, catalog: DocumentCatalog,
    record: dict, warnings: list[str],
) -> None:
    for case in cases:
        row = {
            key: case[key] for key in ("question_id", "question", "question_type", "difficulty", "answerable")
        }
        observed_warnings = len(warnings)
        started = perf_counter()
        try:
            with budget_capture() as observed:
                if record["scope"]["mode"] == "tool":
                    result = await search_rag_context(case["question"], config=runnable)
                else:
                    result = pipeline.query(case["question"])
            elapsed_ms = (perf_counter() - started) * 1000
            scored = score_question(case, result, catalog, record["scope"]["top_k"])
            backend_failure = scored["no_result_reason"] not in (None, "no_relevant_results")
            case_warnings = warnings[observed_warnings:]
            row.update(
                scored, status="failed" if case_warnings or backend_failure else "succeeded",
                latency_ms=elapsed_ms, model_usage=_usage(observed),
                warnings=case_warnings,
            )
        except Exception as exc:
            row.update(
                status="failed", latency_ms=(perf_counter() - started) * 1000,
                exception_type=type(exc).__name__, model_usage=_usage(observed),
            )
        record["questions"].append(row)


def _refresh_model_revisions(args: argparse.Namespace, record: dict, initial: dict) -> None:
    """Record revisions discovered on the first model download, checking all other inputs."""
    _, _, latest = evaluation_inputs(args)
    if latest["full_dataset_fingerprint"] != initial["full_dataset_fingerprint"]:
        raise ValueError("The corpus or a benchmark split changed during evaluation")
    if latest["evaluation_fingerprint"] == initial["evaluation_fingerprint"]:
        return
    old_models = initial["runtime"]["models"]
    new_models = latest["runtime"]["models"]
    stable_inputs = {
        key: value for key, value in initial["components"].items() if key not in ("rag", "environment")
    }
    if (
        stable_inputs != {key: latest["components"][key] for key in stable_inputs}
        or latest["rag_config"] != initial["rag_config"]
        or latest["tool_options"] != initial["tool_options"]
        or latest["source_digests"] != initial["source_digests"]
        or {k: v for k, v in initial["runtime"].items() if k != "models"}
        != {k: v for k, v in latest["runtime"].items() if k != "models"}
        or old_models.keys() != new_models.keys()
        or any(
            old != new_models[name]
            and (
                old.get("revision") is not None or not new_models[name].get("revision")
                or {k: v for k, v in old.items() if not k.startswith("revision")}
                != {k: v for k, v in new_models[name].items() if not k.startswith("revision")}
            )
            for name, old in old_models.items()
        )
    ):
        raise ValueError("Evaluation inputs changed during execution")
    record["evaluation_fingerprint_at_start"] = initial["evaluation_fingerprint"]
    record.update(latest)


def run_evaluation(args: argparse.Namespace) -> tuple[Path, dict, bool]:
    """Run a POR split once or reuse an intact formal result for the same inputs."""
    configuration, cases, fingerprint = evaluation_inputs(args)
    if not args.smoke and fingerprint["full_dataset_fingerprint"] != FROZEN_DATASET_SHA256:
        raise ValueError("The corpus, manifest, or benchmark differs from dataset-v1")
    if not args.force and (previous := find_reusable_record(args.runs_dir, fingerprint)):
        return (*previous, True)

    record = start_record(fingerprint, title=args.title, notes=args.notes, git=git_commit())
    configuration = _isolated_config(configuration, record["run_id"])
    runtime_config = frozen_config(configuration)
    pipeline_config = build_rag_pipeline_config(Configuration.from_runnable_config(runtime_config))
    record["execution_config"] = public_rag_config(pipeline_config.model_dump())
    record["limitations"] = [
        "Coverages measure retrieval and visible tool evidence.",
        "Image coordinates and table structure may be absent; visual evidence can be matched as text.",
        "Token and model call counts cover the shared SDK generation path. Cost is unavailable.",
    ]
    if any(not model.get("revision") and model["revision_status"] != "builtin"
           for model in record["runtime"]["models"].values()):
        record["limitations"].append("Some model revisions are unavailable; provider or cache behavior may change.")
    save_record(args.runs_dir, record)

    phase = "catalog"
    index_usage = {}
    index = {}
    status = "failed"
    exceptions = []
    with _observe_rag_warnings() as warnings:
        try:
            catalog = DocumentCatalog(ROOT / CORPUS, load_cases(ROOT / MANIFEST))
            phase = "index_initialization"
            started = perf_counter()
            pipeline = get_or_create_rag_pipeline(pipeline_config)
            index["initialization_ms"] = (perf_counter() - started) * 1000
            phase = "index_build"
            started = perf_counter()
            with budget_capture() as observed_index:
                pipeline.ensure_indexed()
            index_usage = _usage(observed_index)
            index["build_ms"] = (perf_counter() - started) * 1000
            index.update(collect_index_stats(pipeline))
            index["num_files"] = len({source.source.split("#page=")[0] for source in pipeline.indexer.documents})
            index["num_manifest_files"] = len(catalog.by_id)
            if not index["num_chunks"]:
                raise ValueError("The evaluation index has no chunks")
            phase = "retrieval"
            asyncio.run(_execute_cases(cases, pipeline, runtime_config, catalog, record, warnings))
            phase = "fingerprint_verification"
            _refresh_model_revisions(args, record, fingerprint)
            if all(row["status"] == "succeeded" for row in record["questions"]) and not warnings:
                status = "succeeded"
        except KeyboardInterrupt:
            status = "interrupted"
            exceptions.append({"phase": phase, "exception_type": "KeyboardInterrupt"})
        except Exception as exc:
            exceptions.append({"phase": phase, "exception_type": type(exc).__name__})

    rows = record["questions"]
    succeeded = sum(row["status"] == "succeeded" for row in rows)
    record.update(
        status=status, finished_at=utc_now(), index=index, warnings=warnings, exceptions=exceptions,
        counts={"total": len(cases), "succeeded": succeeded, "failed": len(rows) - succeeded},
        not_executed=len(cases) - len(rows),
        metrics=summarize(rows),
        model_usage={"index": index_usage, "retrieval": _usage({
            key: sum(row["model_usage"][key] for row in rows)
            for key in ("model_calls", "input_tokens", "output_tokens", "total_tokens")
        })},
        first_query_ms=rows[0]["latency_ms"] if rows else None,
        post_first_query_latency=latency_summary([
            row["latency_ms"] for row in rows[1:] if row["status"] == "succeeded"
        ]),
    )
    path = save_record(args.runs_dir, record)
    return path, record, False


def _print_json(value: Any) -> None:
    emit(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, help="Project YAML, with normal extends support")
    common.add_argument("--split", choices=("dev", "test"), default="dev")
    common.add_argument("--scope", choices=("pipeline", "tool"), default="pipeline")
    common.add_argument("--set", dest="overrides", action="append", default=[], metavar="rag_FIELD=YAML")
    common.add_argument("--top-k", type=int, help="Override rag_top_k")
    common.add_argument("--smoke", action="store_true", help="Mark a dev run as ineligible for reuse")
    sample = common.add_mutually_exclusive_group()
    sample.add_argument("--limit", type=int)
    sample.add_argument("--question-id", action="append")
    common.add_argument("--runs-dir", type=Path, default=ROOT / RUNS)
    run = commands.add_parser("run", parents=[common], help="Run or reuse an evaluation")
    run.add_argument("--force", action="store_true", help="Create a fresh result even when one is reusable")
    run.add_argument("--title", default="RAG evaluation")
    run.add_argument("--notes", default="")
    run.add_argument("--cases", type=Path, help="Reproduce an old filename-based evaluation without POR records")
    run.add_argument("--knowledge-base", action="append")
    run.add_argument("--show-misses", action="store_true")
    run.add_argument("--disable-authority-rerank", action="store_true")
    for name in sorted(LEGACY_OVERRIDES.keys() - {"top_k", "knowledge_base"}):
        value_type = int if name in {"chunk_size", "chunk_overlap", "rerank_top_n", "rrf_rank_constant"} else (
            float if name == "structured_metadata_weight" else str
        )
        run.add_argument("--" + name.replace("_", "-"), type=value_type)
    commands.add_parser("fingerprint", parents=[common], help="Print the current evaluation fingerprint")
    commands.add_parser("find", parents=[common], help="Find an intact reusable record")
    freeze = commands.add_parser("dataset-fingerprint", help="Hash the frozen corpus, manifest and both splits")
    freeze.add_argument("--root", type=Path, default=ROOT)
    compare = commands.add_parser("compare", help="Compare two compatible full run records")
    compare.add_argument("before")
    compare.add_argument("after")
    compare.add_argument("--runs-dir", type=Path, default=ROOT / RUNS)
    history = commands.add_parser("history", help="Regenerate the POR table from complete JSON records")
    history.add_argument("--runs-dir", type=Path, default=ROOT / RUNS)
    if not argv or (argv[0].startswith("--") and argv[0] != "--help"):
        argv.insert(0, "run")
    args = parser.parse_args(argv)
    if hasattr(args, "split"):
        if (args.limit is not None and args.limit < 1) or (args.top_k is not None and args.top_k < 1):
            parser.error("--limit and --top-k must be positive")
        if args.split != "dev" and args.smoke:
            parser.error("--smoke is only available for dev")
        if (args.question_id or args.limit) and not args.smoke:
            parser.error("Partial evaluations require --smoke on dev")
        if getattr(args, "embedding_provider", "") == "hash" and not args.smoke:
            parser.error("hash embeddings are allowed only for dev smoke")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_plain_output()
    try:
        if args.command == "dataset-fingerprint":
            _print_json(dataset_fingerprint(args.root.resolve()))
        elif args.command == "compare":
            def record(identifier: str) -> dict:
                file = Path(identifier)
                return load_record(file if file.suffix == ".json" else args.runs_dir / (file.name + ".json"))

            _print_json(compare_records(record(args.before), record(args.after)))
        elif args.command == "history":
            update_history(ROOT / HISTORY, args.runs_dir)
            emit(str(ROOT / HISTORY))
        else:
            load_environment()
            if args.command == "fingerprint":
                _print_json(evaluation_inputs(args)[2])
            elif args.command == "find":
                found = find_reusable_record(args.runs_dir, evaluation_inputs(args)[2])
                _print_json({"run_id": found[1]["run_id"], "path": str(found[0])} if found else None)
                return 0 if found else 1
            elif args.cases:
                cases = load_cases(args.cases)
                if any("gold_evidence" in case for case in cases):
                    raise ValueError("Use --split for POR gold evidence datasets")
                configuration = _isolated_config(resolve_eval_config(args), start_record({}, title="", notes="", git=None)["run_id"])
                validate_metric_embedding_provider(configuration.rag_embedding_provider)
                pipeline = RAGPipeline(build_rag_pipeline_config(configuration))
                k = configuration.rag_top_k
                scores, skipped = evaluate_cases(cases, pipeline, k, include_miss_details=args.show_misses)
                print_report(scores, skipped, k, args.show_misses, collect_index_stats(pipeline))
            else:
                path, record, reused = run_evaluation(args)
                _print_json({
                    "run_id": record["run_id"], "path": str(path), "status": record["status"],
                    "reused": reused, "counts": record["counts"],
                    "evaluation_fingerprint": record["evaluation_fingerprint"],
                    "overall": record["metrics"]["overall"],
                    "exceptions": record["exceptions"],
                })
                if record["status"] != "succeeded":
                    return 1
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
