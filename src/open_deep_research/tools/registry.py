# 根据检索模式与实际可用服务装配研究工具，并生成一致的动态工具说明。
"""Assemble the tools visible to a research agent and describe them."""

from typing import Any

from agents import function_tool
from agents.tool_context import ToolContext
from langchain_core.messages import MessageLikeRepresentation, filter_messages
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from open_deep_research.config_values import get_config_value
from open_deep_research.configuration import Configuration, RetrievalMode, SearchAPI
from open_deep_research.search.tavily import web_search, web_search_raw
from open_deep_research.tools import BusinessTool, ToolEffect, register_tool


def tool_effect(tool: BusinessTool | BaseTool) -> ToolEffect:
    """Treat unknown or unclassified effects as protected."""
    effect = (
        tool.effect
        if isinstance(tool, BusinessTool)
        else (tool.metadata or {}).get("effect")
    )
    return effect if effect in ("read", "idempotent") else "protected"


# 仅记录反思内容，不执行外部调用。
async def _record_reflection(reflection: str) -> str:
    return f"Reflection recorded: {reflection}"


@function_tool(
    description_override="Strategic reflection tool for research planning",
    use_docstring_info=False,
    strict_mode=False,
    failure_error_function=None,
)
async def think_tool(context: ToolContext, reflection: str) -> str:
    return await context.context.tool_executor.execute(
        context, _record_reflection, reflection
    )


# 完成信号本身不产生内容。
async def _complete_research() -> str:
    return ""


@function_tool(
    description_override="Call this tool to indicate that the research is complete.",
    use_docstring_info=False,
    strict_mode=False,
    failure_error_function=None,
)
async def ResearchComplete(context: ToolContext) -> str:
    return await context.context.tool_executor.execute(context, _complete_research)


think_tool = register_tool(think_tool, domain="core", effect="read")
ResearchComplete = register_tool(ResearchComplete, domain="core", effect="read")


# 按搜索供应商选择原生搜索声明或项目搜索工具。
async def get_search_tool(search_api: SearchAPI):
    """Configure and return search tools based on the specified API provider.

    Args:
        search_api: The search API provider to use (Anthropic, OpenAI, Tavily, or None)

    Returns:
        List of configured search tool objects for the specified provider
    """
    if search_api == SearchAPI.ANTHROPIC:
        # Anthropic's native web search with usage limits
        return [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]

    elif search_api == SearchAPI.OPENAI:
        # OpenAI's web search preview functionality
        return [{"type": "web_search_preview"}]

    if search_api == SearchAPI.TAVILY:
        return [web_search]

    return []


async def get_raw_search_tool(search_api: SearchAPI):
    """Select source-bound Tavily results for Graph Producers."""
    if search_api == SearchAPI.TAVILY:
        return [web_search_raw]
    return await get_search_tool(search_api)


# 判断当前检索模式是否要求启用本地 RAG。
def rag_requested(configurable: Configuration) -> bool:
    """Determine whether the current configuration wants local RAG retrieval enabled."""
    retrieval_mode = RetrievalMode(get_config_value(configurable.retrieval_mode))
    return configurable.rag_enabled and retrieval_mode in {
        RetrievalMode.RAG_ONLY,
        RetrievalMode.HYBRID,
    }


# 按联网、本地或混合检索模式组合检索工具。
async def get_retrieval_tools(config: RunnableConfig):
    """Configure retrieval tools based on web/RAG retrieval mode settings."""
    configurable = Configuration.from_runnable_config(config)
    retrieval_mode = RetrievalMode(get_config_value(configurable.retrieval_mode))
    retrieval_tools = []

    if retrieval_mode in {RetrievalMode.WEB_ONLY, RetrievalMode.HYBRID}:
        search_api = SearchAPI(get_config_value(configurable.search_api))
        retrieval_tools.extend(await get_search_tool(search_api))

    if rag_requested(configurable):
        from open_deep_research.tools.rag_tool import rag_search_tool

        retrieval_tools.append(rag_search_tool)

    return retrieval_tools


# 汇总研究控制、搜索和 MCP 工具，形成当前运行的基础工具集合。
async def get_all_tools(config: RunnableConfig):
    """Assemble complete toolkit including research, search, and MCP tools.

    Args:
        config: Runtime configuration specifying search API and MCP settings

    Returns:
        List of all configured and available tools for research operations
    """
    from open_deep_research.mcp import load_mcp_tools

    tools = [ResearchComplete, think_tool, *await get_retrieval_tools(config)]
    existing_tool_names = {
        tool.name if hasattr(tool, "name") else tool.get("name", "web_search")
        for tool in tools
    }
    return [*tools, *await load_mcp_tools(config, existing_tool_names)]


# 判断工具集合是否包含控制工具之外的实际外部研究能力。
def has_external_research_tool(tools: list[Any]) -> bool:
    """Determine whether the tool collection includes anything beyond core control tools."""
    core_tool_names = {"ResearchComplete", "think_tool"}
    for available_tool in tools:
        if isinstance(available_tool, dict):
            return True
        if getattr(available_tool, "name", None) not in core_tool_names:
            return True
    return False


# 生成旧版静态工具说明，保留兼容入口。
def get_research_tool_prompt(configurable: Configuration) -> str:
    """Create a short, mode-aware tool summary for the researcher prompt.

    .. deprecated::
        Prefer :func:`build_dynamic_tool_prompt` which reflects the **actual**
        tool list bound for the current invocation rather than a static
        template.  Kept for backward compatibility with callers that don't
        have access to the concrete tool list.
    """
    retrieval_mode = RetrievalMode(get_config_value(configurable.retrieval_mode))
    prompt_lines = []
    line_number = 1

    if retrieval_mode in {RetrievalMode.WEB_ONLY, RetrievalMode.HYBRID}:
        search_api = SearchAPI(get_config_value(configurable.search_api))
        if search_api != SearchAPI.NONE:
            prompt_lines.append(
                f"{line_number}. **web_search**: Search the live web for external or up-to-date information."
            )
            line_number += 1

    if rag_requested(configurable):
        rag_sources = "local documents"
        if configurable.rag_memory_enabled and (
            configurable.rag_memory_paths or configurable.rag_memory_mysql_url
        ):
            rag_sources += " and chat memory"
        prompt_lines.append(
            f"{line_number}. **rag_search**: Search the configured {rag_sources} and return grounded excerpts with citations. Use only cited excerpts for local-knowledge or memory claims."
        )
        line_number += 1

    prompt_lines.append(
        f"{line_number}. **think_tool**: For reflection and strategic planning during research."
    )

    if retrieval_mode == RetrievalMode.HYBRID and rag_requested(configurable):
        prompt_lines.append(
            "When local documents need external confirmation or missing context, use both `rag_search` and `web_search` and then compare the findings."
        )

    return "\n".join(prompt_lines)


# 检查实际绑定的工具并按领域生成说明，反映当前权限与可用能力。
def build_dynamic_tool_prompt(
    tools: list,
    configurable: Configuration | None = None,
    *,
    active_domains: set[str] | None = None,
) -> str:
    """Build a tool prompt that reflects the **actual** tools bound for this call.

    Unlike :func:`get_research_tool_prompt` which outputs a static template,
    this function inspects the concrete tool list and groups tools by their
    domain, giving the LLM an accurate picture of what is (and isn't)
    available for the current invocation.

    Parameters
    ----------
    tools:
        The filtered tool list that will be passed to ``bind_tools()``.
    configurable:
        Optional Configuration for RAG-mode detection.
    active_domains:
        Optional set of domain names that survived filtering — used to
        add a note about which domains are active vs. inactive.

    Returns:
    -------
    str
        A Markdown-formatted tool listing for inclusion in the system prompt.
    """
    from open_deep_research.mcp.domain_filter import (
        DOMAIN_REGISTRY,
        classify_tools,
        get_domain_label,
        iter_domain_labels,
    )

    buckets = classify_tools(tools)
    active_domain_names = set(buckets.keys())
    lines: list[str] = []
    counter = 1

    # Iterate domains in registry order — dynamically, not hardcoded
    ordered = iter_domain_labels(active_domain_names)
    for domain, label in ordered:
        domain_tools = buckets.pop(domain, [])
        if not domain_tools:
            continue
        lines.append(f"### {label}")
        for domain_tool in domain_tools:
            name = (
                domain_tool.get("name", "")
                if isinstance(domain_tool, dict)
                else getattr(domain_tool, "name", "")
            )
            desc = ""
            if not isinstance(domain_tool, dict):
                desc = (getattr(domain_tool, "description", "") or "").split("\n")[0][
                    :120
                ]
            if desc:
                lines.append(f"{counter}. **{name}**: {desc}")
            else:
                lines.append(f"{counter}. **{name}**")
            counter += 1
        lines.append("")

    # Note: which domains are INACTIVE (helps LLM avoid guessing)
    if active_domains:
        inactive = sorted(set(active_domains) - active_domain_names)
    else:
        inactive = (
            sorted(
                d.name
                for d in DOMAIN_REGISTRY
                if d.name not in active_domain_names and not d.always_active
            )
            if buckets
            else []
        )
    if inactive:
        labels = [get_domain_label(d) for d in inactive]
        lines.append(
            f"*Note: tools from these domains are NOT available for "
            f"this query: {', '.join(labels)}. Do not attempt to use them.*"
        )

    # Guardrails (always included)
    lines.append(
        "**CRITICAL: Use think_tool after each retrieval step to reflect on "
        "results and plan next steps. Do not call think_tool together with "
        "other tools — it should be used alone to reflect on previous results.**"
    )

    if any(
        getattr(t, "name", "") == "rag_search" for t in tools if not isinstance(t, dict)
    ):
        lines.append(
            "**CRITICAL: When using rag_search, only make claims that are "
            "supported by returned SOURCE citations. If the cited excerpts do "
            "not support the answer, say the local knowledge base or chat "
            "memory does not contain enough cited evidence.**"
        )

    return "\n".join(lines)


# 从工具调用消息中提取研究笔记。
def get_notes_from_tool_calls(messages: list[MessageLikeRepresentation]):
    """Extract notes from tool call messages."""
    return [
        tool_msg.content for tool_msg in filter_messages(messages, include_types="tool")
    ]


##########################
# Model Provider Native Websearch Utils
##########################
