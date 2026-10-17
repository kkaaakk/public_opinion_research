# 在指定模型配置下运行成本测量，并捕获流程事件与工具证据。
"""Control run: 5989480 (POR-after worktree) with the CURRENT v4-flash .env.

Same harness patches as run_round2_profile.py, but imports the frozen After
worktree code so the only remaining difference to Round 2 is the architecture.
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
MAIN_REPO = HERE.parents[1]
AFTER_ROOT = Path(r"C:\codex_workspace\POR-after")
sys.path.insert(0, str(HERE))

import run_cost_profile as base  # noqa: E402


# 运行测量流程并收集节点、工具和报告数据。
async def run_control(case: dict, timeout_seconds: int) -> dict:
    from dotenv import load_dotenv

    load_dotenv(base.ENV_PATH, override=True)
    # Same comparability adjustment as Round 2: the current .env enables the
    # Research Graph (added after this commit) without a URI.  The 5989480
    # code predates any handler for it, but disable defensively.
    os.environ["RESEARCH_GRAPH_ENABLED"] = "false"

    sys.path.insert(0, str(AFTER_ROOT / "src"))
    import agent_observer.sdk as observer_sdk
    import open_deep_research.deep_researcher as deep_researcher_module
    import open_deep_research.observability.agent_observer as observer_module
    from langchain_core.messages import HumanMessage

    events: list[dict] = []
    captured_tools: list[dict] = []

    # 将一个观测事件追加到本次测量记录。
    def record_event(run_id, sequence, event_type, *, span_id=None, **payload):
        events.append({
            "run_id": str(run_id),
            "sequence": sequence,
            "type": str(event_type),
            "span_id": str(span_id) if span_id is not None else None,
            **base.jsonable(payload, string_limit=50_000),
        })

    # 提供测量专用观察器，记录运行生命周期。
    class RecordingObserver:
        # 初始化观察器的记录状态。
        def __init__(self, **kwargs):
            self.inner = observer_sdk.AgentObserver(
                enabled=False,
                project=str(kwargs.get("project", "public-opinion-cost-profile")),
            )
            self.inner._record = record_event

        # 记录一次测量运行的开始信息。
        def start_run(self, **kwargs):
            return self.inner.start_run(**kwargs)

        # 结束观察器记录并执行收尾。
        def close(self, timeout: float = 1.0):
            self.inner.close(timeout)

    observer_module.AgentObserver = RecordingObserver
    original_tool = deep_researcher_module.observe_tool_ainvoke
    original_model = deep_researcher_module.observe_model_ainvoke

    # 包装工具执行，保留参数、结果及失败信息。
    async def capture_tool(tool, args, config=None, **kwargs):
        started_at = time.perf_counter()
        item = {
            "tool": str(getattr(tool, "name", None) or type(tool).__name__),
            "started_at": base.utc_now(),
            "args": base.bounded_args(args),
        }
        try:
            result = await original_tool(tool, args, config, **kwargs)
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

    deep_researcher_module.observe_tool_ainvoke = capture_tool

    # Importing the After code after the observer patch; ensure the same
    # recorder wins even if the module re-exports the symbol.
    import open_deep_research.deep_researcher as dr2  # noqa: F811
    dr2.observe_tool_ainvoke = capture_tool

    config = base.runtime_config("after_v4flash", str(case["case_id"]))
    config["configurable"]["rag_knowledge_base_paths"] = [
        str(MAIN_REPO / "data" / "knowledge" / "public_opinion")
    ]
    config["configurable"]["rag_memory_paths"] = [
        str(MAIN_REPO / "data" / "memory" / "public_opinion" / "memories.jsonl")
    ]

    started_at = time.perf_counter()
    started_utc = base.utc_now()
    updates: list[dict] = []
    status = "error"
    error = None
    try:
        graph = dr2.deep_researcher()

        # 消费研究流并收集节点增量和最终输出。
        async def consume():
            async for chunk in graph.astream(
                {"messages": [HumanMessage(content=str(case["query"]))]},
                config,
                stream_mode="updates",
                subgraphs=True,
                version="v2",
            ):
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

    elapsed_ms = max(0, int((time.perf_counter() - started_at) * 1_000))
    role_reports, round_reports = base.parse_reports(updates)
    reviews = base.parse_reviews(updates)
    completed_tasks = base.parse_completed_tasks(updates)
    final_report = ""
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
    result = {
        "schema_version": "1.0",
        "artifact": "single_case_cost_profile",
        "control": "after_5989480_with_v4flash",
        "status": status,
        "version": "after_v4flash",
        "case_id": case["case_id"],
        "case": base.jsonable(case, string_limit=50_000),
        "commit": base.git_info(AFTER_ROOT)["commit"],
        "target_root": str(AFTER_ROOT),
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
        },
        "diagnostics": {
            "observer_event_count": len(events),
            "model_request_count": len([e for e in events if e.get("type") == "model_request"]),
            "model_response_count": len([e for e in events if e.get("type") == "model_response"]),
            "observer_tool_call_count": len([e for e in events if e.get("type") == "tool_call"]),
            "observer_tool_result_count": len([e for e in events if e.get("type") == "tool_result"]),
        },
    }
    return result


# 在独立工作进程中执行测量并保存产物。
async def worker(args: argparse.Namespace) -> int:
    case = json.loads((MAIN_REPO / "evaluation" / "cost_profile" / "single_case.json").read_text(encoding="utf-8"))
    attempts = []
    result = None
    for attempt in range(1, args.max_attempts + 1):
        result = await run_control(case, args.case_timeout)
        attempts.append({
            "attempt": attempt,
            "status": result["status"],
            "elapsed_ms": result["elapsed_ms"],
            "model_calls": len(result["model_calls"]),
            "error_type": (result.get("error") or {}).get("type"),
        })
        if result["status"] == "success":
            break
        if result["elapsed_ms"] > 120_000:
            break
    result["attempts"] = attempts
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"[after_v4flash] {result['case_id']}: {result['status']} "
        f"{result['elapsed_ms']}ms, model_calls={len(result['model_calls'])}, "
        f"tool_calls={len(result['tool_calls'])}, attempts={len(attempts)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=HERE / "after_v4flash_cost_profile.json")
    parser.add_argument("--case-timeout", type=int, default=3600)
    parser.add_argument("--max-attempts", type=int, default=2)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(worker(args)))
