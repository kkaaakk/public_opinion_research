# 根据查询具体程度、种子来源集中度和术语信息量决定是否进行图扩展。
"""Adaptive expansion decision for GraphRAG.

This module determines whether graph expansion should be applied for a
particular query based on three heuristic signals:

1. Query specificity: ratio of long/capitalised tokens. Very generic
   queries (few long tokens) tend to benefit less from expansion.
2. Seed diversity: how many distinct sources the top seed results span.
   If seeds are already spread across many sources, expansion is more
   likely to add noise than value.
3. Term entropy: the average entropy of query terms in the corpus.
   If query terms are ultra-common (low entropy), neighbours are likely
   to be irrelevant.

Decision rule: expand only when **at least two** of the three signals are
favourable.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from open_deep_research.rag.types import RetrievalResult


# 记录是否扩展及其判断依据和相关评分。
@dataclass
class ExpansionDecision:
    """Result of an adaptive expansion check."""

    should_expand: bool
    reason: str
    confidence: float = 0.0
    signals: dict[str, float] = field(default_factory=dict)


# 用长词和大写词比例估算查询具体程度，返回 0 到 1 的分数。
def _query_specificity(query: str) -> float:
    """Return 0-1 score: higher means more specific / less generic.

    Heuristic: ratio of tokens with length >= 5 or capitalised words.
    """
    tokens = query.split()
    if not tokens:
        return 0.0
    specific = sum(
        1
        for t in tokens
        if len(t) >= 5 or (len(t) >= 2 and t[0].isupper())
    )
    return min(1.0, specific / max(1, len(tokens)))


# 计算前四个种子中最大来源占比；值越高表示结果越集中。
def _seed_diversity(seed_results: list[RetrievalResult]) -> float:
    """Return 0-1 score: higher means seeds are concentrated (good for expansion).

    Heuristic: fraction of top-4 seeds that come from the most common source.
    """
    if not seed_results:
        return 0.0
    top = seed_results[:4]
    sources: dict[str, int] = defaultdict(int)
    for r in top:
        sources[r.chunk.source] += 1
    if not sources:
        return 0.0
    max_count = max(sources.values())
    return max_count / len(top)


# 以归一化逆文档频率估算查询术语信息量，抑制过于常见的词。
def _term_entropy(
    query_terms: set[str],
    term_to_chunk_ids: dict[str, set[str]],
) -> float:
    """Return 0-1 score: higher means terms are informative (not ultra-common).

    Heuristic: average inverse document frequency of query terms in the
    term-to-chunk mapping. Normalised by log(total_chunks + 1).
    """
    if not query_terms:
        return 0.0
    total_chunks = max(len(chunks) for chunks in term_to_chunk_ids.values()) if term_to_chunk_ids else 0
    if total_chunks == 0:
        return 0.0

    scores = []
    for term in query_terms:
        df = len(term_to_chunk_ids.get(term, set()))
        # IDF-like score, normalised to 0-1
        idf = math.log(1 + (total_chunks - df + 0.5) / (df + 0.5))
        max_idf = math.log(total_chunks + 1)
        scores.append(idf / max_idf if max_idf > 0 else 0.0)

    return sum(scores) / len(scores)


# 综合查询、种子和术语统计判断图扩展是否可能改善召回。
def decide_expansion(
    query: str,
    seed_results: list[RetrievalResult],
    chunk_terms: dict[str, set[str]],
    term_to_chunk_ids: dict[str, set[str]],
) -> ExpansionDecision:
    """Decide whether graph expansion is likely to help for this query.

    Args:
        query: Original user query.
        seed_results: Seed chunks from vector + BM25 fusion.
        chunk_terms: Mapping chunk_id → extracted terms (for query term lookup).
        term_to_chunk_ids: Mapping term → set of chunk_ids.

    Returns:
        ExpansionDecision with should_expand flag and diagnostic signals.
    """
    # Signal 1: query specificity
    specificity = _query_specificity(query)
    specificity_favourable = specificity >= 0.3

    # Signal 2: seed diversity (concentration is better)
    diversity = _seed_diversity(seed_results)
    diversity_favourable = diversity >= 0.5

    # Signal 3: term entropy (information content of query terms)
    # Build query terms from the first few seed chunks (they share terms with query)
    query_terms: set[str] = set()
    for r in seed_results[:3]:
        query_terms.update(chunk_terms.get(r.chunk.chunk_id, set()))
    entropy = _term_entropy(query_terms, term_to_chunk_ids)
    entropy_favourable = entropy >= 0.3

    favourable_count = sum(
        [specificity_favourable, diversity_favourable, entropy_favourable]
    )

    signals = {
        "query_specificity": round(specificity, 4),
        "seed_diversity": round(diversity, 4),
        "term_entropy": round(entropy, 4),
    }

    if favourable_count >= 2:
        return ExpansionDecision(
            should_expand=True,
            reason="At least 2 of 3 heuristic signals are favourable.",
            confidence=0.5 + 0.25 * favourable_count,
            signals=signals,
        )

    return ExpansionDecision(
        should_expand=False,
        reason="Too few favourable signals for safe graph expansion.",
        confidence=0.5 - 0.1 * (3 - favourable_count),
        signals=signals,
    )
