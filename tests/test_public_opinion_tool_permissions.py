# 验证业务角色严格按明确领域白名单取得工具，未知或无标签工具默认拒绝。
"""Regression tests for public-opinion business-agent tool isolation."""

import asyncio

import pytest
from agents.tool_context import ToolContext

import open_deep_research.runtime.business_agent as business_agent_module
from open_deep_research.mcp.domain_filter import get_tool_domain
from open_deep_research.public_opinion_agents import PUBLIC_OPINION_AGENT_SPECS
from open_deep_research.tools import BusinessTool, business_tool
from open_deep_research.tools import manager as tool_manager_module
from open_deep_research.tools.registry import get_all_tools


# 构造带可控领域声明的真实 BusinessTool 替身。
def _declared(name: str, domain: str = "") -> BusinessTool:
    """A genuine declaration; an empty domain models an unlabelled tool."""

    @business_tool(domain=domain, effect="read", name_override=name)
    async def fixture(context: ToolContext, query: str) -> str:
        return query

    return fixture


# 提取工具集合的名称，便于比较白名单。
def _names(tools) -> set[str]:
    return {
        tool.name if hasattr(tool, "name") else tool.get("name", "web_search")
        for tool in tools
    }


# 构造角色权限测试的运行配置。
def _config() -> dict:
    return {"configurable": {}}


# 替换基础工具来源，避免连接真实 MCP 或搜索服务。
def _patch_tool_source(monkeypatch, tools) -> None:
    # 返回本测试预设的工具集合。
    async def fake_get_all_tools(_config):
        return list(tools)

    # 插件装配由独立测试覆盖，这里保持领域过滤测试只关注角色权限。
    async def fake_load_plugin_tools(*_args, **_kwargs):
        return [], None

    monkeypatch.setattr(tool_manager_module, "get_all_tools", fake_get_all_tools)
    monkeypatch.setattr(
        tool_manager_module, "load_plugin_tools", fake_load_plugin_tools
    )


# 验证公众信号角色仅取得核心、联网和社交工具。
def test_public_signal_gets_only_core_web_and_social_tools(monkeypatch) -> None:
    """Public Signal cannot inherit database, Feishu, document, or RAG tools."""
    tools = [
        _declared("load_skill", "core"),
        _declared("web_search", "web_search"),
        _declared("rag_search", "rag"),
        _declared("db_query", "database"),
        _declared("feishu_send", "feishu"),
        _declared("document_convert", "document"),
        _declared("legacy_lookup", "external_mcp"),
    ]
    _patch_tool_source(
        monkeypatch,
        [*tools, _declared("social_search", "social_media")],
    )

    filtered = asyncio.run(
        business_agent_module._business_agent_tools(_config(), "public_signal")
    )

    assert _names(filtered) == {
        "load_skill",
        "web_search",
        "social_search",
    }
    assert "db_query" not in _names(filtered)
    assert "feishu_send" not in _names(filtered)
    assert "document_convert" not in _names(filtered)


# 验证内部知识角色仅取得核心和 RAG 工具。
def test_internal_knowledge_gets_only_core_and_rag(monkeypatch) -> None:
    """Internal Knowledge cannot inherit public, database, or external MCP tools."""
    tools = [
        _declared("load_skill", "core"),
        _declared("rag_search", "rag"),
        _declared("web_search", "web_search"),
        _declared("social_search", "social_media"),
        _declared("db_query", "database"),
        _declared("feishu_send", "feishu"),
        _declared("external_lookup", "external_mcp"),
    ]
    _patch_tool_source(monkeypatch, tools)

    filtered = asyncio.run(
        business_agent_module._business_agent_tools(_config(), "internal_knowledge")
    )

    assert _names(filtered) == {"load_skill", "rag_search"}


@pytest.mark.parametrize("role", ["risk_assessment", "response_strategy"])
def test_analysis_roles_have_no_agent_or_tool_assembly(monkeypatch, role) -> None:
    def unexpected(*args, **kwargs):
        pytest.fail("An analysis role reached agent tool discovery")

    monkeypatch.setattr(tool_manager_module.ToolManager, "discover", unexpected)
    with pytest.raises(KeyError, match="Unsupported public-opinion agent role"):
        asyncio.run(business_agent_module._business_agent_tools(_config(), role))
    with pytest.raises(KeyError, match="Unsupported public-opinion agent role"):
        asyncio.run(
            business_agent_module.run_business_agent(role=role, state={}, config=_config())
        )

# 验证所有角色都拒绝未知领域。
@pytest.mark.parametrize("role", PUBLIC_OPINION_AGENT_SPECS)
def test_unknown_domain_is_denied_for_every_business_agent(
    monkeypatch, role: str
) -> None:
    """An unregistered domain cannot pass a role's allowlist fallback."""
    tools = [
        _declared("load_skill", "core"),
        _declared("unknown_tool", "unknown_domain"),
    ]
    _patch_tool_source(monkeypatch, tools)

    filtered = asyncio.run(business_agent_module._business_agent_tools(_config(), role))

    assert _names(filtered) == {"load_skill"}


# 验证缺少领域标签的工具默认不可用。
def test_unlabeled_tool_is_denied_by_default(monkeypatch) -> None:
    """A normal tool without domain metadata is not exposed to a business agent."""
    unlabeled = _declared("ordinary_tool")
    _patch_tool_source(
        monkeypatch,
        [
            _declared("load_skill", "core"),
            _declared("web_search", "web_search"),
            unlabeled,
        ],
    )

    filtered = asyncio.run(
        business_agent_module._business_agent_tools(_config(), "internal_knowledge")
    )

    assert _names(filtered) == {"load_skill"}


# 验证仅有 MCP 来源标记不会自动授予公众信号权限。
def test_mcp_source_does_not_grant_public_signal_authorization(monkeypatch) -> None:
    """MCP tools are admitted by domain, not merely because they came from MCP."""
    tools = [
        _declared("load_skill", "core"),
        _declared("social_media_mcp_search", "social_media"),
        _declared("dbhub_query", "database"),
        _declared("feishu_send", "feishu"),
        _declared("markitdown_convert", "document"),
        _declared("legacy_mcp_tool", "external_mcp"),
    ]
    _patch_tool_source(monkeypatch, tools)

    filtered = asyncio.run(
        business_agent_module._business_agent_tools(_config(), "public_signal")
    )

    assert _names(filtered) == {
        "load_skill",
        "social_media_mcp_search",
    }


# 验证各角色显式声明允许领域。
def test_agent_domain_allowlists_are_explicit() -> None:
    """Every business agent declares its domain permissions in its spec."""
    assert PUBLIC_OPINION_AGENT_SPECS["public_signal"].allowed_domains == frozenset(
        {"core", "web_search", "social_media"}
    )
    assert PUBLIC_OPINION_AGENT_SPECS[
        "internal_knowledge"
    ].allowed_domains == frozenset({"core", "rag"})


# 验证共享加载器给已知内置工具附加正确领域，且不再注入旧控制工具。
def test_shared_loader_tags_known_builtin_domains() -> None:
    """Known builtin tools receive domain metadata at the shared loading boundary."""
    tools = asyncio.run(
        get_all_tools(
            {
                "configurable": {
                    "search_api": "tavily",
                    "rag_enabled": False,
                }
            }
        )
    )
    loaded = {tool.name: tool for tool in tools if hasattr(tool, "name")}

    assert get_tool_domain(loaded["web_search"]) == "web_search"
    assert {"think_tool", "ResearchComplete"}.isdisjoint(loaded)


# 验证动态工具说明与角色提示词都不再要求旧控制工具。
def test_prompts_do_not_reference_retired_control_tools() -> None:
    from open_deep_research.configuration import Configuration
    from open_deep_research.tools.registry import build_dynamic_tool_prompt

    dynamic = build_dynamic_tool_prompt(
        [_declared("web_search", "web_search")], Configuration()
    )
    assert "think_tool" not in dynamic
    assert "ResearchComplete" not in dynamic

    for role in PUBLIC_OPINION_AGENT_SPECS:
        rendered = PUBLIC_OPINION_AGENT_SPECS[role].format_system_prompt(
            retrieval_tool_prompt=dynamic,
            mcp_prompt="",
            date="2026-11-18",
            organization_context="org",
        )
        assert "think_tool" not in rendered
        assert "ResearchComplete" not in rendered
        assert "normal assistant message" in rendered


def test_native_catalog_fences_definition_changes():
    from dataclasses import replace

    from agents import function_tool

    from open_deep_research.tools import register_tool

    @function_tool(
        name_override="search-actors",
        description_override="fixture Apify tool",
        use_docstring_info=False,
        strict_mode=False,
        failure_error_function=None,
    )
    async def search_actors(keywords: str = "") -> str:
        return keywords

    available = [
        register_tool(search_actors, domain="social_media", effect="protected")
    ]
    catalog = business_agent_module._tool_catalog(available)
    assert catalog[0]["effect"] == "protected"
    assert business_agent_module._restore_role_tools(catalog, available) == available
    with pytest.raises(ValueError, match="definitions changed"):
        business_agent_module._restore_role_tools(
            catalog, [replace(available[0], effect="read")]
        )

    # A catalog without the effect cannot come from a recoverable journal: the
    # field predates the DBOS Application Version this Executor recovers.
    for old_catalog in (
        [{key: value for key, value in catalog[0].items() if key != "effect"}],
        [{**catalog[0], "schema": {}}],
    ):
        with pytest.raises(ValueError, match="definitions changed"):
            business_agent_module._restore_role_tools(old_catalog, available)
