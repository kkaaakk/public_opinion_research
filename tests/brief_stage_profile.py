"""Measure bounded Plan and Report stages with synthetic inference.

Run with the root locked environment. This measures stage plumbing, NOT provider
latency, generated report quality, or production token savings. Native end-to-end
measurements use tests/test_brief_profile.py instead.
"""

import argparse
import asyncio
import json
import socket
import statistics
import time
from pathlib import Path

import conftest
import pytest
from langchain_core.messages import HumanMessage
from sdk_model_support import install_inference
from test_research_brief import BRIEF

from open_deep_research.budget import budget_capture
from open_deep_research.workflow import intake, reporting
from open_deep_research.workflow.brief import PlanDecision
from open_deep_research.workflow.python_state import apply_patch


def infer(request):
    if request.schema_name == "PlanDecision":
        return PlanDecision(action="ready", questions=[], brief_md=BRIEF)
    assert request.schema_name is None
    return "FINAL brand recall report, sourced and reviewed"


async def measure(kind):
    cfg = {
        "configurable": {
            "allow_clarification": True,
            "research_model": "openai:fixture-profile",
            "final_report_model": "openai:fixture-profile",
            "budget_enabled": True,
            "max_model_calls": 100,
        }
    }
    state = {
        "messages": [
            HumanMessage(
                content="Investigate Brand recall, last 30 days; distinguish facts from allegations."
            )
        ],
        "workflow": {"brief": BRIEF},
        "agents": {
            role: {
                "report": "Verified claim E1, source https://example.org; uncertainty remains."
            }
            for role in (
                "public_signal",
                "internal_knowledge",
                "risk_assessment",
                "response_strategy",
            )
        },
    }
    start = time.perf_counter()
    with budget_capture():
        if kind == "plan":
            apply_patch(state, (await intake.plan_research(state, cfg))["patch"])
        else:
            apply_patch(state, await reporting.generate_final_report(state, cfg))
    return dict(
        seconds=time.perf_counter() - start,
        usage=state.get("runtime", {}).get("budget", {}),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert conftest.os.environ["PYTHON_DOTENV_DISABLED"] == "1"
    if args.samples < 2:
        parser.error("Use at least two samples")

    result = {
        "inference": "identical synthetic 7 input / 3 output token replies, using synthetic inference",
        "stages": {},
    }
    original_connect = socket.socket.connect

    def local_connect(client, address):
        if isinstance(address, tuple) and address[0] not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise AssertionError("Stage profile attempted external network")
        return original_connect(client, address)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "connect", local_connect)
        install_inference(patch, infer)
        for kind in ("plan", "report"):
            samples = [asyncio.run(measure(kind)) for _ in range(args.samples)]
            values = [sample["seconds"] for sample in samples]
            result["stages"][kind] = dict(
                n=len(samples),
                p50=statistics.median(values),
                p95=statistics.quantiles(values, n=20, method="inclusive")[18],
                samples=samples,
            )
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
