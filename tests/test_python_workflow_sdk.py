"""Exercise SDK research agents and bounded model calls offline."""

import asyncio
import json
import sys
import time
from collections import Counter
from contextlib import asynccontextmanager

import pytest
from agents.exceptions import ModelBehaviorError
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from openai.types.responses import ResponseReasoningItem
from reference_workflow import PythonWorkflow, WorkflowExecutionError
from sdk_model_support import Reply, install_inference
from test_python_workflow import ROLES, config, initial
from test_sdk_runtime import TOKENS, _tool

from open_deep_research.budget import ModelBudgetExceeded, budget_capture
from open_deep_research.prompts import response_strategy_prompt, risk_assessment_prompt
from open_deep_research.runtime import business_agent
from open_deep_research.runtime.compaction import RollingCompactOutput
from open_deep_research.runtime.sdk_runtime import SDKAgentRuntime
from open_deep_research.state import (
    ClarifyWithUser,
    ResearchQuestion,
    ResearchReview,
)
from open_deep_research.workflow.brief import BriefRevision, PlanDecision, PlanQuestion

CLARIFICATION_QUESTIONS = (
    PlanQuestion(id="brand", text="Which brand?", kind="text", options=[]),
    PlanQuestion(
        id="period",
        text="Which time range?",
        kind="single",
        options=["Last 7 days", "Last 30 days"],
    ),
    PlanQuestion(id="goal", text="Which decision?", kind="text", options=[]),
)
CLARIFICATION_ANSWERS = (
    {"brand": "Brand A"},
    {"period": "Last 7 days"},
    {"goal": "Decide whether to issue a statement"},
)


def install_sdk(
    monkeypatch,
    tmp_path,
    mode="normal",
    *,
    local_tool=None,
    on_plan=None,
    on_revision=None,
):
    """Replace inference and external tools; keep actual Agent/Runner services."""
    runtimes = []
    models = []
    calls = Counter()
    entered = set()
    barrier = asyncio.Event()
    second_entered = set()
    second_barrier = asyncio.Event()
    blocked = asyncio.Event()
    model_windows = []
    original_state = initial()

    async def respond(request):
        schema = request.schema_name
        if request.model_ref == "openai:fixture-review-attempt":
            return Reply("controlled failed review", 2, 0)
        if schema is None:
            if request.prompt == risk_assessment_prompt:
                return "risk_assessment analysis"
            if request.prompt == response_strategy_prompt:
                return "response_strategy analysis"
            return "FINAL brand recall report, sourced and reviewed"
        if schema == "RollingCompactOutput":
            return RollingCompactOutput(
                rolling_summary="Older verified research history summarized."
            )
        if schema == "BriefRevision":
            assert request.messages[0]["role"] == "system"
            assert request.messages[-1]["role"] == "user"
            data = json.loads(request.messages[-1]["content"])
            parsed = await on_revision(data) if on_revision is not None else None
            return parsed if parsed is not None else BriefRevision(
                brief_md=data["current_brief"] + "\n\n" + data["feedback"]
            )
        if schema == "PlanDecision":
            prompt = request.prompt
            if on_plan is not None:
                on_plan(prompt)
            completed = prompt.count("\nUser response: ")
            target = {"clarify": 1, "clarify-2": 2, "clarify-3": 3}.get(mode, 0)
            if "browser-clarify" in prompt:
                target = 2 if "browser-clarify-multi" in prompt else 1
            if mode == "clarify-over-limit" or (
                completed < target and "Clarification allowed: True." in prompt
            ):
                return PlanDecision(
                    action="ask_user",
                    questions=[CLARIFICATION_QUESTIONS[min(completed, len(CLARIFICATION_QUESTIONS) - 1)]],
                    brief_md="",
                )
            return PlanDecision(
                action="ready",
                questions=[],
                brief_md="# Brand recall research\n\n## Objective\nEstablish reliable recall facts.\n\n## Scope\nBrand, last 30 days, geography unspecified.\n\n## Core questions\nWhat happened and which sources support it?\n\n## Constraints\nDistinguish allegations from facts; cite sources.\n\n## Completion criteria\nAnswer the question or explain missing evidence; match risk and strategy.",
            )
        return {
            "ClarifyWithUser": ClarifyWithUser(
                need_clarification=mode == "clarify", question="Which brand?", verification="Scope confirmed",
            ),
            "ResearchQuestion": ResearchQuestion(research_brief="Investigate brand recall evidence"),
            "ResearchReview": ResearchReview(research_complete=True),
        }[schema]

    class RecordingRuntime(SDKAgentRuntime):
        async def run(self, task):
            runtimes.append(self)
            self.model_windows = model_windows
            assert self.role in ROLES[:2]
            if mode == "admission":
                # Release both producers together, then let admission serialize
                # their physical model calls.
                entered.add(self.role)
                if len(entered) == 2:
                    barrier.set()
                await asyncio.wait_for(barrier.wait(), 3)
            return await super().run(task)

    @asynccontextmanager
    async def model_context(*args, **kwargs):
        current = {}

        async def first(call):
            role = call.system_instructions.split("<Role>\n", 1)[1].split(
                "\n</Role>", 1
            )[0]
            assert role in ROLES[:2]
            current["role"] = role
            text = json.dumps(call.input)
            assert f"private-{role}" not in text
            if mode == "admission":
                started = time.monotonic()
                await asyncio.sleep(0.2)
                model_windows.append((role, started, time.monotonic()))
            else:
                entered.add(role)
                if len(entered) == 2:
                    barrier.set()
                await asyncio.wait_for(barrier.wait(), 3)
            tool_name = (
                "web_search"
                if role == "public_signal"
                else "knowledge__lookup"
                if local_tool
                else "rag_search"
            )
            pending = [
                function_call(tool_name, {"query": role}, call_id=f"lookup-{role}")
            ]
            if mode == "tool_budget_multi":
                pending.append(
                    function_call(
                        tool_name,
                        {"query": role + "-second"},
                        call_id=f"lookup-{role}-2",
                    )
                )
            return pending

        async def second(call):
            role = current["role"]
            text = json.dumps(call.input)
            if mode.startswith("tool_budget"):
                # A budget-rejected sibling returns failure text, not evidence.
                return [assistant_message("producer analysis complete")]
            assert "research_raw_search" in text or "Synthetic internal brand recall evidence" in text
            if mode == "admission":
                started = time.monotonic()
                await asyncio.sleep(0.2)
                model_windows.append((role, started, time.monotonic()))
            if mode == "double_error":
                second_entered.add(role)
                if len(second_entered) == 2:
                    second_barrier.set()
                await second_barrier.wait()
                raise ModelBehaviorError(f"model failed {role}")
            if role == "public_signal":
                if mode == "model_error":
                    raise ModelBehaviorError("failed after successful side effect")
                if mode in {"timeout", "cancel"}:
                    blocked.set()
                    await asyncio.Event().wait()
            return [assistant_message("producer analysis complete")]

        steps = [ModelStep.respond(first), ModelStep.respond(second)]
        if mode == "rolling":

            def continuation(call):
                role = current["role"]
                n = current["turn"] = current.get("turn", 0) + 1
                name = "web_search" if role == "public_signal" else "rag_search"
                return [
                    ResponseReasoningItem(
                        id=f"reason-{n}",
                        type="reasoning",
                        summary=[],
                        content=[
                            {
                                "type": "reasoning_text",
                                "text": "decision about verified brand evidence " * 160,
                            }
                        ],
                    ),
                    function_call(
                        name, {"query": role}, call_id=f"turn-{len(models)}-{n}"
                    ),
                ]

            steps = (
                [ModelStep.respond(first)]
                + [ModelStep.respond(continuation) for _ in range(24)]
                + [[assistant_message("long research complete")]]
            )
        model = ScriptedModel(steps, default_usage=TOKENS)
        models.append(model)
        yield model

    async def tools(config, role, spec):
        assert role in ROLES[:2]

        async def search(query, limit=3):
            calls[role] += 1
            return json.dumps(
                {
                    "type": "research_raw_search",
                    "results": [
                        {
                            "url": f"https://example.test/{role}",
                            "title": f"Notice {role}",
                            "content": f"Official brand recall evidence for {query}.",
                        }
                    ],
                }
            )

        if role == "internal_knowledge" and local_tool is not None:
            return [local_tool]
        return [_tool(role, coroutine=search, effect="read")]

    monkeypatch.setattr(business_agent, "SDKAgentRuntime", RecordingRuntime)
    monkeypatch.setattr(business_agent, "sdk_model", model_context)
    monkeypatch.setattr(business_agent, "_business_agent_tools", tools)
    install_inference(monkeypatch, respond)
    cfg = config(
        recent_raw_steps=0,
        allow_clarification=True,
        research_model="openai:fixture-review",
        max_structured_output_retries=1,
        budget_enabled=True,
        max_model_calls=100,
    )
    if mode == "timeout":
        cfg["configurable"]["workflow_step_timeout_seconds"] = 1.5
    if mode == "rolling":
        cfg["configurable"].update(
            context_capacity_tokens=22_000,
            recent_raw_steps=1,
            max_model_calls=500,
            max_react_tool_calls=40,
        )
    return original_state, cfg, runtimes, models, calls, blocked


def test_real_full_intake_runner_report_and_natural_completion(monkeypatch, tmp_path):
    async def run():
        state, cfg, runtimes, models, calls, _ = install_sdk(
            monkeypatch, tmp_path, "normal"
        )
        with budget_capture() as usage:
            result = await PythonWorkflow().ainvoke(state, cfg)
        assert "FINAL brand recall report" in result["report"]["final"]
        assert result["workflow"]["brief"] == "Investigate brand recall evidence"
        assert result["messages"][1].content == "Scope confirmed"
        assert len(runtimes) == len(models) == 2
        assert {rt.role for rt in runtimes} == set(ROLES[:2])
        assert set(result["agents"]) == set(ROLES)
        for role in ROLES[2:]:
            assert result["agents"][role]["report"] == f"{role} analysis"
        assert all(rt.harness.last_settlement.succeeded for rt in runtimes)
        assert calls == {"public_signal": 1, "internal_knowledge": 1}
        assert result["runtime"]["budget"]["model_calls"] == 7 + usage["model_calls"]
        assert usage["model_calls"] > 0

    asyncio.run(run())


def test_real_clarification_stops_before_any_agent(monkeypatch, tmp_path):
    async def run():
        state, cfg, runtimes, _, calls, _ = install_sdk(
            monkeypatch, tmp_path, "clarify"
        )
        result = await PythonWorkflow().ainvoke(state, cfg)
        assert result["messages"][-1].content == "Which brand?"
        assert not runtimes and not calls
        assert "final" not in result["report"]
        assert result["runtime"]["budget"]["model_calls"] == 8

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["model_error", "double_error", "timeout", "cancel"])
def test_real_sdk_failures_settle_and_never_replay_committed_tools(
    monkeypatch, tmp_path, mode
):
    async def run():
        state, cfg, runtimes, models, calls, blocked = install_sdk(
            monkeypatch, tmp_path, mode
        )
        task = asyncio.create_task(
            PythonWorkflow().ainvoke(state, cfg, start_at="research")
        )
        if mode == "cancel":
            await asyncio.wait_for(blocked.wait(), 3)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
        with pytest.raises(
            asyncio.CancelledError if mode == "cancel" else WorkflowExecutionError
        ) as caught:
            await task
        assert calls == {"public_signal": 1, "internal_knowledge": 1}
        assert len(runtimes) == len(models) == 2
        assert all(rt.harness.last_settlement.succeeded for rt in runtimes)
        assert all(
            rt.harness.results[f"lookup-{rt.role}"].status == "succeeded"
            for rt in runtimes
        )
        error = caught.value
        failures = error.workflow_failures if mode == "cancel" else error.failures
        for failure in failures.values():
            assert failure.context_harness in [rt.harness for rt in runtimes]
        if mode != "cancel":
            assert all(error.context_harnesses.values())
        total = sum(rt.budget_usage["model_calls"] for rt in runtimes)
        assert error.budget_usage["model_calls"] == total
        partial = error.workflow_state if mode == "cancel" else error.state
        assert partial["runtime"]["budget"]["model_calls"] == 7 + total
        assert "final" not in partial["report"]

    asyncio.run(run())


def test_python_workflow_with_real_local_mcp_transport(monkeypatch, tmp_path):
    from agents.mcp import MCPServerStdio

    from open_deep_research.mcp.tool_wrapper import build_mcp_tool

    script = tmp_path / "phase3_mcp.py"
    script.write_text(
        """from mcp.server.fastmcp import FastMCP
server = FastMCP("phase3-synthetic")
@server.tool()
def lookup(query: str) -> str:
    return "Synthetic internal brand recall evidence: " + query
if __name__ == "__main__":
    server.run()
""",
        encoding="utf-8",
    )
    params = {"command": sys.executable, "args": [str(script)]}

    def new_server():
        return MCPServerStdio(
            params,
            name="phase3-synthetic",
            cache_tools_list=False,
            client_session_timeout_seconds=10,
        )

    async def discover():
        server = new_server()
        async with server:
            mcp_tool = next(
                tool for tool in await server.list_tools() if tool.name == "lookup"
            )
            return build_mcp_tool(
                mcp_tool,
                server,
                server_name="phase3-synthetic",
                server_factory=new_server,
                public_name="knowledge__lookup",
                domain="rag",
                effect="read",
            )

    tool = asyncio.run(discover())

    async def run():
        state, cfg, runtimes, _, _, _ = install_sdk(
            monkeypatch, tmp_path, local_tool=tool
        )
        result = await PythonWorkflow().ainvoke(state, cfg)
        assert result["report"]["final"]
        rt = next(rt for rt in runtimes if rt.role == "internal_knowledge")
        assert (
            rt.harness.results["lookup-internal_knowledge"].status == "succeeded"
        )
        assert all(rt.harness.last_settlement.succeeded for rt in runtimes)

    asyncio.run(run())


def test_sdk_budget_stop_degrades_explicitly_without_extra_agent_calls(
    monkeypatch, tmp_path
):
    async def run():
        state, cfg, runtimes, models, calls, _ = install_sdk(monkeypatch, tmp_path)
        cfg["configurable"].update(max_model_calls=8)

        async def tools(config, role, spec):
            return [_tool(role)]

        monkeypatch.setattr(business_agent, "_business_agent_tools", tools)
        with budget_capture() as usage, pytest.raises(WorkflowExecutionError) as caught:
            await PythonWorkflow().ainvoke(state, cfg, start_at="research")

        failure = caught.value
        assert failure.kinds == {"generate_final_report": "budget_exhausted"}
        assert isinstance(failure.failures["generate_final_report"], ModelBudgetExceeded)
        result = failure.state
        assert len(runtimes) == 2
        assert all(not model.calls for model in models)
        assert not calls
        budget = result["runtime"]["budget"]
        assert budget["model_calls"] == 8
        assert failure.budget_usage["model_calls"] == usage["model_calls"] == 1
        assert budget["degradation_reasons"]
        for role in ROLES[2:]:
            assert "research model budget was exhausted" in result["agents"][role]["report"]
        assert "final" not in result["report"]
        assert result["messages"] == state["messages"]

    asyncio.run(run())


def test_rolling_compaction_stays_invocation_local(monkeypatch, tmp_path):
    async def run():
        state, cfg, runtimes, models, calls, _ = install_sdk(
            monkeypatch, tmp_path, "rolling"
        )
        result = await PythonWorkflow().ainvoke(state, cfg, start_at="research")
        for rt in runtimes[:2]:
            assert rt.harness._compacted_turns
            assert rt.harness.rolling_summary
            assert rt.harness.last_settlement.succeeded
            assert len(rt.model.calls) == 26
        assert calls == {"public_signal": 25, "internal_knowledge": 25}
        assert result["report"]["final"]
        assert "rolling_summary" not in result["agents"]["public_signal"]
        assert "memory" not in result["agents"]["public_signal"]

    asyncio.run(run())
