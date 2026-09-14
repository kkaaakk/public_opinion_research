"""Evaluate raw benchmark runs with a fixed, transparent local rubric."""

from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any


URL_RE = re.compile(r"https?://[^\s)\]}>,]+", re.IGNORECASE)
SENTENCE_RE = re.compile(r"[^\n。！？!?\.]+(?:[。！？!?\.]|$)")
CLAIM_RE = re.compile(
    r"(\d|%|日期|时间|发生|导致|造成|事故|处罚|召回|监管|投诉|回应|声明|风险|"
    r"reported|investigat|official|incident|complaint|risk|penalt|recall)",
    re.IGNORECASE,
)
SOURCE_MARKER_RE = re.compile(
    r"(https?://|来源|证据|据报道|据公告|据通报|原文|引用|\[[0-9]+\]|"
    r"官方公告|监管文件|新闻报道|source|citation)",
    re.IGNORECASE,
)
CONFLICT_RE = re.compile(
    r"(冲突|矛盾|不一致|不符|相互印证|交叉核验|交叉验证|口径|一致|"
    r"conflict|contradict|inconsistent|corroborat|cross.?check|discrepanc)",
    re.IGNORECASE,
)


def load_json(path: Path) -> Any:
    """Read one UTF-8 JSON artifact."""
    return json.loads(path.read_text(encoding="utf-8"))


def _dimension(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        identifier = str(value.get("id") or value.get("label") or "dimension")
        label = str(value.get("label") or identifier)
        keywords = [str(item) for item in value.get("keywords", []) if str(item).strip()]
        return {"id": identifier, "label": label, "keywords": keywords}
    identifier = str(value)
    return {"id": identifier, "label": identifier, "keywords": [identifier]}


def dimensions_for_case(case: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize the case contract without changing its source data."""
    return [_dimension(value) for value in case.get("required_dimensions", [])]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return " ".join(f"{key} {item}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return " ".join(_text(item) for item in value)
    return str(value)


def _match_keywords(text: str, keywords: list[str]) -> list[str]:
    lowered = text.casefold()
    return [keyword for keyword in keywords if keyword.casefold() in lowered]


def coverage_metric(report: str, case: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the JSON rubric's lexical evidence coverage rule."""
    normalized = report or ""
    dimensions = dimensions_for_case(case)
    covered: list[str] = []
    hits: dict[str, list[str]] = {}
    for dimension in dimensions:
        matched = _match_keywords(normalized, dimension["keywords"])
        hits[dimension["id"]] = matched
        if matched:
            covered.append(dimension["id"])
    critical_ids = [str(item) for item in case.get("critical_dimensions", [])]
    critical_total = len(critical_ids)
    critical_covered = [item for item in critical_ids if item in covered]
    total = len(dimensions)
    return {
        "covered": len(covered),
        "total": total,
        "percentage": round(100 * len(covered) / total, 2) if total else None,
        "covered_dimensions": covered,
        "keyword_hits": hits,
        "critical": {
            "covered": len(critical_covered),
            "total": critical_total,
            "percentage": round(100 * len(critical_covered) / critical_total, 2) if critical_total else None,
            "covered_dimensions": critical_covered,
        },
    }


def _evidence_text(run: Mapping[str, Any]) -> str:
    """Combine report-visible and captured tool summaries for source analysis."""
    parts = [str(run.get("final_report") or "")]
    role_reports = run.get("role_reports")
    if isinstance(role_reports, Mapping):
        parts.extend(str(value or "") for value in role_reports.values())
    for record in run.get("tool_records", []) or []:
        if isinstance(record, Mapping):
            parts.append(_text(record.get("result")))
    return "\n".join(parts)


def source_diversity(run: Mapping[str, Any], rubric: Mapping[str, Any]) -> dict[str, Any]:
    """Classify source categories from report text and captured tool evidence."""
    text = _evidence_text(run)
    markers = rubric.get("source_type_markers", {})
    observed: list[str] = []
    evidence: dict[str, list[str]] = {}
    for source_type, values in markers.items():
        hits = [str(value) for value in values if str(value).casefold() in text.casefold()]
        if hits:
            observed.append(str(source_type))
            evidence[str(source_type)] = hits
    urls = sorted(set(URL_RE.findall(text)))
    tool_names = []
    for record in run.get("tool_records", []) or []:
        if isinstance(record, Mapping) and record.get("tool"):
            tool_names.append(str(record["tool"]))
    return {
        "observed_types": observed,
        "type_count": len(observed),
        "type_markers": evidence,
        "unique_urls": urls,
        "unique_url_count": len(urls),
        "tool_names": sorted(set(tool_names)),
    }


def unsupported_claim_metric(report: str) -> dict[str, Any]:
    """Estimate unsupported factual/risk claims using nearby source markers."""
    candidates: list[str] = []
    unsupported: list[str] = []
    fragments = SENTENCE_RE.findall(report or "")
    for index, fragment in enumerate(fragments):
        clean = re.sub(r"^\s*[#>*\-\d.)]+\s*", "", fragment).strip()
        if len(clean) < 18 or clean.startswith(("##", "###")) or not CLAIM_RE.search(clean):
            continue
        candidates.append(clean)
        neighborhood = " ".join(fragments[max(0, index - 1): min(len(fragments), index + 2)])
        if not SOURCE_MARKER_RE.search(neighborhood):
            unsupported.append(clean)
    denominator = len(candidates)
    return {
        "unsupported": len(unsupported),
        "total_claim_candidates": denominator,
        "rate": round(len(unsupported) / denominator, 4) if denominator else None,
        "unsupported_examples": unsupported[:12],
        "method": "sentence-level heuristic; review manually before treating as a factual audit",
    }


def conflict_metric(run: Mapping[str, Any], rubric: Mapping[str, Any]) -> dict[str, Any]:
    """Check whether the report/review explicitly handles cross-source conflict."""
    report = str(run.get("final_report") or "")
    reviews = run.get("research_reviews") or []
    review_text = _text(reviews)
    combined = report + "\n" + review_text
    markers = rubric.get("quality_markers", {}).get("comparison", [])
    hits = [str(marker) for marker in markers if str(marker).casefold() in combined.casefold()]
    regex_hits = CONFLICT_RE.findall(combined)
    detected = bool(hits or regex_hits)
    return {
        "detected": detected,
        "score": 1 if detected else 0,
        "signals": sorted(set(hits + [str(item) for item in regex_hits]))[:20],
        "method": "explicit comparison marker in final report or Research Review",
    }


def quality_scores(
    report: str,
    coverage: Mapping[str, Any],
    sources: Mapping[str, Any],
    unsupported: Mapping[str, Any],
    conflict: Mapping[str, Any],
    rubric: Mapping[str, Any],
) -> dict[str, Any]:
    """Produce five fixed 1-5 heuristic scores with the component evidence."""
    report_text = report or ""
    evidence_markers = rubric.get("quality_markers", {}).get("evidence", [])
    risk_markers = rubric.get("quality_markers", {}).get("risk", [])
    uncertainty_markers = rubric.get("quality_markers", {}).get("uncertainty", [])
    evidence_hits = [item for item in evidence_markers if str(item).casefold() in report_text.casefold()]
    risk_hits = [item for item in risk_markers if str(item).casefold() in report_text.casefold()]
    uncertainty_hits = [item for item in uncertainty_markers if str(item).casefold() in report_text.casefold()]
    coverage_ratio = float(coverage.get("percentage") or 0) / 100
    source_score = min(4.0, 0.8 * float(sources.get("type_count", 0)) + (0.4 if sources.get("unique_url_count") else 0))
    unsupported_rate = unsupported.get("rate")
    if unsupported_rate is None:
        unsupported_component = 0.0
    else:
        unsupported_component = max(0.0, 1.0 - float(unsupported_rate))

    def score(value: float) -> float:
        return round(max(1.0, min(5.0, value)), 1)

    scores = {
        "fact_completeness": score(1 + 4 * coverage_ratio),
        "evidence_sufficiency": score(1 + source_score + min(1.5, len(evidence_hits) * 0.15)),
        "risk_judgment_reasonableness": score(1 + 1.4 * coverage_ratio + (1.0 if risk_hits else 0) + (0.6 if conflict.get("detected") else 0) + 0.5 * unsupported_component),
        "uncertainty_expression": score(1 + min(4.0, len(uncertainty_hits) * 0.7)),
        "source_traceability": score(1 + min(4.0, sources.get("unique_url_count", 0) / 3 + len(evidence_hits) * 0.2)),
    }
    return {
        "scores": scores,
        "final_quality": round(statistics.mean(scores.values()), 2) if scores else None,
        "score_method": "fixed deterministic heuristic, not an LLM judge; inspect raw evidence before making a release decision",
        "signals": {
            "evidence_markers": evidence_hits,
            "risk_markers": risk_hits,
            "uncertainty_markers": uncertainty_hits,
        },
    }


def _first_round_text(run: Mapping[str, Any]) -> str:
    reports = run.get("round_reports") or []
    first = [str(item.get("report") or "") for item in reports if item.get("round") == 1 or item.get("mode") == "initial"]
    if first:
        return "\n".join(first)
    return "\n".join(str(value or "") for value in (run.get("role_reports") or {}).values())


def _gap_matches(gap_text: str, dimensions: list[dict[str, Any]]) -> list[str]:
    """Map review language to rubric dimensions using label/keyword overlap."""
    lowered = gap_text.casefold()
    matches: list[str] = []
    for dimension in dimensions:
        terms = [dimension["label"], *dimension["keywords"]]
        if any(term.casefold() in lowered for term in terms if term):
            matches.append(dimension["id"])
    return matches


def dynamic_metrics(run: Mapping[str, Any], case: Mapping[str, Any]) -> dict[str, Any]:
    """Calculate dynamic-loop metrics from observed reviews and reports."""
    dimensions = dimensions_for_case(case)
    critical_ids = [str(item) for item in case.get("critical_dimensions", [])]
    first_text = _first_round_text(run)
    actual_missing = [
        identifier
        for identifier in critical_ids
        for dimension in dimensions
        if dimension["id"] == identifier and not _match_keywords(first_text, dimension["keywords"])
    ]
    reviews = [item for item in run.get("research_reviews", []) if isinstance(item, Mapping)]
    identified: set[str] = set()
    gap_items: list[dict[str, Any]] = []
    for review_index, review in enumerate(reviews, 1):
        gaps = review.get("research_gaps") if isinstance(review.get("research_gaps"), list) else []
        tasks = review.get("next_tasks") if isinstance(review.get("next_tasks"), list) else []
        for raw in [*gaps, *tasks]:
            if isinstance(raw, Mapping):
                text = " ".join(str(raw.get(key) or "") for key in ("task_id", "objective", "evidence_needed", "reason"))
                task_id = str(raw.get("task_id") or "")
            else:
                text = str(raw)
                task_id = ""
            mapped = _gap_matches(text, dimensions)
            identified.update(item for item in mapped if item in critical_ids)
            gap_items.append({"review": review_index, "text": text, "task_id": task_id, "dimensions": mapped})

    completed = [item for item in run.get("completed_research_tasks", []) if isinstance(item, Mapping)]
    completed_ids = {str(item.get("task_id") or "") for item in completed}
    final_text = str(run.get("final_report") or "")
    later_complete = any(bool(review.get("research_complete")) for review in reviews[1:])
    resolved_items = 0
    for gap in gap_items:
        mapped = gap["dimensions"]
        dimension_covered = all(
            any(dimension["id"] == item and _match_keywords(final_text, dimension["keywords"]) for dimension in dimensions)
            for item in mapped
        ) if mapped else False
        task_done = bool(gap["task_id"] and gap["task_id"] in completed_ids)
        if dimension_covered or (task_done and later_complete):
            resolved_items += 1

    metrics = run.get("metrics", {}) if isinstance(run.get("metrics"), Mapping) else {}
    review_found = len(gap_items)
    return {
        "review_count": len(reviews),
        "review_triggered_followup": bool(metrics.get("review_triggered_followup")),
        "followup_rounds": metrics.get("followup_rounds"),
        "first_round_complete": not bool(metrics.get("review_triggered_followup")),
        "actual_missing_critical_dimensions": actual_missing,
        "actual_missing_critical_count": len(actual_missing),
        "identified_critical_gaps": sorted(identified),
        "identified_critical_gap_count": len(identified),
        "gap_detection_recall": round(len(identified.intersection(actual_missing)) / len(actual_missing), 4) if actual_missing else None,
        "review_gap_count": review_found,
        "research_gap_texts": [item["text"] for item in gap_items],
        "next_tasks_count": metrics.get("next_tasks_count"),
        "completed_followup_task_count": len(completed),
        "public_signal_followup_tasks": metrics.get("public_signal_followup_tasks"),
        "internal_knowledge_followup_tasks": metrics.get("internal_knowledge_followup_tasks"),
        "duplicate_task_count": metrics.get("duplicate_task_count"),
        "resolved_gap_count": resolved_items,
        "unresolved_gap_count": max(0, review_found - resolved_items),
        "gap_resolution_rate": round(resolved_items / review_found, 4) if review_found else None,
        "max_research_round_reached": bool(metrics.get("max_research_round_reached")),
        "method_note": "Missing dimensions are screened from first-round role reports; gap matching is lexical and should be read with the raw review/tasks.",
    }


def evaluate_run(run: Mapping[str, Any], case: Mapping[str, Any], rubric: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one raw run while retaining the raw run identity."""
    report = str(run.get("final_report") or "")
    coverage = coverage_metric(report, case)
    sources = source_diversity(run, rubric)
    unsupported = unsupported_claim_metric(report)
    conflict = conflict_metric(run, rubric)
    quality = quality_scores(report, coverage, sources, unsupported, conflict, rubric)
    return {
        "case_id": run.get("case_id"),
        "repeat_index": run.get("repeat_index"),
        "status": run.get("status"),
        "commit": run.get("commit"),
        "metrics": run.get("metrics", {}),
        "evidence_coverage": coverage,
        "source_diversity": sources,
        "unsupported_claim_rate": unsupported,
        "conflict_detection": conflict,
        "quality": quality,
        "dynamic": dynamic_metrics(run, case),
    }


def _numeric(values: list[Any]) -> list[float]:
    return [float(value) for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]


def aggregate_runs(evaluated_runs: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate numeric observations with explicit N/A omission."""
    metric_names = {
        key
        for item in evaluated_runs
        for key, value in (item.get("metrics", {}) or {}).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    metrics: dict[str, Any] = {}
    for name in sorted(metric_names):
        values = _numeric([item.get("metrics", {}).get(name) for item in evaluated_runs])
        if values:
            metrics[name] = {
                "mean": round(statistics.mean(values), 4),
                "median": round(statistics.median(values), 4),
                "total": round(sum(values), 4),
                "n": len(values),
            }
    quality_names = [
        "fact_completeness",
        "evidence_sufficiency",
        "risk_judgment_reasonableness",
        "uncertainty_expression",
        "source_traceability",
    ]
    quality: dict[str, Any] = {}
    for name in [*quality_names, "final_quality"]:
        values = _numeric([
            item.get("quality", {}).get("scores", {}).get(name)
            if name != "final_quality"
            else item.get("quality", {}).get("final_quality")
            for item in evaluated_runs
        ])
        if values:
            quality[name] = {"mean": round(statistics.mean(values), 4), "median": round(statistics.median(values), 4), "n": len(values)}
    coverage_values = _numeric([item.get("evidence_coverage", {}).get("percentage") for item in evaluated_runs])
    critical_values = _numeric([item.get("evidence_coverage", {}).get("critical", {}).get("percentage") for item in evaluated_runs])
    unsupported_values = _numeric([item.get("unsupported_claim_rate", {}).get("rate") for item in evaluated_runs])
    conflict_values = _numeric([item.get("conflict_detection", {}).get("score") for item in evaluated_runs])
    dynamic_names = [
        "gap_detection_recall",
        "gap_resolution_rate",
        "review_gap_count",
        "next_tasks_count",
        "public_signal_followup_tasks",
        "internal_knowledge_followup_tasks",
        "duplicate_task_count",
        "resolved_gap_count",
        "unresolved_gap_count",
    ]
    dynamic: dict[str, Any] = {}
    for name in dynamic_names:
        values = _numeric([item.get("dynamic", {}).get(name) for item in evaluated_runs])
        if values:
            dynamic[name] = {"mean": round(statistics.mean(values), 4), "median": round(statistics.median(values), 4), "total": round(sum(values), 4), "n": len(values)}
    return {
        "run_count": len(evaluated_runs),
        "successful_run_count": sum(item.get("status") == "success" for item in evaluated_runs),
        "status_counts": {
            status: sum(item.get("status") == status for item in evaluated_runs)
            for status in sorted({str(item.get("status")) for item in evaluated_runs})
        },
        "metrics": metrics,
        "quality": quality,
        "evidence_coverage_percentage": _aggregate_simple(coverage_values),
        "critical_evidence_coverage_percentage": _aggregate_simple(critical_values),
        "unsupported_claim_rate": _aggregate_simple(unsupported_values),
        "conflict_detection_rate": _aggregate_simple(conflict_values),
        "dynamic": dynamic,
    }


def _aggregate_simple(values: list[float]) -> dict[str, Any] | None:
    if not values:
        return None
    return {"mean": round(statistics.mean(values), 4), "median": round(statistics.median(values), 4), "n": len(values)}


def evaluate_file(results_path: Path, cases_path: Path, rubric_path: Path) -> dict[str, Any]:
    """Load a raw version artifact and return the machine-readable evaluation."""
    raw = load_json(results_path)
    cases = {str(case["case_id"]): case for case in load_cases(cases_path)}
    rubric = load_json(rubric_path)
    evaluated_runs: list[dict[str, Any]] = []
    missing_cases: list[str] = []
    for run in raw.get("runs", []):
        case_id = str(run.get("case_id"))
        case = cases.get(case_id)
        if case is None:
            missing_cases.append(case_id)
            continue
        evaluated_runs.append(evaluate_run(run, case, rubric))
    return {
        "schema_version": "1.0",
        "artifact": "plan_execute_ab_evaluation",
        "version": raw.get("version"),
        "commit": raw.get("commit"),
        "commit_matches_expected": raw.get("commit_matches_expected"),
        "benchmark_started_at_utc": raw.get("benchmark_started_at_utc"),
        "benchmark_finished_at_utc": raw.get("benchmark_finished_at_utc"),
        "cases_path": str(cases_path),
        "rubric_path": str(rubric_path),
        "repeat_count": raw.get("repeat_count"),
        "configuration_template": raw.get("configuration_template"),
        "environment": raw.get("environment"),
        "runs": evaluated_runs,
        "aggregate": aggregate_runs(evaluated_runs),
        "missing_case_ids": missing_cases,
        "raw_status": raw.get("status", "partial"),
    }


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Local copy of JSONL loading to keep this script directly runnable."""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("cases.jsonl"))
    parser.add_argument("--rubric", type=Path, default=Path(__file__).with_name("rubric.json"))
    parser.add_argument("--output", type=Path)
    parsed = parser.parse_args()
    evaluated = evaluate_file(parsed.results, parsed.cases, parsed.rubric)
    output = parsed.output or parsed.results.with_name(parsed.results.stem.replace("_results", "_evaluated") + ".json")
    output.write_text(json.dumps(evaluated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
