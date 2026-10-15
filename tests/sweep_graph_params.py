# 扫描图扩展参数组合，并根据检索指标筛选帕累托候选。
"""Sweep graph_weight × graph_max_neighbors and report retrieval metrics.

This script runs a grid search over GraphRAG parameter combinations, comparing
each against a graph-off baseline. Output includes a JSON result matrix,
Pareto-optimal combinations, and a summary table sorted by hit@k.

Usage::

    python tests/sweep_graph_params.py \
        --weights "0.05,0.10,0.15,0.20,0.25,0.30,0.35,0.40,0.50,0.60" \
        --max-neighbors "1,2,3,4,5,6,8,10,12,15"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from time import perf_counter

# Ensure tests/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from evaluate_rag_retrieval import (
    average_metric,
    build_pipeline,
    configure_plain_output,
    evaluate_cases,
    load_cases,
    validate_metric_embedding_provider,
)

from open_deep_research.rag.service import reset_rag_pipeline_cache


# 解析逗号分隔浮点参数并稳定排序。
def parse_float_list(value: str) -> list[float]:
    """Parse a comma-separated string into a sorted list of floats."""
    return sorted({float(v.strip()) for v in value.split(",") if v.strip()})


# 解析逗号分隔整数参数并稳定排序。
def parse_int_list(value: str) -> list[int]:
    """Parse a comma-separated string into a sorted list of ints."""
    return sorted({int(v.strip()) for v in value.split(",") if v.strip()})


# 输出一行纯文本扫描进度或结果。
def emit(line: str = "") -> None:
    """Write one plain text line."""
    sys.stdout.write(f"{line}\n")


# 检查当前参数点是否在全部目标上被其他点支配。
def is_pareto_dominated(
    point: dict[str, float],
    others: list[dict[str, float]],
) -> bool:
    """Check if a point is dominated by any other on all objectives."""
    hit = point["hit_at_k"]
    recall = point.get("recall_at_k", 0.0)
    for other in others:
        if other is point:
            continue
        if other["hit_at_k"] >= hit and other.get("recall_at_k", 0.0) >= recall:
            if other["hit_at_k"] > hit or other.get("recall_at_k", 0.0) > recall:
                return True
    return False


# 解析参数扫描范围与评估路径。
def parse_args() -> argparse.Namespace:
    """Parse sweep arguments."""
    parser = argparse.ArgumentParser(
        description="Sweep GraphRAG parameters and report retrieval metrics."
    )
    parser.add_argument(
        "--weights",
        type=parse_float_list,
        default=parse_float_list("0.05,0.10,0.15,0.20,0.25,0.30,0.35,0.40,0.50,0.60"),
        help="Comma-separated graph_weight values to sweep.",
    )
    parser.add_argument(
        "--max-neighbors",
        type=parse_int_list,
        default=parse_int_list("1,2,3,4,5,6,8,10,12,15"),
        help="Comma-separated graph_max_neighbors values to sweep.",
    )
    parser.add_argument("--cases", type=Path, default=Path("tests/rag_eval_cases.jsonl"))
    parser.add_argument(
        "--knowledge-base",
        action="append",
        default=None,
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--chunk-size", type=int, default=400)
    parser.add_argument("--chunk-overlap", type=int, default=50)
    parser.add_argument("--rerank-top-n", type=int, default=20)
    parser.add_argument("--embedding-provider", default="sentence_transformers")
    parser.add_argument(
        "--embedding-model",
        default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    )
    parser.add_argument("--embedding-device", default=None)
    parser.add_argument("--vectorstore-provider", default="milvus")
    parser.add_argument("--vectorstore-path", default="data/indexes/rag")
    parser.add_argument("--milvus-uri", default=None)
    parser.add_argument("--milvus-metric-type", default="COSINE")
    parser.add_argument("--reranker-provider", default="cross_encoder")
    parser.add_argument("--reranker-model", default=".rag_models/cross-encoder-ms-marco-MiniLM-L6-v2")
    parser.add_argument("--reranker-device", default=None)
    parser.add_argument("--rrf-rank-constant", type=int, default=60)
    parser.add_argument("--structured-metadata-weight", type=float, default=0.15)
    parser.add_argument("--graph-backend", default="memory", choices=["memory", "neo4j"])
    parser.add_argument("--neo4j-uri", default=None)
    parser.add_argument("--neo4j-username", default=None)
    parser.add_argument("--neo4j-password", default=None)
    parser.add_argument("--neo4j-database", default=None)
    parser.add_argument("--disable-authority-rerank", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write JSON result matrix.",
    )
    return parser.parse_args()


# 执行全部参数组合并汇总检索效果和帕累托结果。
def run_sweep(args: argparse.Namespace) -> None:
    """Execute the full parameter sweep."""
    configure_plain_output()
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    if args.knowledge_base is None:
        args.knowledge_base = ["data/knowledge"]
    if args.milvus_uri is None:
        args.milvus_uri = f"data/indexes/rag/milvus_sweep_{os.getpid()}.db"

    validate_metric_embedding_provider(args.embedding_provider)
    cases = load_cases(args.cases)
    k = args.top_k
    metric_key = f"hit@{k}"
    recall_key = f"recall@{k}"

    weights = args.weights
    max_neighbors_list = args.max_neighbors
    total_combos = len(weights) * len(max_neighbors_list)

    emit(f"=== GraphRAG Parameter Sweep ===")
    emit(f"weights: {weights}")
    emit(f"max_neighbors: {max_neighbors_list}")
    emit(f"total_combinations: {total_combos}")
    emit(f"cases: {len(cases)}")
    emit(f"graph_backend: {args.graph_backend}")
    emit()

    # Baseline: graph OFF
    emit("--- Baseline (graph OFF) ---")
    args.graph_enabled = False
    args.graph_weight = 0.0
    args.graph_max_neighbors = 0
    reset_rag_pipeline_cache()
    baseline_pipeline = build_pipeline(args)
    baseline_scores, _ = evaluate_cases(cases, baseline_pipeline, k)
    baseline_hit = round(average_metric(baseline_scores, metric_key), 4)
    baseline_recall = round(average_metric(baseline_scores, recall_key), 4)
    emit(f"baseline hit@{k}: {baseline_hit}")
    emit(f"baseline recall@{k}: {baseline_recall}")
    emit()

    results = []
    combo_index = 0

    for weight in weights:
        for max_n in max_neighbors_list:
            combo_index += 1
            reset_rag_pipeline_cache()
            args.graph_enabled = True
            args.graph_weight = weight
            args.graph_max_neighbors = max_n

            pipeline = build_pipeline(args)
            started = perf_counter()
            scores, _ = evaluate_cases(cases, pipeline, k)
            elapsed = round(perf_counter() - started, 2)

            hit = round(average_metric(scores, metric_key), 4)
            recall = round(average_metric(scores, recall_key), 4)
            precision = round(
                average_metric(scores, f"precision@{k}"), 4
            )
            mrr = round(average_metric(scores, f"mrr@{k}"), 4)

            delta_hit = round(hit - baseline_hit, 4)
            delta_recall = round(recall - baseline_recall, 4)

            result = {
                "graph_weight": weight,
                "graph_max_neighbors": max_n,
                f"hit@{k}": hit,
                f"recall@{k}": recall,
                f"precision@{k}": precision,
                f"mrr@{k}": mrr,
                "delta_hit": delta_hit,
                "delta_recall": delta_recall,
                "elapsed_s": elapsed,
                # internal keys for Pareto
                "hit_at_k": hit,
                "recall_at_k": recall,
            }
            results.append(result)

            emit(
                f"[{combo_index}/{total_combos}] "
                f"w={weight} n={max_n} "
                f"hit={hit} recall={recall} "
                f"Δhit={delta_hit:+.4f} Δrecall={delta_recall:+.4f} "
                f"({elapsed}s)"
            )

    # Pareto optimal
    pareto = [r for r in results if not is_pareto_dominated(r, results)]
    pareto.sort(key=lambda r: r["hit_at_k"], reverse=True)

    emit()
    emit("=== Pareto-Optimal Combinations ===")
    for r in pareto:
        emit(
            f"  w={r['graph_weight']} n={r['graph_max_neighbors']} "
            f"hit={r['hit_at_k']} recall={r['recall_at_k']} "
            f"Δhit={r['delta_hit']:+.4f}"
        )

    # Summary table sorted by hit@k
    emit()
    emit("=== Full Results (sorted by hit@k desc) ===")
    sorted_results = sorted(results, key=lambda r: (r["hit_at_k"], r["recall_at_k"]), reverse=True)
    header = (
        f"{'weight':>8} {'neighbors':>10} "
        f"{'hit@k':>8} {'recall@k':>10} "
        f"{'precision':>10} {'mrr':>8} "
        f"{'Δhit':>8} {'Δrecall':>10}"
    )
    emit(header)
    emit("-" * len(header))
    for r in sorted_results:
        emit(
            f"{r['graph_weight']:>8.2f} {r['graph_max_neighbors']:>10} "
            f"{r['hit_at_k']:>8.4f} {r['recall_at_k']:>10.4f} "
            f"{r[f'precision@{k}']:>10.4f} {r[f'mrr@{k}']:>8.4f} "
            f"{r['delta_hit']:>+8.4f} {r['delta_recall']:>+10.4f}"
        )

    # Write JSON output
    output_payload = {
        "baseline": {
            f"hit@{k}": baseline_hit,
            f"recall@{k}": baseline_recall,
        },
        "results": [
            {key: val for key, val in r.items() if key not in ("hit_at_k", "recall_at_k")}
            for r in sorted_results
        ],
        "pareto": [
            {key: val for key, val in r.items() if key not in ("hit_at_k", "recall_at_k")}
            for r in pareto
        ],
        "sweep_config": {
            "weights": weights,
            "max_neighbors": max_neighbors_list,
            "graph_backend": args.graph_backend,
            "top_k": k,
            "cases_count": len(cases),
        },
    }

    output_path = args.output or Path("tests/expt_results/graph_param_sweep.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    emit()
    emit(f"Results written to {output_path}")


if __name__ == "__main__":
    args = parse_args()
    run_sweep(args)
