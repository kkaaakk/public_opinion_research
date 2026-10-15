# GraphRAG Effectiveness Improvements

This document describes the six-phase improvement to the GraphRAG subsystem
in `open_deep_research`.

## Overview

The original GraphRAG implementation had six structural weaknesses:

1. **Hard-coded scoring**: A fixed formula with no confidence measure.
2. **Primitive term extraction**: Only regex tokenization, no NER or IDF weighting.
3. **Noisy expansion**: Always expanded regardless of query quality.
4. **No confidence filtering**: All candidates were accepted.
5. **Only term co-occurrence**: Ignored document structure (same source, adjacent chunks).
6. **No ablation framework**: Impossible to measure each component's contribution.

The six-phase improvement addresses each of these.

---

## Phase 1: Parameter Sweep + Ablation Framework

**Files**: `tests/sweep_graph_params.py`, `tests/evaluate_rag_retrieval.py`

### What changed

- Added `--graph-ablation` flag to `evaluate_rag_retrieval.py`.
- Added four graph-specific metrics:
  - `graph_benefit_rate`: % of queries where graph improved recall.
  - `graph_noise_rate`: % of queries where graph added irrelevant chunks.
  - `graph_net_effect`: `benefit_rate - noise_rate`.
  - `avg_graph_rank_change`: Average rank shift of graph-added chunks.
- Created `sweep_graph_params.py` to grid-search `graph_weight` × `graph_max_neighbors`.

### How to run

```bash
python tests/evaluate_rag_retrieval.py --graph-ablation
python tests/sweep_graph_params.py --cases tests/rag_eval_cases.jsonl
```

---

## Phase 2: Improved Term Extraction

**File**: `src/open_deep_research/rag/graph_terms.py`

### What changed

`TermExtractor` now supports three extraction strategies:

1. **NER (spaCy)**: Named entity recognition for proper nouns.
2. **Chinese tokenization (jieba)**: Word-level segmentation for CJK text.
3. **IDF-weighted filtering**: Filters common terms below a configurable percentile.

### Configuration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `rag_graph_ner_enabled` | bool | True | Enable spaCy NER |
| `rag_graph_idf_enabled` | bool | True | Enable IDF filtering |
| `rag_graph_idf_threshold_percentile` | float | 85.0 | Terms below this percentile are filtered |

### Lazy loading

spaCy and jieba are imported lazily inside `TermExtractor` methods. If not installed,
term extraction falls back to regex tokenization.

---

## Phase 3: Adaptive Expansion

**File**: `src/open_deep_research/rag/graph_adaptive.py`

### What changed

Before expanding, the system now runs three heuristic checks:

1. **Query specificity**: Does the query contain named entities / numbers?
2. **Seed diversity**: Are seed chunks diverse or all from the same source?
3. **Term entropy**: Is there enough term overlap to justify expansion?

Expansion only proceeds if ≥2 signals are favorable. This reduces noise for vague queries
like "tell me something".

### Output

`RetrievalResult.graph_expansion_decision` contains the decision object:

```json
{
  "should_expand": true,
  "reason": "query_specificity=0.7, seed_diversity=0.4, term_entropy=0.8",
  "confidence": 0.63,
  "signals": {"query_specificity": 0.7, "seed_diversity": 0.4, "term_entropy": 0.8}
}
```

---

## Phase 4: Data-Driven Scoring

**File**: `src/open_deep_research/rag/graph_scoring.py`

### What changed

Replaced the hard-coded formula

```
graph_score = graph_weight * max(seed_score, 0.1) * min(1.0, shared_terms / 3)
```

with a data-driven version:

```
graph_score = graph_weight * max(seed_score, 0.1) * overlap_factor * informativeness
confidence  = 0.5 * overlap_factor + 0.5 * informativeness
```

where `informativeness` is the average normalized IDF of shared terms.

### Confidence filtering

Candidates with `confidence < rag_graph_confidence_threshold` (default 0.15) are
filtered out entirely.

### Recommended parameters

`RECOMMENDED_GRAPH_PARAMS` provides per-category defaults (populated from Phase 1
sweep results):

| Category | graph_weight | graph_max_neighbors | graph_confidence_threshold |
|----------|-------------|---------------------|---------------------------|
| default | 0.35 | 4 | 0.15 |
| single_hop | 0.30 | 3 | 0.15 |
| multi_hop | 0.40 | 5 | 0.15 |
| citation | 0.25 | 3 | 0.15 |
| refutation | 0.20 | 2 | 0.15 |

---

## Phase 5: Structured Knowledge Integration

**File**: `src/open_deep_research/rag/graph_structured.py`

### What changed

Added three types of structural edges:

1. **SAME_SOURCE**: Chunks from the same document (weight decays with distance).
2. **NEXT_CHUNK**: Sequentially adjacent chunks (weight = 1.0).
3. **SHARED_TITLE / SHARED_JSON_PATH**: Chunks sharing title or metadata path (weight = 0.8 / 0.6).

### How it works

When `rag_structural_edges_enabled=True`, `GraphRAGIndex` builds a `StructuralIndex`
at initialization. During expansion, structural neighbors are added with half the
graph weight: `struct_score = graph_weight * seed_score * struct_weight * 0.5`.

### Configuration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `rag_structural_edges_enabled` | bool | False | Enable structural edges |

---

## Phase 6: Tests and Documentation

**Files**: `tests/test_graph_rag_ablation.py`, `docs/graphrag_effectiveness.md`

### Test coverage

`test_graph_rag_ablation.py` covers:

- `TestTermExtractor`: Basic extraction, IDF filtering
- `TestAdaptiveExpansion`: Decision logic, adaptive flag
- `TestComputeGraphScore`: Score and confidence computation
- `TestStructuralEdges`: Source, adjacent, and metadata edges
- `TestGraphRAGIndexIntegration`: Full end-to-end expansion with all features

### Run tests

```bash
pytest tests/test_graph_rag_ablation.py -v
```

---

## Summary Table

| Phase | Problem | Solution | Key file |
|-------|---------|----------|----------|
| 1 | No measurement | Ablation metrics + sweep | `tests/sweep_graph_params.py` |
| 2 | Primitive terms | NER + jieba + IDF | `graph_terms.py` |
| 3 | Noisy expansion | Adaptive decision | `graph_adaptive.py` |
| 4 | Hard-coded score | Data-driven + confidence | `graph_scoring.py` |
| 5 | No structure | Structural edges | `graph_structured.py` |
| 6 | No tests/docs | Unit tests + doc | This file |
