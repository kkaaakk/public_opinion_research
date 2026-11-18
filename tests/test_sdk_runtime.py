"""Exercise all four business roles through the real offline SDK Runner."""

import asyncio
from dataclasses import replace
from typing import Annotated

import pytest
from agents import FunctionTool, function_tool
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext
from agents.usage import Usage
from pydantic import Field

from open_deep_research.budget import (
    capture_budget_usage,
)
from open_deep_research.configuration import Configuration
from open_deep_research.public_opinion_agents import (
    PUBLIC_OPINION_AGENT_ORDER,
    get_public_opinion_agent_spec,
)
from open_deep_research.runtime.sdk_runtime import (
    SDKAgentRuntime,
    _BusinessAgentHarness,
    _RunBudget,
)
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


def _tool(
    role="public_signal",
    *,
    coroutine=None,
    func=None,
    with_context=None,
    name=None,
    domain=None,
    effect=None,
) -> BusinessTool:
    tool_name = name or (
        "web_search" if role == "public_signal" else "rag_search"
    )
    tool_domain = domain or (
        "web_search" if role == "public_signal" else "rag"
    )

    async def default_search(query: str, limit: int = 3):
        return f"Evidence {query}, limit={limit} [source: fixture]"

    async def run_business(context, query: str, limit: int):
        if with_context is not None:
            return await with_context(context, query=query, limit=limit)
        if coroutine is not None:
            return await coroutine(query=query, limit=limit)
        if func is not None:
            return await asyncio.to_thread(func, query=query, limit=limit)
        return await default_search(query=query, limit=limit)

    @function_tool(
        name_override=tool_name,
        description_override="Search cited evidence",
        use_docstring_info=False,
        strict_mode=False,
        failure_error_function=None,
    )
    async def declared(
        context: ToolContext,
        query: str,
        limit: Annotated[int, Field(ge=1, le=10)] = 3,
    ) -> str:
        return await context.context.tool_executor.execute(
            context, run_business, context, query, limit
        )

    return BusinessTool(
        declared,
        domain=tool_domain,
        effect=effect or "protected",
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
    initial_budget=None,
    compress=None,
    research_round=1,
    task_id=None,
):
    config = config or Configuration(research_model="openai:fixture")
    spec = get_public_opinion_agent_spec(role)
    return SDKAgentRuntime(
        role=role,
        spec=spec,
        task_id=task_id or f"{role}:round-{research_round}",
        research_round=research_round,
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
        model_factory=lambda model_name, max_tokens: pytest.fail(
            "Unexpected nested model call"
        ),
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
    runtime = _runtime(role, model=model, tools=[tool])
    result = asyncio.run(runtime.run("initial assignment"))
    model.assert_complete()
    assert len(model.calls) == 2
    assert invoked == [("brand", 3)]
    assert all(isinstance(item, FunctionTool) for item in runtime.agent.tools)
    assert runtime.agent.tools[0].params_json_schema == tool.sdk_tool.params_json_schema
    assert f"Agent role: {role}" in result.report
    assert "Research mode: initial" in result.report
    assert f"{role} final report" in result.report
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


# 验证 Agent 在多轮工具调用后由普通最终回复自然结束，且仍执行报告整理。
def test_agent_ends_naturally_after_multiple_tool_rounds():
    queries = []

    async def search(query: str, limit: int = 3):
        queries.append(query)
        return f"evidence for {query}"

    tool = _tool(coroutine=search, effect="read")
    model = ScriptedModel(
        [
            [function_call(tool.name, '{"query":"first"}', call_id="call-1")],
            [function_call(tool.name, '{"query":"second"}', call_id="call-2")],
            [assistant_message("final evidence summary")],
        ],
        default_usage=TOKENS,
    )
    runtime = _runtime(tools=[tool], model=model)
    result = asyncio.run(runtime.run("collect evidence"))
    model.assert_complete()

    assert queries == ["first", "second"]
    assert runtime.harness.results["call-1"].status == "succeeded"
    assert runtime.harness.results["call-2"].status == "succeeded"
    assert result.budget["tool_calls"] == 2
    assert runtime.agent.tool_use_behavior == "run_llm_again"
    assert {bound.name for bound in runtime.tools} == {tool.name}
    # _finish still wraps the compressed role report around the final reply.
    assert result.report.startswith("Agent role: public_signal")
    assert "Role report" in result.report
    assert "final evidence summary" in result.report


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
        executor = runtime.harness.tool_executor
        executor.tools[tool.name] = replace(tool, domain="unclassified")
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


@pytest.mark.parametrize("kind", ["raises", "business_error", "rag_error"])
def test_explicit_tool_failure_status_is_retained(kind):
    async def fail(query: str, limit: int = 3):
        if kind == "raises":
            raise RuntimeError("MCP transport or business failure")
        if kind == "business_error":
            return '{"success":false,"error":"denied"}'
        return "Local RAG search failed: offline fixture"

    tool = _tool("internal_knowledge", coroutine=fail)
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


def test_max_turns_keeps_completed_tool_results_in_report_messages():
    tool = _tool("internal_knowledge")

    def always_call(_call):
        return [function_call(tool.name, '{"query":"again"}', call_id="repeat")]

    model = ScriptedModel(
        [ModelStep.respond(always_call), ModelStep.respond(always_call)]
    )
    runtime = _runtime(
        "internal_knowledge",
        model=model,
        tools=[tool],
        config=Configuration(
            research_model="openai:fixture", max_react_tool_calls=1
        ),
    )
    result = asyncio.run(runtime.run("task"))
    assert "Research mode: initial" in result.report
    messages = runtime._report_messages("task")
    tool_messages = [m for m in messages if getattr(m, "type", "") == "tool"]
    assert tool_messages
    assert all(m.tool_call_id for m in tool_messages)


def test_exhausted_budget_finishes_from_collected_notes():
    tool = _tool("public_signal")
    runtime = _runtime(
        "public_signal",
        tools=[tool],
        model=ScriptedModel(
            [
                [function_call(tool.name, '{"query":"q"}', call_id="one")],
                [assistant_message("should not be requested")],
            ]
        ),
        initial_budget={"model_calls": 5},
        config=Configuration(
            research_model="openai:fixture",
            budget_enabled=True,
            max_model_calls=5,
        ),
    )
    result = asyncio.run(runtime.run("task"))
    assert "Agent role: public_signal" in result.report
    assert runtime.harness.model_calls == 0
    assert result.budget.get("degradation_reasons")


def test_duplicate_business_tool_names_are_rejected_before_execution():
    runtime = _runtime(
        "public_signal", tools=[_tool("public_signal"), _tool("public_signal")]
    )
    with pytest.raises(ValueError, match="Duplicate business tool name"):
        asyncio.run(runtime.run("task"))


def test_role_result_round_trip_has_no_private_memory_or_summary():
    runtime = _runtime(
        "public_signal", research_round=2, model=ScriptedModel([[assistant_message("ok")]])
    )
    result = asyncio.run(runtime.run("follow-up assignment"))
    assert "Research mode: followup" in result.report
    assert set(result.__dict__) == {"report", "budget", "replace_report"}


def test_business_harness_snapshot_round_trip():
    harness = _BusinessAgentHarness(
        role="public_signal",
        task_id="task-1",
        research_round=1,
        config=Configuration(research_model="openai:fixture"),
        budget=_RunBudget(initial={}),
        model_factory=lambda *_: pytest.fail("Unexpected nested model call"),
    )
    harness.model_calls = 3
    harness.rolling_summary = "older work summarized"
    harness._compacted_turns = {1, 2}
    harness.run_budget.delta = {"model_calls": 3}
    state = harness._durable_snapshot()
    harness.model_calls = 0
    harness.rolling_summary = ""
    harness._compacted_turns = set()
    harness.run_budget.delta = {}
    harness._restore_durable_snapshot(state)
    assert harness.model_calls == 3
    assert harness.rolling_summary == "older work summarized"
    assert harness._compacted_turns == {1, 2}
    assert harness.run_budget.delta == {"model_calls": 3}


def test_duplicate_native_call_keeps_its_step_without_reexecution(monkeypatch):
    from open_deep_research.runtime import sdk_context, tool_executor

    class StepDBOS:
        workflow_id = None
        executor_id = None
        names: list[str] = []
        saved: dict[int, dict] = {}

        @classmethod
        async def run_step_async(cls, options, func):
            cls.names.append(options["name"])
            index = len(cls.names) - 1
            cls.saved[index] = await func()
            return cls.saved[index]

    class HarnessDBOS:
        workflow_id = "fixture-workflow"
        executor_id = "fixture-executor"

    monkeypatch.setattr(tool_executor, "DBOS", StepDBOS)
    monkeypatch.setattr(tool_executor, "native_step_available", lambda: True)
    monkeypatch.setattr(sdk_context, "DBOS", HarnessDBOS)

    calls = []

    async def business(query: str) -> str:
        calls.append(query)
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

    async def during_run(_call):
        bound = runtime.agent.tools[0]

        async def invoke():
            context = ToolContext(
                context=runtime.harness,
                tool_name=bound.name,
                tool_call_id="same",
                tool_arguments='{"query":"q"}',
            )
            return await bound.on_invoke_tool(context, '{"query":"q"}')

        assert await asyncio.gather(invoke(), invoke()) == ["durable evidence"] * 2
        return [assistant_message("finished")]

    runtime = _runtime(
        tools=[tool], model=ScriptedModel([ModelStep.respond(during_run)])
    )
    result = asyncio.run(runtime.run("task"))
    assert calls == ["q"]
    # Each invocation keeps its own native Step slot; only the first executes
    # the business function and reserves budget.
    assert StepDBOS.names == ["por.agent_tool", "por.agent_tool"]
    assert [entry["record"]["tool_call_id"] for entry in StepDBOS.saved.values()] == [
        "same",
        "same",
    ]
    assert runtime.harness.results["same"].status == "succeeded"
    assert result.budget["tool_calls"] == 1
