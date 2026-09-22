"""Data-driven graph scoring for GraphRAG.

This module replaces the hard-coded ``graph_score`` formula with a version
that incorporates term informativeness and exposes a confidence score.

The scoring formula is kept backward-compatible in structure::

    graph_score = graph_weight * seed_score * term_informativeness * overlap_factor

where ``term_informativeness`` is the average normalised IDF of shared terms.

A ``confidence`` score is also returned; candidates with ``confidence`` below
a threshold can be filtered out entirely.
"""

from __future__ import annotations

from typing import Any

from open_deep_research.rag.graph_terms import TermExtractor


def compute_graph_score(
    seed_score: float,
    shared_terms: set[str],
    graph_weight: float,
    term_extractor: TermExtractor | None = None,
) -> tuple[float, float]:
    """Compute a graph expansion score and its confidence.

    Args:
        seed_score: Score of the seed chunk that triggered this neighbour.
        shared_terms: Set of terms shared between seed and neighbour.
        graph_weight: Global graph weight parameter.
        term_extractor: Optional TermExtractor for IDF lookup.

    Returns:
        A tuple of (final_score, confidence).
    """
    if not shared_terms or graph_weight <= 0 or seed_score <= 0:
        return 0.0, 0.0

    # Overlap factor: same as original (len(shared_terms) / 3, capped at 1.0)
    overlap_factor = min(1.0, len(shared_terms) / 3)

    # Term informativeness: average normalised IDF of shared terms
    informativeness = _term_informativeness(shared_terms, term_extractor)

    # Confidence: combines overlap and informativeness
    confidence = 0.5 * overlap_factor + 0.5 * informativeness

    # Final score
    final_score = graph_weight * max(seed_score, 0.1) * overlap_factor * informativeness

    return round(final_score, 6), round(confidence, 4)


def _term_informativeness(
    shared_terms: set[str],
    term_extractor: TermExtractor | None,
) -> float:
    """Return average normalised informativeness of the shared terms.

    If a TermExtractor with IDF is available, use average normalised IDF.
    Otherwise fall back to a simple length-based heuristic.
    """
    if not shared_terms:
        return 1.0

    if term_extractor is not None and term_extractor._idf is not None:
        scores = []
        for term in shared_terms:
            idf = term_extractor._idf.get(term, term_extractor._idf_threshold)
            # Normalise by the threshold (the 85th percentile cutoff)
            threshold = max(term_extractor._idf_threshold, 1e-6)
            scores.append(min(1.0, idf / threshold))
        return sum(scores) / len(scores) if scores else 1.0

    # Fallback: longer terms are slightly more informative
    avg_len = sum(len(t) for t in shared_terms) / len(shared_terms)
    return min(1.0, avg_len / 10.0)


# Recommended parameters per eval category (populated from Phase 1 sweep results).
# These are placeholders to be filled in after running the parameter sweep.
RECOMMENDED_GRAPH_PARAMS: dict[str, dict[str, Any]] = {
    "default": {
        "graph_weight": 0.35,
        "graph_max_neighbors": 4,
        "graph_confidence_threshold": 0.15,
    },
    "single_hop": {
        "graph_weight": 0.30,
        "graph_max_neighbors": 3,
        "graph_confidence_threshold": 0.15,
    },
    "multi_hop": {
        "graph_weight": 0.40,
        "graph_max_neighbors": 5,
        "graph_confidence_threshold": 0.15,
    },
    "citation": {
        "graph_weight": 0.25,
        "graph_max_neighbors": 3,
        "graph_confidence_threshold": 0.15,
    },
    "refutation": {
        "graph_weight": 0.20,
        "graph_max_neighbors": 2,
        "graph_confidence_threshold": 0.15,
    },
}
