"""Render single-case cost-profile Markdown artifacts from JSON."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    """Load one profile."""
    return json.loads(path.read_text(encoding="utf-8"))


def fmt(value: Any) -> str:
    """Format missing observations explicitly."""
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def delta(before: Any, after: Any) -> str:
    """Return an observed delta, preserving N/A."""
    if not isinstance(before, (int, float)) or isinstance(before, bool):
        return "N/A"
    if not isinstance(after, (int, float)) or isinstance(after, bool):
        return "N/A"
    return f"{after - before:+.2f}"


def rollup(rollup_value: Mapping[str, Any] | None, field: str) -> str:
    """Show exact token values or a partial known subtotal."""
    if not rollup_value:
        return "N/A"
    exact = rollup_value.get(field)
    if exact is not None:
        return fmt(exact)
    known_field = {
        "input_tokens": "known_input_tokens",
        "output_tokens": "known_output_tokens",
        "total_tokens": "known_total_tokens",
    }[field]
    known = rollup_value.get(known_field)
    return f"N/A (known {fmt(known)})" if known else "N/A"


def calls(profile: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [item for item in profile.get("model_calls", []) if isinstance(item, Mapping)]


def tools(profile: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [item for item in profile.get("tool_calls", []) if isinstance(item, Mapping)]


def stage_table(profile: Mapping[str, Any]) -> str:
    """Render model-call stages."""
    rows = [
        "| Stage | Model Calls | Input Tokens | Output Tokens | Total Tokens | Tool Calls | Model Duration ms |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in profile.get("flow", {}).get("stage_rows", []) or []:
        token_data = item.get("tokens") or {}
        rows.append(
            f"| {item.get('stage', 'N/A')} | {fmt(item.get('model_calls'))} | "
            f"{rollup(token_data, 'input_tokens')} | {rollup(token_data, 'output_tokens')} | "
            f"{rollup(token_data, 'total_tokens')} | {fmt(item.get('tool_calls'))} | "
            f"{fmt(item.get('model_duration_ms'))} |"
        )
    total = profile.get("flow", {}).get("total_tokens") or {}
    model_duration = sum(
        item.get("duration_ms", 0)
        for item in calls(profile)
        if isinstance(item.get("duration_ms"), int)
    )
    rows.append(
        f"| **TOTAL** | **{len(calls(profile))}** | **{rollup(total, 'input_tokens')}** | "
        f"**{rollup(total, 'output_tokens')}** | **{rollup(total, 'total_tokens')}** | "
        f"**{len(tools(profile))}** | **{model_duration}** |"
    )
    return "\n".join(rows)


def node_table(profile: Mapping[str, Any]) -> str:
    """Render graph-node aggregates."""
    rows = [
        "| Node | Executions | Model Calls | Input Tokens | Output Tokens | Total Tokens | Tool Calls | Duration ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    node_rows = profile.get("flow", {}).get("node_rows", []) or []
    if not node_rows:
        rows.append("| N/A — no completed node span | N/A | N/A | N/A | N/A | N/A | N/A | N/A |")
        return "\n".join(rows)
    for item in node_rows:
        token_data = item.get("tokens") or {}
        rows.append(
            f"| {item.get('node', 'N/A')} | {fmt(item.get('executions'))} | {fmt(item.get('model_calls'))} | "
            f"{rollup(token_data, 'input_tokens')} | {rollup(token_data, 'output_tokens')} | "
            f"{rollup(token_data, 'total_tokens')} | {fmt(item.get('tool_calls'))} | {fmt(item.get('duration_ms'))} |"
        )
    return "\n".join(rows)


def review_section(profile: Mapping[str, Any]) -> str:
    """Render Research Review decisions and follow-up task counts."""
    reviews = profile.get("research_reviews", []) or []
    if not reviews:
        return "No Research Review was emitted in this version."
    rows = [
        "| Review | research_complete | Gap count | next_tasks | Follow-up task details |",
        "|---:|---|---:|---:|---|",
    ]
    for item in reviews:
        tasks = item.get("next_tasks", []) or []
        task_text = "; ".join(
            f"{task.get('task_id', 'N/A')} -> {task.get('target_role', 'N/A')}: {str(task.get('objective', ''))[:260]}"
            for task in tasks
            if isinstance(task, Mapping)
        ) or "—"
        rows.append(
            f"| {fmt(item.get('review_index'))} | {fmt(item.get('research_complete'))} | "
            f"{len(item.get('research_gaps', []) or [])} | {len(tasks)} | {task_text.replace('|', '/')} |"
        )
    return "\n".join(rows)


def model_table(profile: Mapping[str, Any]) -> str:
    """Render one row for every observed model request."""
    rows = [
        "| # | call_id | graph_node | agent_role | round | call_type | model | input | output | total | duration ms | success |",
        "|---:|---|---|---|---:|---|---|---:|---:|---:|---:|---|",
    ]
    model_rows = calls(profile)
    if not model_rows:
        rows.append("| — | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |")
        return "\n".join(rows)
    for index, item in enumerate(model_rows, 1):
        rows.append(
            f"| {index} | {item.get('call_id', 'N/A')} | {item.get('graph_node', 'N/A')} | "
            f"{item.get('agent_role', 'N/A')} | {fmt(item.get('research_round'))} | "
            f"{item.get('call_type', 'N/A')} | {item.get('model', 'N/A')} | "
            f"{fmt(item.get('input_tokens'))} | {fmt(item.get('output_tokens'))} | "
            f"{fmt(item.get('total_tokens'))} | {fmt(item.get('duration_ms'))} | {fmt(item.get('success'))} |"
        )
    return "\n".join(rows)


def tool_table(profile: Mapping[str, Any]) -> str:
    """Render tools with bounded benchmark-only query/result captures."""
    rows = [
        "| # | Tool | Graph node | Duration ms | Success | Args | Result chars | URLs |",
        "|---:|---|---|---:|---|---|---:|---|",
    ]
    tool_rows = tools(profile)
    if not tool_rows:
        rows.append("| — | N/A | N/A | N/A | N/A | N/A | N/A | N/A |")
        return "\n".join(rows)
    for index, item in enumerate(tool_rows, 1):
        result = item.get("captured_result") or {}
        args = str(item.get("captured_args") or "N/A")[:260].replace("|", "/")
        urls = ", ".join(result.get("urls", [])[:5]) or "—"
        rows.append(
            f"| {index} | {item.get('tool', 'N/A')} | {item.get('graph_node', 'N/A')} | "
            f"{fmt(item.get('duration_ms'))} | {fmt(item.get('success'))} | {args} | "
            f"{fmt(result.get('chars'))} | {urls} |"
        )
    return "\n".join(rows)


def react_table(profile: Mapping[str, Any]) -> str:
    """Render per-agent ReAct input usage."""
    rows = [
        "| Agent | Round | Calls | Input tokens | Deltas | Growth observable |",
        "|---|---:|---:|---|---|---|",
    ]
    values = profile.get("analysis", {}).get("react_input_progression", []) or []
    if not values:
        rows.append("| N/A | N/A | N/A | N/A | N/A | N/A |")
        return "\n".join(rows)
    for item in values:
        rows.append(
            f"| {item.get('agent_role', 'N/A')} | {fmt(item.get('research_round'))} | "
            f"{len(item.get('call_ids', []))} | {', '.join(fmt(v) for v in item.get('input_tokens', [])) or 'N/A'} | "
            f"{', '.join(fmt(v) for v in item.get('delta_from_previous', [])) or '—'} | "
            f"{fmt(item.get('input_growth_observable'))} |"
        )
    return "\n".join(rows)


def reuse_table(profile: Mapping[str, Any]) -> str:
    """Render estimated role-report reuse."""
    rows = [
        "| Consumer | Model calls | Roles | Report chars/call | Estimated tokens/call | Estimated repeated input |",
        "|---|---:|---|---:|---:|---:|",
    ]
    values = profile.get("analysis", {}).get("role_reports_reuse", {}).get("downstream_reuse", []) or []
    if not values:
        rows.append("| N/A | N/A | N/A | N/A | N/A | N/A |")
        return "\n".join(rows)
    for item in values:
        rows.append(
            f"| {item.get('consumer', 'N/A')} | {fmt(item.get('model_call_count'))} | "
            f"{', '.join(item.get('roles', []))} | {fmt(item.get('report_chars_per_call'))} | "
            f"{fmt(item.get('estimated_tokens_per_call'))} | {fmt(item.get('estimated_repeated_input_tokens'))} |"
        )
    return "\n".join(rows)


def shares_table(profile: Mapping[str, Any]) -> str:
    """Render token-share estimates."""
    labels = [
        ("react_reasoning", "ReAct"),
        ("compression", "Compression"),
        ("research_review", "Research Review"),
        ("risk_plus_strategy", "Risk + Strategy"),
        ("section_writing", "Section Writing"),
        ("final_report_compile", "Final Report Compile"),
    ]
    rows = ["| Component | Known tokens | Share of known tokens | Exact/estimated |", "|---|---:|---:|---|"]
    values = profile.get("analysis", {}).get("token_shares", {}) or {}
    for key, label in labels:
        item = values.get(key, {}) or {}
        rows.append(
            f"| {label} | {fmt(item.get('known_tokens'))} | {fmt(item.get('share_of_known_tokens'))} | "
            f"{'estimated' if item.get('estimated') else 'exact'} |"
        )
    return "\n".join(rows)


def single_markdown(profile: Mapping[str, Any], label: str) -> str:
    """Render one full version profile."""
    cfg = profile.get("effective_configuration", {}) or {}
    total = profile.get("flow", {}).get("total_tokens", {}) or {}
    error = profile.get("error")
    flow = " → ".join(profile.get("flow", {}).get("node_sequence", []) or []) or "N/A"
    error_line = (
        f"Error: {error.get('type')} — {error.get('message')}"
        if isinstance(error, Mapping)
        else ""
    )
    growth_rows = [
        f"| {row.get('role', 'N/A')} | {fmt(row.get('round_1_chars'))} | "
        f"{fmt(row.get('followup_chars'))} | {fmt(row.get('growth_chars'))} | "
        f"{fmt(row.get('round_1_estimated_tokens'))} / {fmt(row.get('followup_estimated_tokens'))} |"
        for row in profile.get("analysis", {}).get("report_growth", {}).get("roles", []) or []
    ]
    followup_rows = [
        f"- {row.get('role', 'N/A')}: round 1={fmt(row.get('round_1_chars'))} chars; "
        f"follow-up={fmt(row.get('followup_chars'))} chars; growth={fmt(row.get('growth_chars'))} chars; "
        f"estimated token growth={fmt(row.get('growth_estimated_tokens'))}"
        for row in profile.get("analysis", {}).get("report_growth", {}).get("roles", []) or []
        if row.get("followup_chars") is not None
    ]
    return f"""# {label} Single Case Cost Profile

## Executive status

- Status: **{profile.get('status', 'N/A')}**
- Case: {profile.get('case_id', 'N/A')}
- Commit: {profile.get('commit', 'N/A')}
- Elapsed: {fmt(profile.get('elapsed_ms'))} ms
- Model: research={cfg.get('research_model', 'N/A')}; summarization={cfg.get('summarization_model', 'N/A')}; compression={cfg.get('compression_model', 'N/A')}; final={cfg.get('final_report_model', 'N/A')}
- Temperature: {fmt(cfg.get('temperature'))} — {cfg.get('temperature_note', 'N/A')}
- Actual billed cost: {fmt(profile.get('environment', {}).get('actual_cost'))}; provider cost was not exposed.
- {error_line}

## Actual execution flow

{flow}

The sequence is reconstructed from Agent Observer span events.

## Effective configuration

| Setting | Value |
|---|---|
| max_react_tool_calls | {fmt(cfg.get('max_react_tool_calls'))} |
| max_research_rounds | {fmt(cfg.get('max_research_rounds'))} |
| max_structured_output_retries | {fmt(cfg.get('max_structured_output_retries'))} |
| search_api | {fmt(cfg.get('search_api'))} |
| RAG enabled / mode | {fmt(cfg.get('rag_enabled'))} / {fmt(cfg.get('retrieval_mode'))} |
| RAG embedding / vectorstore / reranker | {fmt(cfg.get('rag_embedding_provider'))} / {fmt(cfg.get('rag_vectorstore_provider'))} / {fmt(cfg.get('rag_reranker_provider'))} |
| RAG configuration note | {cfg.get('rag_benchmark_override', 'N/A')} |
| Observer | {fmt(cfg.get('agent_observer_project'))}; {fmt(profile.get('environment', {}).get('agent_observer_sdk'))} |

## Full Flow Cost Breakdown

{stage_table(profile)}

### Node / Agent aggregation

{node_table(profile)}

Observer v0.2 span_finished events returned duration_ms=null for graph spans. Node wall-duration is therefore N/A rather than inferred; per-model durations, per-tool durations, and total wall elapsed remain recorded.

## Research Review and follow-up

{review_section(profile)}

## Every model call

{model_table(profile)}

## Tool calls

{tool_table(profile)}

## Token composition

| Component | Value |
|---|---:|
| Input tokens | {rollup(total, 'input_tokens')} |
| Output tokens | {rollup(total, 'output_tokens')} |
| Total tokens | {rollup(total, 'total_tokens')} |
| Known input tokens | {fmt(total.get('known_input_tokens'))} |
| Known output tokens | {fmt(total.get('known_output_tokens'))} |
| Missing input-usage responses | {fmt(total.get('missing_input_count'))} |
| Missing output-usage responses | {fmt(total.get('missing_output_count'))} |
| Input Token Ratio | {fmt(profile.get('analysis', {}).get('input_output_ratios', {}).get('input_token_ratio'))} |
| Output Token Ratio | {fmt(profile.get('analysis', {}).get('input_output_ratios', {}).get('output_token_ratio'))} |

### Token shares

{shares_table(profile)}

## ReAct input-token growth

{react_table(profile)}

## compress_research cost

{compression_table(profile)}

Compression cost share: {fmt(profile.get('analysis', {}).get('token_shares', {}).get('compression', {}).get('share_of_known_tokens'))}.

## role_reports repeated context

### Report sizes

| Role | Round 1 chars | Follow-up chars | Growth chars | Estimated tokens (R1 / follow-up) |
|---|---:|---:|---:|---:|
{chr(10).join(growth_rows) or '| N/A | N/A | N/A | N/A |'}

### Downstream reuse estimate

{reuse_table(profile)}

- Downstream reuse count: {fmt(profile.get('analysis', {}).get('role_reports_reuse', {}).get('downstream_reuse_count'))}
- Estimated repeated input tokens: {fmt(profile.get('analysis', {}).get('role_reports_reuse', {}).get('estimated_repeated_input_tokens'))}
- Estimated repeated-context share of known tokens: {fmt(profile.get('analysis', {}).get('role_reports_reuse', {}).get('estimated_repeated_context_share_of_known_tokens'))}

This is estimated from code-path contracts and report character lengths because Observer events do not include prompt provenance.

## Follow-up report append

{chr(10).join(followup_rows) or '- No follow-up report was recovered.'}

## Section writer context

- Section count observed: {fmt(profile.get('analysis', {}).get('section_writer', {}).get('section_count'))}
- Section writer calls: {fmt(profile.get('analysis', {}).get('section_writer', {}).get('section_writer_calls'))}
- Final section writer calls: {fmt(profile.get('analysis', {}).get('section_writer', {}).get('final_section_writer_calls'))}
- Section prompt input tokens: {', '.join(fmt(value) for value in profile.get('analysis', {}).get('section_writer', {}).get('prompt_input_tokens', [])) or 'N/A'}

## Limitations

- No actual billed cost was exposed.
- Missing token values remain N/A; known partial sums are shown separately.
- The Observer sidecar network sender was replaced only by an in-process recorder; the installed SDK and existing adapter were used.
- If status is not success, this is a diagnostic trace, not a complete cost profile.
"""


def compression_table(profile: Mapping[str, Any]) -> str:
    """Render compression calls."""
    rows = ["| # | Node/role | Round | Input | Output | Total | Duration ms |", "|---:|---|---:|---:|---:|---:|---:|"]
    values = [item for item in calls(profile) if item.get("call_type") == "research_compression"]
    if not values:
        rows.append("| N/A | N/A | N/A | N/A | N/A | N/A | N/A |")
        return "\n".join(rows)
    for index, item in enumerate(values, 1):
        rows.append(
            f"| {index} | {item.get('graph_node', 'N/A')} / {item.get('agent_role', 'N/A')} | "
            f"{fmt(item.get('research_round'))} | {fmt(item.get('input_tokens'))} | "
            f"{fmt(item.get('output_tokens'))} | {fmt(item.get('total_tokens'))} | {fmt(item.get('duration_ms'))} |"
        )
    return "\n".join(rows)


def profile_metric(profile: Mapping[str, Any], name: str) -> Any:
    """Read a comparison metric."""
    if name == "model_calls":
        return len(calls(profile))
    if name == "tool_calls":
        return len(tools(profile))
    if name == "duration_ms":
        return profile.get("elapsed_ms")
    if name in {"input_tokens", "output_tokens", "total_tokens"}:
        return (profile.get("flow", {}).get("total_tokens") or {}).get(name)
    if name == "public_signal":
        return sum(item.get("agent_role") == "public_signal" for item in calls(profile))
    if name == "internal_knowledge":
        return sum(item.get("agent_role") == "internal_knowledge" for item in calls(profile))
    if name == "research_review":
        return sum(item.get("call_type") == "research_review" for item in calls(profile))
    if name == "compression":
        return sum(item.get("call_type") == "research_compression" for item in calls(profile))
    if name == "section_writer":
        return sum(item.get("call_type") == "section_writer" for item in calls(profile))
    return None


def top_node(profile: Mapping[str, Any]) -> str:
    """Find the node with the largest known token subtotal."""
    rows = profile.get("flow", {}).get("node_rows", []) or []
    if not rows:
        return "N/A"
    selected = max(rows, key=lambda item: (item.get("tokens", {}) or {}).get("known_total_tokens", 0))
    return str(selected.get("node", "N/A"))


def comparison_markdown(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    """Render the Before/After cost comparison."""
    metric_rows = [
        ("model_calls", "Model Calls"),
        ("tool_calls", "Tool Calls"),
        ("input_tokens", "Input Tokens"),
        ("output_tokens", "Output Tokens"),
        ("total_tokens", "Total Tokens"),
        ("duration_ms", "Duration (ms)"),
        ("public_signal", "Public Signal model calls"),
        ("internal_knowledge", "Internal Knowledge model calls"),
        ("research_review", "Research Review calls"),
        ("compression", "Compression calls"),
        ("section_writer", "Section Writer calls"),
    ]
    rows = ["| Metric | Before | After | Delta |", "|---|---:|---:|---:|"]
    for key, label in metric_rows:
        b = profile_metric(before, key)
        a = profile_metric(after, key)
        rows.append(f"| {label} | {fmt(b)} | {fmt(a)} | {delta(b, a)} |")

    after_calls = calls(after)
    before_calls = calls(before)
    dynamic_calls = [
        item
        for item in after_calls
        if item.get("call_type") == "research_review"
        or (
            isinstance(item.get("research_round"), int)
            and item.get("research_round") > 1
            and item.get("agent_role") in {"public_signal", "internal_knowledge"}
        )
    ]
    dynamic_known = sum(
        item.get("total_tokens", 0)
        for item in dynamic_calls
        if isinstance(item.get("total_tokens"), int)
    )
    after_cfg = after.get("effective_configuration", {}) or {}
    before_cfg = before.get("effective_configuration", {}) or {}
    return f"""# Before / After Single-Case Cost Comparison

## Executive Summary

- 最大 Token 消耗节点：After={top_node(after)}；Before={top_node(before)}。排序使用已知 token subtotal，缺失 usage 的调用没有被猜测。
- 最大重复上下文来源：After 的 role_reports；估算重复输入为 {fmt(after.get('analysis', {}).get('role_reports_reuse', {}).get('estimated_repeated_input_tokens'))} tokens。
    - Dynamic P&E 直接新增的 Review + public/internal follow-up 调用：{len(dynamic_calls)}；已知 token subtotal={dynamic_known}（Review usage 未返回，故为下界）。After 的 risk/strategy 在 Round 2 继续执行，但 Before 也有对应下游阶段，不计入直接新增。
- 原有架构成本：Before model calls={len(before_calls)}，tool calls={len(before.get('tool_calls', []) or [])}，exact total tokens={fmt((before.get('flow', {}).get('total_tokens') or {}).get('total_tokens'))}，duration={fmt(before.get('elapsed_ms'))} ms。
- 优先检查：P0 ReAct history growth；P0 full role_reports injection；P1 compression 与 section evidence duplication。

## Test identity and configuration

| Item | Before | After |
|---|---|---|
| Commit | {before.get('commit', 'N/A')} | {after.get('commit', 'N/A')} |
| Case | {before.get('case_id', 'N/A')} | {after.get('case_id', 'N/A')} |
| Status | {before.get('status', 'N/A')} | {after.get('status', 'N/A')} |
| Model | {before_cfg.get('research_model', 'N/A')} | {after_cfg.get('research_model', 'N/A')} |
| max_react_tool_calls | {before_cfg.get('max_react_tool_calls', 'N/A')} | {after_cfg.get('max_react_tool_calls', 'N/A')} |
| max_research_rounds | {before_cfg.get('max_research_rounds', 'N/A')} | {after_cfg.get('max_research_rounds', 'N/A')} |

The runner did not override model, temperature, ReAct limit, research-round limit, or tool whitelist. RAG was explicitly enabled identically for both versions because the raw .env/defaults leave RAG disabled and the internal-knowledge role otherwise fails before a complete run; the RAG algorithm was not changed.

## Before vs After

{chr(10).join(rows)}

| Partial observed token subtotal | Before | After | Delta |
|---|---:|---:|---:|
| Known input tokens | {(before.get('flow', {}).get('total_tokens') or {}).get('known_input_tokens', 'N/A')} | {(after.get('flow', {}).get('total_tokens') or {}).get('known_input_tokens', 'N/A')} | {delta((before.get('flow', {}).get('total_tokens') or {}).get('known_input_tokens'), (after.get('flow', {}).get('total_tokens') or {}).get('known_input_tokens'))} |
| Known output tokens | {(before.get('flow', {}).get('total_tokens') or {}).get('known_output_tokens', 'N/A')} | {(after.get('flow', {}).get('total_tokens') or {}).get('known_output_tokens', 'N/A')} | {delta((before.get('flow', {}).get('total_tokens') or {}).get('known_output_tokens'), (after.get('flow', {}).get('total_tokens') or {}).get('known_output_tokens'))} |
| Known total-token subtotal | {(before.get('flow', {}).get('total_tokens') or {}).get('known_total_tokens', 'N/A')} | {(after.get('flow', {}).get('total_tokens') or {}).get('known_total_tokens', 'N/A')} | {delta((before.get('flow', {}).get('total_tokens') or {}).get('known_total_tokens'), (after.get('flow', {}).get('total_tokens') or {}).get('known_total_tokens'))} |

## Dynamic P&E incremental cost

| Component | After calls | Known total tokens | Exact? |
|---|---:|---:|---|
| Research Review | {sum(item.get('call_type') == 'research_review' for item in after_calls)} | {sum(item.get('total_tokens', 0) for item in after_calls if item.get('call_type') == 'research_review' and isinstance(item.get('total_tokens'), int))} | {not any(item.get('call_type') == 'research_review' and item.get('total_tokens') is None for item in after_calls)} |
| Public/internal follow-up role calls | {sum(isinstance(item.get('research_round'), int) and item.get('research_round') > 1 and item.get('agent_role') in {'public_signal', 'internal_knowledge'} for item in after_calls)} | {sum(item.get('total_tokens', 0) for item in after_calls if isinstance(item.get('research_round'), int) and item.get('research_round') > 1 and item.get('agent_role') in {'public_signal', 'internal_knowledge'} and isinstance(item.get('total_tokens'), int))} | {not any(isinstance(item.get('research_round'), int) and item.get('research_round') > 1 and item.get('agent_role') in {'public_signal', 'internal_knowledge'} and item.get('total_tokens') is None for item in after_calls)} |
| Direct Dynamic subset (Review + follow-up) | {len(dynamic_calls)} | {dynamic_known} | estimated when any usage is missing |
| Round-2 risk/strategy downstream (not direct increment) | {sum(isinstance(item.get('research_round'), int) and item.get('research_round') > 1 and item.get('agent_role') in {'risk_assessment', 'response_strategy'} for item in after_calls)} | {sum(item.get('total_tokens', 0) for item in after_calls if isinstance(item.get('research_round'), int) and item.get('research_round') > 1 and item.get('agent_role') in {'risk_assessment', 'response_strategy'} and isinstance(item.get('total_tokens'), int))} | estimated when any usage is missing |

Dynamic P&E cost is the After review/follow-up subset. A causal increase requires completed runs with both versions and stable provider behavior.

## Full Flow Cost Breakdown

### Before

{stage_table(before)}

### After

{stage_table(after)}

## Top Token Consumers

| Rank | Version | Call type | Node | Agent role | Round | Total | Input | Output | Duration ms |
|---:|---|---|---|---|---:|---:|---:|---:|---:|
{chr(10).join(f"| {index} | {version} | {item.get('call_type', 'N/A')} | {item.get('graph_node', 'N/A')} | {item.get('agent_role', 'N/A')} | {fmt(item.get('research_round'))} | {fmt(item.get('total_tokens'))} | {fmt(item.get('input_tokens'))} | {fmt(item.get('output_tokens'))} | {fmt(item.get('duration_ms'))} |" for index, (version, item) in enumerate(sorted([('After', item) for item in after_calls] + [('Before', item) for item in before_calls], key=lambda pair: pair[1].get('total_tokens') if isinstance(pair[1].get('total_tokens'), int) else -1, reverse=True)[:20], 1)) or '| — | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |'}

## Context Duplication Analysis

### ReAct history growth

#### Before

{react_table(before)}

#### After

{react_table(after)}

An increasing exact input-token sequence is evidence that a role's prior history is resent. N/A cannot be converted into a conclusion.

### role_reports reuse

#### Before

{reuse_table(before)}

#### After

{reuse_table(after)}

Character/token estimates are explicitly estimated because Observer events do not include prompt provenance.

### Follow-up report append

{chr(10).join(f"- {row.get('role', 'N/A')}: growth={fmt(row.get('growth_chars'))} chars; estimated token growth={fmt(row.get('growth_estimated_tokens'))}" for row in after.get('analysis', {}).get('report_growth', {}).get('roles', []) or [] if row.get('followup_chars') is not None) or '- N/A — no follow-up report recorded.'}

### Compression and Section Writer

- Before compression share: {fmt(before.get('analysis', {}).get('token_shares', {}).get('compression', {}).get('share_of_known_tokens'))}; After: {fmt(after.get('analysis', {}).get('token_shares', {}).get('compression', {}).get('share_of_known_tokens'))}.
- Before section-writer input tokens: {', '.join(fmt(value) for value in before.get('analysis', {}).get('section_writer', {}).get('prompt_input_tokens', [])) or 'N/A'}.
- After section-writer input tokens: {', '.join(fmt(value) for value in after.get('analysis', {}).get('section_writer', {}).get('prompt_input_tokens', [])) or 'N/A'}.

## Input/output ratios and token shares

### Before

{shares_table(before)}

### After

{shares_table(after)}

## Recommended Optimization Priority

1. P0 — ReAct history growth: verify consecutive exact input usage and full AI/tool history resend.
2. P0 — Full role_reports injection: separate assignment, review, risk, strategy, and section prompts.
3. P1 — compress_research: compare compression input/output against the context it removes.
4. P1 — Section evidence duplication: inspect multi-section prompts and final-report fallback.

## Limitations

- Only one Case and one run per version were requested.
- Current .env model is {before_cfg.get('research_model', 'N/A')}; it is not the named deepseek-v4-flash unless the environment is changed.
- No billed cost field was returned; no online price was used to invent one.
- Missing token fields remain N/A. Report-character context reuse and its share are estimated.
- The Observer HTTP sidecar and social API may be unavailable; raw JSON contains exact errors and in-process Observer events.
"""


def render_all(*, after_path: Path, before_path: Path, results_dir: Path) -> None:
    """Write all requested Markdown files."""
    after = load(after_path)
    before = load(before_path)
    (results_dir / "AFTER_SINGLE_CASE_COST_PROFILE.md").write_text(
        single_markdown(after, "After"),
        encoding="utf-8",
    )
    (results_dir / "BEFORE_SINGLE_CASE_COST_PROFILE.md").write_text(
        single_markdown(before, "Before"),
        encoding="utf-8",
    )
    (results_dir / "BEFORE_AFTER_COST_COMPARISON.md").write_text(
        comparison_markdown(before, after),
        encoding="utf-8",
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parsed = parser.parse_args()
    render_all(
        after_path=parsed.after,
        before_path=parsed.before,
        results_dir=parsed.results_dir,
    )
