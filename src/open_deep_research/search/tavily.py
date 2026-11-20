# 实现 Tavily 并发搜索、原始结果收集和网页摘要生成。
"""Tavily search tools and webpage summarization."""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Annotated, Any, List, Literal

from agents.tool_context import ToolContext
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool
from tavily import AsyncTavilyClient

from open_deep_research.budget import ainvoke_model_with_budget, structured_output_chain
from open_deep_research.configuration import Configuration
from open_deep_research.models import create_chat_model
from open_deep_research.prompts import summarize_webpage_prompt
from open_deep_research.state import Summary
from open_deep_research.time_utils import get_today_str
from open_deep_research.tools import business_tool

TAVILY_SEARCH_DESCRIPTION = (
    "A search engine optimized for comprehensive, accurate, and trusted results. "
    "Useful for when you need to answer questions about current events."
)

# Tool-output safety bounds. These are deliberately not user-tunable: a larger
# value would let one search result bypass the Agent context protection.
MAX_WEB_SOURCE_OUTPUT_CHARS = 6_000
MAX_WEB_SEARCH_OUTPUT_CHARS = 24_000
MAX_SOURCE_TITLE_CHARS = 200
MAX_SOURCE_URL_CHARS = 800
MAX_DEGRADED_EXCERPT_CHARS = 1_500
SOURCE_TRUNCATION_NOTE = (
    "\n[TRUNCATED: source content exceeded the per-source output limit.]"
)
OUTPUT_TRUNCATION_NOTE = "\n[TRUNCATED: web_search output exceeded its total limit.]"
DEGRADED_SUMMARY_PREFIX = (
    "[SUMMARY UNAVAILABLE: webpage summarization {reason}; the excerpt below is "
    "retrieved source text, not a model summary.]"
)


# 按字符边界裁剪文本，并报告是否发生截断。
def _clip_text(text: str, limit: int) -> tuple[str, bool]:
    """Clip text to a character bound and report whether it was truncated."""
    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip(), True


# 摘要失败时只保留受限的 Tavily 片段或网页摘录，绝不回填完整原文。
def _bounded_fallback(
    fallback_content: str | None,
    max_chars: int,
    *,
    reason: str,
) -> str:
    """Return a clearly marked, bounded source excerpt for a failed summary."""
    text = (fallback_content or "").strip()
    prefix = DEGRADED_SUMMARY_PREFIX.format(reason=reason)
    if not text:
        return prefix + "\n(no source excerpt available)"
    excerpt, truncated = _clip_text(text, max(1, max_chars))
    if truncated:
        excerpt += "\n[EXCERPT TRUNCATED]"
    return prefix + "\n" + excerpt


# 预算拒绝、执行控制异常、取消和存储故障不得被当作普通摘要失败吞掉。
def _is_execution_control_failure(error: BaseException) -> bool:
    """Return whether an error must propagate instead of degrading locally."""
    from sqlalchemy.exc import SQLAlchemyError

    from open_deep_research.budget import ModelBudgetExceeded
    from open_deep_research.workflow.execution import (
        ExecutionControlError,
        is_cancellation,
    )

    return isinstance(
        error, (ModelBudgetExceeded, ExecutionControlError, SQLAlchemyError)
    ) or is_cancellation(error)


# 为每条来源分配均等输出额度，避免长网页挤掉后续来源。
def _format_search_results(results: dict[str, dict[str, Any]]) -> str:
    """Format summarized sources under one total output bound."""
    if not results:
        return "No valid search results found. Please try different search queries or use a different search API."

    prefix = "Search results: \n\n"
    entries = list(results.items())
    # Equal shares keep a long first source from consuming the entire bound.
    per_block = max(1, (MAX_WEB_SEARCH_OUTPUT_CHARS - len(prefix)) // len(entries))
    blocks: list[str] = []
    for index, (url, result) in enumerate(entries):
        head = f"\n\n--- SOURCE {index + 1}: "
        middle = " ---\nURL: "
        tail = "\n\nSUMMARY:\n"
        divider = "\n\n" + "-" * 80 + "\n"
        variable = max(0, per_block - len(head) - len(middle) - len(tail) - len(divider))
        title_budget = min(MAX_SOURCE_TITLE_CHARS, max(1, variable // 4))
        url_budget = min(MAX_SOURCE_URL_CHARS, max(1, variable - title_budget))
        content_budget = min(
            MAX_WEB_SOURCE_OUTPUT_CHARS,
            max(
                0,
                variable
                - title_budget
                - url_budget
                - len(SOURCE_TRUNCATION_NOTE),
            ),
        )
        title, _ = _clip_text(str(result.get("title") or ""), title_budget)
        url_text, _ = _clip_text(str(url), url_budget)
        content, truncated = _clip_text(
            str(result.get("content") or ""), content_budget
        )
        if truncated:
            content += SOURCE_TRUNCATION_NOTE
        blocks.append(head + title + middle + url_text + tail + content + divider)

    output = prefix + "".join(blocks)
    if len(output) > MAX_WEB_SEARCH_OUTPUT_CHARS:
        # Defensive bound for pathological source counts. The per-source shares
        # normally keep the joined output within the configured total.
        keep = MAX_WEB_SEARCH_OUTPUT_CHARS - len(OUTPUT_TRUNCATION_NOTE)
        output = output[:keep].rstrip() + OUTPUT_TRUNCATION_NOTE
    return output


# 顺序检索多条查询，对带原文的结果逐篇生成摘要并汇总来源。
async def _tavily_search(
    queries: List[str],
    max_results: Annotated[int, InjectedToolArg] = 5,
    topic: Annotated[
        Literal["general", "news", "finance"], InjectedToolArg
    ] = "general",
    config: RunnableConfig = None,
) -> str:
    """Fetch and summarize search results from Tavily search API.

    Args:
        queries: List of search queries to execute
        max_results: Maximum number of results to return per query
        topic: Topic filter for search results (general, news, or finance)
        config: Runtime configuration for API keys and model settings

    Returns:
        Formatted string containing summarized search results
    """
    # Step 1: Execute search queries asynchronously
    search_results = await tavily_search_async(
        queries,
        max_results=max_results,
        topic=topic,
        include_raw_content=True,
        config=config,
    )

    # Step 2: Deduplicate results by URL to avoid processing the same content multiple times
    unique_results = {}
    for response in search_results:
        for result in response["results"]:
            url = result["url"]
            if url not in unique_results:
                unique_results[url] = {**result, "query": response["query"]}

    # Step 3: Set up the summarization model with configuration
    configurable = Configuration.from_runnable_config(config)

    # Character limit to stay within model token limits (configurable)
    max_char_to_include = configurable.max_content_length

    # Initialize summarization model with retry logic
    summarization_model = structured_output_chain(
        create_chat_model(
            model=configurable.summarization_model,
            max_tokens=configurable.summarization_model_max_tokens,
            config=config,
            tags=["langsmith:nostream"],
        ),
        Summary,
        max_attempts=configurable.max_structured_output_retries,
    )

    # Step 4: Summarize one page at a time. A single in-flight model bill keeps
    # the shared token budget consistent and avoids a burst of DB pool users.
    summaries = []
    for result in unique_results.values():
        if not result.get("raw_content"):
            summaries.append(None)
            continue
        summaries.append(
            await summarize_webpage(
                summarization_model,
                str(result["raw_content"])[:max_char_to_include],
                model_name=configurable.summarization_model,
                fallback_content=str(
                    result.get("content") or result["raw_content"] or ""
                ),
            )
        )

    # Step 5: Combine results with their summaries
    summarized_results = {
        url: {
            "title": result["title"],
            "content": result["content"] if summary is None else summary,
        }
        for url, result, summary in zip(
            unique_results.keys(), unique_results.values(), summaries
        )
    }

    # Step 6: Format the final output under explicit per-source and total caps
    return _format_search_results(summarized_results)


# 保留 Tavily 原始结果，跳过逐网页摘要，供研究图生产流程使用。
async def _tavily_search_raw(
    queries: List[str],
    max_results: Annotated[int, InjectedToolArg] = 5,
    topic: Annotated[
        Literal["general", "news", "finance"], InjectedToolArg
    ] = "general",
    config: RunnableConfig = None,
) -> str:
    """Fetch raw Tavily results without the standard per-URL summary fan-out."""
    search_results = await tavily_search_async(
        queries,
        max_results=max_results,
        topic=topic,
        include_raw_content=True,
        config=config,
    )
    configurable = Configuration.from_runnable_config(config)
    unique_results: dict[str, dict[str, Any]] = {}
    for response in search_results:
        for result in response.get("results", []):
            url = str(result.get("url") or "").strip()
            if not url or url in unique_results:
                continue
            raw_content = result.get("raw_content") or result.get("content") or ""
            unique_results[url] = {
                "url": url,
                "title": result.get("title") or "",
                "content": str(raw_content)[: configurable.max_content_length],
                "query": response.get("query") or "",
                "source_type": "web_search",
                "retrieved_at": datetime.utcnow().isoformat() + "Z",
            }
    if not unique_results:
        return json.dumps(
            {"type": "research_raw_search", "results": []},
            ensure_ascii=False,
        )
    return json.dumps(
        {"type": "research_raw_search", "results": list(unique_results.values())},
        ensure_ascii=False,
    )


TAVILY_RAW_DESCRIPTION = (
    "Fetch raw, source-bound Tavily results for Research Graph extraction. "
    "This path does not call a per-page summarization model."
)


# 运行配置须在受保护操作登记后读取，因此业务函数在 Executor 验参后自行取用。
@business_tool(
    domain="web_search",
    effect="read",
    description_override=TAVILY_SEARCH_DESCRIPTION,
)
async def web_search(context: ToolContext, queries: list[str]) -> str:
    """Search the live web and return summarized, cited results."""
    config = context.context.tool_executor.tool_config(context)
    return await _tavily_search(queries, config=config)


@business_tool(
    domain="web_search",
    effect="read",
    name_override="web_search",
    description_override=TAVILY_RAW_DESCRIPTION,
)
async def web_search_raw(context: ToolContext, queries: list[str]) -> str:
    """Return raw, source-bound results without per-page summarization."""
    config = context.context.tool_executor.tool_config(context)
    return await _tavily_search_raw(queries, config=config)


# Direct LangChain callers use the same functions and their existing signatures.
tavily_search = tool(
    "tavily_search", _tavily_search, description=TAVILY_SEARCH_DESCRIPTION
)
tavily_search_raw = tool(
    "tavily_search_raw", _tavily_search_raw, description=TAVILY_RAW_DESCRIPTION
)


# 并发发送多条 Tavily 查询，并应用结果数量、主题等搜索参数。
async def tavily_search_async(
    search_queries,
    max_results: int = 5,
    topic: Literal["general", "news", "finance"] = "general",
    include_raw_content: bool = True,
    config: RunnableConfig = None,
):
    """Execute multiple Tavily search queries asynchronously.

    Args:
        search_queries: List of search query strings to execute
        max_results: Maximum number of results per query
        topic: Topic category for filtering results
        include_raw_content: Whether to include full webpage content
        config: Runtime configuration for API key access

    Returns:
        List of search result dictionaries from Tavily API
    """
    # Initialize the Tavily client with API key from config
    tavily_client = AsyncTavilyClient(api_key=get_tavily_api_key(config))

    # Create search tasks for parallel execution
    search_tasks = [
        tavily_client.search(
            query,
            max_results=max_results,
            include_raw_content=include_raw_content,
            topic=topic,
        )
        for query in search_queries
    ]

    # Execute all search queries in parallel and return results
    search_results = await asyncio.gather(*search_tasks)
    return search_results


# 在超时保护下调用摘要模型处理网页；普通失败降级为有界摘录，控制类异常继续传播。
async def summarize_webpage(
    model: BaseChatModel,
    webpage_content: str,
    *,
    model_name: str | None = None,
    fallback_content: str | None = None,
    max_excerpt_chars: int = MAX_DEGRADED_EXCERPT_CHARS,
) -> str:
    """Summarize webpage content using AI model with timeout protection.

    Args:
        model: The chat model configured for summarization
        webpage_content: Raw webpage content to be summarized
        model_name: Optional model name for LangSmith correlation metadata
        fallback_content: Short Tavily snippet (preferred) or raw page text
        max_excerpt_chars: Bound for the degraded excerpt returned on failure

    Returns:
        Formatted model summary, or a clearly marked bounded excerpt when the
        provider call fails for an ordinary reason. Budget, execution-control,
        cancellation and storage failures are never swallowed.
    """
    try:
        # Create prompt with current date context
        prompt_content = summarize_webpage_prompt.format(
            webpage_content=webpage_content, date=get_today_str()
        )

        # Execute summarization with timeout to prevent hanging
        response, _budget = await asyncio.wait_for(
            ainvoke_model_with_budget(
                model,
                [HumanMessage(content=prompt_content)],
                model_name=model_name,
                structured_output=True,
                component="webpage_summarization",
            ),
            timeout=60.0,  # 60 second timeout for summarization
        )
        summary = response["parsed"]

        # Format the summary with structured sections
        formatted_summary = (
            f"<summary>\n{summary.summary}\n</summary>\n\n"
            f"<key_excerpts>\n{summary.key_excerpts}\n</key_excerpts>"
        )

        return formatted_summary

    except asyncio.TimeoutError:
        # A timed-out page degrades to a bounded excerpt; it is never retried
        # with the full raw page as if it were a successful summary.
        logging.warning(
            "Webpage summarization timed out; using a bounded source excerpt."
        )
        return _bounded_fallback(
            fallback_content,
            max_excerpt_chars,
            reason="timed out",
        )
    except Exception as exc:
        if _is_execution_control_failure(exc):
            raise
        # Do not put provider messages, stack traces or request parameters into
        # the model context; only the exception type reaches the log.
        logging.warning(
            "Webpage summarization failed (%s); using a bounded source excerpt.",
            type(exc).__name__,
        )
        return _bounded_fallback(
            fallback_content,
            max_excerpt_chars,
            reason="failed",
        )


##########################
# Reflection Tool Utils
##########################


# 从运行配置或环境变量中解析 Tavily API 密钥。
def get_tavily_api_key(config: RunnableConfig):
    """Get Tavily API key from environment or config."""
    should_get_from_config = os.getenv("GET_API_KEYS_FROM_CONFIG", "false")
    if should_get_from_config.lower() == "true":
        api_keys = config.get("configurable", {}).get("apiKeys", {})
        if not api_keys:
            return None
        return api_keys.get("TAVILY_API_KEY")
    else:
        return os.getenv("TAVILY_API_KEY")
