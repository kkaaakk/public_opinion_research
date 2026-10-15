# 验证业务角色严格按明确领域白名单取得工具，未知或无标签工具默认拒绝。
"""Regression tests for public-opinion business-agent tool isolation."""

import asyncio

import pytest

import open_deep_research.runtime.business_agent as business_agent_module
from open_deep_research.mcp.domain_filter import get_tool_domain
from open_deep_research.public_opinion_agents import PUBLIC_OPINION_AGENT_SPECS
from open_deep_research.tools.registry import get_all_tools


# 提供带可控领域元数据的工具替身。
class FixtureTool:
    """Small tool-shaped fixture with optional domain metadata."""

    # 保存工具名及测试领域标签。
    def __init__(self, name: str, domain: str | None = None, *, with_metadata: bool = True):
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
    return {"configurable": {"research_graph_enabled": False}}


# 替换基础工具来源，避免连接真实 MCP 或搜索服务。
def _patch_tool_source(monkeypatch, tools, social_tools=()) -> None:
    # 返回本测试预设的工具集合。
    async def fake_get_all_tools(_config):
        return list(tools)

    monkeypatch.setattr(business_agent_module, "get_all_tools", fake_get_all_tools)
    monkeypatch.setattr(
        business_agent_module,
        "get_social_media_tools",
        lambda: list(social_tools),
    )


# 验证公众信号角色仅取得核心、联网和社交工具。
def test_public_signal_gets_only_core_web_and_social_tools(monkeypatch) -> None:
    """Public Signal cannot inherit database, Feishu, document, or RAG tools."""
    tools = [
        FixtureTool("ResearchComplete"),
        FixtureTool("think_tool"),
        FixtureTool("web_search", "web_search"),
        FixtureTool("rag_search", "rag"),
        FixtureTool("db_query", "database"),
        FixtureTool("feishu_send", "feishu"),
        FixtureTool("document_convert", "document"),
        FixtureTool("legacy_lookup", "external_mcp"),
    ]
    _patch_tool_source(monkeypatch, tools, [FixtureTool("social_search")])

    filtered = asyncio.run(
        business_agent_module._business_agent_tools(_config(), "public_signal")
    )

    assert _names(filtered) == {
        "ResearchComplete",
        "think_tool",
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
        FixtureTool("ResearchComplete"),
        FixtureTool("think_tool"),
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

    assert _names(filtered) == {"ResearchComplete", "think_tool", "rag_search"}


# 验证策略角色不获得 MCP 写入工具。
def test_response_strategy_does_not_get_mcp_write_tools(monkeypatch) -> None:
    """Response Strategy keeps Feishu, database, document, and external tools out."""
    tools = [
        FixtureTool("ResearchComplete"),
        FixtureTool("think_tool"),
        FixtureTool("rag_search", "rag"),
        FixtureTool("feishu_send", "feishu"),
        FixtureTool("dbhub_query", "database"),
        FixtureTool("markitdown_convert", "document"),
        FixtureTool("legacy_mcp_tool", "external_mcp"),
        FixtureTool("social_search", "social_media"),
    ]
    _patch_tool_source(monkeypatch, tools)

    filtered = asyncio.run(
        business_agent_module._business_agent_tools(_config(), "response_strategy")
    )

    assert _names(filtered) == {"ResearchComplete", "think_tool", "rag_search"}


# 验证所有角色都拒绝未知领域。
@pytest.mark.parametrize("role", ["public_signal", "internal_knowledge", "risk_assessment", "response_strategy"])
def test_unknown_domain_is_denied_for_every_business_agent(monkeypatch, role: str) -> None:
    """An unregistered domain cannot pass a role's allowlist fallback."""
    tools = [
        FixtureTool("ResearchComplete"),
        FixtureTool("unknown_tool", "unknown_domain"),
        {"type": "function", "function": {"name": "anonymous_unknown"}},
    ]
    _patch_tool_source(monkeypatch, tools)

    filtered = asyncio.run(
        business_agent_module._business_agent_tools(_config(), role)
    )

    assert _names(filtered) == {"ResearchComplete"}


# 验证缺少领域标签的工具默认不可用。
def test_unlabeled_tool_is_denied_by_default(monkeypatch) -> None:
    """A normal tool without domain metadata is not exposed to a business agent."""
    unlabeled = FixtureTool("ordinary_tool", with_metadata=False)
    _patch_tool_source(
        monkeypatch,
        [FixtureTool("ResearchComplete"), FixtureTool("think_tool"), unlabeled],
    )

    filtered = asyncio.run(
        business_agent_module._business_agent_tools(_config(), "response_strategy")
    )

    assert _names(filtered) == {"ResearchComplete", "think_tool"}


# 验证仅有 MCP 来源标记不会自动授予公众信号权限。
def test_mcp_source_does_not_grant_public_signal_authorization(monkeypatch) -> None:
    """MCP tools are admitted by domain, not merely because they came from MCP."""
    tools = [
        FixtureTool("ResearchComplete"),
        FixtureTool("think_tool"),
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
        "ResearchComplete",
        "think_tool",
        "social_media_mcp_search",
    }


# 验证各角色显式声明允许领域。
def test_agent_domain_allowlists_are_explicit() -> None:
    """Every business agent declares its domain permissions in its spec."""
    assert PUBLIC_OPINION_AGENT_SPECS["public_signal"].allowed_domains == frozenset(
        {"core", "web_search", "social_media"}
    )
    assert PUBLIC_OPINION_AGENT_SPECS["internal_knowledge"].allowed_domains == frozenset(
        {"core", "rag"}
    )
    assert PUBLIC_OPINION_AGENT_SPECS["risk_assessment"].allowed_domains == frozenset(
        {"core", "web_search", "rag", "social_media"}
    )
    assert PUBLIC_OPINION_AGENT_SPECS["response_strategy"].allowed_domains == frozenset(
        {"core", "rag"}
    )


# 验证共享加载器给已知内置工具附加正确领域。
def test_shared_loader_tags_known_builtin_domains() -> None:
    """Known core tools receive domain metadata at the shared loading boundary."""
    tools = asyncio.run(
        get_all_tools(
            {
                "configurable": {
                    "search_api": "none",
                    "rag_enabled": False,
                }
            }
        )
    )
    core_tools = {tool.name: tool for tool in tools if hasattr(tool, "name")}

    assert get_tool_domain(core_tools["ResearchComplete"]) == "core"
    assert get_tool_domain(core_tools["think_tool"]) == "core"
