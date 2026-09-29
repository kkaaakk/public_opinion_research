"""Assemble the tools visible to a research agent and describe them."""

from typing import Any

from langchain_core.messages import MessageLikeRepresentation, filter_messages
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from open_deep_research.config_values import get_config_value
from open_deep_research.configuration import Configuration, RetrievalMode, SearchAPI
from open_deep_research.search.tavily import tavily_search, tavily_search_raw
from open_deep_research.state import ResearchComplete

# Importing rag_tool eagerly makes the RAG package recurse
# through rag.__init__.  Keep this tool lazy so the raw Research Graph search
# path can be imported independently of the local RAG package.
rag_search = None


def _get_rag_search_tool():
    """Load the local RAG tool only when a RAG retrieval mode needs it."""
    global rag_search
    if rag_search is None:
        from open_deep_research.tools.rag_tool import rag_search as loaded_rag_search

        rag_search = loaded_rag_search
    return rag_search

##########################
# Reflection Tool
##########################
@tool(description="Strategic reflection tool for research planning")
def think_tool(reflection: str) -> str:
    """Tool for strategic reflection on research progress and decision-making.

    Use this tool after each search to analyze results and plan next steps systematically.
    This creates a deliberate pause in the research workflow for quality decision-making.

    When to use:
    - After receiving search results: What key information did I find?
    - Before deciding next steps: Do I have enough to answer comprehensively?
    - When assessing research gaps: What specific information am I still missing?
    - Before concluding research: Can I provide a complete answer now?

    Reflection should address:
    1. Analysis of current findings - What concrete information have I gathered?
    2. Gap assessment - What crucial information is still missing?
    3. Quality evaluation - Do I have sufficient evidence/examples for a good answer?
    4. Strategic decision - Should I continue searching or provide my answer?

    Args:
        reflection: Your detailed reflection on research progress, findings, gaps, and next steps

    Returns:
        Confirmation that reflection was recorded for decision-making
    """
    return f"Reflection recorded: {reflection}"


##########################
# Tool Utils
##########################

async def get_search_tool(search_api: SearchAPI):
    """Configure and return search tools based on the specified API provider.

    Args:
        search_api: The search API provider to use (Anthropic, OpenAI, Tavily, or None)

    Returns:
        List of configured search tool objects for the specified provider
    """
    if search_api == SearchAPI.ANTHROPIC:
        # Anthropic's native web search with usage limits
        return [{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5
        }]

    elif search_api == SearchAPI.OPENAI:
        # OpenAI's web search preview functionality
        return [{"type": "web_search_preview"}]

    elif search_api == SearchAPI.TAVILY:
        # Configure Tavily search tool with metadata
        search_tool = tavily_search
        search_tool.name = "web_search"
        search_tool.metadata = {
            **(search_tool.metadata or {}),
            "type": "search",
            "name": "web_search"
        }
        return [search_tool]

    elif search_api == SearchAPI.NONE:
        # No search functionality configured
        return []

    # Default fallback for unknown search API types
    return []


async def get_raw_search_tool(search_api: SearchAPI):
    """Return the Producer-specific raw/batch-friendly search tool.

    Native provider tools do not expose a project-controlled raw result
    envelope, so they remain unchanged and are handled as synthetic source
    documents by the Research Graph adapter.
    """
    if search_api == SearchAPI.TAVILY:
        raw_tool = tavily_search_raw
        raw_tool.name = "web_search"
        raw_tool.metadata = {
            **(raw_tool.metadata or {}),
            "type": "search",
            "name": "web_search",
            "research_graph_raw": True,
        }
        return [raw_tool]
    return await get_search_tool(search_api)

def rag_requested(configurable: Configuration) -> bool:
    """Determine whether the current configuration wants local RAG retrieval enabled."""
    retrieval_mode = RetrievalMode(get_config_value(configurable.retrieval_mode))
    return configurable.rag_enabled and retrieval_mode in {
        RetrievalMode.RAG_ONLY,
        RetrievalMode.HYBRID,
    }

async def get_retrieval_tools(config: RunnableConfig):
    """Configure retrieval tools based on web/RAG retrieval mode settings."""
    configurable = Configuration.from_runnable_config(config)
    retrieval_mode = RetrievalMode(get_config_value(configurable.retrieval_mode))
    retrieval_tools = []

    if retrieval_mode in {RetrievalMode.WEB_ONLY, RetrievalMode.HYBRID}:
        search_api = SearchAPI(get_config_value(configurable.search_api))
        retrieval_tools.extend(await get_search_tool(search_api))

    if rag_requested(configurable):
        rag_tool = _get_rag_search_tool()
        rag_tool.metadata = {
            **(rag_tool.metadata or {}),
            "type": "search",
            "name": "rag_search",
        }
        retrieval_tools.append(rag_tool)

    return retrieval_tools

async def get_all_tools(config: RunnableConfig):
    """Assemble complete toolkit including research, search, and MCP tools.

    Args:
        config: Runtime configuration specifying search API and MCP settings

    Returns:
        List of all configured and available tools for research operations
    """
    from open_deep_research.mcp import load_mcp_tools

    # Start with core research tools
    tools = [tool(ResearchComplete), think_tool]

    # Add configured retrieval tools
    retrieval_tools = await get_retrieval_tools(config)
    tools.extend(retrieval_tools)

    # Track existing tool names to prevent conflicts
    existing_tool_names = {
        tool.name if hasattr(tool, "name") else tool.get("name", "web_search")
        for tool in tools
    }

    # Add MCP tools if configured (multi-server, fault-isolated)
    mcp_tools = await load_mcp_tools(config, existing_tool_names)
    tools.extend(mcp_tools)

    # Tag known built-ins once at the shared loading boundary. Unknown tools
    # remain unclassified and are therefore denied by fixed-role business agents.
    from open_deep_research.mcp.domain_filter import (
        get_tool_domain,
        tag_tools_with_domain,
    )

    for available_tool in tools:
        domain = get_tool_domain(available_tool)
        if domain:
            tag_tools_with_domain([available_tool], domain)
    return tools

def has_external_research_tool(tools: list[Any]) -> bool:
    """Determine whether the tool collection includes anything beyond core control tools."""
    core_tool_names = {"ResearchComplete", "think_tool"}
    for available_tool in tools:
        if isinstance(available_tool, dict):
            return True
        if getattr(available_tool, "name", None) not in core_tool_names:
            return True
    return False

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
        inactive = sorted(
            d.name for d in DOMAIN_REGISTRY
            if d.name not in active_domain_names and not d.always_active
        ) if buckets else []
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

    if any(getattr(t, "name", "") == "rag_search" for t in tools if not isinstance(t, dict)):
        lines.append(
            "**CRITICAL: When using rag_search, only make claims that are "
            "supported by returned SOURCE citations. If the cited excerpts do "
            "not support the answer, say the local knowledge base or chat "
            "memory does not contain enough cited evidence.**"
        )

    return "\n".join(lines)

def get_notes_from_tool_calls(messages: list[MessageLikeRepresentation]):
    """Extract notes from tool call messages."""
    return [tool_msg.content for tool_msg in filter_messages(messages, include_types="tool")]

##########################
# Model Provider Native Websearch Utils
##########################
