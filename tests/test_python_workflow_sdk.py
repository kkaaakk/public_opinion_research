"""Run the real Python Workflow, SDK Runner, evidence Harness and report services."""

import asyncio
import json
import sys
from collections import Counter
from contextlib import asynccontextmanager

import pytest
from agents.exceptions import ModelBehaviorError
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from langchain_core.runnables import RunnableLambda
from openai.types.responses import ResponseReasoningItem
from reference_workflow import (
    PythonWorkflow,
    WorkflowExecutionError,
)
from test_python_workflow import ROLES, config, initial
from test_sdk_context import _payload
from test_sdk_phase2_integration import Services, run_workflow_fixture
from test_sdk_runtime import TOKENS, _tool

from open_deep_research.budget import budget_capture
from open_deep_research.configuration import Configuration
from open_deep_research.research_graph.store import (
    clear_research_graph_store_cache,
    create_research_graph_store,
)
from open_deep_research.runtime import business_agent, sdk_context
from open_deep_research.runtime.sdk_runtime import SDKAgentRuntime
from open_deep_research.state import (
    ClarifyWithUser,
    ResearchQuestion,
    ResearchReview,
    Sections,
)
from open_deep_research.workflow import shared
from open_deep_research.workflow.brief import PlanDecision, PlanQuestion


def test_fixed_fixture_repeated_sdk_runs_preserve_context_and_evidence(
    monkeypatch, tmp_path
):
    original_init = sdk_context.SDKContextHarness.__init__

    def fixed_invocation(self, workspace, **kwargs):
        original_init(self, workspace, **kwargs)
        self.sdk_run_id = f"fixture-{workspace.role}-{workspace.scope.research_round}"

    monkeypatch.setattr(sdk_context.SDKContextHarness, "__init__", fixed_invocation)
    old, old_evidence, old_events, old_tools = run_workflow_fixture(
        monkeypatch, tmp_path / "legacy"
    )
    new, new_evidence, new_events, new_tools = run_workflow_fixture(
        monkeypatch, tmp_path / "python", "python"
    )
    assert new["workflow"] == old["workflow"]
    assert new["report"] == old["report"]
    assert new["runtime"]["budget"] == old["runtime"]["budget"]
    assert new_evidence == old_evidence
    assert sorted(new_tools) == sorted(old_tools)
    assert Counter(new_events) == Counter(old_events)
    for role in ROLES:
        assert new["agents"][role] == old["agents"][role]
        left = new["research"]["working_contexts"][role]
        right = old["research"]["working_contexts"][role]
        for key in ("current_objective", "recent_progress"):
            assert left[key] == right[key]
        for key in ("confirmed_findings", "conflicts", "open_gaps"):
            assert {v["summary"] for v in left[key]} == {
                v["summary"] for v in right[key]
            }
        if role in ROLES[2:]:
            assert left["active_evidence_ids"] == right["active_evidence_ids"]
        all_ids = {node[0] for node in new_evidence}
        for context in (left, right):
            assert context["active_evidence_ids"]
            assert set(context["active_evidence_ids"]).issubset(all_ids)


def install_sdk(monkeypatch, tmp_path, mode="normal", *, local_tool=None):
    """Replace inference and external tools; keep actual Agent/Runner/Graph services."""
    clear_research_graph_store_cache()
    services = Services()
    runtimes = []
    models = []
    calls = Counter()
    entered = set()
    barrier = asyncio.Event()
    second_entered = set()
    second_barrier = asyncio.Event()
    blocked = asyncio.Event()
    original_state = initial()

    class WorkflowServices(Services):
        def with_structured_output(self, schema, include_raw=False):
            values = {
                PlanDecision: PlanDecision(
                    action="ready",
                    questions=[],
                    brief_md="# Brand recall research\n\n## Objective\nEstablish reliable recall facts.\n\n## Scope\nBrand, last 30 days, geography unspecified.\n\n## Core questions\nWhat happened and which sources support it?\n\n## Constraints\nDistinguish allegations from facts; cite sources.\n\n## Completion criteria\nAnswer the question or explain missing evidence; match risk and strategy.",
                ),
                ClarifyWithUser: ClarifyWithUser(
                    need_clarification=mode == "clarify",
                    question="Which brand?",
                    verification="Scope confirmed",
                ),
                ResearchQuestion: ResearchQuestion(
                    research_brief="Investigate brand recall evidence"
                ),
                Sections: Sections(sections=original_state["report"]["sections"]),
                ResearchReview: ResearchReview(research_complete=True),
            }
            if schema not in values:
                return super().with_structured_output(schema, include_raw)

            async def reply(messages, config=None):
                raw = await self.raw.ainvoke(messages, config)
                parsed = values[schema]
                if (
                    schema is PlanDecision
                    and (mode == "clarify" or "browser-clarify" in str(messages))
                    and "Clarification allowed: True." in str(messages)
                ):
                    parsed = PlanDecision(
                        action="ask_user",
                        questions=[
                            PlanQuestion(
                                id="brand", text="Which brand?", kind="text", options=[]
                            )
                        ],
                        brief_md="",
                    )
                return {"raw": raw, "parsed": parsed, "parsing_error": None}

            return RunnableLambda(reply)

    class RecordingRuntime(SDKAgentRuntime):
        async def run(self, task):
            runtimes.append(self)
            return await super().run(task)

    @asynccontextmanager
    async def model_context(*args, **kwargs):
        assert kwargs["max_retries"] == 0
        current = {}

        async def first(call):
            role = call.system_instructions.split("<Role>\n", 1)[1].split(
                "\n</Role>", 1
            )[0]
            current["role"] = role
            text = json.dumps(call.input)
            assert f"private-{role}" in text
            assert all(
                f"private-{other}" not in text for other in ROLES if other != role
            )
            if role in ROLES[:2]:
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
                return [
                    function_call(tool_name, {"query": role}, call_id=f"lookup-{role}")
                ]
            assert "[Evidence]" in text
            if role == "response_strategy":
                assert "risk_assessment GRAPH REPORT" in text
            return [assistant_message(f"{role} analysis")]

        async def second(call):
            role = current["role"]
            assert "Persisted to Research Graph" in json.dumps(call.input)
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
                if mode == "complete":
                    return [
                        function_call(
                            "ResearchComplete", {"query": "done"}, call_id="complete"
                        )
                    ]
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
        if role not in ROLES[:2]:
            return []

        async def search(query, limit=3):
            calls[role] += 1
            return _payload(role)

        if role == "internal_knowledge" and local_tool is not None:
            return [local_tool]
        result = [_tool(role, coroutine=search)]
        if mode == "complete":
            result.append(_tool(role, name="ResearchComplete", domain="core"))
        return result

    monkeypatch.setattr(business_agent, "SDKAgentRuntime", RecordingRuntime)
    monkeypatch.setattr(business_agent, "sdk_model", model_context)
    monkeypatch.setattr(business_agent, "_business_agent_tools", tools)
    monkeypatch.setattr(business_agent, "_CONFIGURABLE_MODEL", services)
    monkeypatch.setattr(shared, "configurable_model", WorkflowServices())
    cfg = config(
        research_graph_enabled=True,
        research_graph_backend="memory",
        research_graph_transcript_dir=str(tmp_path),
        recent_raw_steps=0,
        allow_clarification=True,
        research_model="fixture:review",
        max_structured_output_retries=1,
        budget_enabled=True,
        max_model_calls=100,
    )
    if mode == "timeout":
        cfg["configurable"]["workflow_step_timeout_seconds"] = 1.5
    if mode == "rolling":
        cfg["configurable"].update(
            research_graph_context_capacity_tokens=22_000,
            recent_raw_steps=1,
            max_model_calls=500,
            max_react_tool_calls=40,
        )
    return original_state, cfg, runtimes, models, calls, blocked


@pytest.mark.parametrize("mode", ["normal", "complete"])
def test_real_full_intake_runner_graph_context_report_and_research_complete(
    monkeypatch, tmp_path, mode
):
    async def run():
        state, cfg, runtimes, models, calls, _ = install_sdk(
            monkeypatch, tmp_path, mode
        )
        try:
            with budget_capture() as usage:
                result = await PythonWorkflow().ainvoke(state, cfg)
            assert "FINAL brand recall report" in result["report"]["final"]
            assert result["workflow"]["brief"] == "Investigate brand recall evidence"
            assert result["messages"][1].content == "Scope confirmed"
            assert len(runtimes) == len(models) == 4
            assert all(rt.harness.last_settlement.succeeded for rt in runtimes)
            assert all(
                rt.workspace.working_context.active_evidence_ids for rt in runtimes
            )
            assert calls == {"public_signal": 1, "internal_knowledge": 1}
            assert (
                result["runtime"]["budget"]["model_calls"] == 7 + usage["model_calls"]
            )
            assert usage["model_calls"] == 21
            assert usage["input_tokens"] == 6 * 10 + 15 * 7
            assert usage["output_tokens"] == 6 * 4 + 15 * 3
            assert set(result["research"]["working_contexts"]) == set(ROLES)
            if mode == "complete":
                assert runtimes[0].harness.results["complete"].status == "succeeded"
        finally:
            clear_research_graph_store_cache()

    asyncio.run(run())


def test_real_clarification_stops_before_any_agent(monkeypatch, tmp_path):
    async def run():
        state, cfg, runtimes, _, calls, _ = install_sdk(
            monkeypatch, tmp_path, "clarify"
        )
        try:
            result = await PythonWorkflow().ainvoke(state, cfg)
            assert result["messages"][-1].content == "Which brand?"
            assert not runtimes and not calls
            assert "final" not in result["report"]
            assert result["runtime"]["budget"]["model_calls"] == 8
        finally:
            clear_research_graph_store_cache()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["model_error", "double_error", "timeout", "cancel"])
def test_real_sdk_failures_settle_and_never_replay_committed_tools(
    monkeypatch, tmp_path, mode
):
    async def run():
        state, cfg, runtimes, models, calls, blocked = install_sdk(
            monkeypatch, tmp_path, mode
        )
        try:
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
            assert all(len(rt.workspace.receipts) == 1 for rt in runtimes)
            assert all(
                rt.harness.results[f"lookup-{rt.role}"].consumed for rt in runtimes
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
            store = create_research_graph_store(
                Configuration.from_runnable_config(cfg), run_id="python-run"
            )
            assert (
                len([n for n in store.nodes.values() if n.node_type == "Evidence"]) == 2
            )
        finally:
            clear_research_graph_store_cache()

    asyncio.run(run())


def test_python_workflow_with_real_local_mcp_transport(monkeypatch, tmp_path):
    from langchain_mcp_adapters.tools import load_mcp_tools
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

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

    async def run():
        async with stdio_client(
            StdioServerParameters(command=sys.executable, args=[str(script)])
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tool = next(
                    t for t in await load_mcp_tools(session) if t.name == "lookup"
                )
                tool.name = "knowledge__lookup"
                tool.metadata = {"tool_domain": "rag"}
                state, cfg, runtimes, _, _, _ = install_sdk(
                    monkeypatch, tmp_path, local_tool=tool
                )
                try:
                    result = await PythonWorkflow().ainvoke(state, cfg)
                    assert result["report"]["final"]
                    rt = next(rt for rt in runtimes if rt.role == "internal_knowledge")
                    assert (
                        rt.harness.results["lookup-internal_knowledge"].status
                        == "succeeded"
                    )
                    assert rt.workspace.receipts
                    assert all(rt.harness.last_settlement.succeeded for rt in runtimes)
                finally:
                    clear_research_graph_store_cache()

    asyncio.run(run())


def test_sdk_budget_stop_degrades_explicitly_without_extra_agent_calls(
    monkeypatch, tmp_path
):
    async def run():
        state, cfg, runtimes, models, calls, _ = install_sdk(monkeypatch, tmp_path)
        cfg["configurable"].update(research_graph_enabled=False, max_model_calls=8)

        async def tools(config, role, spec):
            return [_tool(role)]

        monkeypatch.setattr(business_agent, "_business_agent_tools", tools)
        try:
            result = await PythonWorkflow().ainvoke(state, cfg, start_at="research")
            assert len(runtimes) == 4
            assert all(not model.calls for model in models)
            assert not calls
            assert result["runtime"]["budget"]["degradation_reasons"]
            assert result["report"]["final"]
        finally:
            clear_research_graph_store_cache()

    asyncio.run(run())


def test_real_workflow_preserves_sdk_rolling_summaries_and_private_memory(
    monkeypatch, tmp_path
):
    async def run():
        state, cfg, runtimes, models, calls, _ = install_sdk(
            monkeypatch, tmp_path, "rolling"
        )
        try:
            result = await PythonWorkflow().ainvoke(state, cfg, start_at="research")
            for rt in runtimes[:2]:
                assert rt.harness._compacted_turns
                assert rt.workspace.rolling_summary
                assert (
                    result["agents"][rt.role]["rolling_summary"]
                    == rt.workspace.rolling_summary
                )
                assert (
                    result["agents"][rt.role]["memory"][0]["content"]
                    == f"private-{rt.role}"
                )
                assert rt.harness.last_settlement.succeeded
                assert len(rt.model.calls) == 26
            assert calls == {"public_signal": 25, "internal_knowledge": 25}
            assert result["report"]["final"]
        finally:
            clear_research_graph_store_cache()

    asyncio.run(run())
