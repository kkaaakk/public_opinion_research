"""Exercise all four business roles through the real offline SDK Runner."""

import asyncio
import json
import threading
from contextlib import asynccontextmanager

import pytest
from agents import FunctionTool
from agents.exceptions import ModelBehaviorError
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents.usage import Usage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, Field

from open_deep_research.budget import (
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
)

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
        execute_tool=business_agent._execute_tool_safely,
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
    assert runtime.hooks.results["lookup"].status == "succeeded"


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
    assert runtime.hooks.results["invalid"].status == "failed"
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
    assert runtime.hooks.results["revoked"].status == "failed"
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
    assert runtime.hooks.results["failed"].status == "failed"
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
    assert runtime.hooks.results["two"].status == "failed"
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
    assert runtime.hooks.last_settlement.succeeded
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
        assert runtime.hooks.results["cancel"].status == "cancelled"
        assert runtime.hooks.last_settlement.succeeded
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
        assert runtime.hooks.last_settlement.succeeded


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
    assert Configuration().business_agent_runtime == "legacy"
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
        assert runtime.hooks.last_settlement.succeeded

    asyncio.run(run())


def test_sync_tool_cancellation_drains_actual_thread_result_and_usage():
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
        assert runtime.hooks.results["thread"].status == "succeeded"
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
    assert runtime.hooks.results["limited"].status == "failed"


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
    assert runtime.hooks.results["result"].status == (
        "failed" if result_kind == "multimodal" else "cancelled"
    )
    if result_kind == "multimodal":
        assert "multimodal MCP outputs" in runtime.hooks.results["result"].observation
