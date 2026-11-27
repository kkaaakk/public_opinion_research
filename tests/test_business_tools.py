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
    unconfirmed,
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


# 在一次 Run 的 StageContext 下调用工具，模拟恢复后新建的进程。
def _invoke(
    tool: BusinessTool,
    executor: ToolExecutor,
    harness: SDKContextHarness,
    *,
    stage: str = "stage-1",
    call_id: str = "call-1",
) -> Any:
    scope = StageContext("run", stage, "fixture", 0, {"configurable": {}}, {})
    token = stage_context.set(scope)
    try:
        return asyncio.run(
            executor.invoke(
                tool.sdk_tool,
                _context(harness, tool.name, '{"query":"q"}', call_id),
                '{"query":"q"}',
            )
        )
    finally:
        stage_context.reset(token)


class _UncommittedStep:
    """A ``por.agent_tool`` Step that never commits, so each attempt re-runs."""

    workflow_id = "replay-workflow"
    executor_id = "replay-executor"

    @classmethod
    async def run_step_async(cls, _options, function):
        return await function()


# 只保留执行语义，不向 SSE/存储发布观察事件。
def _silence_observations(monkeypatch) -> None:
    async def observe(_self, _kind, _payload, *, call_id=None):
        return None

    monkeypatch.setattr(StageContext, "observe", observe)


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
def test_invalid_arguments_never_admit_a_protected_operation(
    monkeypatch, memory_ledger
) -> None:
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
def test_protected_tool_settles_receipts_and_unknown_failure(
    monkeypatch, memory_ledger
) -> None:
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


# 验证外部服务在启动前明确拒绝时，回执记为 not_executed 且错误返回模型。
def test_confirmed_pre_run_rejection_settles_not_executed(
    monkeypatch, memory_ledger
) -> None:
    from open_deep_research.runtime import tool_executor
    from open_deep_research.tools import ToolNotExecuted

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

    @business_tool(domain="web_search", effect="protected")
    async def web_search(context: ToolContext, query: str) -> str:
        raise ToolNotExecuted("provider rejected the input before starting")

    harness, executor = _executor(web_search)
    output = _invoke(web_search, executor, harness)

    assert output.startswith("Error executing tool:")
    assert "provider rejected the input before starting" in output
    assert starts and len(starts) == 1
    assert finishes[-1][-1] == "not_executed"
    assert harness.results["call-1"].status == "failed"
    assert harness.results["call-1"].effect_status == "not_executed"


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


# 验证绑定阶段只替换调用回调，SDK 声明保持不变。
def test_tool_manager_binds_the_admitted_declaration_unchanged() -> None:
    tool = _search_tool()
    manager = ToolManager(
        "public_signal",
        get_public_opinion_agent_spec("public_signal"),
        {"configurable": {}},
    )

    harness, executor = _executor(tool)
    bound = manager.bind([tool], executor)

    assert [item.name for item in bound] == [tool.name]
    assert bound[0].params_json_schema == tool.sdk_tool.params_json_schema
    assert bound[0].strict_json_schema is tool.sdk_tool.strict_json_schema
    assert bound[0] is not tool.sdk_tool


# 验证装配阶段拒绝非 BusinessTool 的托管检索声明，而不是留到执行期。
def test_tool_manager_rejects_hosted_search_declarations(monkeypatch) -> None:
    async def fake_get_all_tools(_config):
        return [{"type": "web_search_preview"}]

    monkeypatch.setattr(tool_manager, "get_all_tools", fake_get_all_tools)

    manager = ToolManager(
        "public_signal",
        get_public_opinion_agent_spec("public_signal"),
        {"configurable": {}},
    )
    with pytest.raises(TypeError, match="BusinessTool"):
        asyncio.run(manager.discover())


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
    # A read tool keeps the caller's config untouched.
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
    # An idempotent tool adds only the derived key; the run config stays intact.
    rag_query, rag_config = seen["rag"]
    assert rag_query == "rag"
    assert rag_config["configurable"] == {"fixture": "run"}
    assert rag_config["metadata"]["idempotency_key"]


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


# ---------------------------------------------------------------------------
# Recovery: an uncommitted Step follows the declared effect
# ---------------------------------------------------------------------------


# 验证 read 工具在未提交 Step 后重跑，并明确记录重复请求风险。
def test_read_tool_reruns_after_an_uncommitted_step_and_warns(monkeypatch) -> None:
    from open_deep_research.runtime import tool_executor

    invocations: list[str] = []
    admissions: list[str] = []

    @business_tool(domain="web_search", effect="read")
    async def web_search(context: ToolContext, query: str) -> str:
        invocations.append(query)
        return "evidence:" + query

    def admit(_config, _run, _workflow, _executor, call_id, _name, _search):
        admissions.append(call_id)
        # The ledger keeps one row: the second attempt is the same logical call.
        return "admitted" if len(admissions) == 1 else "duplicate"

    _silence_observations(monkeypatch)
    monkeypatch.setattr(tool_executor, "DBOS", _UncommittedStep)
    monkeypatch.setattr(tool_executor, "native_step_available", lambda: True)
    monkeypatch.setattr(tool_executor, "admit_tool_call", admit)
    monkeypatch.setattr(tool_executor, "finish_tool_call", lambda *_args: True)

    attempts = []
    for attempt in range(2):
        harness, executor = _executor(web_search)
        output = _invoke(web_search, executor, harness, stage=f"stage-{attempt}")
        attempts.append((output, executor.budget.delta))

    # The tool ran twice because no committed Step could supply its result...
    assert [output for output, _ in attempts] == ["evidence:q", "evidence:q"]
    assert invocations == ["q", "q"]
    assert len(admissions) == 2 and admissions[0] == admissions[1]
    # ...but each attempt charged exactly one logical call.
    assert [delta["tool_calls"] for _, delta in attempts] == [1, 1]
    assert not attempts[0][1]["budget_warnings"]
    assert "re-ran after an uncommitted tool step" in " ".join(
        attempts[1][1]["budget_warnings"]
    )


# 验证 protected 工具在未提交 Step 后拒绝重跑，保留原回执等待核对。
def test_protected_tool_never_reruns_after_an_uncommitted_step(monkeypatch) -> None:
    from open_deep_research.runtime import tool_executor

    invocations: list[str] = []
    receipts: dict[str, str | None] = {}

    @business_tool(domain="web_search", effect="protected")
    async def publish(context: ToolContext, query: str) -> str:
        invocations.append(query)
        return "published:" + query

    def start_operation(
        _run, _workflow, _executor, operation_id, _call, _name, _fingerprint
    ):
        # Mirrors por_domain.operations: the earlier attempt's receipt exists,
        # but its response was never journaled.
        if operation_id in receipts:
            raise unconfirmed("External tool already ran for this call")
        receipts[operation_id] = None

    def finish_operation(_run, operation_id, outcome):
        receipts[operation_id] = outcome

    async def commit(write, *args):
        await asyncio.to_thread(write, *args)

    _silence_observations(monkeypatch)
    monkeypatch.setattr(tool_executor, "DBOS", _UncommittedStep)
    monkeypatch.setattr(tool_executor, "native_step_available", lambda: True)
    monkeypatch.setattr(tool_executor, "admit_tool_call", lambda *_args: "duplicate")
    monkeypatch.setattr(tool_executor, "finish_tool_call", lambda *_args: True)
    monkeypatch.setattr(tool_executor, "start_operation", start_operation)
    monkeypatch.setattr(tool_executor, "finish_operation", finish_operation)
    monkeypatch.setattr(tool_executor, "commit_receipt", commit)

    first_harness, first_executor = _executor(publish)
    assert _invoke(publish, first_executor, first_harness, stage="stage-1") == (
        "published:q"
    )
    receipt = tool_executor.ToolExecutor.operation_id(first_harness.results["call-1"])

    second_harness, second_executor = _executor(publish)
    with pytest.raises(ExecutionControlError):
        _invoke(publish, second_executor, second_harness, stage="stage-2")

    assert invocations == ["q"]
    assert receipts == {receipt: "returned"}
    assert second_harness.results["call-1"].status == "failed"
    assert second_harness.results["call-1"].error_type == "ExecutionControlError"


# 验证 idempotent 工具取得稳定的幂等键，且不伪造 Operation 身份。
def test_idempotent_tool_receives_a_stable_idempotency_key(monkeypatch) -> None:
    from open_deep_research.runtime import tool_executor

    seen: list[dict[str, Any]] = []

    @business_tool(domain="rag", effect="idempotent")
    async def rag_search(context: ToolContext, query: str) -> str:
        seen.append(
            dict(context.context.tool_executor.tool_config(context).get("metadata", {}))
        )
        return "cited:" + query

    _silence_observations(monkeypatch)
    monkeypatch.setattr(tool_executor, "DBOS", _UncommittedStep)
    monkeypatch.setattr(tool_executor, "native_step_available", lambda: False)
    monkeypatch.setattr(tool_executor, "admit_tool_call", lambda *_args: "admitted")
    monkeypatch.setattr(tool_executor, "finish_tool_call", lambda *_args: True)

    harness, executor = _executor(
        rag_search, role="internal_knowledge", domains=frozenset({"rag"})
    )
    assert _invoke(rag_search, executor, harness) == "cited:q"

    assert seen == [
        {
            "idempotency_key": tool_executor.digest(
                (_UncommittedStep.workflow_id, "rag_search", "call-1")
            )
        }
    ]
