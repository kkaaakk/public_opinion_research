# 评估本地 RAG 的来源和证据召回，并提供图扩展开关消融及漏召诊断。
"""Evaluate local RAG retrieval metrics against a JSONL test set.

This script intentionally measures retrieval only. It does not call an LLM and
does not judge final answer quality.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from typing import Any

from open_deep_research.rag.service import (
    RAGPipeline,
    RAGPipelineConfig,
    apply_authority_adjustment,
    reset_rag_pipeline_cache,
)

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
DEFAULT_EMBEDDING_PROVIDER = "sentence_transformers"
DEFAULT_EMBEDDING_MODEL_REPO = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_EMBEDDING_MODEL_CACHE = (
    Path.home()
    / ".cache"
    / "huggingface"
    / "hub"
    / "models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2"
    / "snapshots"
    / "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
)
DEFAULT_EMBEDDING_MODEL = (
    str(DEFAULT_EMBEDDING_MODEL_CACHE)
    if DEFAULT_EMBEDDING_MODEL_CACHE.exists()
    else DEFAULT_EMBEDDING_MODEL_REPO
)
DEFAULT_VECTORSTORE_PROVIDER = "milvus"
DEFAULT_VECTORSTORE_PATH = "data/indexes/rag"
DEFAULT_MILVUS_URI = str(Path(DEFAULT_VECTORSTORE_PATH) / f"odr_milvus_eval_{os.getpid()}.db")
DEFAULT_RERANKER_PROVIDER = "cross_encoder"
DEFAULT_RERANKER_MODEL = ".rag_models/cross-encoder-ms-marco-MiniLM-L6-v2"


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
    """Load JSONL evaluation cases."""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


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
    """Create the local RAG pipeline used for retrieval-only evaluation."""
    configure_plain_output()
    reset_rag_pipeline_cache()
    validate_metric_embedding_provider(args.embedding_provider)
    return RAGPipeline(
        RAGPipelineConfig(
            knowledge_base_paths=args.knowledge_base,
            chunk_size=args.chunk_size,
            chunk_overlap=args.chunk_overlap,
            top_k=args.top_k,
            rerank_top_n=args.rerank_top_n,
            embedding_provider=args.embedding_provider,
            embedding_model=args.embedding_model,
            embedding_device=args.embedding_device,
            vectorstore_provider=args.vectorstore_provider,
            vectorstore_path=args.vectorstore_path,
            milvus_uri=args.milvus_uri,
            milvus_metric_type=args.milvus_metric_type,
            reranker_provider=args.reranker_provider,
            reranker_model=args.reranker_model,
            reranker_device=args.reranker_device,
            rrf_rank_constant=getattr(args, "rrf_rank_constant", 60),
            structured_metadata_weight=getattr(
                args,
                "structured_metadata_weight",
                0.15,
            ),
            authority_rerank_enabled=not getattr(
                args,
                "disable_authority_rerank",
                False,
            ),
        )
    )


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


# 解析检索评估和消融参数。
def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Evaluate local RAG retrieval metrics against tests/rag_eval_cases.jsonl."
    )
    parser.add_argument("--cases", type=Path, default=Path("tests/rag_eval_cases.jsonl"))
    parser.add_argument(
        "--knowledge-base",
        action="append",
        default=None,
        help="Knowledge-base path. Repeat for multiple paths. Defaults to data/knowledge.",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--chunk-size", type=int, default=400)
    parser.add_argument("--chunk-overlap", type=int, default=50)
    parser.add_argument("--rerank-top-n", type=int, default=20)
    parser.add_argument("--embedding-provider", default=DEFAULT_EMBEDDING_PROVIDER)
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--embedding-device", default=None)
    parser.add_argument("--vectorstore-provider", default=DEFAULT_VECTORSTORE_PROVIDER)
    parser.add_argument("--vectorstore-path", default=DEFAULT_VECTORSTORE_PATH)
    parser.add_argument("--milvus-uri", default=DEFAULT_MILVUS_URI)
    parser.add_argument("--milvus-metric-type", default="COSINE")
    parser.add_argument("--reranker-provider", default=DEFAULT_RERANKER_PROVIDER)
    parser.add_argument("--reranker-model", default=DEFAULT_RERANKER_MODEL)
    parser.add_argument("--reranker-device", default=None)
    parser.add_argument("--rrf-rank-constant", type=int, default=60)
    parser.add_argument("--structured-metadata-weight", type=float, default=0.15)
    parser.add_argument("--disable-authority-rerank", action="store_true")
    parser.add_argument("--show-misses", action="store_true")
    args = parser.parse_args()
    try:
        validate_metric_embedding_provider(args.embedding_provider)
    except ValueError as exc:
        parser.error(str(exc))
    return args


# 运行评估命令行入口并输出报告。
def main() -> None:
    """CLI entry point."""
    args = parse_args()
    if args.knowledge_base is None:
        args.knowledge_base = ["data/knowledge"]

    configure_plain_output()
    cases = load_cases(args.cases)

    print_run_config(args)
    pipeline = build_pipeline(args)
    scores, skipped = evaluate_cases(
        cases,
        pipeline,
        args.top_k,
        include_miss_details=args.show_misses,
    )
    index_stats = collect_index_stats(pipeline)
    print_report(
        scores,
        skipped,
        args.top_k,
        show_misses=args.show_misses,
        index_stats=index_stats,
    )


if __name__ == "__main__":
    main()
