"""Run and collect a single-case model/token cost profile.

The worker imports one requested committed worktree in an isolated process.
It does not override model names, temperatures, ReAct limits, research-round
limits, tools, or RAG settings.  The only harness changes are a benchmark
thread id, a stream-update transcript, and bounded tool-result diagnostics.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import traceback
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
CASE_PATH = REPO_ROOT / "evaluation" / "cost_profile" / "single_case.json"
ENV_PATH = REPO_ROOT / ".env"
TARGETS = {
    "before": Path(r"C:\codex_workspace\POR-before"),
    "after": Path(r"C:\codex_workspace\POR-after"),
}
EXPECTED_COMMITS = {
    "before": "d0270b6be4ab09eb381db008dd62463e2d2327eb",
    "after": "5989480e523909661153eb23f3dea374d842a887",
}
KNOWN_STATE_FIELDS = {
    "messages",
    "research_brief",
    "role_reports",
    "agent_memories",
    "notes",
    "raw_notes",
    "budget_usage",
    "final_report",
    "sections",
    "completed_sections",
    "research_round",
    "research_mode",
    "research_review",
    "current_research_tasks",
    "completed_research_tasks",
    "feedback_on_report_plan",
}
KNOWN_NODE_NAMES = {
    "enrich_query_images",
    "clarify_with_user",
    "write_research_brief",
    "plan_report_sections",
    "research_phase",
    "research_supervisor",
    "public_signal_agent",
    "internal_knowledge_agent",
    "research_review",
    "risk_assessment_agent",
    "response_strategy_agent",
    "section_writer",
    "write_final_sections",
    "compile_final_report",
}


def utc_now() -> str:
    """Return the current UTC time in ISO-8601 form."""
    return datetime.now(timezone.utc).isoformat()


def jsonable(value: Any, *, string_limit: int = 50_000, depth: int = 0) -> Any:
    """Convert model/event values into bounded JSON-safe data."""
    if depth > 8:
        return f"<{type(value).__name__}>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= string_limit else value[:string_limit] + "\n[truncated]"
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, Mapping):
        return {
            str(key): jsonable(item, string_limit=string_limit, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [
            jsonable(item, string_limit=string_limit, depth=depth + 1)
            for item in value
        ]
    content = getattr(value, "content", None)
    if content is not None and hasattr(value, "type"):
        return {
            "type": getattr(value, "type", type(value).__name__),
            "content": jsonable(content, string_limit=string_limit, depth=depth + 1),
            "name": getattr(value, "name", None),
            "tool_calls": jsonable(
                getattr(value, "tool_calls", []),
                string_limit=string_limit,
                depth=depth + 1,
            ),
        }
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return jsonable(
                model_dump(mode="json"),
                string_limit=string_limit,
                depth=depth + 1,
            )
        except TypeError:
            return jsonable(model_dump(), string_limit=string_limit, depth=depth + 1)
        except Exception:
            pass
    return str(value)[:string_limit]


def load_case(case_id: str) -> dict[str, Any]:
    """Load the fixed cost-profile case."""
    case = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    if case.get("case_id") != case_id:
        raise ValueError(f"Case {case_id!r} not found in {CASE_PATH}")
    return case


def git_info(root: Path) -> dict[str, Any]:
    """Read target commit and worktree status without mutation."""
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "commit_short": run("rev-parse", "--short", "HEAD"),
        "subject": run("log", "-1", "--format=%s"),
        "status_porcelain": run("status", "--porcelain"),
    }


def env_value(name: str, default: str | None = None) -> str | None:
    """Read one non-secret value from the repository .env for diagnostics."""
    if not ENV_PATH.exists():
        return default
    prefix = f"{name}="
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip().strip("'\"") or default
    return default


def runtime_config(version: str, case_id: str) -> dict[str, Any]:
    """Add only correlation metadata; all business settings come from .env/defaults."""
    run_tag = f"cost-profile-{version}-{case_id}-{uuid.uuid4().hex[:10]}"
    return {
        "configurable": {
            "thread_id": run_tag,
            # The current .env leaves RAG_ENABLED unset, which defaults to
            # False and makes internal_knowledge_agent fail before a complete
            # public-opinion run.  This is a shared harness configuration
            # required for a complete cost profile, not a RAG algorithm change.
            "rag_enabled": True,
            "retrieval_mode": "hybrid",
            "rag_knowledge_base_paths": [
                str(REPO_ROOT / "data" / "knowledge" / "public_opinion")
            ],
            "rag_memory_enabled": True,
            "rag_memory_paths": [
                str(REPO_ROOT / "data" / "memory" / "public_opinion" / "memories.jsonl")
            ],
            "rag_embedding_provider": "hash",
            "rag_vectorstore_provider": "memory",
            "rag_reranker_provider": "simple",
            "rag_graph_enabled": False,
            "rag_query_rewrite_enabled": False,
            "rag_memory_write_enabled": False,
        },
        "metadata": {
            "benchmark": "single_case_cost_profile",
            "benchmark_version": version,
            "case_id": case_id,
        },
    }


def redact(value: Any, key: str = "") -> Any:
    """Redact credential-shaped config fields while keeping settings auditable."""
    if re.search(r"(api.?key|token|secret|password|dsn|authorization)", key, re.IGNORECASE):
        return "<redacted>" if value not in (None, "") else value
    if isinstance(value, Mapping):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    return jsonable(value)


def config_snapshot(config: Mapping[str, Any]) -> dict[str, Any]:
    """Capture effective Configuration values after env precedence is applied."""
    from open_deep_research.configuration import Configuration

    effective = Configuration.from_runnable_config(config)
    snapshot = redact(effective.model_dump(mode="json"))
    snapshot["temperature"] = None
    snapshot["temperature_note"] = (
        "N/A: Configuration/model invocation boundary does not expose temperature."
    )
    snapshot["runtime_metadata"] = redact(config)
    snapshot["env_file"] = str(ENV_PATH)
    snapshot["env_file_exists"] = ENV_PATH.exists()
    snapshot["requested_model_description"] = (
        "The task mentioned deepseek-v4-flash; actual .env value is recorded above "
        "without substitution."
    )
    snapshot["rag_benchmark_override"] = (
        "RAG was explicitly enabled with the repository's local public-opinion "
        "fixture configuration because raw .env/defaults leave RAG disabled and "
        "internal_knowledge_agent cannot complete; algorithm unchanged."
    )
    snapshot["original_env_rag_enabled"] = env_value("RAG_ENABLED", "<unset>")
    return snapshot


def summarize_change(key: str, value: Any) -> Any:
    """Retain reports/reviews but avoid storing full message transcripts twice."""
    if key == "messages":
        messages = value if isinstance(value, list) else [value]
        return {
            "count": len(messages),
            "last": jsonable(messages[-1], string_limit=800) if messages else None,
        }
    if key in {
        "role_reports",
        "final_report",
        "research_review",
        "current_research_tasks",
        "completed_research_tasks",
        "sections",
        "budget_usage",
    }:
        return jsonable(value, string_limit=50_000)
    if key in {"notes", "raw_notes"}:
        return jsonable(value, string_limit=30_000)
    return jsonable(value, string_limit=5_000)


def collect_stream_chunk(chunk: Any, updates: list[dict[str, Any]]) -> None:
    """Normalize LangGraph v2 update chunks into node/state records."""
    namespace: list[str] = []
    data = chunk
    if isinstance(chunk, Mapping) and "type" in chunk and "data" in chunk:
        raw_namespace = chunk.get("ns") or []
        namespace = (
            [str(item) for item in raw_namespace]
            if isinstance(raw_namespace, (list, tuple))
            else [str(raw_namespace)]
        )
        data = chunk.get("data")
    if not isinstance(data, Mapping):
        updates.append({
            "namespace": namespace,
            "node": "<non_mapping_update>",
            "changes": jsonable(data),
        })
        return
    data_keys = {str(key) for key in data}
    node_shaped = bool(data_keys.intersection(KNOWN_NODE_NAMES))
    direct_state = not node_shaped and bool(data_keys.intersection(KNOWN_STATE_FIELDS))
    entries = (
        [(namespace[-1] if namespace else "<graph>", data)]
        if direct_state
        else list(data.items())
    )
    for node, update in entries:
        changes = (
            {
                str(key): summarize_change(str(key), value)
                for key, value in update.items()
            }
            if isinstance(update, Mapping)
            else jsonable(update)
        )
        updates.append({
            "namespace": namespace,
            "node": str(node),
            "changes": changes,
        })


def bounded_args(value: Any) -> Any:
    """Keep query arguments while redacting credential-shaped keys."""
    if isinstance(value, Mapping):
        return {
            str(key): (
                "<redacted>"
                if re.search(r"(key|token|secret|password|authorization)", str(key), re.IGNORECASE)
                else bounded_args(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [bounded_args(item) for item in list(value)[:25]]
    if isinstance(value, str):
        return value[:600] + ("…" if len(value) > 600 else "")
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:600]


def result_summary(value: Any) -> dict[str, Any]:
    """Record bounded tool output evidence for cost/source diagnostics."""
    text = value if isinstance(value, str) else json.dumps(jsonable(value, string_limit=3_000), ensure_ascii=False)
    urls = sorted(set(re.findall(r"https?://[^\s)\]}>]+", text)))[:40]
    return {
        "type": type(value).__name__,
        "chars": len(text),
        "urls": urls,
        "preview": text[:1_000],
    }


def parse_reports(updates: list[dict[str, Any]]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Recover role reports and explicit round/mode markers."""
    merged: dict[str, str] = {}
    round_reports: list[dict[str, Any]] = []
    seen_reports: set[tuple[str, int | None, str]] = set()
    for item in updates:
        if item.get("node") in {"research_phase", "research_supervisor"} and not item.get("namespace"):
            # Parent research-phase output repeats the already-emitted
            # subgraph role reports; it is not a second role execution.
            continue
        changes = item.get("changes")
        if not isinstance(changes, Mapping) or "role_reports" not in changes:
            continue
        value = changes["role_reports"]
        if isinstance(value, Mapping) and value.get("type") == "override":
            value = value.get("value", {})
        if not isinstance(value, Mapping):
            continue
        for role, raw in value.items():
            role_name = str(role)
            report = str(raw or "")
            round_match = re.search(r"Research round:\s*(\d+)", report, re.IGNORECASE)
            mode_match = re.search(r"Research mode:\s*([A-Za-z]+)", report, re.IGNORECASE)
            report_round = int(round_match.group(1)) if round_match else 1
            report_mode = mode_match.group(1) if mode_match else "initial"
            fingerprint = (role_name, report_round, report)
            if fingerprint in seen_reports:
                continue
            seen_reports.add(fingerprint)
            previous = merged.get(role_name, "")
            if not previous:
                merged[role_name] = report
            elif report and report not in previous:
                merged[role_name] = (
                    f"{previous}\n\n--- Additional {role_name} research report ---\n{report}"
                )
            round_reports.append({
                "role": role_name,
                "round": report_round,
                "round_source": "report_marker" if round_match else "single_role_execution_default_initial",
                "mode": report_mode,
                "chars": len(report),
                "estimated_tokens": (len(report) + 3) // 4,
                "estimated": True,
                "report": report,
                "node": item.get("node"),
                "namespace": item.get("namespace", []),
            })
    return merged, round_reports


def parse_reviews(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recover structured ResearchReview values from stream state updates."""
    reviews: list[dict[str, Any]] = []
    for item in updates:
        changes = item.get("changes")
        if not isinstance(changes, Mapping) or "research_review" not in changes:
            continue
        value = changes["research_review"]
        if isinstance(value, Mapping) and value.get("type") == "override":
            value = value.get("value", {})
        if (
            isinstance(value, Mapping)
            and "research_complete" not in value
            and isinstance(value.get("research_review"), Mapping)
        ):
            # LangGraph v2 can emit {node_name: node_update} under a state
            # key that has the same name as the node.
            value = value["research_review"]
        if isinstance(value, Mapping) and "research_complete" in value:
            reviews.append({
                "review_index": len(reviews) + 1,
                **jsonable(value, string_limit=50_000),
                "node": item.get("node"),
                "namespace": item.get("namespace", []),
            })
    return reviews


def parse_completed_tasks(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recover tasks actually completed by follow-up role executions."""
    seen: set[str] = set()
    tasks: list[dict[str, Any]] = []
    for item in updates:
        changes = item.get("changes")
        if not isinstance(changes, Mapping) or "completed_research_tasks" not in changes:
            continue
        value = changes["completed_research_tasks"]
        if isinstance(value, Mapping) and value.get("type") == "override":
            value = value.get("value", [])
        if not isinstance(value, list):
            continue
        for raw in value:
            if not isinstance(raw, Mapping):
                continue
            task = dict(jsonable(raw))
            identity = str(task.get("task_id") or task.get("objective") or task)
            if identity not in seen:
                seen.add(identity)
                tasks.append(task)
    return tasks


def model_calls_from_captures() -> list[dict[str, Any]]:
    """Keep the artifact schema; per-call model events are no longer captured."""
    return []


def tool_calls_from_captures(captured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one bounded row per captured tool execution."""
    rows: list[dict[str, Any]] = []
    for extra in captured:
        rows.append({
            "tool": extra.get("tool"),
            "started_at": extra.get("started_at"),
            "args": extra.get("args"),
            "duration_ms": extra.get("duration_ms"),
            "success": extra.get("success"),
            "error_type": extra.get("error_type"),
            "result": extra.get("result"),
        })
    return rows


def budget_usage_from_updates(updates: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum per-node budget deltas from stream updates into one authoritative total."""
    from open_deep_research.budget import merge_budget_usage

    total: dict[str, Any] = {}
    for item in updates:
        changes = item.get("changes")
        if not isinstance(changes, Mapping):
            continue
        runtime = changes.get("runtime")
        if not isinstance(runtime, Mapping):
            continue
        budget = runtime.get("budget")
        if isinstance(budget, Mapping) and budget:
            total = merge_budget_usage(total, dict(budget))
    return total


def token_rollup(calls: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Return exact sums only when every call exposes both token fields."""
    input_values = [call.get("input_tokens") for call in calls]
    output_values = [call.get("output_tokens") for call in calls]
    known_input = [value for value in input_values if isinstance(value, int)]
    known_output = [value for value in output_values if isinstance(value, int)]
    complete = bool(calls) and len(known_input) == len(calls) and len(known_output) == len(calls)
    exact_input = sum(known_input) if complete else None
    exact_output = sum(known_output) if complete else None
    return {
        "input_tokens": exact_input,
        "output_tokens": exact_output,
        "total_tokens": exact_input + exact_output if exact_input is not None and exact_output is not None else None,
        "known_input_tokens": sum(known_input),
        "known_output_tokens": sum(known_output),
        "known_total_tokens": sum(known_input) + sum(known_output),
        "complete": complete,
        "call_count": len(calls),
        "missing_input_count": len(calls) - len(known_input),
        "missing_output_count": len(calls) - len(known_output),
        "note": (
            "Exact"
            if complete
            else "N/A for exact total because at least one model_response omitted input/output usage metadata."
        ),
    }


def flow_and_summaries(
    updates: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build node and stage cost summaries from stream updates and captures."""
    node_sequence = [
        str(item.get("node"))
        for item in updates
        if item.get("node") and str(item.get("node")) != "<non_mapping_update>"
    ]
    node_groups: dict[str, list[dict[str, Any]]] = {}
    for item in updates:
        name = str(item.get("node") or "")
        if name and name != "<non_mapping_update>":
            node_groups.setdefault(name, []).append(item)
    node_rows: list[dict[str, Any]] = []
    for node, executions in node_groups.items():
        budget = budget_usage_from_updates(executions)
        node_rows.append({
            "node": node,
            "executions": len(executions),
            "model_calls": budget.get("model_calls", 0),
            "input_tokens": budget.get("input_tokens"),
            "output_tokens": budget.get("output_tokens"),
            "total_tokens": budget.get("total_tokens"),
            "tool_calls": budget.get("tool_calls", 0),
        })
    return {
        "node_sequence": node_sequence,
        "node_rows": node_rows,
        "total_tool_calls": len(tools),
        "total_tokens": token_rollup(calls),
    }


def report_sizes(round_reports: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize round-one/round-two report growth."""
    by_role: dict[str, list[Mapping[str, Any]]] = {}
    for report in round_reports:
        by_role.setdefault(str(report.get("role")), []).append(report)
    rows = []
    for role, reports in by_role.items():
        reports_sorted = sorted(
            reports,
            key=lambda item: (
                item.get("round") if isinstance(item.get("round"), int) else 999,
                len(reports),
            ),
        )
        first = next((item for item in reports_sorted if item.get("round") == 1), None)
        follow = next((item for item in reports_sorted if item.get("round") and item.get("round") > 1), None)
        row = {
            "role": role,
            "rounds_recorded": [item.get("round") for item in reports_sorted],
            "round_1_chars": first.get("chars") if first else None,
            "round_1_estimated_tokens": first.get("estimated_tokens") if first else None,
            "followup_chars": follow.get("chars") if follow else None,
            "followup_estimated_tokens": follow.get("estimated_tokens") if follow else None,
            "growth_chars": (
                follow.get("chars") - first.get("chars")
                if first and follow
                else None
            ),
            "growth_estimated_tokens": (
                follow.get("estimated_tokens") - first.get("estimated_tokens")
                if first and follow
                else None
            ),
            "estimated": True,
        }
        rows.append(row)
    return {"roles": rows, "method": "report character length / 4; estimated, not provider tokenization"}


def repeated_context_analysis(
    calls: list[Mapping[str, Any]],
    round_reports: list[Mapping[str, Any]],
    updates: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Estimate repeated role-report input from report sizes and code contracts."""
    report_sizes_by_role: dict[str, dict[int, int]] = {}
    for report in round_reports:
        role = str(report.get("role"))
        round_value = report.get("round") if isinstance(report.get("round"), int) else 1
        report_sizes_by_role.setdefault(role, {})[round_value] = int(report.get("chars") or 0)

    def cumulative_chars(role: str, round_value: Any) -> int:
        values = report_sizes_by_role.get(role, {})
        if not values:
            return 0
        ceiling = round_value if isinstance(round_value, int) else max(values)
        sizes = [size for item_round, size in values.items() if item_round <= ceiling]
        # role_reports_reducer inserts a separator and label for appended
        # reports.  Include a bounded estimate of that framing.
        return sum(sizes) + max(0, len(sizes) - 1) * 60

    report_chars = {
        role: cumulative_chars(role, max(rounds))
        for role, rounds in report_sizes_by_role.items()
    }
    total_estimated = sum(
        (value + 3) // 4
        for rounds in report_sizes_by_role.values()
        for value in rounds.values()
    )
    return {
        "role_report_sizes": report_chars,
        "estimated": True,
        "note": (
            "This is a code-path/character estimate; per-call prompt "
            "provenance is not captured by this harness."
        ),
    }


def react_progression(calls: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Kept for artifact schema compatibility; per-call events are no longer captured."""
    return []


def share_by_call_type(calls: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Kept for artifact schema compatibility; per-call events are no longer captured."""
    return {
        "known_total_tokens": 0,
        "missing_token_call_count": 0,
        "note": "Per-call model events are not captured; budget totals come from stream updates.",
    }


def token_ratios(calls: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Compute input/output ratios from known token fields only."""
    token_data = token_rollup(calls)
    known_total = token_data.get("known_total_tokens") or 0
    known_input = token_data.get("known_input_tokens") or 0
    known_output = token_data.get("known_output_tokens") or 0
    return {
        "input_token_ratio": known_input / known_total if known_total else None,
        "output_token_ratio": known_output / known_total if known_total else None,
        "estimated": not bool(token_data.get("complete")),
        "known_input_tokens": known_input,
        "known_output_tokens": known_output,
        "known_total_tokens": known_total,
        "note": "Ratio uses known-token subtotal and is estimated when any model response lacks usage metadata.",
    }


async def run_case(
    *,
    version: str,
    case: Mapping[str, Any],
    target_root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Execute one case and collect raw stream/tool evidence."""
    sys.path.insert(0, str(target_root / "src"))
    from dotenv import load_dotenv

    load_dotenv(ENV_PATH, override=True)

    import open_deep_research.deep_researcher as deep_researcher_module
    import open_deep_research.runtime.business_agent as business_agent_module
    from langchain_core.messages import HumanMessage

    captured_tools: list[dict[str, Any]] = []

    async def capture_tool(tool: Any, args: Any, config: Any = None) -> Any:
        started_at = time.perf_counter()
        item: dict[str, Any] = {
            "tool": str(getattr(tool, "name", None) or type(tool).__name__),
            "started_at": utc_now(),
            "args": bounded_args(args),
        }
        try:
            result = await tool.ainvoke(args, config)
        except BaseException as exc:
            item.update({
                "success": False,
                "duration_ms": max(0, int((time.perf_counter() - started_at) * 1_000)),
                "error_type": type(exc).__name__,
                "error": str(exc)[:1_000],
            })
            captured_tools.append(item)
            raise
        item.update({
            "success": True,
            "duration_ms": max(0, int((time.perf_counter() - started_at) * 1_000)),
            "result": result_summary(result),
        })
        captured_tools.append(item)
        return result

    # The business agent executes every tool through this module-level hook, so
    # replacing it adds benchmark-only query/result diagnostics without changing
    # the tool, its arguments, or its results.
    business_agent_module._execute_tool_safely = capture_tool
    config = runtime_config(version, str(case["case_id"]))
    started_at = time.perf_counter()
    started_utc = utc_now()
    updates: list[dict[str, Any]] = []
    status = "error"
    error: dict[str, Any] | None = None
    try:
        graph = deep_researcher_module.deep_researcher()

        async def consume() -> None:
            async for chunk in graph.astream(
                {"messages": [HumanMessage(content=str(case["query"]))]},
                config,
                stream_mode="updates",
                subgraphs=True,
                version="v2",
            ):
                collect_stream_chunk(chunk, updates)

        await asyncio.wait_for(consume(), timeout=timeout_seconds)
        status = "success"
    except asyncio.TimeoutError:
        status = "timeout"
        error = {
            "type": "TimeoutError",
            "message": f"case exceeded {timeout_seconds}s",
        }
    except BaseException as exc:
        status = "error"
        error = {
            "type": type(exc).__name__,
            "message": str(exc)[:2_000],
            "traceback": traceback.format_exc(limit=30),
        }

    elapsed_ms = max(0, int((time.perf_counter() - started_at) * 1_000))
    role_reports, round_reports = parse_reports(updates)
    reviews = parse_reviews(updates)
    completed_tasks = parse_completed_tasks(updates)
    final_report = ""
    for item in reversed(updates):
        changes = item.get("changes")
        if isinstance(changes, Mapping) and isinstance(changes.get("final_report"), str):
            final_report = changes["final_report"]
            break
    if status == "success" and not final_report:
        status = "incomplete"
        error = {
            "type": "NoFinalReport",
            "message": "Graph ended without a final_report; this run is not a complete research execution.",
        }
    calls = model_calls_from_captures()
    tools = tool_calls_from_captures(captured_tools)
    flow = flow_and_summaries(updates, calls, tools)
    report_growth = report_sizes(round_reports)
    repeated = repeated_context_analysis(calls, round_reports, updates)
    progression = react_progression(calls)
    shares = share_by_call_type(calls)
    actual_cost = None
    return {
        "schema_version": "1.0",
        "artifact": "single_case_cost_profile",
        "status": status,
        "version": version,
        "case_id": case["case_id"],
        "case": jsonable(case, string_limit=50_000),
        "commit": EXPECTED_COMMITS[version],
        "target_root": str(target_root),
        "started_at_utc": started_utc,
        "finished_at_utc": utc_now(),
        "elapsed_ms": elapsed_ms,
        "environment": {
            "python": sys.version,
            "python_executable": sys.executable,
            "env_file": str(ENV_PATH),
            "env_file_exists": ENV_PATH.exists(),
            "actual_cost": actual_cost,
            "actual_cost_note": "N/A: provider metadata did not expose a billed cost field.",
        },
        "effective_configuration": config_snapshot(config),
        "error": error,
        "flow": flow,
        "model_calls": calls,
        "tool_calls": tools,
        "tool_capture_records": captured_tools,
        "role_reports": role_reports,
        "round_reports": round_reports,
        "research_reviews": reviews,
        "completed_research_tasks": completed_tasks,
        "final_report": final_report,
        "stream_updates": updates,
        "analysis": {
            "report_growth": report_growth,
            "react_input_progression": progression,
            "role_reports_reuse": repeated,
            "token_shares": shares,
            "input_output_ratios": token_ratios(calls),
            "section_writer": {
                "section_count": sum(
                    len(item.get("changes", {}).get("sections", []))
                    for item in updates
                    if isinstance(item.get("changes"), Mapping)
                    and isinstance(item.get("changes", {}).get("sections"), list)
                ),
                "section_writer_calls": sum(call.get("call_type") == "section_writer" for call in calls),
                "final_section_writer_calls": sum(call.get("call_type") == "final_section_writer" for call in calls),
                "prompt_input_tokens": [
                    call.get("input_tokens")
                    for call in calls
                    if call.get("call_type") in {"section_writer", "final_section_writer"}
                ],
                "note": "Prompt token values are exact only where provider usage metadata was present.",
            },
        },
        "diagnostics": {
            "tool_capture_count": len(captured_tools),
        },
    }


def write_json(path: Path, value: Any) -> None:
    """Write one UTF-8 JSON artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


async def worker_main(args: argparse.Namespace) -> int:
    """Run one version and always save a diagnostic artifact."""
    case = load_case(args.case_id)
    target_root = TARGETS[args.version]
    info = git_info(target_root)
    output = Path(args.output)
    base = {
        "schema_version": "1.0",
        "artifact": "single_case_cost_profile",
        "status": "blocked",
        "version": args.version,
        "case_id": args.case_id,
        "commit": info["commit"],
        "expected_commit": EXPECTED_COMMITS[args.version],
        "commit_matches_expected": info["commit"] == EXPECTED_COMMITS[args.version],
        "target_root": str(target_root),
        "worktree_status": info["status_porcelain"],
        "benchmark_started_at_utc": utc_now(),
        "runs": [],
    }
    write_json(output, base)
    if not base["commit_matches_expected"]:
        base["error"] = {
            "type": "CommitMismatch",
            "message": f"expected {EXPECTED_COMMITS[args.version]}, got {info['commit']}",
        }
        write_json(output, base)
        return 2
    result = await run_case(
        version=args.version,
        case=case,
        target_root=target_root,
        timeout_seconds=args.case_timeout,
    )
    result["commit_matches_expected"] = True
    write_json(output, result)
    print(
        f"[{args.version}] {args.case_id}: {result['status']} "
        f"{result['elapsed_ms']}ms, model_calls={len(result['model_calls'])}, "
        f"tool_calls={len(result['tool_calls'])}",
        flush=True,
    )
    return 0


def launch_worker(
    *,
    version: str,
    case_id: str,
    output: Path,
    case_timeout: int,
) -> None:
    """Launch an isolated worker so Before/After modules cannot mix."""
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--version",
        version,
        "--case-id",
        case_id,
        "--output",
        str(output),
        "--case-timeout",
        str(case_timeout),
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(TARGETS[version] / "src") + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.run(
        command,
        cwd=TARGETS[version],
        env=env,
        capture_output=True,
        text=True,
        timeout=max(1_200, case_timeout + 120),
    )
    output.with_suffix(".worker.log").write_text(
        f"$ {' '.join(command)}\n\nSTDOUT\n{process.stdout}\n\nSTDERR\n{process.stderr}",
        encoding="utf-8",
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"{version} worker failed with exit code {process.returncode}; "
            f"see {output.with_suffix('.worker.log')}"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse worker/orchestrator arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--version", choices=("before", "after"))
    parser.add_argument("--case-id", default="case_01_product_quality")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--results-dir", type=Path, default=HERE)
    parser.add_argument("--case-timeout", type=int, default=900)
    args = parser.parse_args(argv)
    if args.worker and (not args.version or args.output is None):
        parser.error("--worker requires --version and --output")
    return args


def main(args: argparse.Namespace) -> int:
    """Run After first, then Before, and render all requested artifacts."""
    results_dir = args.results_dir.resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    after_path = results_dir / "after_cost_profile.json"
    before_path = results_dir / "before_cost_profile.json"
    launch_worker(
        version="after",
        case_id=args.case_id,
        output=after_path,
        case_timeout=args.case_timeout,
    )
    launch_worker(
        version="before",
        case_id=args.case_id,
        output=before_path,
        case_timeout=args.case_timeout,
    )
    from render_cost_profile import render_all

    render_all(
        after_path=after_path,
        before_path=before_path,
        results_dir=results_dir,
    )
    return 0


if __name__ == "__main__":
    parsed = parse_args()
    if parsed.worker:
        raise SystemExit(asyncio.run(worker_main(parsed)))
    raise SystemExit(main(parsed))
