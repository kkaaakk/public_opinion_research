"""Contracts for ``@business_tool`` and the role ``ToolManager`` assembly.

These tests stay offline: the SDK, real ``ToolExecutor`` and real
``business_tool`` declarations are used, while native DBOS Steps and receipt
writes are replaced by deterministic fakes.
"""

import asyncio
import json
import threading
from dataclasses import asdict
from typing import Any

import pytest
from agents import function_tool
from agents.tool_context import ToolContext

from open_deep_research.configuration import Configuration
from open_deep_research.public_opinion_agents import get_public_opinion_agent_spec
from open_deep_research.runtime.sdk_context import (
    ObservedToolResult,
    SDKContextHarness,
)
from open_deep_research.runtime.sdk_runtime import _RunBudget
from open_deep_research.runtime.tool_executor import ToolExecutor
from open_deep_research.tools import BusinessTool, business_tool
from open_deep_research.tools import manager as tool_manager
from open_deep_research.tools.manager import ToolManager
from open_deep_research.workflow.execution import (
    ExecutionControlError,
    StageContext,
    stage_context,
)


# 构造一个真实 Executor 与 Harness，供工具声明直接调用。
def _executor(
    tool: BusinessTool,
    *,
    role: str = "public_signal",
    domains: frozenset[str] | None = None,
) -> tuple[SDKContextHarness, ToolExecutor]:
    harness = SDKContextHarness(role=role, task_id="business-tool-fixture")
    executor = ToolExecutor(
        role,
        domains or frozenset({"web_search"}),
        Configuration(research_model="openai:fixture"),
        {"configurable": {"fixture": "run"}},
        _RunBudget(initial={}),
        {tool.name: tool},
    )
    harness.tool_executor = executor
    return harness, executor


# 构造与当前工具调用 ID 一致的 SDK 上下文。
def _context(
    harness: SDKContextHarness,
    name: str,
    arguments: str,
    call_id: str = "call-1",
) -> ToolContext:
    return ToolContext(
        context=harness,
        tool_name=name,
        tool_call_id=call_id,
        tool_arguments=arguments,
    )


# 声明一个携带默认值的业务工具，用于 Schema 与执行契约。
def _search_tool(**options: Any) -> BusinessTool:
    @business_tool(
        domain="web_search",
        effect="read",
        description_override="Search cited evidence",
        **options,
    )
    async def web_search(
        context: ToolContext, query: str, limit: int = 3
    ) -> str:
        return f"result:{query}:{limit}"

    return web_search


# 验证装饰器保留官方 SDK 的名称、说明、Schema 与默认值。
def test_declared_tool_matches_the_official_sdk_schema() -> None:
    declared = _search_tool()

    async def web_search(
        context: ToolContext, query: str, limit: int = 3
    ) -> str:
        return f"result:{query}:{limit}"

    official = function_tool(
        web_search,
        name_override="web_search",
        description_override="Search cited evidence",
        use_docstring_info=False,
        strict_mode=False,
        failure_error_function=None,
    )

    assert declared.name == official.name == "web_search"
    assert declared.description == official.description
    assert declared.sdk_tool.strict_json_schema is False
    for key in ("properties", "required"):
        assert declared.sdk_tool.params_json_schema[key] == (
            official.params_json_schema[key]
        )
    assert declared.domain == "web_search" and declared.effect == "read"


# 验证声明后的工具自动进入 ToolExecutor，应用默认值并只执行业务函数一次。
def test_declared_tool_executes_through_the_executor_once() -> None:
    tool = _search_tool()
    harness, executor = _executor(tool)

    output = asyncio.run(
        executor.invoke(
            tool.sdk_tool,
            _context(harness, tool.name, '{"query":"brand"}'),
            '{"query":"brand"}',
        )
    )

    assert output == "result:brand:3"
    assert harness.results["call-1"].status == "succeeded"
    assert harness.results["call-1"].effect_status == "returned"
    assert harness.results["call-1"].operation_id is None
    assert executor.budget.delta["tool_calls"] == 1
    assert executor.budget.delta["search_calls"] == 1


# 验证第一个参数必须是 ToolContext，避免装饰器误生成模型可见参数。
def test_declaration_requires_tool_context_first() -> None:
    with pytest.raises(TypeError, match="ToolContext"):

        @business_tool(domain="web_search", effect="read")
        async def missing_context(query: str) -> str:
            return query


# 验证 SDK 参数错误发生在业务执行之前，受保护操作不会被登记。
def test_invalid_arguments_never_admit_a_protected_operation(monkeypatch) -> None:
    from open_deep_research.runtime import tool_executor

    invoked: list[Any] = []
    starts: list[tuple] = []

    @business_tool(domain="web_search", effect="protected")
    async def web_search(context: ToolContext, query: str) -> str:
        invoked.append(query)
        return "unexpected"

    def start_operation(*args):
        starts.append(args)

    monkeypatch.setattr(tool_executor, "start_operation", start_operation)
    harness, executor = _executor(web_search)
    scope = StageContext("run", "stage", "fixture", 0, {"configurable": {}}, {})
    token = stage_context.set(scope)
    try:
        output = asyncio.run(
            executor.invoke(
                web_search.sdk_tool,
                _context(harness, web_search.name, '{"query":3}'),
                '{"query":3}',
            )
        )
    finally:
        stage_context.reset(token)

    assert output.startswith("Error executing tool:")
    assert invoked == [] and starts == []
    assert harness.results["call-1"].status == "failed"


# 验证受保护工具登记并结算回执，业务失败按不确定结果上报。
def test_protected_tool_settles_receipts_and_unknown_failure(monkeypatch) -> None:
    from open_deep_research.runtime import tool_executor

    starts: list[tuple] = []
    finishes: list[tuple] = []

    def start_operation(*args):
        starts.append(args)

    def finish_operation(*args):
        finishes.append(args)

    async def commit(write, *args):
        await asyncio.to_thread(write, *args)

    async def observe(_self, _kind, _payload, *, call_id=None):
        return None

    monkeypatch.setattr(tool_executor, "start_operation", start_operation)
    monkeypatch.setattr(tool_executor, "finish_operation", finish_operation)
    monkeypatch.setattr(tool_executor, "commit_receipt", commit)
    monkeypatch.setattr(StageContext, "observe", observe)

    async def run(outcome: str):
        @business_tool(domain="web_search", effect="protected")
        async def web_search(context: ToolContext, query: str) -> str:
            # The operation identity exists before the business function runs.
            assert context.context.tool_executor.tool_config(context)["metadata"][
                "operation_id"
            ]
            if outcome == "fail":
                raise RuntimeError("synthetic remote failure")
            return "ok:" + query

        harness, executor = _executor(web_search)
        scope = StageContext(
            "run", f"stage-{outcome}", "fixture", 0, {"configurable": {}}, {}
        )
        token = stage_context.set(scope)
        try:
            return await executor.invoke(
                web_search.sdk_tool,
                _context(harness, web_search.name, '{"query":"q"}'),
                '{"query":"q"}',
            )
        finally:
            stage_context.reset(token)

    assert asyncio.run(run("ok")) == "ok:q"
    assert finishes[-1][-1] == "returned"
    with pytest.raises(ExecutionControlError):
        asyncio.run(run("fail"))
    assert finishes[-1][-1] == "unknown"
    assert starts and len(starts) == 2


# 验证同步业务函数在线程中执行，并自动启用取消等待。
def test_sync_business_function_runs_in_a_thread_and_drains() -> None:
    observed: list[str] = []

    @business_tool(domain="web_search", effect="read")
    def web_search(context: ToolContext, query: str) -> str:
        observed.append(threading.current_thread().name)
        return "sync:" + query

    assert web_search.drain_on_cancel is True
    harness, executor = _executor(web_search)
    output = asyncio.run(
        executor.invoke(
            web_search.sdk_tool,
            _context(harness, web_search.name, '{"query":"q"}'),
            '{"query":"q"}',
        )
    )

    assert output == "sync:q"
    assert len(observed) == 1


# 验证相同 Tool Call ID 的并发调用共享一次业务执行。
def test_duplicate_call_id_runs_the_business_function_once() -> None:
    called: list[str] = []

    @business_tool(domain="web_search", effect="read")
    async def web_search(context: ToolContext, query: str) -> str:
        called.append(query)
        await asyncio.sleep(0)
        return "evidence:" + query

    harness, executor = _executor(web_search)
    arguments = '{"query":"q"}'

    async def run():
        return await asyncio.gather(
            executor.invoke(
                web_search.sdk_tool, _context(harness, web_search.name, arguments), arguments
            ),
            executor.invoke(
                web_search.sdk_tool, _context(harness, web_search.name, arguments), arguments
            ),
        )

    assert asyncio.run(run()) == ["evidence:q", "evidence:q"]
    assert called == ["q"]
    assert executor.budget.delta["tool_calls"] == 1


# 验证已提交的 por.agent_tool 结果恢复时不再调用业务函数。
def test_committed_step_result_restores_without_rerunning(monkeypatch) -> None:
    from open_deep_research.runtime import tool_executor

    called: list[str] = []

    @business_tool(domain="web_search", effect="read")
    async def web_search(context: ToolContext, query: str) -> str:
        called.append(query)
        return "unexpected"

    class FakeDBOS:
        workflow_id = "fixture-workflow"
        executor_id = "fixture-executor"

        @classmethod
        async def run_step_async(cls, _options, _func):
            record = ObservedToolResult(
                sdk_run_id="fixture-run",
                tool_call_id="call-1",
                tool_name="web_search",
                arguments_json='{"query":"q"}',
                args={"query": "q"},
                model_turn=0,
                status="succeeded",
                observation="saved result",
            )
            return {"output": "saved result", "record": asdict(record), "budget": {}}

    monkeypatch.setattr(tool_executor, "DBOS", FakeDBOS)
    monkeypatch.setattr(tool_executor, "native_step_available", lambda: True)

    harness, executor = _executor(web_search)
    output = asyncio.run(
        executor.invoke(
            web_search.sdk_tool,
            _context(harness, web_search.name, '{"query":"q"}'),
            '{"query":"q"}',
        )
    )

    assert output == "saved result" and called == []
    assert harness.results["call-1"].status == "succeeded"


# 验证 ToolManager 按角色领域和插件授权筛选，并保持目录稳定。
def test_tool_manager_filters_role_and_plugin_tools(monkeypatch) -> None:
    def declared(name: str, domain: str) -> BusinessTool:
        @business_tool(domain=domain, effect="read", name_override=name)
        async def fixture(context: ToolContext, query: str) -> str:
            return query

        return fixture

    local = [
        declared("web_search", "web_search"),
        declared("rag_search", "rag"),
        declared("db_query", "database"),
        declared("social_search", "social_media"),
    ]
    plugin = [declared("plugin_social", "social_media")]

    async def fake_get_all_tools(_config):
        return list(local)

    async def fake_load_plugin_tools(*_args, **_kwargs):
        return list(plugin), None

    monkeypatch.setattr(tool_manager, "get_all_tools", fake_get_all_tools)
    monkeypatch.setattr(tool_manager, "load_plugin_tools", fake_load_plugin_tools)

    spec = get_public_opinion_agent_spec("public_signal")
    manager = ToolManager("public_signal", spec, {"configurable": {}})
    discovered = asyncio.run(manager.discover())

    assert {tool.name for tool in discovered} == {
        "web_search",
        "social_search",
        "plugin_social",
    }
    assert asyncio.run(manager.discover()) == discovered


# 验证重复的工具名在装配阶段被拒绝。
def test_tool_manager_rejects_duplicate_tool_names(monkeypatch) -> None:
    def declared(name: str) -> BusinessTool:
        @business_tool(domain="web_search", effect="read", name_override=name)
        async def fixture(context: ToolContext, query: str) -> str:
            return query

        return fixture

    async def fake_get_all_tools(_config):
        return [declared("web_search"), declared("web_search")]

    async def fake_load_plugin_tools(*_args, **_kwargs):
        return [], None

    monkeypatch.setattr(tool_manager, "get_all_tools", fake_get_all_tools)
    monkeypatch.setattr(tool_manager, "load_plugin_tools", fake_load_plugin_tools)

    manager = ToolManager(
        "public_signal",
        get_public_opinion_agent_spec("public_signal"),
        {"configurable": {}},
    )
    with pytest.raises(ValueError, match="Duplicate business tool name"):
        asyncio.run(manager.discover())


# 验证绑定阶段再次执行权限校验，并生成 SDK 可直接使用的声明。
def test_tool_manager_index_and_bind_keep_role_policy() -> None:
    tool = _search_tool()
    manager = ToolManager(
        "internal_knowledge",
        get_public_opinion_agent_spec("internal_knowledge"),
        {"configurable": {}},
    )

    with pytest.raises(TypeError, match="BusinessTool"):
        manager.index([{"type": "web_search_preview"}])
    with pytest.raises(ValueError, match="outside"):
        manager.index([tool])

    public_manager = ToolManager(
        "public_signal",
        get_public_opinion_agent_spec("public_signal"),
        {"configurable": {}},
    )
    indexed = public_manager.index([tool])
    assert indexed == {tool.name: tool}
    with pytest.raises(ValueError, match="Duplicate business tool name"):
        public_manager.index([tool, tool])

    harness, executor = _executor(tool)
    bound = public_manager.bind(indexed.values(), executor)
    assert [item.name for item in bound] == [tool.name]
    assert bound[0].params_json_schema == tool.sdk_tool.params_json_schema


# 验证 Tavily、RAG 声明保留目录身份，并在 Executor 边界读取运行配置。
def test_retrieval_tools_keep_catalog_identity_and_read_run_config(
    monkeypatch,
) -> None:
    from open_deep_research.search import tavily
    from open_deep_research.tools import rag_tool

    seen: dict[str, Any] = {}

    async def fake_search(queries, *, config=None, **_kwargs):
        seen["web"] = (queries, config)
        return "web-evidence"

    async def fake_rag(query, config=None):
        seen["rag"] = (query, config)
        return "rag-evidence"

    monkeypatch.setattr(tavily, "_tavily_search", fake_search)
    monkeypatch.setattr(rag_tool, "_rag_search", fake_rag)

    assert (tavily.web_search.domain, tavily.web_search.effect) == (
        "web_search",
        "read",
    )
    assert (tavily.web_search_raw.name, tavily.web_search_raw.effect) == (
        "web_search",
        "read",
    )
    assert (
        rag_tool.rag_search_tool.name,
        rag_tool.rag_search_tool.domain,
        rag_tool.rag_search_tool.effect,
    ) == ("rag_search", "rag", "idempotent")

    web_harness, web_executor = _executor(tavily.web_search)
    assert (
        asyncio.run(
            web_executor.invoke(
                tavily.web_search.sdk_tool,
                _context(web_harness, "web_search", '{"queries":["a"]}'),
                '{"queries":["a"]}',
            )
        )
        == "web-evidence"
    )
    assert seen["web"] == (["a"], {"configurable": {"fixture": "run"}})

    rag_harness, rag_executor = _executor(
        rag_tool.rag_search_tool,
        role="internal_knowledge",
        domains=frozenset({"rag"}),
    )
    assert (
        asyncio.run(
            rag_executor.invoke(
                rag_tool.rag_search_tool.sdk_tool,
                _context(rag_harness, "rag_search", '{"query":"rag"}'),
                '{"query":"rag"}',
            )
        )
        == "rag-evidence"
    )
    assert seen["rag"] == ("rag", {"configurable": {"fixture": "run"}})


# 验证新声明进入 por.role_tools 目录后可按当前与旧格式恢复。
def test_role_catalog_round_trip_accepts_new_declarations() -> None:
    from open_deep_research.runtime import business_agent

    tool = _search_tool()
    catalog = business_agent._tool_catalog([tool])
    assert catalog[0]["effect"] == "read"
    assert json.loads(json.dumps(catalog[0]["schema"])) == (
        tool.sdk_tool.params_json_schema
    )
    assert business_agent._restore_role_tools(catalog, [tool]) == [tool]

    legacy = [{key: value for key, value in catalog[0].items() if key != "effect"}]
    assert business_agent._restore_role_tools(legacy, [tool]) == [tool]
