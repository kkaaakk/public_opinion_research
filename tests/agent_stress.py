"""Agent-focused stress / reliability test harness.

NOT a traditional load test.  Measures things that matter for agent systems:

  1. Completion rate     — what % of runs finish successfully?
  2. Latency profile     — p50 / p95 / p99 per scenario × mode
  3. Cost profile        — token consumption per run (input / output / total)
  4. Concurrency safety  — do parallel runs interfere with each other?
  5. Stability over time — does memory creep up after N runs?
  6. Error taxonomy      — what breaks? (rate-limit, timeout, graph-loop, …)

Usage:
    # Single-run smoke test
    python tests/agent_stress.py --runs 1

    # Reliability test (serial)
    python tests/agent_stress.py --runs 20 --scenario public_opinion_risk --mode fast

    # Concurrency stress
    python tests/agent_stress.py --runs 10 --concurrency 3

    # Full matrix (CAUTION — expensive!)
    python tests/agent_stress.py --full-matrix --runs 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════
# Test matrix
# ═══════════════════════════════════════════════════════════════════════════

SCENARIOS = ["public_opinion_risk"]
MODES = ["fast", "normal", "deep"]

TOPICS = {
    "public_opinion_risk": [
        "Analyze public sentiment around Tesla Cybertruck",
        "Apple brand reputation in EU regulatory environment",
    ],
}

ORG_CONTEXT = (
    "A Fortune 500 technology company with global consumer brand. "
    "Key stakeholders: C-suite, investors, regulators in US/EU."
)

# ═══════════════════════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class AgentResult:
    """Outcome of a single agent run."""

    scenario: str
    mode: str
    model: str
    topic: str
    success: bool
    error: str | None = None
    duration_s: float = 0.0
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    nodes_visited: list[str] = field(default_factory=list)
    report_length: int = 0


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════

class AgentStressTester:
    """Runs N agent invocations and collects structured results."""

    def __init__(self, base_url: str = "http://localhost:8000", timeout: int = 600):
        self.base_url = base_url
        self.timeout = timeout
        self.results: list[AgentResult] = []

    def run_one(self, scenario: str, mode: str) -> AgentResult:
        """Synchronous — callable from ThreadPoolExecutor for concurrency testing."""
        topic = TOPICS[scenario][0]
        model = "deepseek:deepseek-chat"
        payload = {
            "topic": topic,
            "model": model,
            "search_api": "tavily",
            "mode": mode,
            "scenario": scenario,
            "org_context": ORG_CONTEXT,
            "rag_enabled": False,
        }

        result = AgentResult(scenario=scenario, mode=mode, model=model, topic=topic)

        t0 = time.time()
        try:
            with httpx.stream(
                "POST",
                f"{self.base_url}/api/research",
                json=payload,
                timeout=self.timeout,
            ) as resp:
                if resp.status_code != 200:
                    result.error = f"HTTP {resp.status_code}"
                    result.duration_s = time.time() - t0
                    return result

                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[len("data:"):].strip())
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type")
                    if etype == "stream":
                        node = event.get("node", "")
                        if node and node not in result.nodes_visited:
                            result.nodes_visited.append(node)
                    elif etype == "report":
                        result.report_length = len(event.get("content", ""))
                    elif etype == "usage":
                        result.total_tokens = event.get("total_tokens", 0)
                        result.input_tokens = event.get("input_tokens", 0)
                        result.output_tokens = event.get("output_tokens", 0)
                        result.model_calls = event.get("model_calls", 0)
                    elif etype == "error":
                        result.error = event.get("message", "unknown")
                    elif etype == "done":
                        pass

                result.success = result.error is None and result.report_length > 0

        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"

        result.duration_s = time.time() - t0
        return result

    async def run_serial(self, runs: int, scenario: str, mode: str) -> list[AgentResult]:
        """Run sequentially — measures reliability over time."""
        results = []
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            for i in range(runs):
                print(f"  [{i + 1}/{runs}] {scenario}/{mode} … ", end="", flush=True)
                result = await loop.run_in_executor(pool, self.run_one, scenario, mode)
                results.append(result)
                status = "✅" if result.success else f"❌ {result.error}"
                print(f"{status}  ({result.duration_s:.0f}s, {result.total_tokens} tok)")
        self.results.extend(results)
        return results

    async def run_concurrent(self, runs: int, scenario: str, mode: str,
                             concurrency: int) -> list[AgentResult]:
        """Run with bounded concurrency — finds race conditions."""
        sem = asyncio.Semaphore(concurrency)
        loop = asyncio.get_running_loop()

        async def bounded_run(i: int):
            async with sem:
                print(f"  [{i + 1}/{runs}] {scenario}/{mode} … ", end="", flush=True)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    result = await loop.run_in_executor(pool, self.run_one, scenario, mode)
                status = "✅" if result.success else f"❌ {result.error}"
                print(f"{status}  ({result.duration_s:.0f}s)")
                return result

        tasks = [bounded_run(i) for i in range(runs)]
        results = await asyncio.gather(*tasks)
        self.results.extend(results)
        return results


# ═══════════════════════════════════════════════════════════════════════════
# Reporting
# ═══════════════════════════════════════════════════════════════════════════

def print_report(results: list[AgentResult]):
    if not results:
        print("No results.")
        return

    success = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    durations = [r.duration_s for r in success]

    print("\n" + "=" * 60)
    print("  AGENT STRESS TEST REPORT")
    print("=" * 60)

    # 1. Completion rate
    print(f"\n  Completion rate:  {len(success)}/{len(results)}  ({100 * len(success) / len(results):.0f}%)")

    # 2. Latency
    if durations:
        print(f"\n  Latency (seconds):")
        print(f"    min  : {min(durations):.0f}")
        print(f"    p50  : {statistics.median(durations):.0f}")
        if len(durations) >= 5:
            print(f"    p95  : {sorted(durations)[int(len(durations) * 0.95)]:.0f}")
        print(f"    max  : {max(durations):.0f}")
        print(f"    mean : {statistics.mean(durations):.0f}")

    # 3. Cost profile
    if success:
        tokens = [r.total_tokens for r in success if r.total_tokens > 0]
        if tokens:
            print(f"\n  Token usage (per run):")
            print(f"    median : {statistics.median(tokens):,}")
            print(f"    mean   : {statistics.mean(tokens):,.0f}")
            print(f"    total  : {sum(tokens):,}  (across {len(tokens)} successful runs)")

    # 4. Error taxonomy
    if failed:
        print(f"\n  Failures ({len(failed)}):")
        by_error = defaultdict(list)
        for r in failed:
            by_error[r.error or "unknown"].append(r)
        for err, items in sorted(by_error.items(), key=lambda x: -len(x[1])):
            print(f"    [{len(items)}×] {err[:120]}")

    # 5. Node coverage
    all_nodes = set()
    for r in success:
        all_nodes.update(r.nodes_visited)
    print(f"\n  Graph nodes visited: {sorted(all_nodes)}")

    # 6. Per-scenario breakdown
    print(f"\n  By scenario:")
    for scenario in SCENARIOS:
        subset = [r for r in results if r.scenario == scenario]
        if not subset:
            continue
        ok = sum(1 for r in subset if r.success)
        print(f"    {scenario}: {ok}/{len(subset)} passed")

    print("\n" + "=" * 60)


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(description="Agent stress/reliability test")
    parser.add_argument("--runs", type=int, default=1, help="Runs per combination")
    parser.add_argument("--scenario", choices=SCENARIOS, default="public_opinion_risk")
    parser.add_argument("--mode", choices=MODES, default="fast")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="Parallel runs (finds race conditions)")
    parser.add_argument("--full-matrix", action="store_true",
                        help="Test all scenario × mode combinations")
    parser.add_argument("--base-url", default=os.environ.get("TARGET_HOST", "http://localhost:8000"))
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    tester = AgentStressTester(base_url=args.base_url, timeout=args.timeout)

    print(f"\n  Agent Stress Test")
    print(f"  {'─' * 30}")
    print(f"  Target: {args.base_url}")
    print(f"  Runs:   {args.runs}")
    print(f"  Concurrency: {args.concurrency}")

    if args.full_matrix:
        for scenario in SCENARIOS:
            for mode in MODES:
                print(f"\n── {scenario} / {mode} ──")
                await tester.run_serial(args.runs, scenario, mode)
    elif args.concurrency > 1:
        print(f"\n── {args.scenario} / {args.mode} (concurrency={args.concurrency}) ──")
        await tester.run_concurrent(args.runs, args.scenario, args.mode, args.concurrency)
    else:
        print(f"\n── {args.scenario} / {args.mode} ──")
        await tester.run_serial(args.runs, args.scenario, args.mode)

    print_report(tester.results)

    # Exit non-zero if any run failed (CI-friendly)
    if any(not r.success for r in tester.results):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
