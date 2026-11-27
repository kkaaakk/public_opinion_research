# 验证 MCP 工具通过官方 Agents SDK 发现、声明并进入统一 ToolExecutor。
"""SDK-native MCP migration contracts.

These tests use real lightweight ``FastMCP`` servers for stdio and Streamable
HTTP, plus the same offline SDK runtime fixtures as the local-tool tests.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import uvicorn
from agents import FunctionTool, function_tool
from agents.mcp import MCPServerStdio
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext
from mcp.types import CallToolResult, ImageContent, TextContent, Tool
from test_sdk_runtime import TOKENS, _runtime

from open_deep_research.configuration import Configuration
from open_deep_research.mcp import tools as mcp_tools
from open_deep_research.mcp.tool_wrapper import (
    MCPToolError,
    _normalise_result,
    allocate_tool_name,
    build_mcp_tool,
)
from open_deep_research.runtime.sdk_context import (
    ObservedToolResult,
    SDKContextHarness,
)
from open_deep_research.runtime.sdk_runtime import _RunBudget
from open_deep_research.runtime.tool_executor import ToolExecutor
from open_deep_research.tools import BusinessTool, register_tool
from open_deep_research.workflow.execution import StageContext, stage_context

SERVER_SCRIPT = '''from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from typing import Literal
import time

server = FastMCP("sdk-mcp-fixture")

@server.tool()
def echo(query: str) -> str:
    """Return the query with a deterministic marker."""
    return "echo:" + query

@server.tool()
def fail(query: str) -> str:
    """Raise a model-visible MCP error."""
    raise RuntimeError("synthetic mcp failure: " + query)

@server.tool()
def slow(seconds: float) -> str:
    """Block long enough for a client timeout."""
    time.sleep(seconds)
    return "slept"

@server.tool()
def record(
    path: str,
    query: str,
    action: Literal["general", "latest"] = "general",
) -> str:
    """Record one remote invocation so tests can prove it was not called."""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(query + "\\n")
    return "recorded:" + query


@server.tool(name="reject-before-run")
def reject_before_run(query: str) -> str:
    """Reject like the Apify platform's pre-run input validation."""
    raise ToolError(
        "Failed to call Actor 'fixture/actor': input is invalid. "
        "Please ensure the input is correct and matches the Actor's input schema."
    )

@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def annotated(query: str) -> str:
    """Carry a server-supplied read-only hint that must not lower local policy."""
    return "annotated:" + query

if __name__ == "__main__":
    server.run()
'''


def _write_server(tmp_path: Path) -> Path:
    script = tmp_path / "sdk_mcp_server.py"
    script.write_text(SERVER_SCRIPT, encoding="utf-8")
    return script


def _stdio_spec(
    script: Path,
    *,
    name: str = "sdk-fixture",
    domain: str = "rag",
    effect: str = "read",
) -> mcp_tools.MCPServerSpec:
    return mcp_tools.MCPServerSpec(
        name=name,
        domain=domain,
        effect=effect,
        transport="stdio",
        params={"command": sys.executable, "args": [str(script)]},
    )


def _load(
    spec: mcp_tools.MCPServerSpec, existing: set[str] | None = None
) -> list[BusinessTool]:
    return asyncio.run(
        mcp_tools._load_server_tools(spec, set(existing or ()))
    )


def _call_model(tool_name: str, arguments: str, call_id: str = "mcp-call"):
    return ScriptedModel(
        [
            [function_call(tool_name, arguments, call_id=call_id)],
            [assistant_message("tool finished")],
        ],
        default_usage=TOKENS,
    )


def test_sdk_stdio_discovery_declaration_and_real_call(tmp_path):
    script = _write_server(tmp_path)
    tools = _load(_stdio_spec(script))

    echo = next(tool for tool in tools if tool.name == "echo")
    assert echo.domain == "rag"
    assert echo.effect == "read"
    assert echo.sdk_tool.params_json_schema["properties"]["query"]["type"] == "string"
    assert echo.sdk_tool._mcp_tool_binding[1] == "echo"

    model = _call_model("echo", '{"query":"brand"}')
    runtime = _runtime("internal_knowledge", tools=[echo], model=model)
    result = asyncio.run(runtime.run("task"))

    assert runtime.harness.results["mcp-call"].status == "succeeded"
    # FastMCP returns both text content and structuredContent; the existing
    # executor contract keeps both instead of silently dropping structured data.
    assert "echo:brand" in str(runtime.harness.results["mcp-call"].observation)
    assert "echo:brand" in result.report
    assert result.budget["tool_calls"] == 1


def test_streamable_http_discovery_and_real_call():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("sdk-http-fixture")

    @server.tool()
    def echo(query: str) -> str:
        return "http:" + query

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    config = uvicorn.Config(
        server.streamable_http_app(),
        host="127.0.0.1",
        port=port,
        log_level="error",
    )
    uv_server = uvicorn.Server(config)
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()
    try:
        _wait_for_port(port)
        spec = mcp_tools.MCPServerSpec(
            name="http-fixture",
            domain="rag",
            effect="read",
            transport="streamable_http",
            params={"url": f"http://127.0.0.1:{port}/mcp"},
        )
        tools = _load(spec)
        assert [tool.name for tool in tools] == ["echo"]

        model = _call_model("echo", '{"query":"brand"}')
        runtime = _runtime("internal_knowledge", tools=tools, model=model)
        result = asyncio.run(runtime.run("task"))

        assert runtime.harness.results["mcp-call"].status == "succeeded"
        assert "http:brand" in result.report
    finally:
        uv_server.should_exit = True
        thread.join(timeout=5)


def _wait_for_port(port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError("Streamable HTTP test server did not start")


def test_name_conflicts_and_trusted_domain_effect(tmp_path):
    script = _write_server(tmp_path)
    tools = _load(_stdio_spec(script), {"echo"})
    names = {tool.name for tool in tools}

    assert "sdk-fixture__echo" in names
    assert "echo" not in names
    annotated = next(tool for tool in tools if tool.name == "annotated")
    # The server advertises readOnlyHint, but the local spec says protected.
    protected = _load(_stdio_spec(script, effect="protected"))
    protected_annotated = next(
        tool for tool in protected if tool.name == "annotated"
    )
    assert annotated.effect == "read"
    assert protected_annotated.effect == "protected"
    assert protected_annotated.sdk_tool.mcp_annotations is not None


def test_allocate_tool_name_keeps_duplicates_visible():
    taken = {"lookup", "dbhub__lookup"}
    assert allocate_tool_name("dbhub", "lookup", taken) == "dbhub__lookup_2"


def test_invalid_mcp_arguments_never_reach_the_remote_server(
    tmp_path, monkeypatch, memory_ledger
):
    from types import SimpleNamespace

    from open_deep_research.llm import generation
    from open_deep_research.runtime import sdk_runtime, tool_executor

    script = _write_server(tmp_path)
    tools = _load(_stdio_spec(script, effect="protected"))
    record = next(tool for tool in tools if tool.name == "record")
    counter = tmp_path / "calls.txt"
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

    async def begin_model_call(*_args):
        return None

    monkeypatch.setattr(tool_executor, "start_operation", start_operation)
    monkeypatch.setattr(tool_executor, "finish_operation", finish_operation)
    monkeypatch.setattr(tool_executor, "commit_receipt", commit)
    monkeypatch.setattr(StageContext, "observe", observe)
    monkeypatch.setattr(generation, "begin_model_call", begin_model_call)
    monkeypatch.setattr(
        sdk_runtime, "settings", lambda: SimpleNamespace(session_path=None)
    )

    model = ScriptedModel(
        [
            [
                function_call(
                    "record",
                    '{"path":"' + counter.as_posix() + '","query":"q","action":"综合"}',
                    call_id="enum",
                ),
                function_call(
                    "record",
                    '{"path":"' + counter.as_posix() + '"}',
                    call_id="missing",
                ),
            ],
            [
                function_call(
                    "record",
                    '{"path":"'
                    + counter.as_posix()
                    + '","query":"ok","action":"general"}',
                    call_id="valid",
                )
            ],
            [assistant_message("tool finished")],
        ],
        default_usage=TOKENS,
    )
    runtime = _runtime("internal_knowledge", tools=[record], model=model)
    scope = StageContext(
        "fixture-run", "fixture-stage", "internal_knowledge_agent", 0, runtime.config, {}
    )
    token = stage_context.set(scope)
    try:
        asyncio.run(runtime.run("task"))
    finally:
        stage_context.reset(token)

    enum_result = runtime.harness.results["enum"]
    missing_result = runtime.harness.results["missing"]
    assert enum_result.status == "failed"
    assert "action" in str(enum_result.observation)
    assert "general" in str(enum_result.observation)
    assert enum_result.operation_id is None
    assert missing_result.status == "failed"
    assert "query" in str(missing_result.observation)
    assert "required" in str(missing_result.observation)
    assert missing_result.operation_id is None
    # The corrected call runs after the model receives the validation error.
    assert runtime.harness.results["valid"].status == "succeeded"
    assert runtime.harness.results["valid"].operation_id is not None
    assert counter.read_text(encoding="utf-8").splitlines() == ["ok"]
    assert len(starts) == 1
    assert finishes[-1][-1] == "returned"


def test_confirmed_apify_pre_run_rejection_is_not_executed(tmp_path):
    from open_deep_research.mcp.tool_wrapper import _call_mcp_tool
    from open_deep_research.tools import ToolNotExecuted

    script = _write_server(tmp_path)
    params = {"command": sys.executable, "args": [str(script)]}

    def factory():
        return MCPServerStdio(
            params,
            name="reject-fixture",
            cache_tools_list=False,
            client_session_timeout_seconds=10,
        )

    async def call():
        return await _call_mcp_tool(
            "apify",
            factory,
            "reject-before-run",
            {"query": "q"},
            apify_actor=True,
        )

    with pytest.raises(ToolNotExecuted, match="matches the Actor's input schema"):
        asyncio.run(call())


def test_apify_tool_policy_keeps_only_actor_runs_protected():
    from open_deep_research.mcp.tools import _apify_tool_policy

    assert _apify_tool_policy(
        True, "zen-studio--douyin-search-scraper", "read"
    ) == ("protected", True, False)
    assert _apify_tool_policy(True, "call-actor", "read") == (
        "protected",
        True,
        False,
    )
    assert _apify_tool_policy(True, "get-actor-run", "protected") == (
        "read",
        False,
        True,
    )
    assert _apify_tool_policy(True, "abort-actor-run", "read") == (
        "protected",
        False,
        False,
    )
    assert _apify_tool_policy(False, "echo", "read") == ("read", False, False)


def test_nested_mcp_validation_message_names_the_field():
    from jsonschema.validators import validator_for

    from open_deep_research.mcp.tool_wrapper import _validation_message

    schema = {
        "type": "object",
        "properties": {
            "options": {
                "type": "object",
                "properties": {
                    "sort": {"type": "string", "enum": ["general", "latest"]}
                },
                "required": ["sort"],
            }
        },
        "required": ["options"],
    }
    validator = validator_for(schema)(schema)
    error = next(iter(validator.iter_errors({"options": {"sort": "综合"}})))
    message = _validation_message("fixture", error)

    assert "options.sort" in message
    assert "not one of" in message


def test_mcp_error_and_disconnect_are_recorded_as_failed(tmp_path):
    script = _write_server(tmp_path)
    tools = _load(_stdio_spec(script))
    fail = next(tool for tool in tools if tool.name == "fail")

    runtime = _runtime(
        "internal_knowledge",
        tools=[fail],
        model=_call_model("fail", '{"query":"q"}', call_id="failed"),
    )
    asyncio.run(runtime.run("task"))
    assert runtime.harness.results["failed"].status == "failed"
    assert "synthetic mcp failure" in str(
        runtime.harness.results["failed"].observation
    )

    broken = mcp_tools.MCPServerSpec(
        name="broken",
        domain="rag",
        effect="read",
        transport="stdio",
        params={"command": str(tmp_path / "missing-command"), "args": []},
    )
    broken_tool = _declare_without_discovery(broken, "echo")
    runtime = _runtime(
        "internal_knowledge",
        tools=[broken_tool],
        model=_call_model("echo", '{"query":"q"}', call_id="disconnected"),
    )
    asyncio.run(runtime.run("task"))
    assert runtime.harness.results["disconnected"].status == "failed"


def _declare_without_discovery(
    spec: mcp_tools.MCPServerSpec, tool_name: str
) -> BusinessTool:
    mcp_tool = Tool(
        name=tool_name,
        description="fixture",
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    return build_mcp_tool(
        mcp_tool,
        spec.create(),
        server_name=spec.name,
        server_factory=spec.create,
        public_name=tool_name,
        domain=spec.domain,
        effect=spec.effect,
    )


def test_mcp_timeout_is_recorded_as_failed(tmp_path):
    script = _write_server(tmp_path)
    params = {"command": sys.executable, "args": [str(script)]}

    async def discover():
        server = MCPServerStdio(
            params,
            name="slow-fixture",
            cache_tools_list=False,
            client_session_timeout_seconds=2,
        )
        async with server:
            mcp_tool = next(
                tool for tool in await server.list_tools() if tool.name == "slow"
            )
            return build_mcp_tool(
                mcp_tool,
                server,
                server_name="slow-fixture",
                server_factory=lambda: MCPServerStdio(
                    params,
                    name="slow-fixture",
                    cache_tools_list=False,
                    client_session_timeout_seconds=2,
                ),
                public_name="slow",
                domain="rag",
                effect="read",
            )

    tool = asyncio.run(discover())
    runtime = _runtime(
        "internal_knowledge",
        tools=[tool],
        model=_call_model("slow", '{"seconds":10}', call_id="timeout"),
    )
    asyncio.run(runtime.run("task"))
    assert runtime.harness.results["timeout"].status == "failed"
    assert "Timed out" in str(runtime.harness.results["timeout"].observation)


def test_mcp_result_mapping_rejects_multimodal_and_marks_errors():
    text = TextContent(type="text", text="hello")
    assert _normalise_result("srv", "echo", CallToolResult(content=[text])) == [
        {"type": "text", "text": "hello"}
    ]
    structured = _normalise_result(
        "srv",
        "echo",
        CallToolResult(
            content=[text],
            structuredContent={"value": "hello"},
        ),
    )
    assert structured["structured_content"] == {"value": "hello"}

    with pytest.raises(MCPToolError, match="denied"):
        _normalise_result(
            "srv",
            "echo",
            CallToolResult(
                content=[TextContent(type="text", text="denied")],
                structuredContent={"status": "SUCCEEDED"},
                isError=True,
            ),
        )

    with pytest.raises(MCPToolError, match="multimodal"):
        _normalise_result(
            "srv",
            "echo",
            CallToolResult(
                content=[
                    ImageContent(
                        type="image", data="ZmFrZQ==", mimeType="image/png"
                    )
                ]
            ),
        )


def test_one_optional_failed_server_does_not_block_others(monkeypatch, tmp_path):
    script = _write_server(tmp_path)
    good = _stdio_spec(script, name="good", domain="document", effect="read")
    bad = mcp_tools.MCPServerSpec(
        name="bad",
        domain="feishu",
        effect="protected",
        transport="stdio",
        params={"command": str(tmp_path / "missing-command"), "args": []},
        optional=True,
    )
    monkeypatch.setattr(mcp_tools, "_build_markitdown_server", lambda _config: good)
    monkeypatch.setattr(mcp_tools, "_build_feishu_server", lambda _config: bad)

    existing: set[str] = set()
    loaded = asyncio.run(
        mcp_tools.load_mcp_tools(
            {
                "configurable": {
                    "markitdown_enabled": True,
                    "feishu_enabled": True,
                    "feishu_app_id": "fixture",
                }
            },
            existing,
        )
    )

    assert {tool.name for tool in loaded} == {
        "echo",
        "fail",
        "slow",
        "record",
        "reject-before-run",
        "annotated",
    }
    assert existing == {tool.name for tool in loaded}


def test_required_failed_server_fails_the_start(monkeypatch, tmp_path):
    bad = mcp_tools.MCPServerSpec(
        name="bad",
        domain="feishu",
        effect="protected",
        transport="stdio",
        params={"command": str(tmp_path / "missing-command"), "args": []},
    )
    monkeypatch.setattr(mcp_tools, "_build_feishu_server", lambda _config: bad)
    with pytest.raises(FileNotFoundError):
        asyncio.run(
            mcp_tools.load_mcp_tools(
                {
                    "configurable": {
                        "feishu_enabled": True,
                        "feishu_app_id": "fixture",
                    }
                },
                set(),
            )
        )


def test_committed_step_result_skips_the_mcp_call(monkeypatch):
    from open_deep_research.runtime import tool_executor

    called = []

    async def remote(_context, _arguments):
        called.append(True)
        return "unexpected"

    declaration = FunctionTool(
        name="remote_echo",
        description="fixture",
        params_json_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        on_invoke_tool=remote,
        strict_json_schema=False,
    )
    tool = BusinessTool(declaration, domain="rag", effect="read")

    class FakeDBOS:
        workflow_id = "fixture-workflow"
        executor_id = "fixture-executor"

        @classmethod
        async def run_step_async(cls, _options, _func):
            record = ObservedToolResult(
                sdk_run_id="fixture-run",
                tool_call_id="call-1",
                tool_name="remote_echo",
                arguments_json='{"query":"q"}',
                args={"query": "q"},
                model_turn=0,
                status="succeeded",
                observation="saved result",
            )
            return {
                "output": "saved result",
                "record": asdict(record),
                "budget": {},
            }

    monkeypatch.setattr(tool_executor, "DBOS", FakeDBOS)
    monkeypatch.setattr(tool_executor, "native_step_available", lambda: True)

    harness = SDKContextHarness(role="internal_knowledge", task_id="fixture")
    executor = ToolExecutor(
        "internal_knowledge",
        frozenset({"rag"}),
        Configuration(research_model="openai:fixture"),
        {"configurable": {}},
        _RunBudget(initial={}),
        {tool.name: tool},
    )
    harness.tool_executor = executor
    context = ToolContext(
        context=harness,
        tool_name="remote_echo",
        tool_call_id="call-1",
        tool_arguments='{"query":"q"}',
    )

    output = asyncio.run(
        executor.invoke(declaration, context, '{"query":"q"}')
    )

    assert output == "saved result"
    assert called == []
    assert harness.results["call-1"].status == "succeeded"


def test_mcp_catalog_changes_are_rejected_during_recovery(tmp_path):
    from open_deep_research.runtime import business_agent

    echo = next(
        tool for tool in _load(_stdio_spec(_write_server(tmp_path))) if tool.name == "echo"
    )
    catalog = business_agent._tool_catalog([echo])
    assert catalog[0]["effect"] == "read"

    with pytest.raises(ValueError, match="definitions changed"):
        business_agent._restore_role_tools(
            [{**catalog[0], "name": "renamed_echo"}], [echo]
        )

    changed_schema = replace(
        echo,
        sdk_tool=replace(echo.sdk_tool, params_json_schema={"type": "object"}),
    )
    with pytest.raises(ValueError, match="definitions changed"):
        business_agent._restore_role_tools(catalog, [changed_schema])


def test_duplicate_mcp_tool_call_id_calls_the_remote_once(tmp_path):
    script = _write_server(tmp_path)
    record = next(
        tool
        for tool in _load(_stdio_spec(script))
        if tool.name == "record"
    )
    counter = tmp_path / "duplicate-calls.txt"
    arguments = '{"path":"' + counter.as_posix() + '","query":"once"}'

    harness = SDKContextHarness(role="internal_knowledge", task_id="duplicate")
    executor = ToolExecutor(
        "internal_knowledge",
        frozenset({"rag"}),
        Configuration(research_model="openai:fixture"),
        {"configurable": {}},
        _RunBudget(initial={}),
        {record.name: record},
    )
    harness.tool_executor = executor

    def context():
        return ToolContext(
            context=harness,
            tool_name="record",
            tool_call_id="same-call",
            tool_arguments=arguments,
        )

    async def run():
        return await asyncio.gather(
            executor.invoke(record.sdk_tool, context(), arguments),
            executor.invoke(record.sdk_tool, context(), arguments),
        )

    first, second = asyncio.run(run())

    assert first == second
    assert counter.read_text(encoding="utf-8").splitlines() == ["once"]
    assert harness.results["same-call"].status == "succeeded"


def test_cancelled_protected_mcp_call_settles_unknown(
    monkeypatch, tmp_path, memory_ledger
):
    from open_deep_research.runtime import tool_executor

    script = _write_server(tmp_path)
    slow = next(
        tool
        for tool in _load(_stdio_spec(script, effect="protected"))
        if tool.name == "slow"
    )
    finishes: list[tuple] = []

    def start_operation(*_args):
        return None

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

    harness = SDKContextHarness(role="internal_knowledge", task_id="cancel")
    executor = ToolExecutor(
        "internal_knowledge",
        frozenset({"rag"}),
        Configuration(research_model="openai:fixture"),
        {"configurable": {}},
        _RunBudget(initial={}),
        {slow.name: slow},
    )
    harness.tool_executor = executor
    context = ToolContext(
        context=harness,
        tool_name="slow",
        tool_call_id="cancel-call",
        tool_arguments='{"seconds":5}',
    )
    scope = StageContext(
        "fixture-run",
        "fixture-stage-cancel",
        "fixture",
        0,
        {"configurable": {}},
        {},
    )

    async def run():
        token = stage_context.set(scope)
        task = asyncio.create_task(
            executor.invoke(slow.sdk_tool, context, '{"seconds":5}')
        )
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            stage_context.reset(token)

    asyncio.run(run())

    assert harness.results["cancel-call"].status == "cancelled"
    assert harness.results["cancel-call"].effect_status == "uncertain"
    assert finishes[-1][-1] == "unknown"


@pytest.mark.parametrize("actor_status", [None, "SUCCEEDED", "RUNNING", "FAILED"])
def test_read_and_protected_mcp_effects_settle_operation_receipts(
    monkeypatch, memory_ledger, actor_status
):
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

    def build(effect: str) -> BusinessTool:
        @function_tool(
            name_override="remote_echo",
            description_override="fixture",
            use_docstring_info=False,
            strict_mode=False,
            failure_error_function=None,
        )
        async def declared(context: ToolContext, query: str) -> str:
            async def business(value: str) -> str | dict:
                if actor_status is None:
                    return "ok:" + value
                receipt = {"runId": "actor-fixture", "status": actor_status}
                return _normalise_result(
                    "apify",
                    "call-actor",
                    CallToolResult(
                        content=[TextContent(type="text", text=json.dumps(receipt))],
                        structuredContent=receipt,
                    ),
                )

            return await context.context.tool_executor.execute(
                context, business, query
            )

        return register_tool(declared, domain="rag", effect=effect)

    async def run(effect: str):
        tool = build(effect)
        harness = SDKContextHarness(
            role="internal_knowledge", task_id=f"task-{effect}"
        )
        executor = ToolExecutor(
            "internal_knowledge",
            frozenset({"rag"}),
            Configuration(research_model="openai:fixture"),
            {"configurable": {}},
            _RunBudget(initial={}),
            {tool.name: tool},
        )
        harness.tool_executor = executor
        context = ToolContext(
            context=harness,
            tool_name="remote_echo",
            tool_call_id=f"call-{effect}",
            tool_arguments='{"query":"q"}',
        )
        scope = StageContext(
            "fixture-run",
            f"fixture-stage-{effect}",
            "fixture",
            0,
            {"configurable": {}},
            {},
        )
        token = stage_context.set(scope)
        try:
            return await executor.invoke(
                tool.sdk_tool, context, '{"query":"q"}'
            )
        finally:
            stage_context.reset(token)

    read_result = asyncio.run(run("read"))
    if actor_status is None:
        assert read_result == "ok:q"
    else:
        assert json.loads(read_result)["structured_content"] == {
            "runId": "actor-fixture",
            "status": actor_status,
        }
    assert starts == [] and finishes == []

    assert asyncio.run(run("protected")) == read_result
    assert len(starts) == 1
    assert finishes[-1][-1] == "returned"


def test_dedicated_apify_actor_reuses_the_original_run(
    tmp_path, monkeypatch, memory_ledger
):
    from open_deep_research.runtime import tool_executor

    actor_receipt = {
        "runId": "run1abc",
        "status": "SUCCEEDED",
        "defaultDatasetId": "ds-1",
    }
    results = [
        CallToolResult(
            content=[TextContent(type="text", text=json.dumps(actor_receipt))],
            structuredContent=actor_receipt,
        ),
        CallToolResult(
            content=[TextContent(type="text", text=json.dumps(actor_receipt))],
            structuredContent=actor_receipt,
        ),
    ]
    calls: list[str] = []

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def call_tool(self, name, _args):
            calls.append(name)
            return results.pop(0)

    def factory():
        return Connection()

    class DeclarationServer:
        name = "apify"
        tool_input_guardrails = None
        tool_output_guardrails = None

        @staticmethod
        def _get_failure_error_function(_failure):
            return None

        @staticmethod
        def _get_needs_approval_for_tool(_tool, _agent):
            return False

    tool = build_mcp_tool(
        Tool(
            name="zen-studio--rednote-search-scraper",
            description="fixture",
            inputSchema={
                "type": "object",
                "properties": {"keywords": {"type": "array"}},
                "required": ["keywords"],
            },
        ),
        DeclarationServer(),
        server_name="apify",
        server_factory=factory,
        public_name="zen-studio--rednote-search-scraper",
        domain="social_media",
        effect="protected",
        apify_actor=True,
    )
    starts: list[tuple] = []
    finishes: list[tuple] = []
    receipts: list[tuple] = []

    def start_operation(*_args):
        starts.append(True)
        # The second attempt recovers the receipt recorded by the first one.
        return "run1abc" if len(starts) == 2 else None

    def finish_operation(*args):
        finishes.append(args)

    def record_apify_run(*args):
        receipts.append(args)

    async def commit(write, *args):
        await asyncio.to_thread(write, *args)

    async def observe(_self, _kind, _payload, *, call_id=None):
        return None

    monkeypatch.setattr(tool_executor, "start_operation", start_operation)
    monkeypatch.setattr(tool_executor, "finish_operation", finish_operation)
    monkeypatch.setattr(tool_executor, "record_apify_run", record_apify_run)
    monkeypatch.setattr(tool_executor, "commit_receipt", commit)
    monkeypatch.setattr(StageContext, "observe", observe)

    harness = SDKContextHarness(role="public_signal", task_id="apify-dedicated")
    executor = ToolExecutor(
        "public_signal",
        frozenset({"social_media"}),
        Configuration(research_model="openai:fixture"),
        {"configurable": {}},
        _RunBudget(initial={}),
        {tool.name: tool},
    )
    harness.tool_executor = executor
    scope = StageContext(
        "fixture-run",
        "fixture-stage-apify-dedicated",
        "public_signal_agent",
        0,
        {"configurable": {}},
        {},
    )
    arguments = '{"keywords":["售后"],"sortType":"general"}'

    async def invoke(call_id):
        token = stage_context.set(scope)
        try:
            context = ToolContext(
                context=harness,
                tool_name=tool.name,
                tool_call_id=call_id,
                tool_arguments=arguments,
            )
            return await executor.invoke(tool.sdk_tool, context, arguments)
        finally:
            stage_context.reset(token)

    first = asyncio.run(invoke("call-1"))
    assert json.loads(first)["structured_content"]["runId"] == "run1abc"
    assert calls == ["zen-studio--rednote-search-scraper"]
    assert receipts and receipts[-1][-1] == "run1abc"
    assert finishes[0][-1] == "returned"

    second = asyncio.run(invoke("call-2"))
    assert json.loads(second)["structured_content"]["runId"] == "run1abc"
    # Recovery polls the original run; it never starts a second Actor run.
    assert calls == ["zen-studio--rednote-search-scraper", "get-actor-run"]
    assert finishes[1][-1] == "run1abc"
