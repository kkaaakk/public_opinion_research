"""Compare actual bounded business services with identical synthetic inference.

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
from langchain_core.runnables import RunnableLambda
from test_research_brief import BRIEF
from test_sdk_phase2_integration import Services

from open_deep_research.budget import budget_capture
from open_deep_research.state import (
    ClarifyWithUser,
    ResearchQuestion,
    Section,
    Sections,
)
from open_deep_research.workflow import intake, reporting, shared
from open_deep_research.workflow.brief import PlanDecision
from open_deep_research.workflow.python_state import apply_patch

SECTIONS = [
    Section(
        name="Findings",
        description="Answer the research question with evidence",
        research=True,
    ),
    Section(name="Introduction", description="Scope and method", research=False),
    Section(name="Conclusion", description="Risk and next actions", research=False),
]


class Inference(Services):
    def with_structured_output(self, schema, include_raw=False):
        parsed = {
            ClarifyWithUser: ClarifyWithUser(
                need_clarification=False, question="", verification="Scope confirmed"
            ),
            ResearchQuestion: ResearchQuestion(research_brief=BRIEF),
            Sections: Sections(sections=SECTIONS),
            PlanDecision: PlanDecision(action="ready", questions=[], brief_md=BRIEF),
        }[schema]

        async def reply(messages, config=None):
            raw = await self.raw.ainvoke(messages, config)
            return dict(raw=raw, parsed=parsed, parsing_error=None)

        return RunnableLambda(reply)


async def measure(kind):
    cfg = {
        "configurable": {
            "allow_clarification": True,
            "research_graph_enabled": False,
            "research_model": "fixture:profile",
            "final_report_model": "fixture:profile",
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
        "report": {"sections": [s.model_copy(deep=True) for s in SECTIONS]},
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
        if kind == "old_plan":
            for fn in (
                intake.clarification_step,
                intake.research_brief_step,
                intake.section_plan_step,
            ):
                decision = await fn(state, cfg)
                apply_patch(state, decision.update or {})
        elif kind == "new_plan":
            apply_patch(state, (await intake.plan_research(state, cfg))["patch"])
        elif kind == "old_report":
            for fn in (
                reporting.section_writer,
                reporting.write_final_sections,
                reporting.compile_final_report,
            ):
                apply_patch(state, await fn(state, cfg))
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

    async def no_memory(*a, **kw):
        pass

    result = {
        "inference": "identical synthetic 7 input / 3 output token replies; no live provider; 3-section report fixture",
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
        patch.setattr(shared, "configurable_model", Inference())
        patch.setattr(reporting, "maybe_persist_chat_memory", no_memory)
        for kind in ("old_plan", "new_plan", "old_report", "new_report"):
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
