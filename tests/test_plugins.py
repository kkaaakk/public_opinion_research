# 验证插件发现、角色授权、Skill 按需加载及 MCP 工具的统一执行链。
"""Plugin + Skills assembly contracts.

These tests use temporary plugin directories and real lightweight ``FastMCP``
servers, plus the same offline ScriptedModel runtime fixtures as the local-tool
tests. No paid model or external service is called.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from agents import function_tool
from agents.testing import ScriptedModel, assistant_message, function_call
from test_sdk_runtime import TOKENS, _runtime

from open_deep_research.configuration import Configuration
from open_deep_research.public_opinion_agents import get_public_opinion_agent_spec
from open_deep_research.runtime import business_agent as business_agent_module
from open_deep_research.tools import BusinessTool
from open_deep_research.tools.plugins import (
    LOAD_SKILL_TOOL,
    _skill_payload,
    build_skill_prompt,
    load_plugin_tools,
    load_role_plugins,
)

ROOT = Path(__file__).resolve().parents[1]
SOCIAL_DOMAINS = frozenset({"core", "web_search", "social_media"})
SOCIAL_TOOLS = {
    "search_posts",
    "search_complaints",
    "fetch_thread",
    "fetch_comments",
    "fetch_author_profile",
    "get_trending_keywords",
    "get_public_opinion_snapshot",
}

MCP_SERVER = '''from mcp.server.fastmcp import FastMCP

server = FastMCP("plugin-mcp-fixture")


@server.tool()
def echo(query: str) -> str:
    """Return the query with a deterministic plugin marker."""
    return "plugin-echo:" + query


@server.tool()
def search_posts(query: str) -> str:
    """Conflict with an existing local tool name."""
    return "remote:" + query


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
    plugin_dir.mkdir(parents=True)
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
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            f"title: {title}\n"
            f"description: {description}\n"
            "---\n\n"
            f"# {title}\n\n"
            "Use search_posts with the platform filter, then compare evidence.\n",
            encoding="utf-8",
        )
    return plugin_dir


# 写入可被 SDK 原生 MCP 客户端启动的 stdio 测试服务。
def _write_mcp_server(tmp_path: Path) -> Path:
    script = tmp_path / "plugin_mcp_server.py"
    script.write_text(MCP_SERVER, encoding="utf-8")
    return script


# 返回插件声明的 stdio 服务条目。
def _stdio_server(script: Path, *, domain: str = "social_media") -> dict:
    return {
        "name": "fixture",
        "transport": "stdio",
        "domain": domain,
        "effect": "read",
        "params": {"command": sys.executable, "args": [str(script)]},
    }


# 验证插件清单、Skills 与声明工具被正确读取。
def test_plugin_discovery_reads_manifest_and_skills(tmp_path):
    _write_plugin(
        tmp_path,
        skills=[("xiaohongshu", "小红书舆情调研", "Note and comment research guidance.")],
        tools=("search_posts", "fetch_comments"),
    )

    role_plugins = load_role_plugins(
        _config(tmp_path), "public_signal", SOCIAL_DOMAINS
    )

    assert [plugin.plugin_id for plugin in role_plugins.plugins] == ["fixture-plugin"]
    plugin = role_plugins.plugins[0]
    assert plugin.version == "1.0.0"
    assert plugin.tools == ("search_posts", "fetch_comments")
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
    tools, _plugins = asyncio.run(
        load_plugin_tools(
            _config(tmp_path),
            role="public_signal",
            allowed_domains=SOCIAL_DOMAINS,
            local_tool_names=set(SOCIAL_TOOLS),
            existing_tool_names=set(SOCIAL_TOOLS),
        )
    )

    assert not business_agent_module.has_external_research_tool(tools)
    assert business_agent_module.has_external_research_tool(
        [*tools, BusinessTool(_tool_declaration(), domain="web_search", effect="read")]
    )


# 验证未启用或目录缺失时不会装配任何插件能力，也不影响原有流程。
def test_disabled_or_missing_plugins_contribute_nothing(tmp_path):
    _write_plugin(tmp_path, skills=[("xiaohongshu", "XHS", "Guidance.")])

    disabled = _config(tmp_path, enabled=())
    tools, plugins = asyncio.run(
        load_plugin_tools(
            disabled,
            role="public_signal",
            allowed_domains=SOCIAL_DOMAINS,
            local_tool_names=set(SOCIAL_TOOLS),
            existing_tool_names=set(SOCIAL_TOOLS),
        )
    )
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
    assert "Use search_posts with the platform filter" in str(record.observation)
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


# 验证加载 Skill 后仍可调用现有社媒工具并保留来源。
def test_agent_loads_skill_then_uses_existing_social_tools(monkeypatch):
    monkeypatch.setenv(
        "SOCIAL_MEDIA_DATA_PATHS",
        str(ROOT / "data" / "social_media" / "bulk_mock_posts.jsonl"),
    )
    monkeypatch.delenv("SOCIAL_MEDIA_API_BASE_URL", raising=False)
    monkeypatch.delenv("PUBLIC_OPINION_API_BASE_URL", raising=False)
    config = {
        "configurable": {
            "enabled_plugins": ["social-research"],
            "plugins_dir": str(ROOT / "plugins"),
            "search_api": "none",
            "rag_enabled": False,
        }
    }

    tools = asyncio.run(
        business_agent_module._business_agent_tools(config, "public_signal")
    )
    assert LOAD_SKILL_TOOL in {tool.name for tool in tools}
    assert SOCIAL_TOOLS <= {tool.name for tool in tools}

    model = ScriptedModel(
        [
            [function_call(LOAD_SKILL_TOOL, '{"skill_name":"xiaohongshu"}', call_id="skill")],
            [
                function_call(
                    "search_posts",
                    '{"query":"Acme EV","platforms":"xiaohongshu","limit":5}',
                    call_id="search",
                )
            ],
            [
                function_call(
                    "fetch_comments",
                    '{"post_id":"xiaohongshu-001","limit":5}',
                    call_id="comments",
                )
            ],
            [assistant_message("summary")],
        ],
        default_usage=TOKENS,
    )
    runtime = _runtime("public_signal", tools=tools, model=model)
    result = asyncio.run(runtime.run("task"))

    assert runtime.harness.results["skill"].status == "succeeded"
    assert runtime.harness.results["search"].status == "succeeded"
    assert runtime.harness.results["comments"].status == "succeeded"
    assert "https://example.com/xhs/" in str(runtime.harness.results["search"].observation)
    assert "温度和胎压" in str(runtime.harness.results["comments"].observation)
    assert "https://example.com/xhs/" in result.report
    assert result.budget["tool_calls"] == 3
    # Social tools are not counted as web searches by the existing budget rules.
    assert result.budget.get("search_calls", 0) == 0


# 验证插件声明的 MCP 工具经现有 ToolExecutor 执行并计入预算。
def test_plugin_mcp_tool_runs_through_existing_tool_executor(tmp_path):
    script = _write_mcp_server(tmp_path)
    _write_plugin(tmp_path, servers=[_stdio_server(script)], mcp=True)

    tools, plugins = asyncio.run(
        load_plugin_tools(
            _config(tmp_path),
            role="public_signal",
            allowed_domains=SOCIAL_DOMAINS,
            local_tool_names=set(SOCIAL_TOOLS),
            existing_tool_names=set(SOCIAL_TOOLS),
        )
    )
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

    tools, _plugins = asyncio.run(
        load_plugin_tools(
            _config(tmp_path),
            role="public_signal",
            allowed_domains=SOCIAL_DOMAINS,
            local_tool_names=set(SOCIAL_TOOLS),
            existing_tool_names=set(SOCIAL_TOOLS),
        )
    )
    names = {tool.name for tool in tools}
    assert "fixture__search_posts" in names
    assert "search_posts" not in names

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
                local_tool_names=set(SOCIAL_TOOLS),
                existing_tool_names=set(SOCIAL_TOOLS),
            )
        )


# 验证 Skill 内容或插件版本变化会改变工具目录并在恢复时失败关闭。
def test_plugin_or_skill_changes_fail_closed_during_recovery(tmp_path):
    plugin_dir = _write_plugin(
        tmp_path, skills=[("xiaohongshu", "XHS", "Guidance.")]
    )

    def load():
        tools, _plugins = asyncio.run(
            load_plugin_tools(
                _config(tmp_path),
                role="public_signal",
                allowed_domains=SOCIAL_DOMAINS,
                local_tool_names=set(SOCIAL_TOOLS),
                existing_tool_names=set(SOCIAL_TOOLS),
            )
        )
        return tools

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
