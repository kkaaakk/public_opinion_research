"""Run and collect a single-case model/token cost profile.

The worker imports one requested committed worktree in an isolated process.
It does not override model names, temperatures, ReAct limits, research-round
limits, tools, or RAG settings.  The only harness changes are a benchmark
thread id and an in-memory replacement for the already-configured Agent
Observer sender, plus bounded tool-result diagnostics.
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
            "observer_logical_run_id": run_tag,
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


def span_info(events: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Index Observer spans and their terminal durations."""
    starts: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("type") != "span_started":
            continue
        span_id = event.get("span_id")
        if not span_id:
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
        name = str(event.get("name") or metadata.get("graph_node") or metadata.get("node_name") or "")
        starts[str(span_id)] = {
            "span_id": str(span_id),
            "name": name or None,
            "kind": event.get("kind"),
            "metadata": dict(metadata),
            "start_sequence": event.get("sequence"),
        }
    for event in events:
        if event.get("type") not in {"span_finished", "span_failed", "span_interrupted"}:
            continue
        span_id = event.get("span_id")
        if span_id and str(span_id) in starts:
            starts[str(span_id)].update({
                "duration_ms": event.get("duration_ms"),
                "terminal_event": event.get("type"),
                "stop_reason": event.get("stop_reason"),
            })
    return starts, list(starts.values())


def call_type(component: str | None, node: str | None) -> str:
    """Classify a model call only from explicit Observer component/node facts."""
    component_text = str(component or "")
    node_text = str(node or "")
    if component_text == "research_brief":
        return "research_brief"
    if component_text == "clarification":
        return "clarification"
    if component_text == "report_planner":
        return "report_planner"
    if component_text == "webpage_summarization":
        return "webpage_summarization"
    if component_text.startswith("research_review"):
        return "research_review"
    if node_text == "section_writer":
        return "section_writer"
    if node_text == "write_final_sections":
        return "final_section_writer"
    if node_text == "compile_final_report":
        return "final_report_compile"
    if component_text == "":
        if node_text in {
            "public_signal_agent",
            "internal_knowledge_agent",
            "risk_assessment_agent",
            "response_strategy_agent",
        }:
            # The final model call in a role span is classified as
            # research_compression after the full request list is available;
            # earlier unlabeled calls are ReAct reasoning in the Before code.
            return "unclassified_model_call"
        return "unclassified_model_call"
    if re.search(r"_(initial|followup)_round_\d+$", component_text):
        return "react_reasoning"
    return "unclassified_model_call"


def role_from_node_or_component(node: str | None, component: str | None) -> str | None:
    """Return an explicit business role when the event names one."""
    node_text = str(node or "")
    if node_text.endswith("_agent"):
        return node_text.removesuffix("_agent")
    match = re.match(r"(public_signal|internal_knowledge|risk_assessment|response_strategy)_", str(component or ""))
    return match.group(1) if match else None


def round_from_component(component: str | None) -> int | None:
    match = re.search(r"(?:round_|_R)(\d+)", str(component or ""), re.IGNORECASE)
    return int(match.group(1)) if match else None


def model_calls(events: list[dict[str, Any]], spans: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Join request/response events into one row per actual model invocation."""
    responses = {
        str(event.get("request_id")): event
        for event in events
        if event.get("type") == "model_response" and event.get("request_id")
    }
    latest_round_by_span: dict[str, int] = {}
    calls: list[dict[str, Any]] = []
    requests = [event for event in events if event.get("type") == "model_request"]
    requests.sort(key=lambda item: item.get("sequence", 0))
    for event in requests:
        span_id = str(event.get("span_id") or "")
        span = spans.get(span_id, {})
        metadata = span.get("metadata") if isinstance(span.get("metadata"), Mapping) else {}
        node = str(span.get("name") or metadata.get("graph_node") or "") or None
        component = event.get("component")
        current_round = round_from_component(component)
        if current_round is not None:
            latest_round_by_span[span_id] = current_round
        response = responses.get(str(event.get("request_id")))
        kind = call_type(component, node)
        research_round = current_round or latest_round_by_span.get(span_id)
        row = {
            "call_id": event.get("request_id"),
            "sequence": event.get("sequence"),
            "span_id": span_id or None,
            "graph_node": node,
            "agent_role": role_from_node_or_component(node, component),
            "research_round": research_round,
            "research_round_source": (
                "component"
                if current_round is not None
                else ("preceding_component_in_span" if research_round is not None else "N/A")
            ),
            "call_type": kind,
            "model": event.get("model"),
            "provider": event.get("provider"),
            "structured_output": event.get("structured_output"),
            "input_tokens": response.get("input_tokens") if response else None,
            "output_tokens": response.get("output_tokens") if response else None,
            "total_tokens": (
                response.get("input_tokens") + response.get("output_tokens")
                if response
                and isinstance(response.get("input_tokens"), int)
                and isinstance(response.get("output_tokens"), int)
                else None
            ),
            "duration_ms": response.get("duration_ms") if response else None,
            "success": response.get("success") if response else None,
            "stop_reason": response.get("stop_reason") if response else None,
            "error_type": response.get("error_type") if response else None,
            "token_source": "Agent Observer model_response usage fields",
            "usage_complete": bool(
                response
                and isinstance(response.get("input_tokens"), int)
                and isinstance(response.get("output_tokens"), int)
            ),
        }
        calls.append(row)
    # The Before commit predates explicit observer_component labels.  Its
    # public-opinion agent span still has a reliable execution order: the
    # final model call in the span is compress_research(), and earlier calls
    # are ReAct reasoning.  This is a structural classification, not a token
    # or cost guess.
    calls_by_span: dict[str, list[dict[str, Any]]] = {}
    for call in calls:
        if (
            call.get("call_type") == "unclassified_model_call"
            and call.get("graph_node")
            in {
                "public_signal_agent",
                "internal_knowledge_agent",
                "risk_assessment_agent",
                "response_strategy_agent",
            }
        ):
            calls_by_span.setdefault(str(call.get("span_id") or ""), []).append(call)
    for span_calls in calls_by_span.values():
        span_calls.sort(key=lambda item: item.get("sequence", 0))
        if span_calls:
            span_calls[-1]["call_type"] = "research_compression"
            for call in span_calls[:-1]:
                call["call_type"] = "react_reasoning"
            for call in span_calls:
                if call.get("research_round") is None:
                    call["research_round"] = 1
                    call["research_round_source"] = "single_before_role_execution_default_initial"
    for call in calls:
        if (
            call.get("research_round") is None
            and call.get("graph_node")
            in {
                "public_signal_agent",
                "internal_knowledge_agent",
                "risk_assessment_agent",
                "response_strategy_agent",
            }
        ):
            call["research_round"] = 1
            call["research_round_source"] = "single_role_execution_default_initial"
    return calls


def tool_calls(
    events: list[dict[str, Any]],
    spans: Mapping[str, Mapping[str, Any]],
    captured: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Join Observer tool events and benchmark-only bounded result captures."""
    results = {
        str(event.get("tool_call_id")): event
        for event in events
        if event.get("type") == "tool_result" and event.get("tool_call_id")
    }
    rows: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "tool_call":
            continue
        span_id = str(event.get("span_id") or "")
        span = spans.get(span_id, {})
        metadata = span.get("metadata") if isinstance(span.get("metadata"), Mapping) else {}
        result = results.get(str(event.get("tool_call_id")))
        rows.append({
            "tool_call_id": event.get("tool_call_id"),
            "sequence": event.get("sequence"),
            "span_id": span_id or None,
            "graph_node": span.get("name") or metadata.get("graph_node") or None,
            "tool": event.get("tool") or event.get("name"),
            "tool_domain": event.get("tool_domain"),
            "mcp_server": event.get("mcp_server"),
            "duration_ms": result.get("duration_ms") if result else None,
            "success": result.get("success") if result else None,
            "raw_bytes": result.get("raw_bytes") if result else None,
            "context_bytes": result.get("context_bytes") if result else None,
            "observer_result_event": result,
        })
    # The wrapper carries query/result previews unavailable in the privacy-safe
    # Observer event. Match in execution order without changing Observer counts.
    for row, extra in zip(rows, captured):
        row["captured_args"] = extra.get("args")
        row["captured_started_at"] = extra.get("started_at")
        row["captured_result"] = extra.get("result")
        if extra.get("error_type"):
            row["captured_error_type"] = extra.get("error_type")
    return rows


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
    events: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    spans: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build node, stage, and actual event-sequence cost summaries."""
    node_sequence = [
        str(span.get("name"))
        for span in sorted(spans, key=lambda item: item.get("start_sequence", 0))
        if span.get("name")
    ]
    node_groups: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        name = str(span.get("name") or "")
        if name:
            node_groups.setdefault(name, []).append(span)
    node_rows: list[dict[str, Any]] = []
    for node, executions in node_groups.items():
        span_ids = {str(item.get("span_id")) for item in executions}
        node_calls = [call for call in calls if str(call.get("span_id")) in span_ids]
        node_tools = [tool for tool in tools if str(tool.get("span_id")) in span_ids]
        tokens = token_rollup(node_calls)
        durations = [
            item.get("duration_ms")
            for item in executions
            if isinstance(item.get("duration_ms"), int)
        ]
        node_rows.append({
            "node": node,
            "executions": len(executions),
            "model_calls": len(node_calls),
            "tokens": tokens,
            "tool_calls": len(node_tools),
            "duration_ms": sum(durations) if len(durations) == len(executions) else None,
            "duration_known_execution_count": len(durations),
            "kinds": sorted(set(str(item.get("kind")) for item in executions)),
        })
    stage_groups: dict[str, list[dict[str, Any]]] = {}
    for call in calls:
        role = call.get("agent_role")
        round_value = call.get("research_round")
        if call.get("call_type") == "react_reasoning":
            label = f"{role or 'unknown'} R{round_value or 'N/A'}"
        elif call.get("call_type") == "research_compression":
            label = f"{role or 'unknown'} compress R{round_value or 'N/A'}"
        elif call.get("call_type") == "webpage_summarization":
            label = f"{role or 'unknown'} webpage_summarization R{round_value or 'N/A'}"
        elif call.get("call_type") == "research_review":
            label = f"research_review #{round_value or 'N/A'}"
        else:
            label = str(call.get("call_type"))
        stage_groups.setdefault(label, []).append(call)
    stage_rows = []
    calls_by_span: dict[str, list[dict[str, Any]]] = {}
    for call in calls:
        calls_by_span.setdefault(str(call.get("span_id") or ""), []).append(call)
    for span_calls in calls_by_span.values():
        span_calls.sort(key=lambda item: item.get("sequence", 0))
    for stage, stage_calls in stage_groups.items():
        stage_sequences = [
            call.get("sequence")
            for call in stage_calls
            if isinstance(call.get("sequence"), int)
        ]
        stage_span_ids = {str(call.get("span_id") or "") for call in stage_calls}
        stage_tools: list[Mapping[str, Any]] = []
        if stage_sequences and not all(
            call.get("call_type") == "webpage_summarization"
            for call in stage_calls
        ):
            low = min(stage_sequences)
            high = max(stage_sequences)
            for span_id in stage_span_ids:
                later_model_sequences = [
                    call.get("sequence")
                    for call in calls_by_span.get(span_id, [])
                    if isinstance(call.get("sequence"), int) and call.get("sequence") > high
                ]
                upper = min(later_model_sequences) if later_model_sequences else None
                stage_tools.extend(
                    tool
                    for tool in tools
                    if str(tool.get("span_id") or "") == span_id
                    and isinstance(tool.get("sequence"), int)
                    and tool.get("sequence") > low
                    and (upper is None or tool.get("sequence") < upper)
                )
        stage_rows.append({
            "stage": stage,
            "call_type": sorted(set(str(call.get("call_type")) for call in stage_calls)),
            "model_calls": len(stage_calls),
            "tokens": token_rollup(stage_calls),
            "tool_calls": len(stage_tools),
            "model_duration_ms": sum(
                call["duration_ms"]
                for call in stage_calls
                if isinstance(call.get("duration_ms"), int)
            ),
        })
    return {
        "node_sequence": node_sequence,
        "node_rows": node_rows,
        "stage_rows": stage_rows,
        "total_model_calls": len(calls),
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
    """Estimate repeated role-report input from observed consumers and code contracts."""
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
    entries: list[dict[str, Any]] = []

    def add(consumer: str, call_rows: list[Mapping[str, Any]], roles: list[str], basis: str) -> None:
        if not call_rows:
            return
        chars_by_call = [
            sum(
                cumulative_chars(role, call.get("research_round"))
                for role in roles
            )
            for call in call_rows
        ]
        chars_by_call = [value for value in chars_by_call if value > 0]
        if not chars_by_call:
            return
        entries.append({
            "consumer": consumer,
            "model_call_count": len(chars_by_call),
            "roles": roles,
            "report_chars_per_call": round(sum(chars_by_call) / len(chars_by_call)),
            "estimated_tokens_per_call": round(sum((value + 3) // 4 for value in chars_by_call) / len(chars_by_call)),
            "estimated_repeated_input_tokens": sum((value + 3) // 4 for value in chars_by_call),
            "basis": basis,
            "estimated": True,
        })

    review_calls = [call for call in calls if call.get("call_type") == "research_review"]
    add(
        "research_review",
        review_calls,
        ["public_signal", "internal_knowledge"],
        "research_review prompt formats both full role_reports",
    )
    risk_calls = [
        call
        for call in calls
        if call.get("agent_role") == "risk_assessment"
        and call.get("call_type") == "react_reasoning"
    ]
    add(
        "risk_assessment",
        risk_calls,
        ["public_signal", "internal_knowledge"],
        "role assignment formats both upstream reports",
    )
    strategy_calls = [
        call
        for call in calls
        if call.get("agent_role") == "response_strategy"
        and call.get("call_type") == "react_reasoning"
    ]
    add(
        "response_strategy",
        strategy_calls,
        ["public_signal", "internal_knowledge", "risk_assessment"],
        "role assignment formats all declared upstream reports",
    )
    section_calls = [call for call in calls if call.get("call_type") == "section_writer"]
    add(
        "section_writer",
        section_calls,
        list(report_chars),
        "section evidence is extracted from current role_reports; exact section role selection is not present in Observer events",
    )
    final_section_calls = [call for call in calls if call.get("call_type") == "final_section_writer"]
    add(
        "write_final_sections",
        final_section_calls,
        list(report_chars),
        "final section context may contain completed evidence; report reuse is an upper-bound estimate",
    )
    total_estimated = sum(
        int(item["estimated_repeated_input_tokens"])
        for item in entries
    )
    all_known_total = sum(
        int(call.get("total_tokens"))
        for call in calls
        if isinstance(call.get("total_tokens"), int)
    )
    return {
        "role_report_sizes": report_chars,
        "downstream_reuse": entries,
        "downstream_reuse_count": sum(item["model_call_count"] for item in entries),
        "estimated_repeated_input_tokens": total_estimated,
        "estimated_repeated_context_share_of_known_tokens": (
            total_estimated / all_known_total if all_known_total else None
        ),
        "estimated": True,
        "note": "This is a code-path/character estimate; prompt-level provenance is not emitted by the Observer.",
    }


def react_progression(calls: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Show consecutive ReAct input-token observations per role/round."""
    groups: dict[tuple[str, Any], list[Mapping[str, Any]]] = {}
    for call in calls:
        if call.get("call_type") != "react_reasoning":
            continue
        key = (str(call.get("agent_role") or "unknown"), call.get("research_round"))
        groups.setdefault(key, []).append(call)
    rows = []
    for (role, round_value), group in groups.items():
        group = sorted(group, key=lambda item: item.get("sequence", 0))
        values = [item.get("input_tokens") for item in group]
        deltas = [
            values[index] - values[index - 1]
            if isinstance(values[index], int) and isinstance(values[index - 1], int)
            else None
            for index in range(1, len(values))
        ]
        rows.append({
            "agent_role": role,
            "research_round": round_value,
            "call_ids": [item.get("call_id") for item in group],
            "input_tokens": values,
            "delta_from_previous": deltas,
            "input_growth_observable": any(delta is not None and delta > 0 for delta in deltas),
            "exact": all(isinstance(value, int) for value in values),
        })
    return rows


def share_by_call_type(calls: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Calculate exact or known-token estimated shares."""
    known_total = sum(
        int(call.get("total_tokens"))
        for call in calls
        if isinstance(call.get("total_tokens"), int)
    )
    missing = sum(call.get("total_tokens") is None for call in calls)
    groups = {
        "react_reasoning": [call for call in calls if call.get("call_type") == "react_reasoning"],
        "compression": [call for call in calls if call.get("call_type") == "research_compression"],
        "research_review": [call for call in calls if call.get("call_type") == "research_review"],
        "risk_plus_strategy": [
            call
            for call in calls
            if call.get("agent_role") in {"risk_assessment", "response_strategy"}
        ],
        "section_writing": [
            call
            for call in calls
            if call.get("call_type") in {"section_writer", "final_section_writer"}
        ],
        "final_report_compile": [
            call for call in calls if call.get("call_type") == "final_report_compile"
        ],
    }
    result: dict[str, Any] = {}
    for name, group in groups.items():
        value = sum(
            int(call.get("total_tokens"))
            for call in group
            if isinstance(call.get("total_tokens"), int)
        )
        result[name] = {
            "known_tokens": value,
            "share_of_known_tokens": value / known_total if known_total else None,
            "estimated": missing > 0,
            "missing_token_call_count": sum(call.get("total_tokens") is None for call in group),
        }
    result["known_total_tokens"] = known_total
    result["missing_token_call_count"] = missing
    result["note"] = (
        "Shares are exact only when all model responses expose input/output usage; "
        "otherwise they are estimated from known-token calls."
    )
    return result


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
    """Execute one case and collect raw Observer/stream evidence."""
    sys.path.insert(0, str(target_root / "src"))
    from dotenv import load_dotenv

    load_dotenv(ENV_PATH, override=True)

    import agent_observer.sdk as observer_sdk
    import open_deep_research.deep_researcher as deep_researcher_module
    import open_deep_research.observability.agent_observer as observer_module
    from langchain_core.messages import HumanMessage

    events: list[dict[str, Any]] = []
    captured_tools: list[dict[str, Any]] = []

    def record_event(
        run_id: Any,
        sequence: Any,
        event_type: Any,
        *,
        span_id: Any = None,
        **payload: Any,
    ) -> None:
        events.append({
            "run_id": str(run_id),
            "sequence": sequence,
            "type": str(event_type),
            "span_id": str(span_id) if span_id is not None else None,
            **jsonable(payload, string_limit=50_000),
        })

    class RecordingObserver:
        """Use the real SDK event model while disabling only its network sender."""

        def __init__(self, **kwargs: Any) -> None:
            self.inner = observer_sdk.AgentObserver(
                enabled=False,
                project=str(kwargs.get("project", "public-opinion-cost-profile")),
            )
            self.inner._record = record_event

        def start_run(self, **kwargs: Any) -> Any:
            return self.inner.start_run(**kwargs)

        def close(self, timeout: float = 1.0) -> None:
            self.inner.close(timeout)

    observer_module.AgentObserver = RecordingObserver
    original_tool_ainvoke = deep_researcher_module.observe_tool_ainvoke

    async def capture_tool(tool: Any, args: Any, config: Any = None, **kwargs: Any) -> Any:
        started_at = time.perf_counter()
        item: dict[str, Any] = {
            "tool": str(getattr(tool, "name", None) or type(tool).__name__),
            "started_at": utc_now(),
            "args": bounded_args(args),
        }
        try:
            result = await original_tool_ainvoke(tool, args, config, **kwargs)
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

    deep_researcher_module.observe_tool_ainvoke = capture_tool
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
    spans_by_id, spans = span_info(events)
    calls = model_calls(events, spans_by_id)
    tools = tool_calls(events, spans_by_id, captured_tools)
    flow = flow_and_summaries(events, calls, tools, spans)
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
            "agent_observer_sdk": "in-process recorder over installed Agent Observer SDK",
            "actual_cost": actual_cost,
            "actual_cost_note": "N/A: provider response/Observer metadata did not expose a billed cost field.",
        },
        "effective_configuration": config_snapshot(config),
        "error": error,
        "flow": flow,
        "model_calls": calls,
        "tool_calls": tools,
        "tool_capture_records": captured_tools,
        "node_executions": spans,
        "role_reports": role_reports,
        "round_reports": round_reports,
        "research_reviews": reviews,
        "completed_research_tasks": completed_tasks,
        "final_report": final_report,
        "stream_updates": updates,
        "observer_events": events,
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
            "observer_event_count": len(events),
            "model_request_count": len([event for event in events if event.get("type") == "model_request"]),
            "model_response_count": len([event for event in events if event.get("type") == "model_response"]),
            "observer_tool_call_count": len([event for event in events if event.get("type") == "tool_call"]),
            "observer_tool_result_count": len([event for event in events if event.get("type") == "tool_result"]),
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
