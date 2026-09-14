"""Align evaluated Before/After runs and generate Markdown artifacts."""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


METRIC_NAMES = [
    ("duration_ms", "Duration (ms)"),
    ("node_executions", "Node executions"),
    ("agent_executions", "Agent executions"),
    ("model_calls", "Model calls"),
    ("tool_calls", "Tool calls"),
    ("web_search_calls", "Web/Search calls"),
    ("rag_calls", "RAG calls"),
    ("mcp_calls", "MCP calls"),
    ("social_media_calls", "Social-media calls"),
    ("public_signal_executions", "Public Signal executions"),
    ("internal_knowledge_executions", "Internal Knowledge executions"),
    ("risk_assessment_executions", "Risk Assessment executions"),
    ("response_strategy_executions", "Response Strategy executions"),
    ("research_review_executions", "Research Review executions"),
    ("research_rounds", "Research rounds"),
    ("followup_rounds", "Follow-up rounds"),
    ("input_tokens", "Input tokens"),
    ("output_tokens", "Output tokens"),
    ("total_tokens", "Total tokens"),
    ("final_report_chars", "Final report characters"),
]
QUALITY_METRICS = [
    ("evidence_coverage_percentage", "Evidence Coverage (%)", "pp"),
    ("critical_evidence_coverage_percentage", "Critical Evidence Coverage (%)", "pp"),
    ("unsupported_claim_rate", "Unsupported Claim Rate", "rate"),
    ("conflict_detection_rate", "Conflict Detection", "pp"),
    ("final_quality", "Final Research Quality (1-5)", "score"),
]


def load_json(path: Path) -> Any:
    """Read one JSON artifact."""
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    """Write one stable JSON artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fmt(value: Any, digits: int = 2) -> str:
    """Format a value for a report without turning missing data into zero."""
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        if math.isnan(value):
            return "N/A"
        return f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return str(value)


def _mean_metric(evaluated: Mapping[str, Any], name: str) -> float | None:
    aggregate = evaluated.get("aggregate", {})
    if name == "final_quality":
        return _as_float((aggregate.get("quality", {}).get("final_quality") or {}).get("mean"))
    if name == "evidence_coverage_percentage":
        return _as_float((aggregate.get("evidence_coverage_percentage") or {}).get("mean"))
    if name == "critical_evidence_coverage_percentage":
        return _as_float((aggregate.get("critical_evidence_coverage_percentage") or {}).get("mean"))
    if name == "unsupported_claim_rate":
        return _as_float((aggregate.get("unsupported_claim_rate") or {}).get("mean"))
    if name == "conflict_detection_rate":
        return _as_float((aggregate.get("conflict_detection_rate") or {}).get("mean"))
    return _as_float((aggregate.get("metrics", {}).get(name) or {}).get("mean"))


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _delta(before: float | None, after: float | None, kind: str = "number") -> str:
    if before is None or after is None:
        return "N/A"
    absolute = after - before
    if kind == "pp":
        return f"{absolute:+.2f} pp"
    if kind == "rate":
        return f"{absolute:+.4f} ({absolute * 100:+.2f} pp)"
    if kind == "score":
        return f"{absolute:+.2f}"
    relative = "N/A" if before == 0 else f"{absolute / before * 100:+.1f}%"
    return f"{absolute:+.2f} ({relative})"


def _aggregate_cell(evaluated: Mapping[str, Any], metric: str, statistic: str) -> Any:
    values = (evaluated.get("aggregate", {}).get("metrics", {}).get(metric) or {})
    return values.get(statistic)


def _run_map(raw: Mapping[str, Any]) -> dict[tuple[str, int], Mapping[str, Any]]:
    return {
        (str(run.get("case_id")), int(run.get("repeat_index", 1))): run
        for run in raw.get("runs", [])
    }


def _eval_map(evaluated: Mapping[str, Any]) -> dict[tuple[str, int], Mapping[str, Any]]:
    return {
        (str(run.get("case_id")), int(run.get("repeat_index", 1))): run
        for run in evaluated.get("runs", [])
    }


def _unique_run_for_case(runs: Mapping[tuple[str, int], Mapping[str, Any]], case_id: str) -> Mapping[str, Any] | None:
    choices = [value for (identifier, _), value in runs.items() if identifier == case_id]
    return choices[0] if choices else None


def _flow(run: Mapping[str, Any] | None) -> str:
    if not run:
        return "N/A — run missing"
    metrics = run.get("metrics", {})
    sequence = metrics.get("node_sequence") or []
    if sequence:
        return " → ".join(str(item) for item in sequence)
    updates = run.get("stream_updates") or []
    nodes = [str(item.get("node")) for item in updates if item.get("node")]
    return " → ".join(dict.fromkeys(nodes)) if nodes else "N/A — no node events recorded"


def _review_rows(run: Mapping[str, Any] | None) -> list[str]:
    if not run:
        return ["N/A — run missing"]
    reviews = run.get("research_reviews") or []
    if not reviews:
        return ["No Research Review emitted (expected for Before; verify raw events)."]
    rows = []
    for index, review in enumerate(reviews, 1):
        gaps = review.get("research_gaps") or []
        tasks = review.get("next_tasks") or []
        conflicts = review.get("conflicts") or []
        gap_text = "; ".join(str(item) for item in gaps) or "—"
        task_text = "; ".join(
            f"{item.get('task_id', 'task')} → {item.get('target_role', 'unknown')}: {item.get('objective', '')}"
            for item in tasks
            if isinstance(item, Mapping)
        ) or "—"
        rows.append(
            f"| {index} | {fmt(review.get('research_complete'))} | {gap_text} | {task_text} | "
            f"{'; '.join(str(item) for item in conflicts) or '—'} |"
        )
    return rows


def _followup_evidence(run: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not run:
        return []
    return [
        item
        for item in (run.get("round_reports") or [])
        if item.get("mode") == "followup" or (isinstance(item.get("round"), int) and item.get("round") > 1)
    ]


def _tool_repeat_candidates(run: Mapping[str, Any] | None) -> list[str]:
    """Find exact repeated query strings; do not label them semantically useless."""
    if not run:
        return []
    seen: Counter[str] = Counter()
    for record in run.get("tool_records", []) or []:
        if not isinstance(record, Mapping):
            continue
        if record.get("tool") not in {"web_search", "search_posts", "search_complaints", "rag_search"}:
            continue
        args = record.get("args")
        if not isinstance(args, Mapping):
            continue
        for key in ("query", "queries"):
            value = args.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        seen[item.strip()] += 1
            elif isinstance(value, str) and value.strip():
                seen[value.strip()] += 1
    return [query for query, count in seen.items() if count > 1]


def _case_markdown(
    case: Mapping[str, Any],
    before_raw: Mapping[str, Any] | None,
    after_raw: Mapping[str, Any] | None,
    before_eval: Mapping[str, Any] | None,
    after_eval: Mapping[str, Any] | None,
) -> str:
    """Render the evidence-oriented per-case comparison."""
    case_id = str(case.get("case_id"))
    before_metrics = (before_raw or {}).get("metrics", {})
    after_metrics = (after_raw or {}).get("metrics", {})
    before_dynamic = (before_eval or {}).get("dynamic", {})
    after_dynamic = (after_eval or {}).get("dynamic", {})
    before_quality = (before_eval or {}).get("quality", {})
    after_quality = (after_eval or {}).get("quality", {})
    before_sources = (before_eval or {}).get("source_diversity", {})
    after_sources = (after_eval or {}).get("source_diversity", {})
    after_followups = _followup_evidence(after_raw)
    followup_text = "\n\n".join(
        f"### {item.get('role', 'unknown')} / round {item.get('round', 'N/A')}\n\n{item.get('report', '').strip()}"
        for item in after_followups
    ) or "No follow-up role report was recorded."
    before_report = str((before_raw or {}).get("final_report") or "")
    after_report = str((after_raw or {}).get("final_report") or "")
    extra_after = sorted(set(after_sources.get("unique_urls", [])) - set(before_sources.get("unique_urls", [])))
    if after_dynamic.get("review_triggered_followup") and after_followups:
        followup_conclusion = "是：After 记录了非初始轮次的角色报告；请结合下方原文判断新增证据是否改变风险判断。"
    elif after_dynamic.get("review_triggered_followup"):
        followup_conclusion = "Review 声称需要补查，但未恢复出 follow-up 角色报告；应视为观测异常，而不是质量提升。"
    else:
        followup_conclusion = "否：本 Case 没有观察到带任务的动态补查。"

    execution_keys = [name for name, _ in METRIC_NAMES if name in before_metrics or name in after_metrics]
    execution_rows = []
    for name, label in METRIC_NAMES:
        if name in execution_keys:
            execution_rows.append(f"| {label} | {fmt(before_metrics.get(name))} | {fmt(after_metrics.get(name))} | {_delta(_as_float(before_metrics.get(name)), _as_float(after_metrics.get(name)))} |")
    quality_rows = []
    for label, metric_key, before_value, after_value, kind in [
        ("Evidence Coverage", "percentage", (before_eval or {}).get("evidence_coverage", {}).get("percentage"), (after_eval or {}).get("evidence_coverage", {}).get("percentage"), "pp"),
        ("Critical Evidence Coverage", "critical", (before_eval or {}).get("evidence_coverage", {}).get("critical", {}).get("percentage"), (after_eval or {}).get("evidence_coverage", {}).get("critical", {}).get("percentage"), "pp"),
        ("Unsupported Claim Rate", "rate", (before_eval or {}).get("unsupported_claim_rate", {}).get("rate"), (after_eval or {}).get("unsupported_claim_rate", {}).get("rate"), "rate"),
        ("Conflict Detection", "score", (before_eval or {}).get("conflict_detection", {}).get("score"), (after_eval or {}).get("conflict_detection", {}).get("score"), "pp"),
        ("Final Quality", "quality", (before_quality or {}).get("final_quality"), (after_quality or {}).get("final_quality"), "score"),
    ]:
        quality_rows.append(f"| {label} | {fmt(before_value)} | {fmt(after_value)} | {_delta(_as_float(before_value), _as_float(after_value), kind)} |")

    return f"""# {case_id}: {case.get('title', '')}

## Case 信息

- Category: {case.get('category', 'N/A')}
- Query: {case.get('query', '')}
- Required dimensions: {', '.join(str(item.get('label', item)) if isinstance(item, Mapping) else str(item) for item in case.get('required_dimensions', []))}
- Critical dimensions: {', '.join(str(item) for item in case.get('critical_dimensions', []))}
- Expected source types: {', '.join(str(item) for item in case.get('expected_source_types', []))}
- Before status/commit: {fmt((before_raw or {{}}).get('status'))} / {(before_raw or {{}}).get('commit', 'N/A')}
- After status/commit: {fmt((after_raw or {{}}).get('status'))} / {(after_raw or {{}}).get('commit', 'N/A')}

## Before 实际执行链路

`{_flow(before_raw)}`

Observer node sequence and model/tool counts are stored in the raw artifact. No Research Review node is expected in the Before commit.

## After 实际执行链路

`{_flow(after_raw)}`

The sequence above is reconstructed from Agent Observer `span_started` events, with stream updates as a fallback.

## After Research Review 判断

| Review | research_complete | Research gaps | Generated tasks | Conflicts |
|---:|---|---|---|---|
{chr(10).join(_review_rows(after_raw))}

## 发现的 Research Gaps

{chr(10).join(f'- {text}' for review in (after_raw or {{}}).get('research_reviews', []) for text in (review.get('research_gaps') or [])) or '- None recorded.'}

## Follow-up Tasks

{chr(10).join(f"- `{task.get('task_id', 'N/A')}` → **{task.get('target_role', 'N/A')}**: {task.get('objective', '')}；evidence needed: {task.get('evidence_needed', '')}; reason: {task.get('reason', '')}" for review in (after_raw or {{}}).get('research_reviews', []) for task in (review.get('next_tasks') or []) if isinstance(task, Mapping)) or '- None recorded.'}

## Follow-up 新增证据

{followup_text}

Captured exact repeated query candidates (not automatically judged meaningless): {', '.join(_tool_repeat_candidates(after_raw)) or 'none'}.

## Before / After 最终研究差异

- Dynamic follow-up conclusion: {followup_conclusion}
- New URL candidates visible in After evaluation: {', '.join(extra_after) or 'none or not measurable'}
- Before final report characters: {fmt(before_metrics.get('final_report_chars'))}; After: {fmt(after_metrics.get('final_report_chars'))}
- Before final report excerpt: {before_report[:1_000] or 'N/A'}
- After final report excerpt: {after_report[:1_000] or 'N/A'}

## 执行指标

| Metric | Before | After | Delta |
|---|---:|---:|---:|
{chr(10).join(execution_rows) or '| No metrics | N/A | N/A | N/A |'}

## 研究质量指标

| Metric | Before | After | Delta |
|---|---:|---:|---:|
{chr(10).join(quality_rows)}

### Method note

Coverage, unsupported claims, conflict detection, and 1–5 quality scores use the fixed lexical/deterministic rubric in `rubric.json`; they are not an LLM judge and require human review for release decisions.

## Case 结论

This case does not by itself determine the architecture decision. The important evidence is whether the After review created valid role-specific tasks and whether the follow-up report added source-backed content that was absent from the Before run. In this case: **{followup_conclusion}**
"""


def _architecture_section() -> str:
    return """## B. 架构流程对比

Before：

`Initial Research → Risk Assessment → Response Strategy`

After：

`Initial Research → Research Review → (Gap → Follow-up → Research Review)* → Risk Assessment → Response Strategy`

The node-level records below are observed executions, not an assumed diagram.
"""


def _cost_table(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    lines = [
        "| Metric | Before mean | After mean | Δ mean | Before median | After median | Δ median | Before total | After total | Δ total |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, label in METRIC_NAMES:
        before_mean = _aggregate_cell(before, key, "mean")
        after_mean = _aggregate_cell(after, key, "mean")
        before_median = _aggregate_cell(before, key, "median")
        after_median = _aggregate_cell(after, key, "median")
        before_total = _aggregate_cell(before, key, "total")
        after_total = _aggregate_cell(after, key, "total")
        if all(value is None for value in (before_mean, after_mean, before_median, after_median, before_total, after_total)):
            continue
        lines.append(
            f"| {label} | {fmt(before_mean)} | {fmt(after_mean)} | {_delta(_as_float(before_mean), _as_float(after_mean))} | "
            f"{fmt(before_median)} | {fmt(after_median)} | {_delta(_as_float(before_median), _as_float(after_median))} | "
            f"{fmt(before_total)} | {fmt(after_total)} | {_delta(_as_float(before_total), _as_float(after_total))} |"
        )
    return "\n".join(lines)


def _quality_table(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    lines = ["| Metric | Before | After | Delta |", "|---|---:|---:|---:|"]
    for key, label, kind in QUALITY_METRICS:
        before_value = _mean_metric(before, key)
        after_value = _mean_metric(after, key)
        lines.append(f"| {label} | {fmt(before_value)} | {fmt(after_value)} | {_delta(before_value, after_value, kind)} |")
    return "\n".join(lines)


def _mean_dynamic(evaluated: Mapping[str, Any], name: str) -> float | None:
    return _as_float(((evaluated.get("aggregate", {}).get("dynamic", {}) or {}).get(name) or {}).get("mean"))


def _sum_dynamic(evaluated: Mapping[str, Any], name: str) -> float | None:
    return _as_float(((evaluated.get("aggregate", {}).get("dynamic", {}) or {}).get(name) or {}).get("total"))


def _dynamic_analysis(
    cases: list[Mapping[str, Any]],
    after_eval: Mapping[str, Any],
    after_raw: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    eval_runs = [item for item in after_eval.get("runs", []) if isinstance(item, Mapping)]
    first_round = sum(bool(item.get("dynamic", {}).get("first_round_complete")) for item in eval_runs)
    round_two = sum(bool(item.get("dynamic", {}).get("review_triggered_followup")) for item in eval_runs)
    review_gap_values = [item.get("dynamic", {}).get("review_gap_count") for item in eval_runs]
    recall_values = [item.get("dynamic", {}).get("gap_detection_recall") for item in eval_runs]
    resolution_values = [item.get("dynamic", {}).get("gap_resolution_rate") for item in eval_runs]
    recall_numeric = [float(item) for item in recall_values if isinstance(item, (int, float))]
    resolution_numeric = [float(item) for item in resolution_values if isinstance(item, (int, float))]
    public_tasks = sum((item.get("dynamic", {}).get("public_signal_followup_tasks") or 0) for item in eval_runs)
    internal_tasks = sum((item.get("dynamic", {}).get("internal_knowledge_followup_tasks") or 0) for item in eval_runs)
    max_round = sum(bool(item.get("dynamic", {}).get("max_research_round_reached")) for item in eval_runs)
    duplicate_tasks = sum((item.get("dynamic", {}).get("duplicate_task_count") or 0) for item in eval_runs)
    repeated_searches = []
    for run in after_raw.get("runs", []) or []:
        repeated_searches.extend(_tool_repeat_candidates(run))

    case_by_id = {str(item.get("case_id")): item for item in cases}
    gap_labels: Counter[str] = Counter()
    for evaluated_run in eval_runs:
        case = case_by_id.get(str(evaluated_run.get("case_id")), {})
        dimensions = [item for item in case.get("required_dimensions", []) if isinstance(item, Mapping)]
        for gap in evaluated_run.get("dynamic", {}).get("research_gap_texts", []) or []:
            lowered = str(gap).casefold()
            labels = [
                str(item.get("label", item.get("id")))
                for item in dimensions
                if any(str(keyword).casefold() in lowered for keyword in item.get("keywords", []))
            ]
            if labels:
                gap_labels.update(labels)
            else:
                gap_labels["unmapped gap text"] += 1

    analysis = {
        "case_count": len(eval_runs),
        "first_round_complete_cases": first_round,
        "followup_round_2_cases": round_two,
        "average_research_gaps": round(statistics.mean([float(value) for value in review_gap_values if isinstance(value, (int, float))]), 4) if any(isinstance(value, (int, float)) for value in review_gap_values) else None,
        "gap_detection_recall_mean": round(statistics.mean(recall_numeric), 4) if recall_numeric else None,
        "gap_detection_recall_defined_cases": len(recall_numeric),
        "gap_resolution_rate_mean": round(statistics.mean(resolution_numeric), 4) if resolution_numeric else None,
        "gap_resolution_rate_defined_cases": len(resolution_numeric),
        "public_signal_followup_tasks": public_tasks,
        "internal_knowledge_followup_tasks": internal_tasks,
        "duplicate_task_count": duplicate_tasks,
        "max_research_round_cases": max_round,
        "exact_repeated_query_candidates": sorted(set(repeated_searches)),
        "common_gap_types": [{"label": label, "count": count} for label, count in gap_labels.most_common(10)],
    }
    text = f"""## F. Dynamic Loop 专项分析

- 第一轮直接完成：**{first_round} / {len(eval_runs)} Case**。
- 进入 Round 2：**{round_two} / {len(eval_runs)} Case**。
- 平均 Research Gap 数量：**{fmt(analysis['average_research_gaps'])}**（按记录到的 gap 文本计）。
- Gap Detection Recall：**{fmt(analysis['gap_detection_recall_mean'])}**（{len(recall_numeric)} 个有可定义分母的 Case；初始缺口和 Review 映射均为词法筛查）。
- Gap Resolution Rate：**{fmt(analysis['gap_resolution_rate_mean'])}**（{len(resolution_numeric)} 个有 Review gap 的 Case）。
- Public Signal follow-up tasks：**{public_tasks}**；Internal Knowledge follow-up tasks：**{internal_tasks}**。
- 重复任务数：**{duplicate_tasks}**。
- 达到 `max_research_rounds` 后仍未完成的 Case：**{max_round}**。
- 是否出现无意义重复搜索：**无法仅凭工具名判定**；Benchmark 记录了 exact query candidates，候选数量为 **{len(set(repeated_searches))}**。它们可能是有意的交叉验证，需人工看逐 Case 原文。

常见 Gap 类型（词法映射）：{'; '.join(f"{item['label']} ({item['count']})" for item in analysis['common_gap_types']) or 'N/A'}。
"""
    return analysis, text


def _decision(before_eval: Mapping[str, Any], after_eval: Mapping[str, Any], dynamic: Mapping[str, Any]) -> tuple[str, str]:
    coverage_delta = _mean_metric(after_eval, "evidence_coverage_percentage")
    before_coverage = _mean_metric(before_eval, "evidence_coverage_percentage")
    critical_delta = _mean_metric(after_eval, "critical_evidence_coverage_percentage")
    before_critical = _mean_metric(before_eval, "critical_evidence_coverage_percentage")
    quality_delta = _mean_metric(after_eval, "final_quality")
    before_quality = _mean_metric(before_eval, "final_quality")
    unsupported_delta = _mean_metric(after_eval, "unsupported_claim_rate")
    before_unsupported = _mean_metric(before_eval, "unsupported_claim_rate")
    coverage_pp = (coverage_delta - before_coverage) if coverage_delta is not None and before_coverage is not None else 0
    critical_pp = (critical_delta - before_critical) if critical_delta is not None and before_critical is not None else 0
    quality_change = (quality_delta - before_quality) if quality_delta is not None and before_quality is not None else 0
    unsupported_change = (unsupported_delta - before_unsupported) if unsupported_delta is not None and before_unsupported is not None else 0
    after_cost = _aggregate_cell(after_eval, "model_calls", "mean")
    before_cost = _aggregate_cell(before_eval, "model_calls", "mean")
    cost_growth = (after_cost / before_cost - 1) if after_cost is not None and before_cost not in (None, 0) else 0

    improvement = coverage_pp >= 5 or critical_pp >= 5 or quality_change >= 0.2 or unsupported_change <= -0.05
    material_cost = cost_growth > 0.25
    unresolved = sum(
        int(item.get("dynamic", {}).get("unresolved_gap_count") or 0)
        for item in after_eval.get("runs", [])
        if isinstance(item, Mapping)
    ) > 0
    if improvement:
        decision = "KEEP_WITH_CHANGES" if material_cost or unresolved or dynamic.get("duplicate_task_count", 0) else "KEEP"
        reason = (
            f"After shows measurable quality movement (coverage {coverage_pp:+.2f} pp, critical coverage {critical_pp:+.2f} pp, "
            f"quality {quality_change:+.2f}, unsupported-claim rate {unsupported_change:+.4f}). "
            + (f"The observed model-call mean grew {cost_growth * 100:+.1f}%, so cost/termination safeguards need follow-up. " if material_cost else "")
            + ("Some review gaps remained unresolved, so the loop should be retained with changes. " if unresolved else "")
            + "This is a data-backed conditional decision, not an assumption that the newer architecture is better."
        )
    else:
        decision = "ROLLBACK"
        reason = (
            f"The benchmark did not show a material quality improvement (coverage {coverage_pp:+.2f} pp, critical coverage {critical_pp:+.2f} pp, "
            f"quality {quality_change:+.2f}, unsupported-claim rate {unsupported_change:+.4f}); the additional loop cost is not justified by this test set."
        )
    return decision, reason


def _report(
    *,
    cases: list[Mapping[str, Any]],
    before_raw: Mapping[str, Any],
    after_raw: Mapping[str, Any],
    before_eval: Mapping[str, Any],
    after_eval: Mapping[str, Any],
    dynamic: Mapping[str, Any],
    dynamic_text: str,
    decision: str,
    decision_reason: str,
    benchmark_started_at: str,
) -> str:
    before_cfg = before_raw.get("configuration_template") or {}
    after_cfg = after_raw.get("configuration_template") or {}
    case_rows = []
    before_runs = _run_map(before_raw)
    after_runs = _run_map(after_raw)
    before_eval_runs = _eval_map(before_eval)
    after_eval_runs = _eval_map(after_eval)
    for case in cases:
        case_id = str(case.get("case_id"))
        after_run = _unique_run_for_case(after_runs, case_id)
        after_eval_run = _unique_run_for_case(after_eval_runs, case_id)
        after_dynamic = (after_eval_run or {}).get("dynamic", {})
        followup = "Yes" if after_dynamic.get("review_triggered_followup") else "No"
        task_text = "; ".join(
            f"{task.get('target_role')}: {task.get('objective', '')[:90]}"
            for review in (after_run or {}).get("research_reviews", [])
            for task in (review.get("next_tasks") or [])
            if isinstance(task, Mapping)
        ) or "—"
        case_rows.append(f"| {case_id} | `{_flow(_unique_run_for_case(before_runs, case_id))}` | {fmt(after_dynamic.get('followup_rounds'), 0)} | {followup} | {task_text} |")

    limitations = [
        "One run per Case was executed (`repeat_count=1`) because this benchmark was run as a cost-controlled first pass; no variance estimate is claimed.",
        "Tavily Web results are live and Before/After were run in two continuous version blocks; result drift is a limitation.",
        "Social-media calls used the repository's fixed JSON fixtures because the configured API at 127.0.0.1:9000 was not running. The Observer sidecar at 127.0.0.1:8766 was also not running; the installed Agent Observer v0.2 SDK and LangGraph adapter recorded events in-process.",
        "Token metrics are N/A when any model response omitted usage metadata; final-report token counts are N/A because no tokenizer was introduced by the harness.",
        "Quality and gap metrics use the fixed deterministic rubric and lexical matching, not a blinded LLM judge. Raw reports and task text are retained for expert review.",
        "No record/replay layer was added, and no core logic, prompt, tool whitelist, RAG algorithm, MCP configuration, or dynamic-loop decision rule was changed.",
    ]
    return f"""# Dynamic Research Plan-and-Execute Before / After Benchmark

## A. Benchmark 基本信息

| Item | Value |
|---|---|
| Before commit | `{before_raw.get('commit', 'N/A')}` ({before_raw.get('commit_subject', 'N/A')}) |
| After commit | `{after_raw.get('commit', 'N/A')}` ({after_raw.get('commit_subject', 'N/A')}) |
| Commit check | Before={fmt(before_raw.get('commit_matches_expected'))}; After={fmt(after_raw.get('commit_matches_expected'))} |
| Case count | {len(cases)} configured; Before runs={before_eval.get('aggregate', {}).get('run_count', 0)}; After runs={after_eval.get('aggregate', {}).get('run_count', 0)} |
| Repeat count | Before={before_raw.get('repeat_count', 'N/A')}; After={after_raw.get('repeat_count', 'N/A')} |
| Model | research/summarization/compression/final: `{after_cfg.get('research_model', 'N/A')}` / `{after_cfg.get('summarization_model', 'N/A')}` / `{after_cfg.get('compression_model', 'N/A')}` / `{after_cfg.get('final_report_model', 'N/A')}` |
| Generation parameters | research max tokens={after_cfg.get('research_model_max_tokens', 'N/A')}; compression={after_cfg.get('compression_model_max_tokens', 'N/A')}; final={after_cfg.get('final_report_model_max_tokens', 'N/A')}; temperature={after_cfg.get('temperature', 'N/A')} |
| Other controls | max_react_tool_calls={after_cfg.get('max_react_tool_calls', 'N/A')}; max_research_rounds={after_cfg.get('max_research_rounds', 'N/A')}; structured-output retries={after_cfg.get('max_structured_output_retries', 'N/A')}; clarification={after_cfg.get('allow_clarification', 'N/A')} |
| Search/tool config | Search API=`{after_cfg.get('search_api', 'N/A')}`; social=`{after_cfg.get('social_media_mode', 'N/A')}`; MCP calls are observed, not added |
| RAG config | enabled={after_cfg.get('rag_enabled', 'N/A')}; mode={after_cfg.get('retrieval_mode', 'N/A')}; embedding={after_cfg.get('rag_embedding_provider', 'N/A')}; vectorstore={after_cfg.get('rag_vectorstore_provider', 'N/A')}; reranker={after_cfg.get('rag_reranker_provider', 'N/A')}; graph={after_cfg.get('rag_graph_enabled', 'N/A')}; query rewrite={after_cfg.get('rag_query_rewrite_enabled', 'N/A')} |
| Observer config | {after_cfg.get('observer_mode', 'N/A')}; project=`{after_cfg.get('agent_observer_project', 'N/A')}` |
| Benchmark time | started={benchmark_started_at}; Before finished={before_raw.get('benchmark_finished_at_utc', 'N/A')}; After finished={after_raw.get('benchmark_finished_at_utc', 'N/A')} |

Both versions received the same benchmark configuration and the same case order. The only intended variable is the committed code at the two exact SHAs.

{_architecture_section()}

## C. 每个 Case 的流程摘要

| Case | Before 流程 | After rounds | Review 是否触发补查 | 主要补查内容 |
|---|---|---:|---|---|
{chr(10).join(case_rows)}

## D. 执行成本对比

The table provides mean, median, and total. `N/A` is preserved when the observer or provider did not expose a reliable value.

{_cost_table(before_eval.get('aggregate', {}), after_eval.get('aggregate', {}))}

## E. 研究质量对比

{_quality_table(before_eval, after_eval)}

Coverage is calculated against each Case's `required_dimensions` and `critical_dimensions`. Unsupported Claim Rate is lower-is-better. Conflict Detection is the share of runs with an explicit cross-source comparison marker.

{dynamic_text}

## G. 成本收益分析

- Model calls: **{_delta(_mean_metric(before_eval, 'model_calls'), _mean_metric(after_eval, 'model_calls'))}** per Case mean (when exposed).
- Tool calls: **{_delta(_mean_metric(before_eval, 'tool_calls'), _mean_metric(after_eval, 'tool_calls'))}** per Case mean (when exposed).
- Total tokens: **{_delta(_mean_metric(before_eval, 'total_tokens'), _mean_metric(after_eval, 'total_tokens'))}** per Case mean; N/A is not treated as zero.
- Duration: **{_delta(_mean_metric(before_eval, 'duration_ms'), _mean_metric(after_eval, 'duration_ms'))}** per Case mean.
- Evidence Coverage: **{_delta(_mean_metric(before_eval, 'evidence_coverage_percentage'), _mean_metric(after_eval, 'evidence_coverage_percentage'), 'pp')}**.
- Critical Evidence Coverage: **{_delta(_mean_metric(before_eval, 'critical_evidence_coverage_percentage'), _mean_metric(after_eval, 'critical_evidence_coverage_percentage'), 'pp')}**.
- Unsupported Claim Rate: **{_delta(_mean_metric(before_eval, 'unsupported_claim_rate'), _mean_metric(after_eval, 'unsupported_claim_rate'), 'rate')}** (negative is an improvement).
- Conflict Detection: **{_delta(_mean_metric(before_eval, 'conflict_detection_rate'), _mean_metric(after_eval, 'conflict_detection_rate'), 'pp')}**.
- Final Quality: **{_delta(_mean_metric(before_eval, 'final_quality'), _mean_metric(after_eval, 'final_quality'), 'score')}** on the fixed 1–5 heuristic.

The net value of the loop is therefore judged on evidence and quality movement, not token/duration reduction alone.

## H. 最终结论

**{decision}**

{decision_reason}

This decision is provisional to this 10-Case, single-repeat benchmark. A second repeat and a blinded expert/LLM judge are recommended before a production migration decision.

## Limitations and anomalies

{chr(10).join(f'- {item}' for item in limitations)}

If a Case failed, its exact error and traceback are in the corresponding raw JSON; no missing run was replaced with synthetic data.

## Artifact index

- `before_results.json` — raw Before stream, Observer events, tool records, reports, and metrics.
- `after_results.json` — raw After stream, Observer events, tool records, reports, Research Reviews, tasks, and metrics.
- `before_evaluated.json` / `after_evaluated.json` — per-run rubric output and aggregate statistics.
- `comparison.json` — stable machine-readable aligned comparison.
- `cases/case_*.md` — one evidence-oriented flow comparison per Case.
- `before_results.worker.log` / `after_results.worker.log` — worker stdout/stderr and any runtime warnings.
"""


def compare_files(
    *,
    before_path: Path,
    after_path: Path,
    before_eval_path: Path,
    after_eval_path: Path,
    cases_path: Path,
    rubric_path: Path,
    results_dir: Path,
    benchmark_started_at: str,
) -> dict[str, Any]:
    """Generate per-case reports, the summary Markdown, and a JSON-ready comparison."""
    before_raw = load_json(before_path)
    after_raw = load_json(after_path)
    before_eval = load_json(before_eval_path)
    after_eval = load_json(after_eval_path)
    cases = [
        json.loads(line)
        for line in cases_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    before_runs = _run_map(before_raw)
    after_runs = _run_map(after_raw)
    before_evals = _eval_map(before_eval)
    after_evals = _eval_map(after_eval)
    case_comparisons: list[dict[str, Any]] = []
    cases_dir = results_dir / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)
    for case in cases:
        case_id = str(case["case_id"])
        key = (case_id, 1)
        before_run = before_runs.get(key) or _unique_run_for_case(before_runs, case_id)
        after_run = after_runs.get(key) or _unique_run_for_case(after_runs, case_id)
        before_eval_run = before_evals.get(key) or _unique_run_for_case(before_evals, case_id)
        after_eval_run = after_evals.get(key) or _unique_run_for_case(after_evals, case_id)
        case_comparisons.append({
            "case_id": case_id,
            "before": {
                "status": (before_run or {}).get("status"),
                "metrics": (before_run or {}).get("metrics", {}),
                "quality": before_eval_run,
            },
            "after": {
                "status": (after_run or {}).get("status"),
                "metrics": (after_run or {}).get("metrics", {}),
                "quality": after_eval_run,
            },
        })
        (cases_dir / f"{case_id}.md").write_text(
            _case_markdown(case, before_run, after_run, before_eval_run, after_eval_run),
            encoding="utf-8",
        )
    dynamic, dynamic_text = _dynamic_analysis(cases, after_eval, after_raw)
    decision, decision_reason = _decision(before_eval, after_eval, dynamic)
    report = _report(
        cases=cases,
        before_raw=before_raw,
        after_raw=after_raw,
        before_eval=before_eval,
        after_eval=after_eval,
        dynamic=dynamic,
        dynamic_text=dynamic_text,
        decision=decision,
        decision_reason=decision_reason,
        benchmark_started_at=benchmark_started_at,
    )
    (results_dir / "PLAN_EXECUTE_AB_COMPARISON.md").write_text(report, encoding="utf-8")
    comparison = {
        "schema_version": "1.0",
        "artifact": "plan_execute_ab_comparison",
        "benchmark_started_at_utc": benchmark_started_at,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "before": {
            "commit": before_raw.get("commit"),
            "expected_commit": before_raw.get("expected_commit"),
            "run_count": before_eval.get("aggregate", {}).get("run_count"),
            "aggregate": before_eval.get("aggregate"),
            "configuration": before_raw.get("configuration_template"),
        },
        "after": {
            "commit": after_raw.get("commit"),
            "expected_commit": after_raw.get("expected_commit"),
            "run_count": after_eval.get("aggregate", {}).get("run_count"),
            "aggregate": after_eval.get("aggregate"),
            "configuration": after_raw.get("configuration_template"),
        },
        "decision": {"value": decision, "reason": decision_reason},
        "dynamic_analysis": dynamic,
        "cases": case_comparisons,
        "limitations": [
            "Single repeat per case.",
            "Live Tavily data can drift between sequential version blocks.",
            "Social adapter used fixed local JSON fixtures; Observer events were recorded in-process because sidecar port 8766 was unavailable.",
            "Quality/gap metrics are deterministic lexical screening, not expert or blinded LLM judgement.",
            "N/A is retained for unavailable token/final-tokenizer metrics.",
        ],
        "artifact_paths": {
            "before_results": str(before_path),
            "after_results": str(after_path),
            "before_evaluated": str(before_eval_path),
            "after_evaluated": str(after_eval_path),
            "comparison": str(results_dir / "comparison.json"),
            "report": str(results_dir / "PLAN_EXECUTE_AB_COMPARISON.md"),
            "cases": str(cases_dir),
            "rubric": str(rubric_path),
        },
    }
    return comparison


if __name__ == "__main__":
    raise SystemExit("Use run_benchmark.py to run and compare both committed versions.")
