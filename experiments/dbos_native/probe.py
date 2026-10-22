"""Public-interface DBOSRunner replay probe, with standard (non-Graph) context."""

from __future__ import annotations

import engine
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from business import gate
from dbos import DBOS
from langchain_core.tools import StructuredTool
from runtime_ext import ExtensibleSDKRuntime
from test_sdk_runtime import TOKENS

from open_deep_research.configuration import Configuration
from open_deep_research.public_opinion_agents import get_public_opinion_agent_spec
from open_deep_research.research_graph import ResearchWorkspace, create_context_strategy
from open_deep_research.runtime.business_agent import _execute_tool_safely


def standard_runtime(id, model, tool, cls=ExtensibleSDKRuntime, compress=None):
    settings = Configuration(
        research_graph_enabled=False,
        research_model="fixture:controlled",
        business_agent_runtime="openai_agents",
        budget_enabled=True,
        max_model_calls=100,
    )
    ws = ResearchWorkspace(
        strategy=create_context_strategy("standard", graph_enabled=False),
        research_state={},
        role="public_signal",
        run_id=id,
        configurable=settings,
        model_factory=lambda *_: None,
    )
    spec = get_public_opinion_agent_spec("public_signal")

    async def default_compress(*args):
        return "standard probe complete", [], {}

    return cls(
        role="public_signal",
        spec=spec,
        agent_state={},
        workspace=ws,
        config=settings,
        runtime_config={},
        model=model,
        system_prompt=spec.format_system_prompt(
            retrieval_tool_prompt="Controlled web search",
            mcp_prompt="",
            date="2026-10-22",
            organization_context="Synthetic",
        ),
        tools=[tool],
        initial_budget={},
        execute_tool=_execute_tool_safely,
        compress_report=compress or default_compress,
    )


@DBOS.step()
async def replay_tool(request, query):
    engine.domain().fact(request["run_id"], "actual_tool", {})
    return engine.domain().effect(
        request["run_id"] + ":sdk",
        request["run_id"] + ":lookup",
        "known external effect",
    )


@DBOS.step()
async def replay_compress():
    return "standard probe complete", [], {}


@DBOS.workflow(name="native_sdk_replay")
async def sdk_replay(request):
    async def provider(call):
        has_result = any(
            isinstance(i, dict) and i.get("type") == "function_call_output"
            for i in call.input
        )
        if has_result:
            await gate(request, "native_model_boundary")
        engine.domain().fact(
            request["run_id"], "actual_provider", {"turn": 2 if has_result else 1}
        )
        return (
            [assistant_message("SDK finished")]
            if has_result
            else [function_call("web_search", {"query": "brand"}, call_id="lookup")]
        )

    async def tool(query: str):
        return await replay_tool(request, query)

    async def compress(*args):
        return await replay_compress()

    model = ScriptedModel(
        [ModelStep.respond(provider) for _ in range(5)], default_usage=TOKENS
    )
    runtime = standard_runtime(
        request["run_id"],
        model,
        StructuredTool.from_function(
            coroutine=tool,
            name="web_search",
            description="Controlled effect",
            metadata={"tool_domain": "web_search"},
        ),
        compress=compress,
    )
    result = await runtime.run("Investigate brand")
    return {
        "report": result.report,
        "budget": result.budget,
        "settled": runtime.harness.last_settlement.succeeded,
    }
