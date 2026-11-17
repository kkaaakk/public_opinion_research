"""Exercise all four business roles through the real offline SDK Runner."""

import asyncio
import gc
import json
import threading
import weakref
from contextlib import asynccontextmanager
from copy import deepcopy

import pytest
from agents import Agent, FunctionTool, function_tool
from agents.exceptions import ModelBehaviorError
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext
from agents.usage import Usage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, Field

from open_deep_research.budget import (
    ainvoke_model_with_budget,
    budget_capture,
    capture_budget_usage,
)
from open_deep_research.configuration import Configuration
from open_deep_research.public_opinion_agents import (
    PUBLIC_OPINION_AGENT_ORDER,
    get_public_opinion_agent_spec,
)
from open_deep_research.research_graph import ResearchWorkspace, create_context_strategy
from open_deep_research.runtime import business_agent
from open_deep_research.runtime.sdk_runtime import (
    SDKAgentRuntime,
    SDKContextLimitExceeded,
    _BusinessAgentHarness,
    _ReasoningStopped,
    _RunBudget,
)
from open_deep_research.runtime.tool_executor import ToolExecutor
from open_deep_research.tools import BusinessTool, register_tool

ROLES = PUBLIC_OPINION_AGENT_ORDER
TOKENS = Usage(
    requests=1,
    input_tokens=10,
    output_tokens=4,
    total_tokens=14,
    input_tokens_details={"cached_tokens": 2, "cache_write_tokens": 1},
    output_tokens_details={"reasoning_tokens": 3},
)


class SearchArgs(BaseModel):
    query: str
    limit: int = Field(default=3, ge=1, le=10)


def _tool(
    role="public_signal", *, coroutine=None, func=None, name=None, domain=None, **kwargs
):
    async def search(query: str, limit: int = 3):
        return f"Evidence {query}, limit={limit} [source: fixture]"

    return StructuredTool(
        name=name or ("web_search" if role == "public_signal" else "rag_search"),
        description="Search cited evidence",
        args_schema=SearchArgs,
        coroutine=coroutine or (None if func else search),
        func=func,
        metadata={
            "tool_domain": domain
            or ("web_search" if role == "public_signal" else "rag")
        },
        **kwargs,
    )


async def _compress(messages, task, output_contract, cumulative_budget):
    notes = [
        str(message.content) for message in messages if message.type in {"ai", "tool"}
    ]
    return "Role report\n" + "\n".join(notes), notes, {}


def _runtime(
    role="public_signal",
    *,
    model=None,
    tools=None,
    config=None,
    agent_state=None,
    workspace=None,
    initial_budget=None,
    compress=None,
    research_round=1,
):
    config = config or Configuration(
        research_graph_enabled=False, research_model="openai:fixture"
    )
    spec = get_public_opinion_agent_spec(role)
    workspace = workspace or ResearchWorkspace(
        strategy=create_context_strategy("standard", graph_enabled=False),
        research_state={},
        role=role,
        configurable=config,
        model_factory=lambda *_: pytest.fail("Unexpected graph model"),
        run_id="sdk-runtime-test",
        research_round=research_round,
    )
    return SDKAgentRuntime(
        role=role,
        spec=spec,
        agent_state=agent_state or {},
        workspace=workspace,
        config=config,
        runtime_config={"configurable": {"fixture": "operator-config"}},
        model=model or ScriptedModel([[assistant_message("finished")]]),
        system_prompt=spec.format_system_prompt(
            retrieval_tool_prompt="fixture tools",
            mcp_prompt="",
            date="2026-10-17",
            organization_context="fixture organization",
        ),
        tools=tools if tools is not None else [_tool(role)],
        initial_budget=initial_budget or {},
        compress_report=compress or _compress,
    )


@pytest.mark.parametrize("role", ROLES)
def test_four_roles_execute_sdk_model_tool_model_with_contracts(role):
    invoked = []

    async def search(query: str, limit: int = 3):
        invoked.append((query, limit))
        return "CITED_EVIDENCE"

    tool = _tool(role, coroutine=search)

    def final(call):
        assert all(isinstance(item, dict) for item in call.input)
        outputs = [
            item for item in call.input if item.get("type") == "function_call_output"
        ]
        assert [(item["call_id"], item["output"]) for item in outputs] == [
            ("lookup", "CITED_EVIDENCE")
        ]
        assert call.system_instructions == runtime.system_prompt
        assert runtime.spec.expected_output in call.system_instructions
        return [assistant_message(f"{role} final report")]

    model = ScriptedModel(
        [
            [function_call(tool.name, '{"query":"brand"}', call_id="lookup")],
            ModelStep.respond(final),
        ],
        default_usage=TOKENS,
    )
    runtime = _runtime(
        role,
        model=model,
        tools=[tool],
        agent_state={
            "memory": [{"content": f"PRIVATE_{role}"}],
            "rolling_summary": "SAVED_SUMMARY",
        },
    )
    result = asyncio.run(runtime.run("initial assignment"))
    model.assert_complete()
    assert len(model.calls) == 2
    assert invoked == [("brand", 3)]
    assert all(isinstance(tool, FunctionTool) for tool in runtime.agent.tools)
    assert (
        runtime.agent.tools[0].params_json_schema
        == tool.tool_call_schema.model_json_schema()
    )
    assert f"PRIVATE_{role}" in json.dumps(model.calls[0].input)
    assert "SAVED_SUMMARY" in json.dumps(model.calls[0].input)
    assert f"Agent role: {role}" in result.report
    assert "Research mode: initial" in result.report
    assert f"{role} final report" in result.report
    assert result.memory["source"] == "current_public_opinion_run"
    assert result.memory["raw_note_count"] >= 2
    assert result.rolling_summary == "SAVED_SUMMARY"
    assert result.replace_report is False
    assert result.budget["model_calls"] == 2
    assert result.budget["tool_calls"] == result.budget["search_calls"] == 1
    assert result.budget["input_tokens"] == 20
    assert result.budget["total_tokens"] == 28
    assert result.budget["input_token_details"] == {
        "cache_read": 4,
        "cache_creation": 2,
    }
    assert result.budget["output_token_details"] == {"reasoning": 6}
    assert runtime.sdk_result.context_wrapper.usage.input_tokens == 20
    assert runtime.harness.results["lookup"].status == "succeeded"


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(
    "arguments", ['{"query":3}', '{"limit":100}', '{"query":"q","limit":0}', "[]", "{"]
)
def test_tool_schema_validation_prevents_invalid_execution(role, arguments):
    invoked = []

    async def search(**kwargs):
        invoked.append(kwargs)
        return "unexpected"

    tool = _tool(role, coroutine=search)
    model = ScriptedModel(
        [
            [function_call(tool.name, arguments, call_id="invalid")],
            [assistant_message("validation handled")],
        ]
    )
    runtime = _runtime(role, model=model, tools=[tool])
    result = asyncio.run(runtime.run("task"))
    assert not invoked
    assert runtime.harness.results["invalid"].status == "failed"
    assert "validation handled" in result.report
    assert result.budget["model_calls"] == 2


@pytest.mark.parametrize("role", ROLES)
def test_registration_and_execution_both_enforce_role_permission(role):
    forbidden = _tool(name="untrusted", domain="unclassified")
    runtime = _runtime(role, tools=[forbidden])
    with pytest.raises(ValueError, match="outside"):
        asyncio.run(runtime.run("task"))
    assert not runtime.model.calls

    tool = _tool(role)

    def revoke(call):
        tool.metadata["tool_domain"] = "unclassified"
        return [function_call(tool.name, '{"query":"q"}', call_id="revoked")]

    model = ScriptedModel(
        [ModelStep.respond(revoke), [assistant_message("permission denied")]]
    )
    runtime = _runtime(role, model=model, tools=[tool])
    asyncio.run(runtime.run("task"))
    assert runtime.harness.results["revoked"].status == "failed"
    assert runtime.budget_usage["tool_calls"] == 0


@pytest.mark.parametrize("role", ROLES)
def test_sync_async_tools_execute_once_in_sdk_parallel_batch(role):
    calls = []

    def sync_search(query: str, limit: int = 3):
        calls.append(("sync", query))
        capture_budget_usage(
            {"model_calls": 1, "input_tokens": 7, "output_tokens": 2, "total_tokens": 9}
        )
        return "sync evidence"

    async def async_search(query: str, limit: int = 3):
        calls.append(("async", query))
        return "async evidence"

    tools = [
        _tool(role, func=sync_search),
        _tool(role, coroutine=async_search, name="second_search"),
    ]
    model = ScriptedModel(
        [
            [
                function_call(tools[0].name, '{"query":"s"}', call_id="s"),
                function_call(tools[1].name, '{"query":"a"}', call_id="a"),
            ],
            [assistant_message("both executed")],
        ],
        default_usage=TOKENS,
    )
    runtime = _runtime(role, model=model, tools=tools)
    result = asyncio.run(runtime.run("task"))
    assert sorted(calls) == [("async", "a"), ("sync", "s")]
    assert result.budget["model_calls"] == 3
    assert result.budget["input_tokens"] == 27
    assert result.budget["total_tokens"] == 37


@pytest.mark.parametrize(
    "kind", ["raises", "handled_error", "business_error", "rag_error"]
)
def test_explicit_tool_failure_status_is_retained(kind):
    async def fail(query: str, limit: int = 3):
        if kind in {"raises", "handled_error"}:
            raise ToolException("MCP transport or business failure")
        if kind == "business_error":
            return '{"success":false,"error":"denied"}'
        return "Local RAG search failed: offline fixture"

    tool = _tool(
        "internal_knowledge", coroutine=fail, handle_tool_error=kind == "handled_error"
    )
    runtime = _runtime(
        "internal_knowledge",
        tools=[tool],
        model=ScriptedModel(
            [
                [function_call(tool.name, '{"query":"q"}', call_id="failed")],
                [assistant_message("failure acknowledged")],
            ]
        ),
    )
    result = asyncio.run(runtime.run("task"))
    assert runtime.harness.results["failed"].status == "failed"
    assert result.budget["tool_calls"] == 1
    assert "failure acknowledged" in result.report


@pytest.mark.parametrize("role", ROLES)
def test_parallel_tool_search_budget_reserves_before_execution(role):
    calls = []

    async def search(query: str, limit: int = 3):
        calls.append(query)
        await asyncio.sleep(0)
        return query

    tool = _tool(role, coroutine=search)
    runtime = _runtime(
        role,
        tools=[tool],
        config=Configuration(
            research_graph_enabled=False,
            budget_enabled=True,
            max_search_calls=1,
        ),
        model=ScriptedModel(
            [
                [
                    function_call(tool.name, '{"query":"one"}', call_id="one"),
                    function_call(tool.name, '{"query":"two"}', call_id="two"),
                ]
            ],
            default_usage=TOKENS,
        ),
    )
    result = asyncio.run(runtime.run("task"))
    assert calls == ["one"]
    assert result.budget["search_calls"] == result.budget["tool_calls"] == 1
    assert result.budget["model_calls"] == 1
    assert runtime.harness.results["two"].status == "failed"
    assert any("skipped" in reason for reason in result.budget["degradation_reasons"])


@pytest.mark.parametrize("role", ROLES)
def test_model_failure_keeps_attempts_tokens_and_never_replays_tools(role):
    tool = _tool(role)
    runtime = _runtime(
        role,
        tools=[tool],
        model=ScriptedModel(
            [
                [function_call(tool.name, '{"query":"q"}', call_id="one")],
                ModelStep.raise_error(RuntimeError("provider failed")),
            ],
            default_usage=TOKENS,
        ),
    )

    async def run():
        with budget_capture() as captured:
            with pytest.raises(RuntimeError, match="provider failed") as error:
                await runtime.run("task")
        assert error.value.budget_usage == captured == runtime.budget_usage
        return captured

    captured = asyncio.run(run())
    assert captured["model_calls"] == 2
    assert captured["input_tokens"] == 10
    assert captured["tool_calls"] == 1
    assert runtime.harness.last_settlement.succeeded
    assert len(runtime.model.calls) == 2


@pytest.mark.parametrize("role", ROLES)
def test_tool_cancellation_propagates_with_budget_and_terminal_status(role):
    async def run():
        entered = asyncio.Event()

        async def wait(query: str, limit: int = 3):
            capture_budget_usage({"model_calls": 1})
            entered.set()
            await asyncio.Event().wait()

        tool = _tool(role, coroutine=wait)
        runtime = _runtime(
            role,
            tools=[tool],
            model=ScriptedModel(
                [
                    [function_call(tool.name, '{"query":"q"}', call_id="cancel")],
                ],
                default_usage=TOKENS,
            ),
        )
        running = asyncio.create_task(runtime.run("task"))
        await asyncio.wait_for(entered.wait(), 3)
        running.cancel()
        with pytest.raises(asyncio.CancelledError) as error:
            await running
        assert error.value.budget_usage["model_calls"] == 2
        assert error.value.budget_usage["tool_calls"] == 1
        assert runtime.harness.results["cancel"].status == "cancelled"
        assert runtime.harness.last_settlement.succeeded
        assert len(runtime.model.calls) == 1

    asyncio.run(run())


def test_model_budget_exhaustion_does_not_invoke_sdk_model():
    runtime = _runtime(
        config=Configuration(
            research_graph_enabled=False,
            budget_enabled=True,
            max_model_calls=2,
            reserve_final_report_call=True,
        ),
        initial_budget={"model_calls": 1},
    )
    result = asyncio.run(runtime.run("task"))
    assert not runtime.model.calls
    assert result.budget["model_calls"] == 0
    assert result.budget["degradation_reasons"]


def test_max_turns_and_research_complete_still_finalize_without_extra_runner():
    for name in ("web_search", "ResearchComplete"):
        tool = _tool(
            name=name, domain="core" if name == "ResearchComplete" else "web_search"
        )
        runtime = _runtime(
            tools=[tool],
            config=Configuration(
                research_graph_enabled=False,
                max_react_tool_calls=1,
            ),
            model=ScriptedModel(
                [
                    [
                        function_call(name, '{"query":"q"}', call_id="last"),
                    ]
                ],
                default_usage=TOKENS,
            ),
        )
        result = asyncio.run(runtime.run("task"))
        assert "Evidence q" in result.report
        assert result.budget["model_calls"] == 1
        assert result.budget["tool_calls"] == 1
        assert runtime.harness.last_settlement.succeeded


def test_unknown_tool_fails_closed_without_business_execution():
    runtime = _runtime(
        model=ScriptedModel(
            [
                [
                    function_call("unauthorized_write", "{}", call_id="unknown"),
                ]
            ],
            default_usage=TOKENS,
        )
    )
    with pytest.raises(ModelBehaviorError):
        asyncio.run(runtime.run("task"))
    assert runtime.budget_usage["tool_calls"] == 0
    assert runtime.budget_usage["model_calls"] == 1


def test_context_pressure_has_explicit_phase2_boundary():
    runtime = _runtime(
        config=Configuration(
            research_graph_enabled=False,
            research_graph_context_capacity_tokens=128,
        )
    )
    with pytest.raises(SDKContextLimitExceeded, match="Phase 2"):
        asyncio.run(runtime.run("large evidence " * 1000))
    assert not runtime.model.calls
    assert runtime.budget_usage["model_calls"] == 0


def test_sdk_refuses_langchain_model_and_unverified_hosted_search():
    runtime = _runtime(
        model=FakeMessagesListChatModel(responses=[AIMessage(content="bad")])
    )
    with pytest.raises(TypeError, match="SDK Model"):
        asyncio.run(runtime.run("task"))
    runtime = _runtime(tools=[{"type": "web_search_preview"}])
    with pytest.raises(TypeError, match="hosted/native"):
        asyncio.run(runtime.run("task"))


def test_real_report_service_usage_is_not_lost_or_counted_twice(monkeypatch):
    report_model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="synthesized report",
                response_metadata={"model_name": "fixture-report"},
                usage_metadata={
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "total_tokens": 10,
                },
            )
        ]
    )
    monkeypatch.setattr(business_agent, "_CONFIGURABLE_MODEL", report_model)
    config = Configuration(
        research_graph_enabled=False, compression_model="openai:fixture"
    )

    async def compress(messages, task, output_contract, cumulative):
        return await business_agent._compress_research(
            messages,
            task,
            output_contract,
            cumulative,
            configurable=config,
            config={},
        )

    runtime = _runtime(
        config=config,
        compress=compress,
        model=ScriptedModel(
            [
                [assistant_message("final analysis")],
            ],
            default_usage=TOKENS,
        ),
    )

    async def run():
        with budget_capture() as captured:
            result = await runtime.run("task")
        assert captured == result.budget
        return result

    result = asyncio.run(run())
    assert "synthesized report" in result.report
    assert result.budget["model_calls"] == 2
    assert result.budget["input_tokens"] == 17
    assert result.budget["total_tokens"] == 24


@pytest.mark.parametrize("role", ROLES)
def test_business_assembly_selects_sdk_and_keeps_followup_state(monkeypatch, role):
    models = []

    @asynccontextmanager
    async def offline_model(model_ref, *, config, max_retries):
        assert model_ref == "openai:fixture"
        assert max_retries == 0
        model = ScriptedModel(
            [[assistant_message(f"{role} followup conclusion")]], default_usage=TOKENS
        )
        models.append(model)
        yield model

    async def tools(config, selected_role, spec):
        return [_tool(selected_role)]

    monkeypatch.setattr(business_agent, "sdk_model", offline_model)
    monkeypatch.setattr(business_agent, "_business_agent_tools", tools)
    monkeypatch.setattr(
        business_agent, "_compress_research", lambda *args, **kwargs: _compress(*args)
    )
    state = {
        "workflow": {
            "brief": "brand task",
            "round": 2,
            "pending_tasks": [
                {
                    "task_id": "gap",
                    "target_role": role if role in ROLES[:2] else "public_signal",
                    "objective": "resolve gap",
                    "evidence_needed": "specific evidence",
                    "reason": "risk could change",
                    "priority": "high",
                }
            ],
        },
        "agents": {
            role: {"memory": [{"content": f"OWN_{role}"}]},
            "other_role": {"memory": [{"content": "OTHER_PRIVATE_SECRET"}]},
        },
        "runtime": {"budget": {"model_calls": 9}},
        "research": {"run_id": "assembly-run"},
    }
    result = asyncio.run(
        business_agent.run_business_agent(
            role=role,
            state=state,
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
    if role in ROLES[:2]:
        from open_deep_research.state import ResearchRoleResult
        from open_deep_research.workflow.python_state import assemble_research_results

        assert isinstance(result, ResearchRoleResult)
        assert (result.role, result.round) == (role, 2)
        assembled = deepcopy(state)
        assemble_research_results(assembled, {role: result}, {role: state})
        assert "Research mode: followup" in assembled["agents"][role]["report"]
        assert assembled["workflow"]["completed_tasks"][0].task_id == "gap"
        assert assembled["workflow"]["round"] == 2
        assert result.budget_delta["model_calls"] == 1
    else:
        assert "Research mode: followup" in result["agents"][role]["report"]
        assert result["workflow"]["round"] == 2
        assert result["workflow"]["completed_tasks"][0].task_id == "gap"
        assert result["runtime"]["budget"]["model_calls"] == 1
    actual_input = json.dumps(models[0].calls[0].input)
    assert f"OWN_{role}" in actual_input
    assert "OTHER_PRIVATE_SECRET" not in actual_input
    if role in ROLES[:2]:
        assert "resolve gap" in actual_input
        assert "Do not repeat the first-round" in actual_input
    else:
        assert "Do not repeat the first-round" not in actual_input


def test_runtime_configuration_default_and_invalid_values():
    assert "business_agent_runtime" not in Configuration.model_fields
    assert "workflow_backend" not in Configuration.model_fields
    with pytest.raises(ValueError, match="business_agent_runtime"):
        Configuration(business_agent_runtime="typo")


def test_duplicate_native_call_id_does_not_duplicate_tool_or_budget():
    calls = []

    async def search(query: str, limit: int = 3):
        calls.append(query)
        await asyncio.sleep(0)
        return "one result"

    tool = _tool(coroutine=search)
    runtime = _runtime(
        tools=[tool],
        model=ScriptedModel(
            [
                [
                    function_call(tool.name, '{"query":"q"}', call_id="same"),
                    function_call(tool.name, '{"query":"q"}', call_id="same"),
                ],
                [assistant_message("completed")],
            ]
        ),
    )
    result = asyncio.run(runtime.run("task"))
    assert calls == ["q"]
    assert result.budget["tool_calls"] == result.budget["search_calls"] == 1


def test_failed_report_service_keeps_attempt_budget(monkeypatch):
    from langchain_core.language_models.chat_models import BaseChatModel

    class FailedReport(BaseChatModel):
        @property
        def _llm_type(self):
            return "fixture"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("report service failed")

    monkeypatch.setattr(business_agent, "_CONFIGURABLE_MODEL", FailedReport())
    config = Configuration(research_graph_enabled=False)

    async def compress(messages, task, contract, cumulative):
        return await business_agent._compress_research(
            messages,
            task,
            contract,
            cumulative,
            configurable=config,
            config={},
        )

    runtime = _runtime(config=config, compress=compress)
    with pytest.raises(RuntimeError, match="report service failed") as error:
        asyncio.run(runtime.run("task"))
    assert error.value.budget_usage["model_calls"] == 2


def test_model_cancellation_keeps_started_attempt():
    async def run():
        entered = asyncio.Event()

        async def hang(call):
            entered.set()
            await asyncio.Event().wait()

        runtime = _runtime(model=ScriptedModel([ModelStep.respond(hang)]))
        task = asyncio.create_task(runtime.run("task"))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as error:
            await task
        assert error.value.budget_usage["model_calls"] == 1
        assert error.value.budget_usage["input_tokens"] == 0
        assert runtime.harness.last_settlement.succeeded

    asyncio.run(run())


@pytest.mark.parametrize("native", [False, True])
def test_sync_tool_cancellation_drains_actual_thread_result_and_usage(native):
    async def run():
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def blocking(query: str, limit: int = 3):
            entered.set()
            assert release.wait(5)
            calls.append(query)
            capture_budget_usage({"model_calls": 1, "input_tokens": 5})
            return "late synchronous evidence"

        if native:

            @function_tool(
                name_override="web_search",
                description_override="Search",
                use_docstring_info=False,
                strict_mode=False,
                failure_error_function=None,
            )
            async def declared(context: ToolContext, query: str, limit: int = 3):
                return await context.context.tool_executor.execute(
                    context, blocking, query, limit=limit
                )

            tool = register_tool(
                declared, domain="web_search", effect="read", drain_on_cancel=True
            )
        else:
            tool = _tool(func=blocking)
        runtime = _runtime(
            tools=[tool],
            model=ScriptedModel(
                [
                    [
                        function_call(tool.name, '{"query":"q"}', call_id="thread"),
                    ]
                ],
                default_usage=TOKENS,
            ),
        )
        task = asyncio.create_task(runtime.run("task"))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError) as error:
            await asyncio.wait_for(task, 5)
        assert calls == ["q"]
        assert runtime.harness.results["thread"].status == "succeeded"
        assert error.value.budget_usage["model_calls"] == 2
        assert error.value.budget_usage["input_tokens"] == 15
        assert error.value.context_settlement.succeeded

    asyncio.run(run())


@pytest.mark.parametrize("limit", ["max_input_tokens", "max_output_tokens"])
def test_sdk_token_budget_stops_tools_and_further_reasoning(limit):
    config = Configuration(
        research_graph_enabled=False,
        budget_enabled=True,
        **{limit: 10 if limit == "max_input_tokens" else 4},
    )
    runtime = _runtime(
        config=config,
        model=ScriptedModel(
            [
                [
                    function_call("web_search", '{"query":"q"}', call_id="limited"),
                ]
            ],
            default_usage=TOKENS,
        ),
    )
    result = asyncio.run(runtime.run("task"))
    assert result.budget["model_calls"] == 1
    assert result.budget["tool_calls"] == 0
    assert runtime.harness.results["limited"].status == "failed"


@pytest.mark.parametrize("result_kind", ["multimodal", "cancelled"])
def test_unverified_multimodal_and_explicit_cancelled_results_are_not_success(
    result_kind,
):
    async def search(query: str, limit: int = 3):
        if result_kind == "multimodal":
            return [
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.test/image.png"},
                }
            ]
        return '{"status":"cancelled","reason":"MCP request cancelled"}'

    tool = _tool(coroutine=search)
    runtime = _runtime(
        tools=[tool],
        model=ScriptedModel(
            [
                [function_call(tool.name, '{"query":"q"}', call_id="result")],
                [assistant_message("missing evidence acknowledged")],
            ]
        ),
    )
    result = asyncio.run(runtime.run("task"))
    assert result.budget["tool_calls"] == 1
    assert runtime.harness.results["result"].status == (
        "failed" if result_kind == "multimodal" else "cancelled"
    )
    if result_kind == "multimodal":
        assert "multimodal MCP outputs" in runtime.harness.results["result"].observation


@pytest.mark.parametrize("role", ROLES)
def test_retained_harness_and_registered_tools_do_not_keep_runtime_alive(role):
    async def run():
        tool = _tool(role)
        model = ScriptedModel(
            [
                [function_call(tool.name, '{"query":"brand"}', call_id="retained")],
                [assistant_message("completed without a Runtime backreference")],
            ],
            default_usage=TOKENS,
        )
        runtime = _runtime(role, model=model, tools=[tool])
        result = await runtime.run("task")
        harness = runtime.harness
        assert not hasattr(runtime, "hooks")
        assert harness.last_settlement.succeeded
        assert result.budget["model_calls"] == 2
        assert result.budget["tool_calls"] == 1
        reference = weakref.ref(runtime)
        del runtime
        gc.collect()
        # Retain the real harness, SDK Agent and FunctionTool invokers. A hidden
        # bound Runtime callback or closure would keep this weakref alive.
        assert reference() is None
        assert harness.results["retained"].status == "succeeded"
        assert harness.run_budget.delta == result.budget
        model.assert_complete()

    asyncio.run(run())


def test_standalone_business_harness_observes_live_nested_budget_without_runtime():
    async def run():
        config = Configuration(
            research_graph_enabled=False,
            research_model="openai:fixture",
            budget_enabled=True,
            max_model_calls=4,
            reserve_final_report_call=True,
        )
        workspace = ResearchWorkspace(
            strategy=create_context_strategy("standard", graph_enabled=False),
            research_state={},
            role="public_signal",
            configurable=config,
            model_factory=lambda *_: pytest.fail("Unexpected graph model"),
            run_id="standalone-business-harness",
        )
        budget = _RunBudget(initial={"model_calls": 1})
        harness = _BusinessAgentHarness(
            workspace=workspace,
            config=config,
            budget=budget,
            evidence_tools=frozenset(),
        )
        nested_model = FakeMessagesListChatModel(
            responses=[
                AIMessage(
                    content="nested model evidence",
                    response_metadata={"model_name": "fixture-nested"},
                    usage_metadata={
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "total_tokens": 10,
                    },
                )
            ]
        )

        async def invoke(context, arguments):
            response, _ = await ainvoke_model_with_budget(
                nested_model, [AIMessage(content="query")]
            )
            return response.content

        declared = FunctionTool(
            name="nested_model",
            description="Collect nested model evidence",
            params_json_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            on_invoke_tool=invoke,
        )
        business = BusinessTool(declared, domain="web_search", effect="read")
        executor = ToolExecutor(
            role=workspace.role,
            allowed_domains=frozenset({"web_search"}),
            config=config,
            runtime_config={},
            budget=budget,
            tools={business.name: business},
        )
        harness.tool_executor = executor
        tool = executor.bind(declared)
        model = ScriptedModel(
            [
                [function_call(tool.name, "{}", call_id="nested")],
                [assistant_message("must not spend the reserved call")],
            ],
            default_usage=TOKENS,
        )
        with budget_capture() as nested:
            # Bind the capture after harness construction, as the Runtime does.
            budget.nested = nested
            with pytest.raises(_ReasoningStopped, match="Business budget stopped"):
                await harness.run(
                    Agent(name="standalone", model=model, tools=[tool]), "task"
                )
        assert len(model.calls) == 1
        assert budget.delta["model_calls"] == nested["model_calls"] == 1
        assert budget.projected()["model_calls"] == 3
        assert budget.projected()["input_tokens"] == 17
        assert budget.projected()["total_tokens"] == 24
        assert harness.results["nested"].status == "succeeded"
        assert harness.last_settlement.succeeded

    asyncio.run(run())


def test_native_sdk_tool_owns_defaults_and_receives_call_context():
    called = []

    @function_tool(
        name_override="web_search",
        description_override="Search cited evidence",
        use_docstring_info=False,
        strict_mode=False,
        failure_error_function=None,
    )
    async def declared(context: ToolContext, query: str, limit: int = 3):
        called.append((query, limit, context.tool_call_id))
        return "evidence"

    search = register_tool(declared, domain="web_search", effect="read")

    runtime = _runtime(
        tools=[search],
        model=ScriptedModel(
            [
                [
                    function_call(
                        "web_search", '{"query":"fixture"}', call_id="compat-call"
                    )
                ],
                [assistant_message("finished")],
            ]
        ),
    )
    assert not hasattr(runtime, "execute_tool")
    result = asyncio.run(runtime.run("research fixture"))
    assert called == [("fixture", 3, "compat-call")]
    declaration = runtime.agent.tools[0]
    assert declaration.params_json_schema == search.sdk_tool.params_json_schema
    assert declaration.params_json_schema["required"] == ["query"]
    assert declaration.params_json_schema["properties"]["limit"]["default"] == 3
    assert not declaration.strict_json_schema
    assert runtime.harness.results["compat-call"].status == "succeeded"
    assert result.budget["tool_calls"] == result.budget["search_calls"] == 1


def test_concurrent_native_duplicate_shares_the_original_and_its_budget():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        @function_tool(
            name_override="web_search",
            description_override="Search",
            use_docstring_info=False,
            strict_mode=False,
            failure_error_function=None,
        )
        async def declared(context: ToolContext, query: str):
            calls.append(query)
            entered.set()
            await release.wait()
            capture_budget_usage({"model_calls": 1})
            return "same result"

        web_search = register_tool(declared, domain="web_search", effect="read")

        async def during_run(_call):
            tool = runtime.agent.tools[0]

            async def invoke(arguments):
                context = ToolContext(
                    context=runtime.harness,
                    tool_name=tool.name,
                    tool_call_id="one-call",
                    tool_arguments=arguments,
                )
                return await tool.on_invoke_tool(context, arguments)

            first = asyncio.create_task(invoke('{"query":"q"}'))
            await asyncio.wait_for(entered.wait(), 2)
            duplicate = asyncio.create_task(invoke('{ "query": "q" }'))
            cancelled_duplicate = asyncio.create_task(invoke('{"query":"q"}'))
            await asyncio.sleep(0)
            with pytest.raises(ValueError, match="reused"):
                await invoke('{"query":"other"}')
            cancelled_duplicate.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled_duplicate
            assert not first.done() and not duplicate.done()
            release.set()
            assert await asyncio.gather(first, duplicate) == [
                "same result",
                "same result",
            ]
            assert await invoke('{"query":"q"}') == "same result"
            return [assistant_message("completed")]

        runtime = _runtime(
            tools=[web_search], model=ScriptedModel([ModelStep.respond(during_run)])
        )
        result = await runtime.run("task")
        assert calls == ["q"]
        assert result.budget["tool_calls"] == result.budget["search_calls"] == 1
        assert result.budget["model_calls"] == 2
        assert runtime.harness.results["one-call"].status == "succeeded"
        assert runtime.harness.last_settlement.succeeded

    asyncio.run(scenario())


def test_protected_sdk_validation_runs_before_external_admission(monkeypatch):
    from types import SimpleNamespace

    from open_deep_research.runtime import tool_executor
    from open_deep_research.workflow.execution import stage_context

    @function_tool(
        name_override="web_search",
        description_override="Search",
        use_docstring_info=False,
        strict_mode=False,
        failure_error_function=None,
    )
    async def declared(_context: ToolContext, query: str) -> str:
        pytest.fail("Invalid input must not reach the business callable")

    web_search = register_tool(declared, domain="web_search")

    def start(*args):
        pytest.fail("Invalid input must not enter operations")

    monkeypatch.setattr(tool_executor, "start_operation", start)

    async def during_run(_call):
        arguments = '{"query":123}'
        context = ToolContext(
            context=runtime.harness,
            tool_name=web_search.name,
            tool_call_id="invalid",
            tool_arguments=arguments,
        )
        token = stage_context.set(SimpleNamespace(run_id="test"))
        try:
            output = await runtime.agent.tools[0].on_invoke_tool(context, arguments)
        finally:
            stage_context.reset(token)
        assert output.startswith("Error executing tool:")
        return [assistant_message("completed")]

    runtime = _runtime(
        tools=[web_search], model=ScriptedModel([ModelStep.respond(during_run)])
    )
    result = asyncio.run(runtime.run("task"))
    assert result.budget["tool_calls"] == 1
    record = runtime.harness.results["invalid"]
    assert record.status == "failed"
    assert record.operation_id is None


def test_protected_tool_config_is_read_after_operation_admission(monkeypatch):
    from open_deep_research.runtime import tool_executor
    from open_deep_research.workflow.execution import stage_context

    class Scope:
        run_id = "test"

        async def observe(self, *args, **kwargs):
            return None

    events = []

    def start(*args):
        events.append("admitted")

    def finish(*args):
        events.append("settled")

    monkeypatch.setattr(tool_executor, "start_operation", start)
    monkeypatch.setattr(tool_executor, "finish_operation", finish)

    async def business(context: ToolContext) -> str:
        config = context.context.tool_executor.tool_config(context)
        events.append(config["metadata"]["operation_id"])
        return "protected result"

    @function_tool(
        name_override="web_search",
        description_override="Search",
        use_docstring_info=False,
        strict_mode=False,
        failure_error_function=None,
    )
    async def declared(context: ToolContext, query: str) -> str:
        return await context.context.tool_executor.execute(
            context, business, context
        )

    web_search = register_tool(declared, domain="web_search")

    async def during_run(_call):
        arguments = '{"query":"q"}'
        context = ToolContext(
            context=runtime.harness,
            tool_name=web_search.name,
            tool_call_id="protected",
            tool_arguments=arguments,
        )
        token = stage_context.set(Scope())
        try:
            output = await runtime.agent.tools[0].on_invoke_tool(context, arguments)
        finally:
            stage_context.reset(token)
        assert output == "protected result"
        return [assistant_message("completed")]

    runtime = _runtime(
        tools=[web_search], model=ScriptedModel([ModelStep.respond(during_run)])
    )
    asyncio.run(runtime.run("task"))
    operation_id = events[1]
    assert events[0] == "admitted" and operation_id and events[2] == "settled"
    assert runtime.harness.results["protected"].operation_id == operation_id
    assert runtime.harness.results["protected"].effect_status == "returned"


def test_native_catalog_tools_dispatch_with_their_original_defaults(monkeypatch):
    from open_deep_research import social_media_mcp
    from open_deep_research.search import tavily
    from open_deep_research.social_media.tools import fetch_comments
    from open_deep_research.tools.registry import ResearchComplete, think_tool

    searches = []
    comments = []

    async def search(queries, *, max_results, topic, include_raw_content, config):
        searches.append((queries, max_results, topic, include_raw_content, config))
        return []

    def fetch(post_id, *, limit):
        comments.append((post_id, limit))
        return {"success": True, "comments": []}

    monkeypatch.setattr(tavily, "tavily_search_async", search)
    monkeypatch.setattr(social_media_mcp, "fetch_comments", fetch)
    model = ScriptedModel(
        [
            [
                function_call("web_search", {"queries": ["recall"]}, call_id="raw"),
                function_call("fetch_comments", {"post_id": "one"}, call_id="defaults"),
                function_call(
                    "fetch_comments",
                    {"post_id": "two", "limit": 7.0},
                    call_id="integral",
                ),
                function_call(
                    "fetch_comments", {"post_id": "three", "limit": "8"}, call_id="bad"
                ),
            ],
            [function_call("think_tool", {"reflection": "reviewed"}, call_id="think")],
            [function_call("ResearchComplete", {}, call_id="complete")],
        ]
    )
    runtime = _runtime(
        model=model,
        tools=[tavily.web_search_raw, fetch_comments, think_tool, ResearchComplete],
    )
    result = asyncio.run(runtime.run("task"))
    model.assert_complete()
    assert searches == [(["recall"], 5, "general", True, runtime.runtime_config)]
    assert sorted(comments) == [("one", 20), ("two", 7)]
    assert set(tavily.web_search_raw.sdk_tool.params_json_schema["properties"]) == {
        "queries"
    }
    assert runtime.harness.results["bad"].status == "failed"
    assert runtime.harness.results["bad"].effect_status is None
    assert (
        runtime.harness.results["think"].observation == "Reflection recorded: reviewed"
    )
    assert runtime.harness.results["complete"].observation == ""
    assert result.budget["tool_calls"] == 6
    assert result.budget["search_calls"] == 1


def test_tool_executor_owns_agent_tool_step_and_restores_committed_result(monkeypatch):
    from open_deep_research.runtime import sdk_context, tool_executor

    class StepDBOS:
        workflow_id = None
        executor_id = None
        replay = False
        saved = None
        names: list[str] = []

        @classmethod
        async def run_step_async(cls, options, func):
            cls.names.append(options["name"])
            if cls.replay:
                return cls.saved
            cls.saved = await func()
            return cls.saved

    class HarnessDBOS:
        # A recovering execution keeps the same workflow identity.
        workflow_id = "fixture-workflow"
        executor_id = "fixture-executor"

    monkeypatch.setattr(tool_executor, "DBOS", StepDBOS)
    monkeypatch.setattr(tool_executor, "native_step_available", lambda: True)
    monkeypatch.setattr(sdk_context, "DBOS", HarnessDBOS)

    calls = []

    async def business(query: str) -> str:
        calls.append(query)
        capture_budget_usage({"model_calls": 1})
        return "durable evidence"

    @function_tool(
        name_override="web_search",
        description_override="Search",
        use_docstring_info=False,
        strict_mode=False,
        failure_error_function=None,
    )
    async def declared(context: ToolContext, query: str) -> str:
        return await context.context.tool_executor.execute(context, business, query)

    tool = register_tool(declared, domain="web_search", effect="read")

    def scripted():
        return ScriptedModel(
            [
                [function_call("web_search", '{"query":"q"}', call_id="step")],
                [assistant_message("finished")],
            ],
            default_usage=TOKENS,
        )

    first = _runtime(tools=[tool], model=scripted())
    first_result = asyncio.run(first.run("task"))
    assert calls == ["q"]
    assert StepDBOS.names == ["por.agent_tool"]
    assert StepDBOS.saved["record"]["status"] == "succeeded"
    assert StepDBOS.saved["budget"]["tool_calls"] == 1
    assert StepDBOS.saved["budget"]["model_calls"] == 1
    assert first.harness.results["step"].observation == "durable evidence"
    assert first_result.budget["tool_calls"] == 1
    assert first_result.budget["model_calls"] == 3

    StepDBOS.replay = True
    calls.clear()
    second = _runtime(tools=[tool], model=scripted())
    second_result = asyncio.run(second.run("task"))
    assert calls == []
    assert StepDBOS.names == ["por.agent_tool", "por.agent_tool"]
    assert second.harness.results["step"].status == "succeeded"
    assert second.harness.results["step"].observation == "durable evidence"
    assert (
        second.harness.results["step"].graph_call_id
        == first.harness.results["step"].graph_call_id
    )
    assert second_result.budget["tool_calls"] == 1
    assert second_result.budget["model_calls"] == 3
