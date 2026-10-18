"""Run the unchanged workflow and existing MCP/Graph boundaries with SDK roles."""

import asyncio
import json
from collections import Counter
from contextlib import asynccontextmanager

import httpx2
import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPTool
from openai import AsyncOpenAI, InternalServerError
from test_public_opinion_research_loop import _initial_state, _ReviewModel
from test_sdk_context import _GraphModel, _payload, _Store
from test_sdk_models import _reply
from test_sdk_runtime import ROLES, TOKENS, _compress, _runtime, _tool

from open_deep_research.configuration import Configuration
from open_deep_research.models.sdk import sdk_model
from open_deep_research.public_opinion_agents import get_public_opinion_agent_spec
from open_deep_research.research_graph import ResearchWorkspace, create_context_strategy
from open_deep_research.runtime import business_agent
from open_deep_research.state import ResearchReview, ResearchTask
from open_deep_research.workflow import research, shared


@pytest.mark.parametrize("is_error", [False, True])
def test_existing_mcp_adapter_schema_protocol_status_and_artifact(is_error):
    calls = []

    class Session:
        async def call_tool(self, name, args, **kwargs):
            calls.append((name, args))
            return CallToolResult(
                content=[
                    TextContent(
                        type="text", text="MCP error" if is_error else "MCP evidence"
                    )
                ],
                structuredContent={
                    "source_id": "mcp-source",
                    "text": "structured evidence",
                },
                isError=is_error,
            )

    tool = convert_mcp_tool_to_langchain_tool(
        Session(),
        MCPTool(
            name="social_media__lookup",
            description="Read a social post",
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
    )
    tool.metadata = {"tool_domain": "social_media"}
    runtime = _runtime(
        tools=[tool],
        model=ScriptedModel(
            [
                [function_call(tool.name, '{"query":"brand"}', call_id="mcp")],
                [assistant_message("MCP report")],
            ],
            default_usage=TOKENS,
        ),
    )
    result = asyncio.run(runtime.run("task"))
    assert calls == [("social_media__lookup", {"query": "brand"})]
    assert result.budget["tool_calls"] == 1
    assert runtime.harness.results["mcp"].status == (
        "failed" if is_error else "succeeded"
    )
    if not is_error:
        assert (
            json.loads(runtime.harness.results["mcp"].observation)[
                "structured_content"
            ]["source_id"]
            == "mcp-source"
        )
    else:
        assert "MCP error" in runtime.harness.results["mcp"].observation


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("use_role_tool_alias", [False, True])
def test_four_sdk_roles_reuse_phase0_graph_and_keep_report_override(
    tmp_path, role, use_role_tool_alias
):
    extractor = _GraphModel()
    report_model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content=f"Graph grounded {role} report",
                response_metadata={"model_name": "fixture-report"},
                usage_metadata={
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "total_tokens": 10,
                },
            )
        ]
    )

    # Keep GraphExtractor/ContextManager deterministic as in Phase 0. The role
    # report uses an actual LangChain lifecycle, while the role loop is SDK-only.
    class GraphServices:
        def with_structured_output(self, *args, **kwargs):
            return extractor.with_structured_output(*args, **kwargs)

        async def ainvoke(self, messages, config=None):
            return await report_model.ainvoke(messages, config)

    config = Configuration(
        research_graph_enabled=True,
        research_graph_backend="memory",
        research_graph_transcript_dir=str(tmp_path),
        recent_raw_steps=0,
    )
    spec = get_public_opinion_agent_spec(role)
    workspace = ResearchWorkspace(
        strategy=create_context_strategy(spec.context_strategy, graph_enabled=True),
        research_state={},
        role=role,
        configurable=config,
        model_factory=lambda *_: GraphServices(),
        store=_Store(),
        run_id="phase1-scoped-run",
    )
    tool = _tool(role, coroutine=lambda **kwargs: _async_payload())
    if use_role_tool_alias:
        tool.name = (
            "social_media__lookup"
            if role == "public_signal"
            else "local_knowledge_lookup"
        )
        tool.metadata["tool_domain"] = (
            "social_media" if role == "public_signal" else "rag"
        )

    async def _async_payload():
        return _payload(role)

    runtime = _runtime(
        role,
        config=config,
        workspace=workspace,
        tools=[tool],
        model=ScriptedModel(
            [
                [function_call(tool.name, '{"query":"brand"}', call_id="graph-tool")],
                [assistant_message("evidence analysis")],
            ],
            default_usage=TOKENS,
        ),
    )
    result = asyncio.run(runtime.run("task"))
    assert result.replace_report is True
    assert f"Graph grounded {role} report" in result.report
    assert runtime.harness.last_settlement.succeeded
    assert result.budget["model_calls"] == 3
    assert result.budget["input_tokens"] == 27
    if role in ROLES[:2]:
        assert workspace.store.writes == 1
        assert len(extractor.extractions) == 1
        assert len(extractor.context_updates) == 1
    else:
        assert workspace.store.writes == 0
        assert not extractor.extractions
    update = business_agent._build_state_patch(
        role=role,
        state={},
        research_round=1,
        result=result,
        workspace=workspace,
    )
    assert update["agents"][role]["report"] == {
        "type": "override",
        "value": result.report,
    }
    assert update["research"]["run_id"] == "phase1-scoped-run"
    assert role in update["research"]["working_contexts"]


def test_unchanged_langgraph_parallel_join_review_followup_risk_strategy_with_sdk(
    monkeypatch,
):
    async def scenario():
        started = []
        completed = []
        models = []
        parallel_entered = set()
        barrier = asyncio.Event()
        review_inputs = []
        gap = ResearchTask(
            task_id="new-evidence",
            target_role="public_signal",
            objective="Find missing recall notice",
            evidence_needed="dated recall notice",
            reason="could change risk",
            priority="high",
        )

        class Reviewer(_ReviewModel):
            async def _structured_call(self, messages, config=None):
                assert set(completed[:2]) == set(ROLES[:2])
                review_inputs.append(str(messages[0].content))
                return await super()._structured_call(messages, config)

        reviewer = Reviewer(
            [
                ResearchReview(research_complete=False, next_tasks=[gap]),
                ResearchReview(
                    research_complete=True, confirmed_findings=["gap resolved"]
                ),
            ]
        )

        @asynccontextmanager
        async def model_context(*args, **kwargs):
            current = {}

            async def begin(call):
                role = call.system_instructions.split("<Role>\n", 1)[1].split(
                    "\n</Role>", 1
                )[0]
                current["role"] = role
                started.append(role)
                if len(started) <= 2:
                    parallel_entered.add(role)
                    if len(parallel_entered) == 2:
                        barrier.set()
                    await asyncio.wait_for(barrier.wait(), 3)
                if role == "risk_assessment":
                    assert reviewer.calls == 2
                    assert Counter(completed)["public_signal"] == 2
                if role == "response_strategy":
                    assert completed[-1] == "risk_assessment"
                    assert "risk_assessment SDK report" in json.dumps(call.input)
                name = "web_search" if role == "public_signal" else "rag_search"
                return [function_call(name, '{"query":"brand"}', call_id="evidence")]

            def end(call):
                role = current["role"]
                completed.append(role)
                return [assistant_message(f"{role} SDK report")]

            model = ScriptedModel(
                [ModelStep.respond(begin), ModelStep.respond(end)], default_usage=TOKENS
            )
            models.append(model)
            yield model

        async def tools(config, role, spec):
            return [_tool(role)]

        async def compress(messages, *args, **kwargs):
            return await _compress(messages, *args)

        monkeypatch.setattr(shared, "configurable_model", reviewer)
        monkeypatch.setattr(business_agent, "sdk_model", model_context)
        monkeypatch.setattr(business_agent, "_business_agent_tools", tools)
        monkeypatch.setattr(business_agent, "_compress_research", compress)
        state = _initial_state()
        state["runtime"]["budget"] = {
            "model_calls": 7,
            "input_tokens": 70,
            "output_tokens": 7,
            "total_tokens": 77,
        }
        result = await research.public_opinion_subgraph.ainvoke(
            state,
            {
                "configurable": {
                    "business_agent_runtime": "openai_agents",
                    "research_graph_enabled": False,
                    "research_model": "openai:fixture",
                    "enabled_business_agents": list(ROLES),
                    "max_research_rounds": 2,
                }
            },
        )
        assert Counter(started) == Counter(
            {
                "public_signal": 2,
                "internal_knowledge": 1,
                "risk_assessment": 1,
                "response_strategy": 1,
            }
        )
        assert result["workflow"]["round"] == 2
        assert result["workflow"]["review"].research_complete
        assert result["workflow"]["completed_tasks"][0].task_id == gap.task_id
        assert set(result["agents"]) == set(ROLES)
        assert len(result["agents"]["public_signal"]["memory"]) == 2
        assert all(
            f"{role} SDK report" in result["agents"][role]["report"] for role in ROLES
        )
        assert "public_signal SDK report" in review_inputs[0]
        assert "internal_knowledge SDK report" in review_inputs[0]
        assert result["runtime"]["budget"]["model_calls"] == 17
        assert result["runtime"]["budget"]["input_tokens"] == 170
        assert result["runtime"]["budget"]["tool_calls"] == 5
        assert all(len(model.calls) == 2 for model in models)

    asyncio.run(scenario())


@pytest.mark.parametrize("role", ROLES)
def test_legacy_business_assembly_still_executes_original_loop(monkeypatch, role):
    name = "web_search" if role == "public_signal" else "rag_search"

    class LegacyModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    model = LegacyModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": name, "id": "legacy", "args": {"query": "brand"}}],
            ),
            AIMessage(content="legacy analysis"),
            AIMessage(content="legacy synthesized report"),
        ]
    )

    async def tools(config, selected_role, spec):
        return [_tool(selected_role)]

    def unexpected_sdk(*args, **kwargs):
        pytest.fail("Legacy selection must not open an SDK model")

    monkeypatch.setattr(business_agent, "_CONFIGURABLE_MODEL", model)
    monkeypatch.setattr(business_agent, "_business_agent_tools", tools)
    monkeypatch.setattr(business_agent, "sdk_model", unexpected_sdk)
    patch = asyncio.run(
        business_agent.run_business_agent(
            role=role,
            state=_initial_state(),
            config={
                "configurable": {
                    "business_agent_runtime": "legacy",
                    "research_graph_enabled": False,
                    "research_model": "openai:fixture",
                    "enabled_business_agents": list(ROLES),
                }
            },
        )
    )
    assert "legacy synthesized report" in patch["agents"][role]["report"]
    assert patch["runtime"]["budget"]["model_calls"] == 3
    assert patch["runtime"]["budget"]["tool_calls"] == 1


@pytest.mark.parametrize("role", ROLES)
def test_real_business_tool_loading_applies_role_allowlist_to_sdk(monkeypatch, role):
    candidates = [
        _tool(name="web_search", domain="web_search"),
        _tool(name="rag_search", domain="rag"),
        _tool(name="social_lookup", domain="social_media"),
        _tool(name="think_tool", domain="core"),
        _tool(name="unclassified", domain="unknown"),
    ]

    async def all_tools(config):
        return candidates

    def inspect(call):
        allowed = get_public_opinion_agent_spec(role).allowed_domains
        expected = {
            tool.name for tool in candidates if tool.metadata["tool_domain"] in allowed
        }
        assert {tool.name for tool in call.tools} == expected
        assert "unclassified" not in expected
        if role == "response_strategy":
            assert "social_lookup" not in expected
        return [assistant_message("whitelist verified")]

    @asynccontextmanager
    async def model_context(*args, **kwargs):
        yield ScriptedModel([ModelStep.respond(inspect)])

    async def compress(messages, *args, **kwargs):
        return await _compress(messages, *args)

    monkeypatch.setattr(business_agent, "get_all_tools", all_tools)
    monkeypatch.setattr(business_agent, "get_social_media_tools", lambda: [])
    monkeypatch.setattr(business_agent, "sdk_model", model_context)
    monkeypatch.setattr(business_agent, "_compress_research", compress)
    patch = asyncio.run(
        business_agent.run_business_agent(
            role=role,
            state=_initial_state(),
            config={
                "configurable": {
                    "business_agent_runtime": "openai_agents",
                    "research_graph_enabled": False,
                    "research_model": "openai:fixture",
                    "enabled_business_agents": list(ROLES),
                }
            },
        )
    )
    assert "whitelist verified" in patch["agents"][role]["report"]


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
def test_sdk_business_runtime_with_official_provider_serialization(api):
    async def run():
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx2.Response(
                200, json=_reply(api, len(requests), "fixture-model")
            )

        async with AsyncOpenAI(
            api_key="fixture",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
        ) as client:
            async with sdk_model(
                "openai:fixture-model", api=api, client=client
            ) as model:
                runtime = _runtime(model=model, tools=[_tool(name="lookup")])
                result = await runtime.run("business assignment")
        assert len(requests) == 2
        assert result.budget["model_calls"] == 2
        assert result.budget["input_tokens"] == 24
        assert result.budget["total_tokens"] == 30
        assert result.budget["tool_calls"] == 1
        assert "SDK_MODEL_OK" in result.report
        schema = requests[0]["tools"][0]
        if api == "chat_completions":
            schema = schema["function"]
        assert schema["strict"] is False
        assert schema["parameters"]["required"] == ["query"]

    asyncio.run(run())


def test_sdk_business_provider_error_is_one_attempt_without_opaque_retries():
    async def run():
        requests = []

        def handle(request):
            requests.append(request)
            return httpx2.Response(
                503, json={"error": {"message": "offline service failure"}}
            )

        async with AsyncOpenAI(
            api_key="fixture",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
        ) as client:
            async with sdk_model("openai:fixture-model", client=client) as model:
                runtime = _runtime(model=model)
                with pytest.raises(InternalServerError) as error:
                    await runtime.run("task")
        assert len(requests) == 1
        assert error.value.budget_usage["model_calls"] == 1
        assert error.value.budget_usage["input_tokens"] == 0

    asyncio.run(run())


def test_graph_report_budget_reserve_is_explicit(tmp_path):
    config = Configuration(
        research_graph_enabled=True,
        research_graph_backend="memory",
        research_graph_transcript_dir=str(tmp_path),
        budget_enabled=True,
        max_model_calls=2,
        reserve_final_report_call=True,
    )
    workspace = ResearchWorkspace(
        strategy=create_context_strategy("research_graph_producer", graph_enabled=True),
        research_state={},
        role="public_signal",
        configurable=config,
        model_factory=lambda *_: pytest.fail(
            "Report reserve must prevent a graph model call"
        ),
        store=_Store(),
        run_id="budget-graph",
    )
    runtime = _runtime(
        config=config, workspace=workspace, initial_budget={"model_calls": 1}
    )
    result = asyncio.run(runtime.run("task"))
    assert not runtime.model.calls
    assert result.replace_report
    assert result.budget["model_calls"] == 0
    assert "without role report synthesis" in result.report
    assert any(
        "preserve the final report" in reason
        for reason in result.budget["degradation_reasons"]
    )
