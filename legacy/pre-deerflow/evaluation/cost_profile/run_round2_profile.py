# 测量补充研究轮次，按新状态域恢复报告并周期保存检查点。
"""Round 2 cost-profile worker: run the fixed Tesla case against current main.

Reuses the analysis helpers from run_cost_profile.py but imports the target
modules from THIS repository checkout (current main) instead of the frozen
before/after worktrees.  The observer patch targets the refactored
``runtime.business_agent`` module where instrumentation now lives.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "src"))

import run_cost_profile as base  # noqa: E402

# The ownership-grouped state schema (commit 49954f9) stores role reports
# under `agents`, reviews/tasks under `workflow`, and the final report under
# `report`.  Keep those payloads at full length instead of the 5k default.
_original_summarize = base.summarize_change


# 提取需要保留的状态变化，减少重复消息内容。
def _summarize_change(key, value):
    if key in {"agents", "workflow", "report"}:
        return base.jsonable(value, string_limit=50_000)
    return _original_summarize(key, value)


base.summarize_change = _summarize_change


# 从按归属分组的状态结构恢复报告、复核和已完成任务。
def parse_ownership_state(updates: list[dict]) -> dict:
    """Recover reports/reviews/tasks from the ownership-grouped state schema."""
    import re as _re

    role_reports: dict[str, str] = {}
    round_reports: list[dict] = []
    reviews: list[dict] = []
    completed_tasks: list[dict] = []
    final_report = ""
    seen: set = set()
    for item in updates:
        changes = item.get("changes")
        if not isinstance(changes, dict):
            continue
        agents = changes.get("agents")
        if isinstance(agents, dict):
            for role, payload in agents.items():
                if not isinstance(payload, dict):
                    continue
                report = str(payload.get("report") or "")
                if not report:
                    continue
                match = _re.search(r"Research round:\s*(\d+)", report, _re.IGNORECASE)
                report_round = int(match.group(1)) if match else 1
                fingerprint = (role, report_round, report[:2000])
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                previous = role_reports.get(role, "")
                role_reports[role] = (
                    report if not previous
                    else (previous if report in previous
                          else previous + f"\n\n--- round {report_round} ---\n\n" + report)
                )
                round_reports.append({
                    "role": role,
                    "round": report_round,
                    "chars": len(report),
                    "estimated_tokens": (len(report) + 3) // 4,
                    "node": item.get("node"),
                    "namespace": item.get("namespace", []),
                })
        workflow = changes.get("workflow")
        if isinstance(workflow, dict):
            review = workflow.get("review")
            if isinstance(review, dict) and "research_complete" in review:
                reviews.append(review)
            for task in workflow.get("completed_tasks") or []:
                if isinstance(task, dict):
                    completed_tasks.append(task)
        report_state = changes.get("report")
        if isinstance(report_state, dict):
            final = report_state.get("final")
            if isinstance(final, str) and final:
                final_report = final
            elif isinstance(final, dict) and isinstance(final.get("value"), str):
                final_report = final["value"]
    return {
        "role_reports": role_reports,
        "round_reports": round_reports,
        "reviews": reviews,
        "completed_tasks": completed_tasks,
        "final_report": final_report,
    }


# 运行第二轮测量，捕获事件及中间状态。
async def run_round2(case: dict, timeout_seconds: int) -> dict:
    from dotenv import load_dotenv

    load_dotenv(base.ENV_PATH, override=True)
    # Round 2 comparability note: RESEARCH_GRAPH_ENABLED=true in the repo .env
    # (from commit 2bdb9ad) has no research_graph_uri configured, which makes
    # every business agent raise before doing any work.  Configuration
    # precedence here is env-var > runnable config, so this must be cleared in
    # the process environment.  The 5989480 After benchmark predates this
    # setting entirely, so disabling it matches the benchmarked path.
    os.environ["RESEARCH_GRAPH_ENABLED"] = "false"

    import agent_observer.sdk as observer_sdk
    import open_deep_research.deep_researcher as deep_researcher_module
    import open_deep_research.observability.agent_observer as observer_module
    import open_deep_research.runtime.business_agent as business_agent_module
    from langchain_core.messages import HumanMessage

    events: list[dict] = []
    captured_tools: list[dict] = []

    # 向本次运行追加观测事件。
    def record_event(run_id, sequence, event_type, *, span_id=None, **payload):
        events.append({
            "run_id": str(run_id),
            "sequence": sequence,
            "type": str(event_type),
            "span_id": str(span_id) if span_id is not None else None,
            **base.jsonable(payload, string_limit=50_000),
        })

    # 记录第二轮测量的运行生命周期。
    class RecordingObserver:
        # 初始化测量观察器。
        def __init__(self, **kwargs):
            self.inner = observer_sdk.AgentObserver(
                enabled=False,
                project=str(kwargs.get("project", "public-opinion-cost-profile")),
            )
            self.inner._record = record_event

        # 记录运行开始及关联信息。
        def start_run(self, **kwargs):
            return self.inner.start_run(**kwargs)

        # 执行观察器结束时的记录收尾。
        def close(self, timeout: float = 1.0):
            self.inner.close(timeout)

    observer_module.AgentObserver = RecordingObserver

    # Tool capture: patch the instrumentation entry points that exist post-refactor.
    original_tool_ba = business_agent_module.observe_tool_ainvoke

    # 捕获工具执行参数、结果及用量诊断。
    async def capture_tool(tool, args, config=None, **kwargs):
        started_at = time.perf_counter()
        item = {
            "tool": str(getattr(tool, "name", None) or type(tool).__name__),
            "started_at": base.utc_now(),
            "args": base.bounded_args(args),
        }
        try:
            result = await original_tool_ba(tool, args, config, **kwargs)
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
            "result": base.result_summary(result),
        })
        captured_tools.append(item)
        return result

    business_agent_module.observe_tool_ainvoke = capture_tool

    config = base.runtime_config("round2", str(case["case_id"]))
    config["configurable"]["rag_knowledge_base_paths"] = [
        str(REPO_ROOT / "data" / "knowledge" / "public_opinion")
    ]
    config["configurable"]["rag_memory_paths"] = [
        str(REPO_ROOT / "data" / "memory" / "public_opinion" / "memories.jsonl")
    ]
    # Round 2 comparability note: RESEARCH_GRAPH_ENABLED=true in the repo .env
    # (added by commit 2bdb9ad) but no research_graph_uri is configured, which
    # makes every business agent raise before doing any work.  The 5989480
    # After benchmark predates this setting entirely (its store is not
    # instantiated), so disabling it here matches the benchmarked execution
    # path rather than changing the researched algorithm.
    config["configurable"]["research_graph_enabled"] = False

    started_at = time.perf_counter()
    started_utc = base.utc_now()
    updates: list[dict] = []
    status = "error"
    error = None

    checkpoint_path = Path(os.environ.get(
        "ROUND2_CHECKPOINT",
        str(HERE / "round2_checkpoint.json"),
    ))

    # 定期把内存证据写入检查点，进程被中止时仍可恢复部分数据。
    async def checkpoint_loop() -> None:
        """Flush in-memory evidence periodically so a killed worker still leaves data."""
        while True:
            await asyncio.sleep(60)
            checkpoint = {
                "started_at_utc": started_utc,
                "elapsed_ms": max(0, int((time.perf_counter() - started_at) * 1_000)),
                "model_call_count": len([e for e in events if e.get("type") == "model_request"]),
                "tool_count": len(captured_tools),
                "observer_events": events,
                "tool_capture_records": captured_tools,
                "stream_updates": updates,
            }
            tmp = checkpoint_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8")
            tmp.replace(checkpoint_path)

    checkpoint_task = asyncio.create_task(checkpoint_loop())
    try:
        graph = deep_researcher_module.deep_researcher()

        # 消费研究图事件并更新测量状态。
        async def consume():
            async for chunk in graph.astream(
                {"messages": [HumanMessage(content=str(case["query"]))]},
                config,
                stream_mode="updates",
                subgraphs=True,
                version="v2",
            ):
                # Current LangGraph yields (namespace, data) tuples for v2
                # updates with subgraphs; normalize to the dict shape the
                # shared collector understands.
                if isinstance(chunk, tuple) and len(chunk) == 2:
                    namespace, data = chunk
                    chunk = {
                        "type": "updates",
                        "ns": list(namespace) if isinstance(namespace, (list, tuple)) else [namespace],
                        "data": data,
                    }
                base.collect_stream_chunk(chunk, updates)

        await asyncio.wait_for(consume(), timeout=timeout_seconds)
        status = "success"
    except asyncio.TimeoutError:
        status = "timeout"
        error = {"type": "TimeoutError", "message": f"case exceeded {timeout_seconds}s"}
    except BaseException as exc:
        status = "error"
        error = {
            "type": type(exc).__name__,
            "message": str(exc)[:2_000],
            "traceback": traceback.format_exc(limit=30),
        }
    finally:
        checkpoint_task.cancel()

    elapsed_ms = max(0, int((time.perf_counter() - started_at) * 1_000))
    legacy_role_reports, legacy_round_reports = base.parse_reports(updates)
    legacy_reviews = base.parse_reviews(updates)
    legacy_tasks = base.parse_completed_tasks(updates)
    ownership = parse_ownership_state(updates)
    role_reports = ownership["role_reports"] or legacy_role_reports
    round_reports = ownership["round_reports"] or legacy_round_reports
    reviews = ownership["reviews"] or legacy_reviews
    completed_tasks = ownership["completed_tasks"] or legacy_tasks
    final_report = ownership["final_report"]
    if not final_report:
        for item in reversed(updates):
            changes = item.get("changes")
            if isinstance(changes, dict) and isinstance(changes.get("final_report"), str):
                final_report = changes["final_report"]
                break
    if status == "success" and not final_report:
        status = "incomplete"
        error = {
            "type": "NoFinalReport",
            "message": "Graph ended without a final_report; this run is not a complete research execution.",
        }
    spans_by_id, spans = base.span_info(events)
    calls = base.model_calls(events, spans_by_id)
    tools = base.tool_calls(events, spans_by_id, captured_tools)
    flow = base.flow_and_summaries(events, calls, tools, spans)
    return {
        "schema_version": "1.0",
        "artifact": "single_case_cost_profile",
        "round": 2,
        "status": status,
        "version": "round2",
        "case_id": case["case_id"],
        "case": base.jsonable(case, string_limit=50_000),
        "commit": base.git_info(REPO_ROOT)["commit"],
        "target_root": str(REPO_ROOT),
        "started_at_utc": started_utc,
        "finished_at_utc": base.utc_now(),
        "elapsed_ms": elapsed_ms,
        "effective_configuration": base.config_snapshot(config),
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
            "report_growth": base.report_sizes(round_reports),
            "react_input_progression": base.react_progression(calls),
            "role_reports_reuse": base.repeated_context_analysis(calls, round_reports, updates),
            "token_shares": base.share_by_call_type(calls),
            "input_output_ratios": base.token_ratios(calls),
            "section_writer": {
                "section_count": sum(
                    len(item.get("changes", {}).get("sections", []))
                    for item in updates
                    if isinstance(item.get("changes"), dict)
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
            "model_request_count": len([e for e in events if e.get("type") == "model_request"]),
            "model_response_count": len([e for e in events if e.get("type") == "model_response"]),
            "observer_tool_call_count": len([e for e in events if e.get("type") == "tool_call"]),
            "observer_tool_result_count": len([e for e in events if e.get("type") == "tool_result"]),
        },
    }


# 执行隔离测量并保存最终或失败产物。
async def worker(args: argparse.Namespace) -> int:
    case = json.loads((REPO_ROOT / "evaluation" / "cost_profile" / "single_case.json").read_text(encoding="utf-8"))
    attempts = []
    result = None
    for attempt in range(1, args.max_attempts + 1):
        result = await run_round2(case, args.case_timeout)
        attempts.append({
            "attempt": attempt,
            "status": result["status"],
            "elapsed_ms": result["elapsed_ms"],
            "model_calls": len(result["model_calls"]),
            "error_type": (result.get("error") or {}).get("type"),
            "error_message": ((result.get("error") or {}).get("message") or "")[:300],
        })
        if result["status"] == "success":
            break
        # Provider flakiness: structured clarification output can return None
        # (fast failure) and mid-run connection errors can occur at any time.
        # Retry both; hard timeouts and other slow failures stop the loop.
        error_type = (result.get("error") or {}).get("type")
        if result["elapsed_ms"] > 120_000 and error_type not in {
            "APIConnectionError",
            "APITimeoutError",
        }:
            break
    result["attempts"] = attempts
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"[round2] {result['case_id']}: {result['status']} "
        f"{result['elapsed_ms']}ms, model_calls={len(result['model_calls'])}, "
        f"tool_calls={len(result['tool_calls'])}, attempts={len(attempts)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=HERE / "round2_cost_profile.json")
    parser.add_argument("--case-timeout", type=int, default=1800)
    parser.add_argument("--max-attempts", type=int, default=6)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(worker(args)))
