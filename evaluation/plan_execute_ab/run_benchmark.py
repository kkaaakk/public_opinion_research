"""Run the committed Before/After public-opinion benchmark.

This module deliberately lives outside ``src/``.  It loads the requested
worktree in a subprocess, runs the same cases with the same runtime
configuration, and records the existing Agent Observer v0.2 events in memory.
The in-process recording adapter is only a benchmark harness: no application
logic or prompt is changed.
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
CASES_PATH = HERE / "cases.jsonl"
DEFAULT_RESULTS_DIR = HERE / "results"
TARGETS = {
    "before": Path(r"C:\codex_workspace\POR-before"),
    "after": Path(r"C:\codex_workspace\POR-after"),
}
EXPECTED_COMMITS = {
    "before": "d0270b6be4ab09eb381db008dd62463e2d2327eb",
    "after": "5989480e523909661153eb23f3dea374d842a887",
}
SOCIAL_DATA_DIR = TARGETS["after"] / "data" / "social_media"
RAG_KNOWLEDGE_DIR = TARGETS["after"] / "data" / "knowledge" / "public_opinion"
RAG_MEMORY_FILE = TARGETS["after"] / "data" / "memory" / "public_opinion" / "memories.jsonl"
ENV_FILE = REPO_ROOT / ".env"
BENCHMARK_MODEL = "google_genai:gemma-4-26b-a4b-it"

SOCIAL_TOOL_NAMES = {
    "search_posts",
    "fetch_thread",
    "fetch_comments",
    "fetch_author_profile",
    "search_complaints",
    "get_trending_keywords",
    "get_public_opinion_snapshot",
}
KNOWN_STATE_FIELDS = {
    "messages",
    "research_brief",
    "role_reports",
    "agent_memories",
    "notes",
    "raw_notes",
    "budget_usage",
    "research_round",
    "research_mode",
    "research_review",
    "current_research_tasks",
    "completed_research_tasks",
    "sections",
    "completed_sections",
    "feedback_on_report_plan",
    "final_report",
}


def utc_now() -> str:
    """Return a stable, timezone-aware timestamp for an artifact."""
    return datetime.now(timezone.utc).isoformat()


def jsonable(value: Any, *, string_limit: int = 50_000, depth: int = 0) -> Any:
    """Convert LangChain/Pydantic values to bounded JSON-safe data."""
    if depth > 8:
        return f"<{type(value).__name__}>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= string_limit:
            return value
        return value[:string_limit].rstrip() + "\n[truncated by benchmark harness]"
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
    # BaseMessage is also a Pydantic model, so inspect its useful public shape
    # before trying generic model_dump.
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


def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    """Load and validate the JSONL benchmark cases."""
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on {path}:{line_number}: {exc}") from exc
        required = {"case_id", "query", "required_dimensions", "critical_dimensions", "expected_source_types"}
        missing = required.difference(case)
        if missing:
            raise ValueError(f"Case {line_number} is missing fields: {sorted(missing)}")
        cases.append(case)
    if len(cases) < 10:
        raise ValueError(f"Benchmark requires at least 10 cases; found {len(cases)}")
    return cases


def git_info(root: Path) -> dict[str, Any]:
    """Read the exact commit and worktree cleanliness without mutation."""
    def run(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "commit_short": run("rev-parse", "--short", "HEAD"),
        "subject": run("log", "-1", "--format=%s"),
        "status_porcelain": run("status", "--porcelain"),
    }


def _read_env_value(name: str, default: str) -> str:
    """Read a non-secret setting from the explicitly selected .env file."""
    if not ENV_FILE.exists():
        return default
    prefix = f"{name}="
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip().strip("'\"") or default
    return default


def benchmark_config(version: str, case_id: str, repeat_index: int) -> dict[str, Any]:
    """Return the one identical runtime config used by every case/version."""
    run_tag = f"plan-execute-ab-{version}-{case_id}-repeat-{repeat_index}-{uuid.uuid4().hex[:8]}"
    return {
        # Serialize provider requests at the harness boundary.  Both commits
        # receive the same setting; it avoids free-tier provider concurrency
        # errors being mistaken for workflow behavior.
        "max_concurrency": 1,
        "configurable": {
            "thread_id": run_tag,
            "observer_logical_run_id": run_tag,
            "agent_observer_enabled": True,
            "agent_observer_endpoint": "in-process-recording",
            "agent_observer_project": "public-opinion-research-ab-benchmark",
            "agent_observer_timeout": 0.35,
            "search_api": "none",
            "max_react_tool_calls": 1,
            "max_research_rounds": 2,
            "max_structured_output_retries": 1,
            "max_content_length": 10_000,
            "research_model": BENCHMARK_MODEL,
            "summarization_model": BENCHMARK_MODEL,
            "compression_model": BENCHMARK_MODEL,
            "final_report_model": BENCHMARK_MODEL,
            "research_model_max_tokens": 512,
            "summarization_model_max_tokens": 256,
            "compression_model_max_tokens": 512,
            "final_report_model_max_tokens": 800,
            "section_writer_model_max_tokens": 600,
            "allow_clarification": False,
            "allow_plan_feedback": False,
            "business_scenario": "public_opinion_risk",
            "organization_context": (
                "Benchmark context: use cited public sources and the configured local knowledge base. "
                "Do not invent private company facts; separate facts, inferences, and unresolved items."
            ),
            "enabled_business_agents": [
                "public_signal",
                "internal_knowledge",
                "risk_assessment",
                "response_strategy",
            ],
            # The same lightweight, local RAG configuration is applied to both
            # commits so internal-knowledge follow-ups are observable without
            # downloading an embedding/cross-encoder model.
            "rag_enabled": True,
            "retrieval_mode": "hybrid",
            "rag_knowledge_base_paths": [str(RAG_KNOWLEDGE_DIR)],
            "rag_memory_enabled": True,
            "rag_memory_paths": [str(RAG_MEMORY_FILE)],
            "rag_embedding_provider": "hash",
            "rag_vectorstore_provider": "memory",
            "rag_reranker_provider": "simple",
            "rag_graph_enabled": False,
            "rag_query_rewrite_enabled": False,
            "rag_memory_write_enabled": False,
            # These are consumed by the existing social-media adapter but are
            # intentionally outside Configuration, so they do not alter graph logic.
            "social_media_data_paths": [
                str(SOCIAL_DATA_DIR / "bulk_mock_posts.jsonl"),
                str(SOCIAL_DATA_DIR / "sample_posts.jsonl"),
            ],
        },
        "metadata": {
            "benchmark": "plan_execute_ab",
            "benchmark_version": version,
            "case_id": case_id,
            "repeat_index": repeat_index,
        },
    }


def effective_config_snapshot(config: Mapping[str, Any]) -> dict[str, Any]:
    """Capture comparable settings without serializing credentials."""
    configurable = config.get("configurable", {})
    if not isinstance(configurable, Mapping):
        configurable = {}
    selected = [
        "search_api",
        "max_react_tool_calls",
        "max_research_rounds",
        "max_structured_output_retries",
        "allow_clarification",
        "allow_plan_feedback",
        "business_scenario",
        "organization_context",
        "enabled_business_agents",
        "rag_enabled",
        "retrieval_mode",
        "rag_knowledge_base_paths",
        "rag_memory_enabled",
        "rag_memory_paths",
        "rag_embedding_provider",
        "rag_embedding_model",
        "rag_vectorstore_provider",
        "rag_reranker_provider",
        "rag_reranker_model",
        "rag_graph_enabled",
        "rag_query_rewrite_enabled",
        "rag_memory_write_enabled",
        "agent_observer_enabled",
        "agent_observer_endpoint",
        "agent_observer_project",
        "agent_observer_timeout",
    ]
    snapshot = {key: jsonable(configurable.get(key)) for key in selected if key in configurable}
    for model_field in ("research_model", "summarization_model", "compression_model", "final_report_model"):
        snapshot[model_field] = configurable.get(model_field) or _read_env_value(model_field.upper(), BENCHMARK_MODEL)
    snapshot["original_env_models"] = {
        model_field.lower(): _read_env_value(model_field, "<unset>")
        for model_field in ("RESEARCH_MODEL", "SUMMARIZATION_MODEL", "COMPRESSION_MODEL", "FINAL_REPORT_MODEL")
    }
    snapshot["model_override_reason"] = "DeepSeek returned HTTP 402 Insufficient Balance and Gemini 2.5 hit HTTP 429 quota during smoke runs; Google Gemma fallback was used uniformly for both versions."
    snapshot["original_env_search_api"] = _read_env_value("SEARCH_API", "<unset>")
    snapshot["search_api_override_reason"] = "Tavily summary fan-out repeatedly hit provider availability limits during smoke validation; local social fixtures and RAG remain enabled uniformly for both versions."
    snapshot["max_content_length"] = configurable.get("max_content_length", 50_000)
    snapshot["research_model_max_tokens"] = configurable.get("research_model_max_tokens", 10_000)
    snapshot["summarization_model_max_tokens"] = configurable.get("summarization_model_max_tokens", 8_192)
    snapshot["compression_model_max_tokens"] = configurable.get("compression_model_max_tokens", 8_192)
    snapshot["final_report_model_max_tokens"] = configurable.get("final_report_model_max_tokens", 10_000)
    snapshot["section_writer_model_max_tokens"] = configurable.get("section_writer_model_max_tokens")
    snapshot["temperature"] = None
    snapshot["temperature_note"] = "N/A: Configuration and model boundary do not expose a temperature parameter."
    snapshot["social_media_data_paths"] = jsonable(configurable.get("social_media_data_paths"))
    snapshot["max_concurrency"] = config.get("max_concurrency")
    snapshot["social_media_mode"] = "local JSON fixtures"
    snapshot["observer_mode"] = "Agent Observer v0.2 SDK with in-process event recorder"
    return snapshot


def _unwrap_override(value: Any) -> Any:
    if isinstance(value, Mapping) and value.get("type") == "override":
        return value.get("value", {})
    return value


def summarize_change(key: str, value: Any) -> Any:
    """Keep workflow evidence while avoiding duplicate message transcripts."""
    if key == "messages":
        messages = value if isinstance(value, list) else [value]
        return {
            "count": len(messages),
            "last": jsonable(messages[-1], string_limit=600) if messages else None,
        }
    if key in {"role_reports", "final_report", "research_review", "current_research_tasks", "completed_research_tasks", "budget_usage", "sections"}:
        return jsonable(value, string_limit=50_000)
    if key in {"notes", "raw_notes"}:
        return jsonable(value, string_limit=20_000)
    return jsonable(value, string_limit=4_000)


def collect_stream_chunk(chunk: Any, stream_updates: list[dict[str, Any]]) -> None:
    """Normalize LangGraph v2 update chunks into explicit node changes."""
    namespace: list[str] = []
    data = chunk
    if isinstance(chunk, Mapping) and "type" in chunk and "data" in chunk:
        namespace_value = chunk.get("ns") or []
        namespace = [str(item) for item in namespace_value] if isinstance(namespace_value, (list, tuple)) else [str(namespace_value)]
        data = chunk.get("data")
    if not isinstance(data, Mapping):
        stream_updates.append({"namespace": namespace, "node": "<non_mapping_update>", "changes": jsonable(data)})
        return

    # In v2 update mode ``data`` is normally {node_name: state_update}.  A
    # subgraph implementation can also return a state update directly; detect
    # that shape by its known state keys.
    direct_state = bool(set(str(key) for key in data).intersection(KNOWN_STATE_FIELDS))
    entries = [(namespace[-1] if namespace else "<graph>", data)] if direct_state else list(data.items())
    for node, update in entries:
        if isinstance(update, Mapping):
            changes = {
                str(key): summarize_change(str(key), value)
                for key, value in update.items()
            }
        else:
            changes = jsonable(update)
        stream_updates.append({
            "namespace": namespace,
            "node": str(node),
            "changes": changes,
        })


def _bounded_args(value: Any) -> Any:
    """Preserve search terms and structural arguments, never credentials."""
    secret_key = re.compile(r"(key|token|secret|password|authorization)", re.IGNORECASE)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if secret_key.search(key_text):
                result[key_text] = "<redacted>"
            else:
                result[key_text] = _bounded_args(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_bounded_args(item) for item in list(value)[:20]]
    if isinstance(value, str):
        return value[:500] + ("…" if len(value) > 500 else "")
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:500]


def _result_summary(result: Any) -> dict[str, Any]:
    """Record bounded tool-result evidence for replay/drift diagnostics."""
    if isinstance(result, str):
        urls = sorted(set(re.findall(r'https?://[^\s)\]}>]+', result)))[:30]
        return {
            "type": "str",
            "chars": len(result),
            "urls": urls,
            "preview": result[:800],
        }
    normalized = jsonable(result, string_limit=3_000)
    return {
        "type": type(result).__name__,
        "chars": len(json.dumps(normalized, ensure_ascii=False)),
        "urls": sorted(set(re.findall(r'https?://[^\s)\]}>]+', json.dumps(normalized, ensure_ascii=False))))[:30],
        "preview": normalized,
    }


def parse_role_reports(stream_updates: list[dict[str, Any]]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Recover merged role reports and their actual research rounds."""
    role_reports: dict[str, str] = {}
    round_reports: list[dict[str, Any]] = []
    for item in stream_updates:
        changes = item.get("changes")
        if not isinstance(changes, Mapping) or "role_reports" not in changes:
            continue
        value = _unwrap_override(changes.get("role_reports"))
        if not isinstance(value, Mapping):
            continue
        for role, raw_report in value.items():
            report = str(raw_report or "")
            role_name = str(role)
            previous = role_reports.get(role_name, "")
            if not previous:
                role_reports[role_name] = report
            elif report and report not in previous:
                role_reports[role_name] = f"{previous}\n\n--- Additional {role_name} research report ---\n{report}"
            round_match = re.search(r"Research round:\s*(\d+)", report, re.IGNORECASE)
            mode_match = re.search(r"Research mode:\s*([a-zA-Z]+)", report, re.IGNORECASE)
            round_reports.append({
                "role": role_name,
                "round": int(round_match.group(1)) if round_match else None,
                "mode": mode_match.group(1) if mode_match else None,
                "report": report,
                "node": item.get("node"),
                "namespace": item.get("namespace", []),
            })
    return role_reports, round_reports


def parse_reviews(stream_updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recover every structured ResearchReview emitted by the After graph."""
    reviews: list[dict[str, Any]] = []
    for item in stream_updates:
        changes = item.get("changes")
        if not isinstance(changes, Mapping) or "research_review" not in changes:
            continue
        value = _unwrap_override(changes.get("research_review"))
        if isinstance(value, Mapping) and "research_complete" in value:
            reviews.append({
                "round": len(reviews) + 1,
                **jsonable(value, string_limit=50_000),
                "node": item.get("node"),
                "namespace": item.get("namespace", []),
            })
    return reviews


def _task_identity(task: Mapping[str, Any]) -> str:
    task_id = str(task.get("task_id") or "").strip()
    if task_id:
        return task_id
    return ":".join(str(task.get(key) or "").strip() for key in ("target_role", "objective", "evidence_needed"))


def parse_completed_tasks(stream_updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recover tasks actually returned by follow-up agent executions."""
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in stream_updates:
        changes = item.get("changes")
        if not isinstance(changes, Mapping) or "completed_research_tasks" not in changes:
            continue
        value = _unwrap_override(changes.get("completed_research_tasks"))
        if not isinstance(value, list):
            continue
        for task in value:
            if not isinstance(task, Mapping):
                continue
            normalized = dict(jsonable(task))
            identity = _task_identity(normalized)
            if identity not in seen:
                seen.add(identity)
                tasks.append(normalized)
    return tasks


def observer_metrics(
    events: list[dict[str, Any]],
    tool_records: list[dict[str, Any]],
    *,
    elapsed_ms: int,
    final_report: str,
    round_reports: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    completed_tasks: list[dict[str, Any]],
    max_research_rounds: int,
) -> dict[str, Any]:
    """Aggregate only observed values; missing values remain null/N/A."""
    span_started = [event for event in events if event.get("type") == "span_started"]
    model_requests = [event for event in events if event.get("type") == "model_request"]
    model_responses = [event for event in events if event.get("type") == "model_response"]
    observer_tool_calls = [event for event in events if event.get("type") == "tool_call"]
    tool_calls = tool_records or observer_tool_calls

    node_sequence: list[str] = []
    node_kinds: dict[str, str] = {}
    for event in span_started:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
        name = event.get("name") or metadata.get("graph_node") or metadata.get("node_name")
        if not name:
            continue
        name = str(name)
        node_sequence.append(name)
        node_kinds[name] = str(event.get("kind") or "graph_node")

    def tool_name(record: Mapping[str, Any]) -> str:
        return str(record.get("tool") or record.get("name") or "")

    tool_names = [tool_name(record) for record in tool_calls]
    web_calls = sum(name == "web_search" for name in tool_names)
    rag_calls = sum(name == "rag_search" for name in tool_names)
    social_calls = sum(name in SOCIAL_TOOL_NAMES for name in tool_names)
    mcp_calls = sum(
        bool(record.get("mcp_server") or record.get("tool_domain") == "external_mcp")
        for record in tool_calls
        if isinstance(record, Mapping)
    )

    token_fields = ("input_tokens", "output_tokens")
    observed_token_values = {field: [] for field in token_fields}
    missing_token_responses = 0
    for response in model_responses:
        missing = False
        for field in token_fields:
            value = response.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                observed_token_values[field].append(value)
            else:
                missing = True
        if missing:
            missing_token_responses += 1
    input_tokens = (
        sum(observed_token_values["input_tokens"])
        if model_responses and missing_token_responses == 0 and observed_token_values["input_tokens"]
        else None
    )
    output_tokens = (
        sum(observed_token_values["output_tokens"])
        if model_responses and missing_token_responses == 0 and observed_token_values["output_tokens"]
        else None
    )
    total_tokens = input_tokens + output_tokens if input_tokens is not None and output_tokens is not None else None

    review_rounds = sorted({
        int(report["round"])
        for report in round_reports
        if isinstance(report.get("round"), int)
    })
    research_round = max(review_rounds or [1])
    followup_rounds = max(0, research_round - 1)
    review_gap_count = sum(
        len(review.get("research_gaps", []))
        for review in reviews
        if isinstance(review.get("research_gaps"), list)
    )
    next_task_count = sum(
        len(review.get("next_tasks", []))
        for review in reviews
        if isinstance(review.get("next_tasks"), list)
    )
    public_followup_tasks = sum(task.get("target_role") == "public_signal" for task in completed_tasks)
    internal_followup_tasks = sum(task.get("target_role") == "internal_knowledge" for task in completed_tasks)
    review_task_ids = [
        _task_identity(task)
        for review in reviews
        for task in review.get("next_tasks", [])
        if isinstance(task, Mapping)
    ]
    duplicate_task_count = len(review_task_ids) - len(set(review_task_ids))
    review_triggered = any(
        not bool(review.get("research_complete")) and bool(review.get("next_tasks"))
        for review in reviews
    )
    max_round_reached = research_round >= max_research_rounds and bool(reviews) and not bool(reviews[-1].get("research_complete"))

    return {
        "duration_ms": elapsed_ms,
        "node_executions": len(span_started) if span_started else None,
        "agent_executions": sum(node_kinds.get(name) == "agent" for name in node_sequence) if span_started else None,
        "model_calls": len(model_requests) if events else None,
        "tool_calls": len(tool_calls) if events or tool_records else None,
        "web_search_calls": web_calls if tool_calls else None,
        "rag_calls": rag_calls if tool_calls else None,
        "mcp_calls": mcp_calls if tool_calls else None,
        "social_media_calls": social_calls if tool_calls else None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "token_usage_observed_responses": len(model_responses),
        "token_usage_missing_responses": missing_token_responses,
        "public_signal_executions": node_sequence.count("public_signal_agent") if span_started else None,
        "internal_knowledge_executions": node_sequence.count("internal_knowledge_agent") if span_started else None,
        "risk_assessment_executions": node_sequence.count("risk_assessment_agent") if span_started else None,
        "response_strategy_executions": node_sequence.count("response_strategy_agent") if span_started else None,
        "research_review_executions": node_sequence.count("research_review") if span_started else None,
        "research_rounds": research_round,
        "followup_rounds": followup_rounds,
        "research_review_count": len(reviews),
        "research_gap_count": review_gap_count,
        "next_tasks_count": next_task_count,
        "public_signal_followup_tasks": public_followup_tasks,
        "internal_knowledge_followup_tasks": internal_followup_tasks,
        "duplicate_task_count": max(0, duplicate_task_count),
        "review_triggered_followup": review_triggered,
        "max_research_round_reached": max_round_reached,
        "node_sequence": node_sequence,
        "node_kinds": node_kinds,
        "tool_names": tool_names,
        "final_report_chars": len(final_report),
        "final_report_tokens": None,
        "final_report_token_note": "N/A: no tokenizer is used by the benchmark harness; characters are exact.",
        "observer_event_count": len(events),
    }


async def run_one_case(
    *,
    target_root: Path,
    version: str,
    case: Mapping[str, Any],
    repeat_index: int,
    case_timeout_seconds: int,
) -> dict[str, Any]:
    """Execute one case through the selected committed graph."""
    target_src = target_root / "src"
    sys.path.insert(0, str(target_src))
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE, override=True)
    # Configuration.from_runnable_config gives environment variables precedence
    # over configurable values.  Set the explicit, same-for-both fallback model
    # after loading the user's .env; the original DeepSeek setting is retained
    # in the run log/limitations as the failed initial configuration.
    for model_field in ("RESEARCH_MODEL", "SUMMARIZATION_MODEL", "COMPRESSION_MODEL", "FINAL_REPORT_MODEL"):
        os.environ[model_field] = BENCHMARK_MODEL

    # Imports intentionally happen after target_src is first on sys.path.
    import agent_observer.sdk as observer_sdk
    import open_deep_research.deep_researcher as deep_researcher_module
    import open_deep_research.observability.agent_observer as observer_module
    from langchain_core.messages import HumanMessage

    events: list[dict[str, Any]] = []
    tool_records: list[dict[str, Any]] = []

    def record_event(run_id: Any, sequence: Any, event_type: Any, *, span_id: Any = None, **payload: Any) -> None:
        events.append({
            "run_id": str(run_id),
            "sequence": sequence,
            "type": str(event_type),
            "span_id": str(span_id) if span_id is not None else None,
            **jsonable(payload, string_limit=50_000),
        })

    class RecordingObserver:
        """Use the installed SDK data model while replacing only its sender."""

        def __init__(self, **kwargs: Any) -> None:
            self.inner = observer_sdk.AgentObserver(
                enabled=False,
                project=str(kwargs.get("project", "public-opinion-research-ab-benchmark")),
            )
            self.inner._record = record_event

        def start_run(self, **kwargs: Any) -> Any:
            return self.inner.start_run(**kwargs)

        def close(self, timeout: float = 1.0) -> None:
            self.inner.close(timeout)

    observer_module.AgentObserver = RecordingObserver
    original_tool_ainvoke = deep_researcher_module.observe_tool_ainvoke

    async def recording_tool_ainvoke(tool: Any, args: Any, config: Any = None, **kwargs: Any) -> Any:
        started_at = time.perf_counter()
        record: dict[str, Any] = {
            "tool": str(getattr(tool, "name", None) or type(tool).__name__),
            "started_at": utc_now(),
            "args": _bounded_args(args),
        }
        try:
            result = await original_tool_ainvoke(tool, args, config, **kwargs)
        except BaseException as exc:
            record.update({
                "success": False,
                "duration_ms": max(0, int((time.perf_counter() - started_at) * 1_000)),
                "error_type": type(exc).__name__,
                "error": str(exc)[:800],
            })
            tool_records.append(record)
            raise
        record.update({
            "success": True,
            "duration_ms": max(0, int((time.perf_counter() - started_at) * 1_000)),
            "result": _result_summary(result),
        })
        tool_records.append(record)
        return result

    # execute_tool_safely resolves this module-level binding, so this wrapper
    # adds benchmark-only query/result summaries without changing the tool.
    deep_researcher_module.observe_tool_ainvoke = recording_tool_ainvoke

    config = benchmark_config(version, str(case["case_id"]), repeat_index)
    started_utc = utc_now()
    started_at = time.perf_counter()
    stream_updates: list[dict[str, Any]] = []
    final_report = ""
    role_reports: dict[str, str] = {}
    round_reports: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    completed_tasks: list[dict[str, Any]] = []
    error: dict[str, Any] | None = None
    status = "error"
    try:
        graph = deep_researcher_module.deep_researcher()

        async def consume() -> None:
            nonlocal final_report
            async for chunk in graph.astream(
                {"messages": [HumanMessage(content=str(case["query"]))]},
                config,
                stream_mode="updates",
                subgraphs=True,
                version="v2",
            ):
                collect_stream_chunk(chunk, stream_updates)

        await asyncio.wait_for(consume(), timeout=case_timeout_seconds)
        role_reports, round_reports = parse_role_reports(stream_updates)
        reviews = parse_reviews(stream_updates)
        completed_tasks = parse_completed_tasks(stream_updates)
        for update in reversed(stream_updates):
            changes = update.get("changes")
            if isinstance(changes, Mapping) and isinstance(changes.get("final_report"), str):
                final_report = changes["final_report"]
                break
        status = "success" if final_report else "incomplete"
    except asyncio.TimeoutError:
        error = {"type": "TimeoutError", "message": f"case exceeded {case_timeout_seconds}s"}
        status = "timeout"
    except BaseException as exc:
        error = {
            "type": type(exc).__name__,
            "message": str(exc)[:2_000],
            "traceback": traceback.format_exc(limit=20),
        }
        status = "error"
    finally:
        if not role_reports or not round_reports:
            role_reports, round_reports = parse_role_reports(stream_updates)
        if not reviews:
            reviews = parse_reviews(stream_updates)
        if not completed_tasks:
            completed_tasks = parse_completed_tasks(stream_updates)
        if not final_report:
            for update in reversed(stream_updates):
                changes = update.get("changes")
                if isinstance(changes, Mapping) and isinstance(changes.get("final_report"), str):
                    final_report = changes["final_report"]
                    break

    elapsed_ms = max(0, int((time.perf_counter() - started_at) * 1_000))
    metrics = observer_metrics(
        events,
        tool_records,
        elapsed_ms=elapsed_ms,
        final_report=final_report,
        round_reports=round_reports,
        reviews=reviews,
        completed_tasks=completed_tasks,
        max_research_rounds=2,
    )
    return {
        "schema_version": "1.0",
        "version": version,
        "case_id": case["case_id"],
        "repeat_index": repeat_index,
        "status": status,
        "started_at_utc": started_utc,
        "finished_at_utc": utc_now(),
        "elapsed_ms": elapsed_ms,
        "target_root": str(target_root),
        "commit": EXPECTED_COMMITS[version],
        "query": case["query"],
        "config": effective_config_snapshot(config),
        "error": error,
        "stream_updates": stream_updates,
        "observer_events": events,
        "tool_records": tool_records,
        "role_reports": role_reports,
        "round_reports": round_reports,
        "research_reviews": reviews,
        "completed_research_tasks": completed_tasks,
        "final_report": final_report,
        "metrics": metrics,
    }


def write_json(path: Path, value: Any) -> None:
    """Write an artifact with deterministic UTF-8 formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def worker_main(args: argparse.Namespace) -> int:
    """Run every case for one version, saving a resumable partial artifact."""
    cases = load_cases(Path(args.cases))
    if args.max_cases:
        cases = cases[: args.max_cases]
    target_root = TARGETS[args.version]
    info = git_info(target_root)
    result_path = Path(args.output)
    results: dict[str, Any] = {
        "schema_version": "1.0",
        "artifact": "before_after_plan_execute_benchmark_raw",
        "version": args.version,
        "commit": info["commit"],
        "commit_short": info["commit_short"],
        "commit_subject": info["subject"],
        "worktree_status": info["status_porcelain"],
        "expected_commit": EXPECTED_COMMITS[args.version],
        "commit_matches_expected": info["commit"] == EXPECTED_COMMITS[args.version],
        "benchmark_started_at_utc": utc_now(),
        "environment": {
            "python": sys.version,
            "python_executable": sys.executable,
            "env_file": str(ENV_FILE),
            "env_file_exists": ENV_FILE.exists(),
        },
        "cases_path": str(Path(args.cases).resolve()),
        "repeat_count": args.repeat,
        "case_timeout_seconds": args.case_timeout,
        "configuration_template": effective_config_snapshot(benchmark_config(args.version, "template", 0)),
        "runs": [],
    }
    write_json(result_path, results)
    if not results["commit_matches_expected"]:
        raise RuntimeError(
            f"{args.version} target commit mismatch: expected {EXPECTED_COMMITS[args.version]}, got {info['commit']}"
        )

    for repeat_index in range(1, args.repeat + 1):
        for index, case in enumerate(cases, 1):
            print(f"[{args.version}] case {index}/{len(cases)} repeat {repeat_index}: {case['case_id']}", flush=True)
            result = await run_one_case(
                target_root=target_root,
                version=args.version,
                case=case,
                repeat_index=repeat_index,
                case_timeout_seconds=args.case_timeout,
            )
            results["runs"].append(result)
            write_json(result_path, results)
            print(
                f"[{args.version}] {case['case_id']} -> {result['status']} "
                f"{result['metrics']['duration_ms']}ms, "
                f"reviews={result['metrics']['research_review_count']}, "
                f"followup={result['metrics']['review_triggered_followup']}",
                flush=True,
            )
    results["benchmark_finished_at_utc"] = utc_now()
    results["status"] = "completed"
    write_json(result_path, results)
    return 0


def run_worker_process(
    *,
    version: str,
    cases_path: Path,
    output_path: Path,
    repeat: int,
    case_timeout: int,
) -> dict[str, Any]:
    """Launch an isolated version worker so imports cannot cross-contaminate."""
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--version",
        version,
        "--cases",
        str(cases_path),
        "--output",
        str(output_path),
        "--repeat",
        str(repeat),
        "--case-timeout",
        str(case_timeout),
    ]
    process_env = os.environ.copy()
    target_src = str(TARGETS[version] / "src")
    process_env["PYTHONPATH"] = target_src + os.pathsep + process_env.get("PYTHONPATH", "")
    process = subprocess.run(
        command,
        cwd=TARGETS[version],
        env=process_env,
        capture_output=True,
        text=True,
        timeout=max(3_600, case_timeout * max(1, repeat) * 2),
    )
    log_path = output_path.with_suffix(".worker.log")
    log_path.write_text(
        f"$ {' '.join(command)}\n\nSTDOUT\n{process.stdout}\n\nSTDERR\n{process.stderr}",
        encoding="utf-8",
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"{version} worker failed with exit code {process.returncode}; "
            f"see {log_path}. Last output:\n{process.stdout[-2_000:]}\n{process.stderr[-2_000:]}"
        )
    return json.loads(output_path.read_text(encoding="utf-8"))


def orchestrator_main(args: argparse.Namespace) -> int:
    """Run both blocks and invoke the evaluator/comparator."""
    cases_path = Path(args.cases).resolve()
    results_dir = Path(args.results_dir).resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    before_path = results_dir / "before_results.json"
    after_path = results_dir / "after_results.json"
    benchmark_started = utc_now()
    for version, path in (("before", before_path), ("after", after_path)):
        print(f"Starting {version} worker at {utc_now()} (continuous version block).", flush=True)
        run_worker_process(
            version=version,
            cases_path=cases_path,
            output_path=path,
            repeat=args.repeat,
            case_timeout=args.case_timeout,
        )
    from evaluate_results import evaluate_file
    from compare_results import compare_files

    before_eval = evaluate_file(before_path, cases_path, HERE / "rubric.json")
    after_eval = evaluate_file(after_path, cases_path, HERE / "rubric.json")
    before_eval_path = results_dir / "before_evaluated.json"
    after_eval_path = results_dir / "after_evaluated.json"
    write_json(before_eval_path, before_eval)
    write_json(after_eval_path, after_eval)
    comparison = compare_files(
        before_path=before_path,
        after_path=after_path,
        before_eval_path=before_eval_path,
        after_eval_path=after_eval_path,
        cases_path=cases_path,
        rubric_path=HERE / "rubric.json",
        results_dir=results_dir,
        benchmark_started_at=benchmark_started,
    )
    write_json(results_dir / "comparison.json", comparison)
    print(f"Benchmark completed. Report: {results_dir / 'PLAN_EXECUTE_AB_COMPARISON.md'}", flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the public runner and private worker modes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help="Run one isolated version worker.")
    parser.add_argument("--version", choices=("before", "after"), help="Version for --worker.")
    parser.add_argument("--cases", default=str(CASES_PATH))
    parser.add_argument("--output", help="Raw output path for --worker.")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument("--case-timeout", type=int, default=900)
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.worker and (not args.version or not args.output):
        parser.error("--worker requires --version and --output")
    return args


if __name__ == "__main__":
    parsed = parse_args()
    if parsed.worker:
        raise SystemExit(asyncio.run(worker_main(parsed)))
    raise SystemExit(orchestrator_main(parsed))
