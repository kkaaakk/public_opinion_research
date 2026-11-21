# 验证业务角色严格按明确领域白名单取得工具，未知或无标签工具默认拒绝。
"""Regression tests for public-opinion business-agent tool isolation."""

import asyncio

import pytest

import open_deep_research.runtime.business_agent as business_agent_module
from open_deep_research.mcp.domain_filter import get_tool_domain
from open_deep_research.public_opinion_agents import PUBLIC_OPINION_AGENT_SPECS
from open_deep_research.tools import manager as tool_manager_module
from open_deep_research.tools.registry import get_all_tools


# 提供带可控领域元数据的工具替身。
class FixtureTool:
    """Small tool-shaped fixture with optional domain metadata."""

    # 保存工具名及测试领域标签。
    def __init__(
        self, name: str, domain: str | None = None, *, with_metadata: bool = True
    ):
        self.name = name
        if with_metadata:
            self.metadata = {} if domain is None else {"tool_domain": domain}


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
        FixtureTool("load_skill", "core"),
        FixtureTool("web_search", "web_search"),
        FixtureTool("rag_search", "rag"),
        FixtureTool("db_query", "database"),
        FixtureTool("feishu_send", "feishu"),
        FixtureTool("document_convert", "document"),
        FixtureTool("legacy_lookup", "external_mcp"),
    ]
    _patch_tool_source(
        monkeypatch,
        [*tools, FixtureTool("social_search", "social_media")],
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
        FixtureTool("load_skill", "core"),
        FixtureTool("rag_search", "rag"),
        FixtureTool("web_search", "web_search"),
        FixtureTool("social_search", "social_media"),
        FixtureTool("db_query", "database"),
        FixtureTool("feishu_send", "feishu"),
        FixtureTool("external_lookup", "external_mcp"),
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
        FixtureTool("load_skill", "core"),
        FixtureTool("unknown_tool", "unknown_domain"),
        {"type": "function", "function": {"name": "anonymous_unknown"}},
    ]
    _patch_tool_source(monkeypatch, tools)

    filtered = asyncio.run(business_agent_module._business_agent_tools(_config(), role))

    assert _names(filtered) == {"load_skill"}


# 验证缺少领域标签的工具默认不可用。
def test_unlabeled_tool_is_denied_by_default(monkeypatch) -> None:
    """A normal tool without domain metadata is not exposed to a business agent."""
    unlabeled = FixtureTool("ordinary_tool", with_metadata=False)
    _patch_tool_source(
        monkeypatch,
        [
            FixtureTool("load_skill", "core"),
            FixtureTool("web_search", "web_search"),
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
        FixtureTool("load_skill", "core"),
        FixtureTool("social_media_mcp_search", "social_media"),
        FixtureTool("dbhub_query", "database"),
        FixtureTool("feishu_send", "feishu"),
        FixtureTool("markitdown_convert", "document"),
        FixtureTool("legacy_mcp_tool", "external_mcp"),
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


# 验证动态与静态工具说明以及角色提示词都不再要求旧控制工具。
def test_prompts_do_not_reference_retired_control_tools() -> None:
    from open_deep_research.configuration import Configuration
    from open_deep_research.tools.registry import (
        build_dynamic_tool_prompt,
        get_research_tool_prompt,
    )

    dynamic = build_dynamic_tool_prompt(
        [FixtureTool("web_search", "web_search")], Configuration()
    )
    static = get_research_tool_prompt(Configuration())
    for prompt in (dynamic, static):
        assert "think_tool" not in prompt
        assert "ResearchComplete" not in prompt

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


def test_native_catalog_fences_definition_changes_and_restores_legacy_catalog():
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

    # Existing journals stored the same catalog without the effect field.
    old_catalog = [{key: value for key, value in catalog[0].items() if key != "effect"}]
    restored = business_agent_module._restore_role_tools(old_catalog, available)
    assert restored[0].sdk_tool is available[0].sdk_tool
    assert restored[0].effect == "protected"
    old_catalog[0]["schema"] = {}
    with pytest.raises(ValueError, match="definitions changed"):
        business_agent_module._restore_role_tools(old_catalog, available)
