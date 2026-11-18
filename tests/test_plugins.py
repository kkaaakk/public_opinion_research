# 验证插件发现、角色授权、Skill 按需加载及 Apify MCP 的统一执行链。
"""Plugin + Skills assembly contracts.

These tests use temporary plugin directories and real lightweight ``FastMCP``
servers, plus the same offline ScriptedModel runtime fixtures as the local-tool
tests. No paid model, Apify account or external service is called.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from agents import function_tool
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from dbos import DBOS, DBOSClient, SetWorkflowID
from test_sdk_runtime import TOKENS, _runtime

from open_deep_research.configuration import Configuration
from open_deep_research.mcp import tools as mcp_tools_module
from open_deep_research.mcp.tools import MCPServerSpec
from open_deep_research.public_opinion_agents import get_public_opinion_agent_spec
from open_deep_research.runtime import business_agent as business_agent_module
from open_deep_research.runtime import sdk_runtime
from open_deep_research.tools import BusinessTool, register_tool
from open_deep_research.tools.plugins import (
    LOAD_SKILL_TOOL,
    _server_identity,
    _skill_payload,
    build_skill_prompt,
    load_plugin_tools,
    load_role_plugins,
)
from open_deep_research.web.run_data import RunJSON
from open_deep_research.workflow.execution import StageContext, stage_context

ROOT = Path(__file__).resolve().parents[1]
SOCIAL_DOMAINS = frozenset({"core", "web_search", "social_media"})
CORE_TOOL_NAMES = {"web_search"}
RETIRED_SOCIAL_TOOLS = {
    "search_posts",
    "search_complaints",
    "fetch_thread",
    "fetch_comments",
    "fetch_author_profile",
    "get_trending_keywords",
    "get_public_opinion_snapshot",
}
APIFY_TOOL_NAMES = {
    "search-actors",
    "fetch-actor-details",
    "call-actor",
    "get-actor-run",
    "get-dataset-items",
}

MCP_SERVER = '''from mcp.server.fastmcp import FastMCP

server = FastMCP("plugin-mcp-fixture")


@server.tool()
def echo(query: str) -> str:
    """Return the query with a deterministic plugin marker."""
    return "plugin-echo:" + query


@server.tool()
def web_search(query: str) -> str:
    """Conflict with an existing local tool name."""
    return "remote:" + query


if __name__ == "__main__":
    server.run()
'''

GLOBAL_MCP_SERVER = '''from mcp.server.fastmcp import FastMCP

server = FastMCP("global-mcp-fixture")


@server.tool()
def echo(query: str) -> str:
    """Return the query with a deterministic global marker."""
    return "global-echo:" + query


if __name__ == "__main__":
    server.run()
'''

# 模拟官方 Apify MCP 的五个社媒工具；参数名以官方文档和实际 Schema 为准。
APIFY_MCP_SERVER = '''import json

from mcp.server.fastmcp import FastMCP

server = FastMCP("apify-fixture")

ITEMS = [
    {
        "url": "https://www.xiaohongshu.com/explore/n1",
        "published_at": "2026-06-02T09:00:00+08:00",
        "author": "用户A",
        "content": "售后推诿，退款失败，已投诉",
        "likes": 120,
        "comments": 8,
    }
]


@server.tool(name="search-actors")
def search_actors(keywords: str = "", limit: int = 5) -> str:
    """Search Apify Store Actors."""
    return json.dumps(
        [{"id": "fixture/xhs-notes", "title": "XHS Notes", "pricing": "per 1000 results"}],
        ensure_ascii=False,
    )


@server.tool(name="fetch-actor-details")
def fetch_actor_details(actor: str) -> str:
    """Return a fixture Actor input schema and pricing."""
    return json.dumps(
        {
            "id": actor,
            "inputSchema": {"keywords": "array", "maxItems": "number"},
            "pricing": "per 1000 results",
        },
        ensure_ascii=False,
    )


@server.tool(name="call-actor")
def call_actor(
    actor: str,
    input: dict | None = None,
    callOptions: dict | None = None,
    waitSecs: int = 30,
) -> str:
    """Start a fixture run and return its status and dataset id."""
    return json.dumps(
        {
            "runId": "run-1",
            "status": "SUCCEEDED",
            "defaultDatasetId": "ds-1",
            "itemCount": len(ITEMS),
        },
        ensure_ascii=False,
    )


@server.tool(name="get-actor-run")
def get_actor_run(runId: str) -> str:
    """Return a terminal fixture run."""
    return json.dumps(
        {"runId": runId, "status": "SUCCEEDED", "defaultDatasetId": "ds-1"},
        ensure_ascii=False,
    )


@server.tool(name="get-dataset-items")
def get_dataset_items(datasetId: str, limit: int = 20) -> str:
    """Return fixture dataset items."""
    return json.dumps(
        {"datasetId": datasetId, "items": ITEMS[:limit]}, ensure_ascii=False
    )


if __name__ == "__main__":
    server.run()
'''

FAILING_APIFY_SERVER = '''from mcp.server.fastmcp import FastMCP

server = FastMCP("apify-failure-fixture")


@server.tool(name="call-actor")
def call_actor(actor: str, input: dict | None = None, callOptions: dict | None = None) -> str:
    """Fail like an Actor run that never produced a dataset."""
    raise RuntimeError("synthetic actor failure: " + actor)


if __name__ == "__main__":
    server.run()
'''

EMPTY_DATASET_SERVER = '''import json

from mcp.server.fastmcp import FastMCP

server = FastMCP("apify-empty-fixture")


@server.tool(name="get-dataset-items")
def get_dataset_items(datasetId: str, limit: int = 20) -> str:
    """Return a real empty dataset result."""
    return json.dumps({"datasetId": datasetId, "items": []})


if __name__ == "__main__":
    server.run()
'''

# 记录每次 Actor 启动，用于证明恢复时不会再次产生远端调用。
APIFY_RECORDING_SERVER = '''import json

from mcp.server.fastmcp import FastMCP

RECORD_PATH = __RECORD_PATH__

server = FastMCP("apify-recording-fixture")


@server.tool(name="call-actor")
def call_actor(
    actor: str,
    input: dict | None = None,
    callOptions: dict | None = None,
    waitSecs: int = 30,
) -> str:
    """Record one Actor start and return a terminal fixture run."""
    with open(RECORD_PATH, "a", encoding="utf-8") as handle:
        handle.write(actor + "\\n")
    return json.dumps(
        {"runId": "run-1", "status": "SUCCEEDED", "defaultDatasetId": "ds-1"},
        ensure_ascii=False,
    )


@server.tool(name="get-dataset-items")
def get_dataset_items(datasetId: str, limit: int = 20) -> str:
    """Return one fixture item from the recorded run."""
    return json.dumps(
        {
            "datasetId": datasetId,
            "items": [{"url": "https://www.xiaohongshu.com/explore/n1"}],
        },
        ensure_ascii=False,
    )


if __name__ == "__main__":
    server.run()
'''


# 构造插件根目录与可覆盖的配置。
def _config(tmp_path: Path, *, enabled: tuple[str, ...] = ("fixture-plugin",), **extra):
    return {
        "configurable": {
            "enabled_plugins": list(enabled),
            "plugins_dir": str(tmp_path / "plugins"),
            **extra,
        }
    }


# 构造启用仓库内置插件的配置。
def _repository_config(**extra):
    return {
        "configurable": {
            "enabled_plugins": ["social-research"],
            "plugins_dir": str(ROOT / "plugins"),
            **extra,
        }
    }


# 写入一个测试插件清单及其 Skills/MCP 声明。
def _write_plugin(
    tmp_path: Path,
    *,
    plugin_id: str = "fixture-plugin",
    roles: tuple[str, ...] = ("public_signal",),
    skills: tuple[tuple[str, str, str], ...] = (),
    tools: tuple[str, ...] = (),
    version: str = "1.0.0",
    servers: list[dict] | None = None,
    mcp: bool = False,
) -> Path:
    plugin_dir = tmp_path / "plugins" / plugin_id
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": plugin_id,
        "name": plugin_id.replace("-", " ").title(),
        "version": version,
        "roles": list(roles),
        "tools": list(tools),
        "skills": [name for name, _title, _description in skills],
    }
    if mcp:
        manifest["mcp"] = "mcp.json"
        (plugin_dir / "mcp.json").write_text(
            json.dumps({"servers": servers or []}), encoding="utf-8"
        )
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    for name, title, description in skills:
        skill_dir = plugin_dir / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            f"title: {title}\n"
            f"description: {description}\n"
            "---\n\n"
            f"# {title}\n\n"
            "Use the platform guidance and cite retrieved evidence.\n",
            encoding="utf-8",
        )
    return plugin_dir


# 写入可被 SDK 原生 MCP 客户端启动的 stdio 测试服务。
def _write_mcp_server(tmp_path: Path, source: str = MCP_SERVER, name: str = "plugin") -> Path:
    script = tmp_path / f"{name}_mcp_server.py"
    script.write_text(source, encoding="utf-8")
    return script


# 返回插件声明的 stdio 服务条目。
def _stdio_server(
    script: Path, *, domain: str = "social_media", effect: str = "read", name: str = "fixture"
) -> dict:
    return {
        "name": name,
        "transport": "stdio",
        "domain": domain,
        "effect": effect,
        "params": {"command": sys.executable, "args": [str(script)]},
    }


# 返回插件声明的 Streamable HTTP 服务条目。
def _http_server(url: str, *, headers: dict | None = None) -> dict:
    params: dict = {"url": url}
    if headers:
        params["headers"] = headers
    return {
        "name": "fixture",
        "transport": "streamable_http",
        "domain": "social_media",
        "effect": "read",
        "params": params,
    }


# 重写插件 MCP 声明，用于比较配置变化后的目录快照。
def _rewrite_servers(tmp_path: Path, servers: list[dict]) -> None:
    (tmp_path / "plugins" / "fixture-plugin" / "mcp.json").write_text(
        json.dumps({"servers": servers}), encoding="utf-8"
    )


# 用受控替身替代服务发现，隔离连接错误并记录装配次数。
def _fake_mcp_discovery(monkeypatch, calls: list[str] | None = None, fail: set[str] | None = None):
    async def fake(spec, existing, *, allowed_tool_names=None):
        if calls is not None:
            calls.append(spec.name)
        if fail and spec.name in fail:
            raise RuntimeError("synthetic discovery failure")
        public_name = "echo" if "echo" not in existing else f"{spec.name}__echo"
        existing.add(public_name)
        return [_fake_tool(public_name, spec.domain, spec.effect)]

    monkeypatch.setattr(mcp_tools_module, "_load_server_tools", fake)


# 构造受控替身 MCP 工具，声明与领域信息保持真实装配路径一致。
def _fake_tool(name: str, domain: str, effect: str) -> BusinessTool:
    @function_tool(
        name_override=name,
        description_override="fixture MCP tool",
        use_docstring_info=False,
        strict_mode=False,
        failure_error_function=None,
    )
    async def echo(query: str) -> str:
        return f"fake:{query}"

    return register_tool(echo, domain=domain, effect=effect)


# 加载临时插件贡献的工具。
def _load_fixture_tools(tmp_path: Path, config: dict | None = None):
    return asyncio.run(
        load_plugin_tools(
            config or _config(tmp_path),
            role="public_signal",
            allowed_domains=SOCIAL_DOMAINS,
            local_tool_names=set(CORE_TOOL_NAMES),
            existing_tool_names=set(CORE_TOOL_NAMES),
        )
    )


# 验证插件清单、Skills 与声明工具被正确读取。
def test_plugin_discovery_reads_manifest_and_skills(tmp_path):
    _write_plugin(
        tmp_path,
        skills=[("xiaohongshu", "小红书舆情调研", "Note and comment research guidance.")],
        tools=("web_search",),
    )

    role_plugins = load_role_plugins(
        _config(tmp_path), "public_signal", SOCIAL_DOMAINS
    )

    assert [plugin.plugin_id for plugin in role_plugins.plugins] == ["fixture-plugin"]
    plugin = role_plugins.plugins[0]
    assert plugin.version == "1.0.0"
    assert plugin.tools == ("web_search",)
    assert plugin.servers == ()
    assert [skill.name for skill in role_plugins.skills] == ["xiaohongshu"]
    skill = role_plugins.skills[0]
    assert skill.title == "小红书舆情调研"
    assert skill.description == "Note and comment research guidance."
    assert skill.body.startswith("# 小红书舆情调研")
    assert len(role_plugins.fingerprint) == 12

    prompt = build_skill_prompt(_config(tmp_path), "public_signal", SOCIAL_DOMAINS)
    assert LOAD_SKILL_TOOL in prompt
    assert "- xiaohongshu: 小红书舆情调研" in prompt


# 构造一个最小的外部检索声明，用于能力判断。
def _tool_declaration():
    @function_tool(
        name_override="web_search",
        description_override="fixture search",
        use_docstring_info=False,
        strict_mode=False,
        failure_error_function=None,
    )
    async def search(query: str) -> str:
        return query

    return search


# 验证 load_skill 不计为外部研究能力，生产者仍必须装配真实检索工具。
def test_load_skill_alone_does_not_satisfy_research_capability(tmp_path):
    _write_plugin(tmp_path, skills=[("xiaohongshu", "XHS", "Guidance.")])
    tools, _plugins = _load_fixture_tools(tmp_path)

    assert not business_agent_module.has_external_research_tool(tools)
    assert business_agent_module.has_external_research_tool(
        [*tools, BusinessTool(_tool_declaration(), domain="web_search", effect="read")]
    )


# 验证未启用或目录缺失时不会装配任何插件能力，也不影响原有流程。
def test_disabled_or_missing_plugins_contribute_nothing(tmp_path):
    _write_plugin(tmp_path, skills=[("xiaohongshu", "XHS", "Guidance.")])

    disabled = _config(tmp_path, enabled=())
    tools, plugins = _load_fixture_tools(tmp_path, disabled)
    assert tools == []
    assert plugins.plugins == ()
    assert build_skill_prompt(disabled, "public_signal", SOCIAL_DOMAINS) == ""

    missing = _config(tmp_path)
    missing["configurable"]["plugins_dir"] = str(tmp_path / "missing")
    assert load_role_plugins(missing, "public_signal", SOCIAL_DOMAINS).plugins == ()

    unknown = _config(tmp_path, enabled=("not-installed",))
    assert load_role_plugins(unknown, "public_signal", SOCIAL_DOMAINS).plugins == ()


# 验证不同角色只能获得清单中授权的插件。
def test_role_authorization_limits_plugin_assembly(tmp_path):
    _write_plugin(tmp_path, skills=[("xiaohongshu", "XHS", "Guidance.")])

    assert load_role_plugins(
        _config(tmp_path), "public_signal", SOCIAL_DOMAINS
    ).plugins
    assert (
        load_role_plugins(
            _config(tmp_path), "risk_assessment", SOCIAL_DOMAINS
        ).plugins
        == ()
    )
    assert build_skill_prompt(
        _config(tmp_path), "risk_assessment", SOCIAL_DOMAINS
    ) == ""


# 验证仓库内置 social-research 只声明 Skills 和 Apify MCP，不再引用本地社媒工具。
def test_repository_social_plugin_declares_only_apify_mcp(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "fixture-token")

    role_plugins = load_role_plugins(
        _repository_config(), "public_signal", SOCIAL_DOMAINS
    )

    plugin = role_plugins.plugins[0]
    assert plugin.plugin_id == "social-research"
    assert plugin.version == "2.0.0"
    assert plugin.tools == ()
    assert [skill.name for skill in role_plugins.skills] == [
        "xiaohongshu",
        "douyin",
        "weibo",
    ]
    assert len(plugin.servers) == 1
    server = plugin.servers[0]
    assert server.name == "apify"
    assert server.transport == "streamable_http"
    assert server.domain == "social_media"
    assert server.effect == "protected"
    assert server.params["headers"] == {"Authorization": "Bearer fixture-token"}
    parsed = urlsplit(server.params["url"])
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://mcp.apify.com"
    assert set(parse_qs(parsed.query)["tools"][0].split(",")) == APIFY_TOOL_NAMES
    for skill in role_plugins.skills:
        assert skill.plugin_id == "social-research"
        assert "call-actor" in skill.body
        assert "get-dataset-items" in skill.body
        for retired in RETIRED_SOCIAL_TOOLS:
            assert f"`{retired}`" not in skill.body


# 验证没有 Apify Token 时保留 Skills，跳过 MCP 服务并明确记录配置缺失。
def test_repository_plugin_without_token_skips_server_and_reports_gap(
    monkeypatch, caplog
):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)

    role_plugins = load_role_plugins(
        _repository_config(), "public_signal", SOCIAL_DOMAINS
    )

    assert [skill.name for skill in role_plugins.skills] == [
        "xiaohongshu",
        "douyin",
        "weibo",
    ]
    assert role_plugins.plugins[0].servers == ()
    assert "APIFY_TOKEN" in caplog.text
    for skill in role_plugins.skills:
        assert "APIFY_TOKEN" in skill.body
        assert "Apify MCP" in skill.body


# 验证 Agent 初始看到简介，并能通过 ToolExecutor 加载对应 Markdown。
def test_agent_sees_skill_summary_and_loads_it_through_executor(tmp_path):
    _write_plugin(tmp_path, skills=[("xiaohongshu", "XHS Skill", "Note research guidance.")])
    config = _config(tmp_path)

    tools = asyncio.run(
        business_agent_module._business_agent_tools(config, "public_signal")
    )
    load_skill = next(
        tool for tool in tools if isinstance(tool, BusinessTool) and tool.name == LOAD_SKILL_TOOL
    )
    assert load_skill.domain == "core"
    assert load_skill.effect == "read"
    assert "xiaohongshu (XHS Skill)" in load_skill.description
    skill_prompt = build_skill_prompt(
        Configuration.from_runnable_config(config), "public_signal", SOCIAL_DOMAINS
    )
    assert "xiaohongshu" in skill_prompt
    spec = get_public_opinion_agent_spec("public_signal")
    rendered = spec.format_system_prompt(
        retrieval_tool_prompt="tools",
        mcp_prompt="",
        date="2026-11-18",
        organization_context="org",
        skill_prompt=skill_prompt,
    )
    assert "<Available Skills>" in rendered
    assert "XHS Skill" in rendered
    assert "<Available Skills>" not in spec.format_system_prompt(
        retrieval_tool_prompt="tools",
        mcp_prompt="",
        date="2026-11-18",
        organization_context="org",
    )

    model = ScriptedModel(
        [
            [function_call(LOAD_SKILL_TOOL, '{"skill_name":"xiaohongshu"}', call_id="skill")],
            [assistant_message("summary")],
        ],
        default_usage=TOKENS,
    )
    runtime = _runtime("public_signal", tools=tools, model=model)
    result = asyncio.run(runtime.run("task"))

    record = runtime.harness.results["skill"]
    assert record.status == "succeeded"
    assert "Use the platform guidance" in str(record.observation)
    assert "XHS Skill" in result.report
    assert result.budget["tool_calls"] == 1
    # load_skill is a read-only tool: it never opens a protected operation receipt.
    assert record.operation_id is None


# 验证非法名称与路径形式的 Skill 请求被拒绝且不会泄露文件。
def test_load_skill_rejects_unknown_and_path_names(tmp_path):
    _write_plugin(tmp_path, skills=[("xiaohongshu", "XHS", "Guidance.")])
    config = _config(tmp_path)
    tools = asyncio.run(
        business_agent_module._business_agent_tools(config, "public_signal")
    )
    model = ScriptedModel(
        [
            [function_call(LOAD_SKILL_TOOL, '{"skill_name":"unknown"}', call_id="unknown")],
            [
                function_call(
                    LOAD_SKILL_TOOL,
                    '{"skill_name":"../plugin.json"}',
                    call_id="traversal",
                )
            ],
            [assistant_message("summary")],
        ],
        default_usage=TOKENS,
    )
    runtime = _runtime("public_signal", tools=tools, model=model)
    asyncio.run(runtime.run("task"))

    assert runtime.harness.results["unknown"].status == "failed"
    assert "not enabled or authorized" in str(runtime.harness.results["unknown"].observation)
    assert runtime.harness.results["traversal"].status == "failed"
    assert "lowercase" in str(runtime.harness.results["traversal"].observation)

    skills = {"xiaohongshu": load_role_plugins(config, "public_signal", SOCIAL_DOMAINS).skills[0]}
    with pytest.raises(ValueError, match="lowercase"):
        _skill_payload(skills, "public_signal", "/etc/passwd")


# 验证 Public Signal 保留 Tavily、移除七个本地社媒工具和旧控制工具。
def test_public_signal_catalog_keeps_tavily_and_drops_retired_tools(monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)

    tools = asyncio.run(
        business_agent_module._business_agent_tools(
            _repository_config(search_api="tavily", rag_enabled=False),
            "public_signal",
        )
    )
    names = {tool.name for tool in tools}

    assert "web_search" in names
    assert LOAD_SKILL_TOOL in names
    assert RETIRED_SOCIAL_TOOLS.isdisjoint(names)
    assert APIFY_TOOL_NAMES.isdisjoint(names)
    assert {"think_tool", "ResearchComplete"}.isdisjoint(names)


# 验证 Agent 按 Skill 指引依次调用 Actor 发现、详情、执行与数据集工具。
def test_agent_runs_apify_actor_chain_through_tool_executor(
    tmp_path, monkeypatch
):
    from open_deep_research.runtime import tool_executor

    script = _write_mcp_server(tmp_path, APIFY_MCP_SERVER, "apify")
    _write_plugin(
        tmp_path,
        skills=[("xiaohongshu", "小红书舆情调研", "XHS guidance.")],
        servers=[_stdio_server(script, effect="protected", name="apify")],
        mcp=True,
    )
    tools, plugins = _load_fixture_tools(tmp_path)

    assert APIFY_TOOL_NAMES <= {tool.name for tool in tools}
    assert all(
        tool.domain == "social_media" and tool.effect == "protected"
        for tool in tools
        if tool.name in APIFY_TOOL_NAMES
    )
    assert plugins.assembled_servers == frozenset({"apify"})

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
    # This test isolates plugins and MCP; model and execution storage are out of scope.
    async def begin_model_call():
        return None

    monkeypatch.setattr(sdk_runtime, "begin_model_call", begin_model_call)
    monkeypatch.setattr(
        sdk_runtime, "settings", lambda: SimpleNamespace(session_path=None)
    )

    model = ScriptedModel(
        [
            [function_call(LOAD_SKILL_TOOL, '{"skill_name":"xiaohongshu"}', call_id="skill")],
            [
                function_call(
                    "search-actors",
                    '{"keywords":"xiaohongshu notes","limit":5}',
                    call_id="actors",
                )
            ],
            [
                function_call(
                    "fetch-actor-details",
                    '{"actor":"fixture/xhs-notes"}',
                    call_id="details",
                )
            ],
            [
                function_call(
                    "call-actor",
                    json.dumps(
                        {
                            "actor": "fixture/xhs-notes",
                            "input": {"keywords": ["售后"], "maxItems": 20},
                            "callOptions": {"maxTotalChargeUsd": 0.2, "maxItems": 20},
                            "waitSecs": 45,
                        },
                        ensure_ascii=False,
                    ),
                    call_id="run",
                )
            ],
            [
                function_call(
                    "get-dataset-items",
                    '{"datasetId":"ds-1","limit":20}',
                    call_id="items",
                )
            ],
            [assistant_message("summary")],
        ],
        default_usage=TOKENS,
    )
    runtime = _runtime("public_signal", tools=tools, model=model)
    scope = StageContext(
        "fixture-run",
        "fixture-stage-apify",
        "public_signal_agent",
        0,
        runtime.config,
        {},
    )
    token = stage_context.set(scope)
    try:
        result = asyncio.run(runtime.run("task"))
    finally:
        stage_context.reset(token)

    assert runtime.harness.results["skill"].status == "succeeded"
    for call_id in ("actors", "details", "run", "items"):
        assert runtime.harness.results[call_id].status == "succeeded"
    assert "fixture/xhs-notes" in str(runtime.harness.results["actors"].observation)
    assert "per 1000 results" in str(runtime.harness.results["details"].observation)
    assert "run-1" in str(runtime.harness.results["run"].observation)
    assert "https://www.xiaohongshu.com/explore/n1" in str(
        runtime.harness.results["items"].observation
    )
    assert result.budget["tool_calls"] == 5
    # Each Apify MCP call is protected; load_skill stays a read-only tool.
    for call_id in ("actors", "details", "run", "items"):
        assert runtime.harness.results[call_id].operation_id is not None
    assert runtime.harness.results["skill"].operation_id is None
    assert len(starts) == 4
    assert [entry[-1] for entry in finishes] == ["returned"] * 4


# 验证 Actor 失败会作为工具失败报告，不会伪装成成功证据。
def test_failed_actor_is_reported_as_tool_failure(tmp_path):
    script = _write_mcp_server(tmp_path, FAILING_APIFY_SERVER, "apify_fail")
    _write_plugin(
        tmp_path,
        servers=[_stdio_server(script, effect="protected", name="apify")],
        mcp=True,
    )
    tools, _plugins = _load_fixture_tools(tmp_path)

    model = ScriptedModel(
        [
            [
                function_call(
                    "call-actor",
                    '{"actor":"fixture/broken"}',
                    call_id="run",
                )
            ],
            [assistant_message("summary")],
        ],
        default_usage=TOKENS,
    )
    runtime = _runtime("public_signal", tools=tools, model=model)
    asyncio.run(runtime.run("task"))

    record = runtime.harness.results["run"]
    assert record.status == "failed"
    assert "synthetic actor failure" in str(record.observation)


# 验证空数据集只返回空结果，不会制造帖子或 URL。
def test_empty_dataset_returns_no_synthetic_evidence(tmp_path):
    script = _write_mcp_server(tmp_path, EMPTY_DATASET_SERVER, "apify_empty")
    _write_plugin(
        tmp_path,
        servers=[_stdio_server(script, effect="protected", name="apify")],
        mcp=True,
    )
    tools, _plugins = _load_fixture_tools(tmp_path)

    model = ScriptedModel(
        [
            [
                function_call(
                    "get-dataset-items",
                    '{"datasetId":"ds-empty"}',
                    call_id="empty",
                )
            ],
            [assistant_message("summary")],
        ],
        default_usage=TOKENS,
    )
    runtime = _runtime("public_signal", tools=tools, model=model)
    asyncio.run(runtime.run("task"))

    record = runtime.harness.results["empty"]
    assert record.status == "succeeded"
    assert "ds-empty" in str(record.observation)
    assert "http" not in str(record.observation).lower()


# 验证插件声明的 MCP 工具经现有 ToolExecutor 执行并计入预算。
def test_plugin_mcp_tool_runs_through_existing_tool_executor(tmp_path):
    script = _write_mcp_server(tmp_path)
    _write_plugin(tmp_path, servers=[_stdio_server(script)], mcp=True)

    tools, plugins = _load_fixture_tools(tmp_path)
    assert [plugin.plugin_id for plugin in plugins.plugins] == ["fixture-plugin"]
    echo = next(tool for tool in tools if tool.name == "echo")
    assert isinstance(echo, BusinessTool)
    assert echo.domain == "social_media"
    assert echo.effect == "read"

    model = ScriptedModel(
        [
            [function_call("echo", '{"query":"brand"}', call_id="mcp")],
            [assistant_message("done")],
        ],
        default_usage=TOKENS,
    )
    runtime = _runtime("public_signal", tools=[echo], model=model)
    result = asyncio.run(runtime.run("task"))

    record = runtime.harness.results["mcp"]
    assert record.status == "succeeded"
    assert "plugin-echo:brand" in str(record.observation)
    assert result.budget["tool_calls"] == 1


# 验证全局与插件 MCP 各自只装配一次，并都经 ToolExecutor 执行。
def test_global_and_plugin_mcp_servers_load_once_and_execute(tmp_path, monkeypatch):
    global_script = _write_mcp_server(tmp_path, GLOBAL_MCP_SERVER, "global")
    plugin_script = _write_mcp_server(tmp_path)
    _write_plugin(
        tmp_path,
        skills=[("xiaohongshu", "XHS", "Guidance.")],
        servers=[_stdio_server(plugin_script)],
        mcp=True,
    )
    global_spec = MCPServerSpec(
        name="global-mcp",
        domain="social_media",
        effect="read",
        transport="stdio",
        params={"command": sys.executable, "args": [str(global_script)]},
    )
    monkeypatch.setattr(
        mcp_tools_module, "_build_markitdown_server", lambda _config: global_spec
    )
    calls: list[str] = []
    original = mcp_tools_module._load_server_tools

    async def record(spec, existing, *, allowed_tool_names=None):
        calls.append(spec.name)
        return await original(spec, existing, allowed_tool_names=allowed_tool_names)

    monkeypatch.setattr(mcp_tools_module, "_load_server_tools", record)

    config = _config(
        tmp_path,
        markitdown_enabled=True,
        search_api="tavily",
        rag_enabled=False,
    )
    tools = asyncio.run(
        business_agent_module._business_agent_tools(config, "public_signal")
    )
    names = [tool.name for tool in tools]

    assert calls == ["global-mcp", "fixture"]
    assert names.count("echo") == 1
    assert names.count("fixture__echo") == 1
    # The plugin's conflicting tool is namespaced instead of replacing web_search.
    assert names.count("web_search") == 1
    assert names.count("fixture__web_search") == 1

    model = ScriptedModel(
        [
            [
                function_call("echo", '{"query":"g"}', call_id="global"),
                function_call("fixture__echo", '{"query":"p"}', call_id="plugin"),
            ],
            [assistant_message("done")],
        ],
        default_usage=TOKENS,
    )
    runtime = _runtime("public_signal", tools=tools, model=model)
    result = asyncio.run(runtime.run("task"))

    assert "global-echo:g" in str(runtime.harness.results["global"].observation)
    assert "plugin-echo:p" in str(runtime.harness.results["plugin"].observation)
    assert result.budget["tool_calls"] == 2


# 验证 MCP 地址变化会通过现有 por.role_tools 目录校验失败关闭。
def test_mcp_url_change_fails_closed_but_unchanged_config_passes(
    tmp_path, monkeypatch
):
    _write_plugin(
        tmp_path,
        skills=[("xiaohongshu", "XHS", "Guidance.")],
        servers=[_http_server("http://127.0.0.1:9000/mcp")],
        mcp=True,
    )
    _fake_mcp_discovery(monkeypatch)

    def load():
        return _load_fixture_tools(tmp_path)[0]

    tools = load()
    catalog = business_agent_module._tool_catalog(tools)
    reloaded = load()
    assert business_agent_module._restore_role_tools(catalog, reloaded) is reloaded

    _rewrite_servers(tmp_path, [_http_server("http://127.0.0.1:9100/mcp")])
    changed = load()
    assert _load_skill_tool(catalog)["description"] != _load_skill_tool(
        business_agent_module._tool_catalog(changed)
    )["description"]
    with pytest.raises(ValueError, match="definitions changed"):
        business_agent_module._restore_role_tools(catalog, changed)


# 验证工具集合变化（例如 MCP tools 过滤参数改变发现结果）会失败关闭。
def test_mcp_tool_set_change_fails_closed_during_recovery():
    tool = _fake_tool("search-actors", "social_media", "protected")
    extra = _fake_tool("get-dataset-items", "social_media", "protected")
    catalog = business_agent_module._tool_catalog([tool])

    assert business_agent_module._restore_role_tools(catalog, [tool]) == [tool]
    with pytest.raises(ValueError, match="definitions changed"):
        business_agent_module._restore_role_tools(catalog, [tool, extra])


# 验证令牌轮换不改变稳定身份，且密钥不进入目录快照或日志。
def test_token_rotation_keeps_identity_and_secrets_out_of_snapshot(
    tmp_path, monkeypatch, caplog
):
    _write_plugin(
        tmp_path,
        skills=[("xiaohongshu", "XHS", "Guidance.")],
        servers=[
            _http_server(
                "http://127.0.0.1:9000/mcp",
                headers={"Authorization": "Bearer ${PLUGIN_FIXTURE_TOKEN}"},
            )
        ],
        mcp=True,
    )
    _fake_mcp_discovery(monkeypatch)

    def load():
        return _load_fixture_tools(tmp_path)[0]

    monkeypatch.setenv("PLUGIN_FIXTURE_TOKEN", "alpha-placeholder-secret")
    first_catalog = business_agent_module._tool_catalog(load())
    monkeypatch.setenv("PLUGIN_FIXTURE_TOKEN", "beta-placeholder-secret")
    second = load()
    second_catalog = business_agent_module._tool_catalog(second)

    assert _load_skill_tool(first_catalog)["description"] == _load_skill_tool(
        second_catalog
    )["description"]
    assert (
        business_agent_module._restore_role_tools(first_catalog, second) is second
    )
    snapshot = json.dumps(first_catalog, ensure_ascii=False, default=str) + json.dumps(
        second_catalog, ensure_ascii=False, default=str
    )
    for secret in ("alpha-placeholder-secret", "beta-placeholder-secret"):
        assert secret not in snapshot
        assert secret not in caplog.text


# 验证只有成功装配的插件 MCP Server 参与身份摘要。
def test_failed_plugin_mcp_server_is_excluded_from_identity(tmp_path, monkeypatch):
    _write_plugin(
        tmp_path,
        skills=[("xiaohongshu", "XHS", "Guidance.")],
        servers=[
            _http_server("http://127.0.0.1:9000/mcp"),
            {**_http_server("http://127.0.0.1:9001/mcp"), "name": "broken"},
        ],
        mcp=True,
    )
    _fake_mcp_discovery(monkeypatch, fail={"broken"})

    def load():
        return _load_fixture_tools(tmp_path)[0]

    with_broken = load()
    catalog_with_broken = business_agent_module._tool_catalog(with_broken)
    _rewrite_servers(tmp_path, [_http_server("http://127.0.0.1:9000/mcp")])
    without_broken = load()
    catalog_without_broken = business_agent_module._tool_catalog(without_broken)

    assert _load_skill_tool(catalog_with_broken)["description"] == _load_skill_tool(
        catalog_without_broken
    )["description"]
    assert (
        business_agent_module._restore_role_tools(catalog_with_broken, without_broken)
        is without_broken
    )


# 验证 HTTP 身份只保留服务地址，丢弃凭据、查询参数和 Header。
def test_http_identity_drops_credentials_and_query_tokens():
    spec = MCPServerSpec(
        name="web",
        domain="social_media",
        effect="read",
        transport="streamable_http",
        params={
            "url": "https://user:pass@example.com:8443/mcp?token=secret-value",
            "headers": {"Authorization": "Bearer secret-value"},
        },
    )

    assert _server_identity(spec) == {
        "name": "web",
        "transport": "streamable_http",
        "url": "https://example.com:8443/mcp",
    }


# 验证 stdio 身份过滤敏感参数值，同时保留可识别的非敏感启动参数。
def test_stdio_identity_filters_secret_values_but_tracks_safe_args():
    spec = MCPServerSpec(
        name="db",
        domain="database",
        effect="read",
        transport="stdio",
        params={
            "command": "npx",
            "args": [
                "-y",
                "@bytebase/dbhub",
                "--transport",
                "stdio",
                "--dsn",
                "postgres://user:pass@db.local/app",
                "--read-only",
                "--max-rows",
                "100",
            ],
        },
    )

    identity = _server_identity(spec)
    encoded = json.dumps(identity, ensure_ascii=False)
    assert identity["command"] == "npx"
    assert "--dsn" in identity["args"]
    assert "--read-only" in identity["args"]
    assert "postgres://user:pass@db.local/app" not in encoded
    assert "user:pass" not in encoded

    changed = replace(
        spec, params={**spec.params, "args": [*spec.params["args"], "--danger"]}
    )
    assert _server_identity(changed) != identity


# 验证插件不能把外部工具挂到角色未授权的领域。
def test_plugin_mcp_cannot_bypass_role_domain(tmp_path):
    script = _write_mcp_server(tmp_path)
    _write_plugin(
        tmp_path,
        servers=[_stdio_server(script, domain="database")],
        mcp=True,
    )

    with pytest.raises(ValueError, match="outside the role's authorization"):
        load_role_plugins(_config(tmp_path), "public_signal", SOCIAL_DOMAINS)


# 验证与现有工具重名时按服务命名空间处理，且未知本地工具引用会失败。
def test_plugin_tool_conflicts_are_namespaced_and_references_validated(tmp_path):
    script = _write_mcp_server(tmp_path)
    _write_plugin(tmp_path, servers=[_stdio_server(script)], mcp=True)

    tools, _plugins = _load_fixture_tools(tmp_path)
    names = {tool.name for tool in tools}
    assert "fixture__web_search" in names
    assert "web_search" not in names

    _write_plugin(
        tmp_path,
        plugin_id="broken-plugin",
        tools=("missing_tool",),
    )
    with pytest.raises(ValueError, match="unknown or unauthorized local tools"):
        asyncio.run(
            load_plugin_tools(
                _config(tmp_path, enabled=("broken-plugin",)),
                role="public_signal",
                allowed_domains=SOCIAL_DOMAINS,
                local_tool_names=set(CORE_TOOL_NAMES),
                existing_tool_names=set(CORE_TOOL_NAMES),
            )
        )


# 验证 Skill 内容或插件版本变化会改变工具目录并在恢复时失败关闭。
def test_plugin_or_skill_changes_fail_closed_during_recovery(tmp_path):
    plugin_dir = _write_plugin(
        tmp_path, skills=[("xiaohongshu", "XHS", "Guidance.")]
    )

    def load():
        return _load_fixture_tools(tmp_path)[0]

    tools = load()
    catalog = business_agent_module._tool_catalog(tools)
    reloaded = load()
    assert business_agent_module._restore_role_tools(catalog, reloaded) is reloaded

    skill_file = plugin_dir / "skills" / "xiaohongshu" / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8") + "\nAdditional guidance.\n",
        encoding="utf-8",
    )
    changed_skill = load()
    assert _load_skill_tool(catalog)["description"] != _load_skill_tool(
        business_agent_module._tool_catalog(changed_skill)
    )["description"]
    with pytest.raises(ValueError, match="definitions changed"):
        business_agent_module._restore_role_tools(catalog, changed_skill)

    manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    manifest["version"] = "1.1.0"
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="definitions changed"):
        business_agent_module._restore_role_tools(
            business_agent_module._tool_catalog(changed_skill), load()
        )


# 从目录快照中取出 load_skill 定义，便于比较 Skill 标识。
def _load_skill_tool(catalog: list[dict]) -> dict:
    return next(entry for entry in catalog if entry.get("name") == LOAD_SKILL_TOOL)


# 验证密钥只通过环境变量注入，缺失时仅禁用该 MCP 服务。
def test_plugin_secrets_resolve_from_environment_only(tmp_path, monkeypatch):
    _write_plugin(
        tmp_path,
        servers=[
            {
                "name": "fixture",
                "transport": "streamable_http",
                "domain": "social_media",
                "effect": "protected",
                "params": {
                    "url": "https://example.invalid/mcp",
                    "headers": {"Authorization": "Bearer ${PLUGIN_FIXTURE_TOKEN}"},
                },
            }
        ],
        mcp=True,
    )
    monkeypatch.delenv("PLUGIN_FIXTURE_TOKEN", raising=False)

    without_secret = load_role_plugins(_config(tmp_path), "public_signal", SOCIAL_DOMAINS)
    assert without_secret.plugins[0].servers == ()

    monkeypatch.setenv("PLUGIN_FIXTURE_TOKEN", "resolved")
    with_secret = load_role_plugins(_config(tmp_path), "public_signal", SOCIAL_DOMAINS)
    assert (
        with_secret.plugins[0].servers[0].params["headers"]["Authorization"]
        == "Bearer resolved"
    )
    assert "${PLUGIN_FIXTURE_TOKEN}" in (
        tmp_path / "plugins" / "fixture-plugin" / "mcp.json"
    ).read_text(encoding="utf-8")


# 验证已提交的 Actor 调用 Step 在恢复时直接复用结果，不会再次启动 Actor。
def test_committed_actor_step_restores_without_rerunning_the_run(tmp_path, monkeypatch):
    from open_deep_research.runtime import tool_executor

    url = f"sqlite:///{(tmp_path / 'apify.sqlite').as_posix()}"
    workflow_id = "replay-apify"
    record_path = tmp_path / "actor_calls.txt"

    script = _write_mcp_server(
        tmp_path,
        APIFY_RECORDING_SERVER.replace("__RECORD_PATH__", repr(str(record_path))),
        "apify_replay",
    )
    _write_plugin(
        tmp_path,
        servers=[_stdio_server(script, effect="protected", name="apify")],
        mcp=True,
    )
    tools, _plugins = _load_fixture_tools(tmp_path)

    def start_operation(*_args):
        return None

    def finish_operation(*_args):
        return None

    async def commit(write, *args):
        await asyncio.to_thread(write, *args)

    async def observe(_self, _kind, _payload, *, call_id=None):
        return None

    monkeypatch.setattr(tool_executor, "start_operation", start_operation)
    monkeypatch.setattr(tool_executor, "finish_operation", finish_operation)
    monkeypatch.setattr(tool_executor, "commit_receipt", commit)
    monkeypatch.setattr(StageContext, "observe", observe)

    async def begin_model_call():
        assert DBOS.step_id is not None
        return None

    monkeypatch.setattr(sdk_runtime, "begin_model_call", begin_model_call)
    monkeypatch.setattr(
        sdk_runtime, "settings", lambda: SimpleNamespace(session_path=None)
    )

    attempts = 0

    def final(call):
        outputs = {
            item["call_id"]: item["output"]
            for item in call.input
            if item.get("type") == "function_call_output"
        }
        assert "https://www.xiaohongshu.com/explore/n1" in json.dumps(
            outputs, ensure_ascii=False
        )
        return [assistant_message("finished with one source")]

    @DBOS.workflow(name=f"por.test.apify_replay.{uuid4().hex}")
    async def role():
        nonlocal attempts
        attempts += 1
        model = ScriptedModel(
            [
                [
                    function_call(
                        "call-actor",
                        '{"actor":"fixture/xhs-notes","callOptions":{"maxTotalChargeUsd":0.2}}',
                        call_id="run",
                    )
                ],
                [
                    function_call(
                        "get-dataset-items",
                        '{"datasetId":"ds-1","limit":20}',
                        call_id="items",
                    )
                ],
                ModelStep.respond(final),
            ],
            default_usage=TOKENS,
        )
        runtime = _runtime(tools=tools, model=model)
        scope = StageContext(
            workflow_id,
            workflow_id + ":role",
            "public_signal_agent",
            0,
            runtime.config,
            {},
        )
        token = stage_context.set(scope)
        try:
            outcome = await runtime.run("collect evidence")
            if attempts == 1:
                # Stop after the Agent result was built but before the enclosing
                # Workflow returns it. Recovery must restore the committed Steps.
                raise RuntimeError("simulated executor failure before result")
            return outcome.budget, outcome.report
        finally:
            stage_context.reset(token)

    def launch():
        DBOS(
            config={
                "name": "por-test-apify",
                "system_database_url": url,
                "application_version": "apify-replay-test",
                "serializer": RunJSON(),
            }
        )
        DBOS.launch()

    async def first_attempt():
        with SetWorkflowID(workflow_id):
            await role()

    launch()
    client = DBOSClient(system_database_url=url, serializer=RunJSON())
    try:
        with pytest.raises(RuntimeError, match="simulated executor failure"):
            asyncio.run(first_attempt())
        before = client.list_workflow_steps(workflow_id)
        assert sum(s["function_name"] == "por.agent_tool" for s in before) == 2
        assert record_path.read_text(encoding="utf-8").splitlines() == [
            "fixture/xhs-notes"
        ]
        DBOS.destroy()
        launch()
        handle = client.rewind_workflow(
            workflow_id,
            start_step=max(s["function_id"] for s in before) + 1,
        )
        usage, report = handle.get_result()
        assert usage["tool_calls"] == 2
        assert "finished with one source" in report
        assert record_path.read_text(encoding="utf-8").splitlines() == [
            "fixture/xhs-notes"
        ]
        assert client.list_workflow_steps(workflow_id) == before
        assert attempts == 2
    finally:
        DBOS.destroy(workflow_completion_timeout_sec=2)
        client.destroy()
