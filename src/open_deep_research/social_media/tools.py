"""Expose the existing social media queries as SDK function tools."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from typing import Annotated, Any

from agents import function_tool
from agents.tool_context import ToolContext
from pydantic import BeforeValidator, StrictInt

from open_deep_research import social_media_mcp
from open_deep_research.tools import BusinessTool, ToolEffect, register_tool


def _integral_limit(value: Any) -> Any:
    # JSON Schema integer also accepts numeric values such as 20.0.
    return int(value) if isinstance(value, float) and value.is_integer() else value


SocialLimit = Annotated[StrictInt, BeforeValidator(_integral_limit)]


# 将工具结果序列化为保留中文的格式化 JSON。
def _to_json(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


# 调用同步社媒适配器并保留原有 JSON 输出格式，由 ToolExecutor 放入线程执行。
def _serialized(function: Callable[..., dict[str, Any]], /, **arguments: Any) -> str:
    return _to_json(function(**arguments))


# ── search ──────────────────────────────────────────────────────────────


# 按关键词、平台、时间和游标检索帖子，并返回统一 JSON 结果。
@function_tool(
    description_override="Search social media posts across platforms by keyword, date range, and cursor pagination.",
    use_docstring_info=False,
    strict_mode=False,
    failure_error_function=None,
)
async def search_posts(
    context: ToolContext,
    query: str,
    platforms: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: SocialLimit = 20,
    cursor: str | None = None,
) -> str:
    return await context.context.tool_executor.execute(
        context,
        partial(
            _serialized,
            social_media_mcp.search_posts,
            query=query,
            platforms=platforms,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            cursor=cursor,
        ),
    )


# ── thread & comments ───────────────────────────────────────────────────


# 按帖子 ID 获取完整讨论串并序列化输出。
@function_tool(
    description_override="Fetch the full thread/conversation for a social media post by post ID.",
    use_docstring_info=False,
    strict_mode=False,
    failure_error_function=None,
)
async def fetch_thread(context: ToolContext, post_id: str) -> str:
    return await context.context.tool_executor.execute(
        context, partial(_serialized, social_media_mcp.fetch_thread, post_id=post_id)
    )


# 按帖子 ID 和数量限制读取评论。
@function_tool(
    description_override="Fetch comments for a social media post by post ID.",
    use_docstring_info=False,
    strict_mode=False,
    failure_error_function=None,
)
async def fetch_comments(
    context: ToolContext, post_id: str, limit: SocialLimit = 20
) -> str:
    return await context.context.tool_executor.execute(
        context,
        partial(
            _serialized, social_media_mcp.fetch_comments, post_id=post_id, limit=limit
        ),
    )


# ── author ──────────────────────────────────────────────────────────────


# 查询作者资料及相关发帖历史。
@function_tool(
    description_override="Fetch the profile and post history for a social media author.",
    use_docstring_info=False,
    strict_mode=False,
    failure_error_function=None,
)
async def fetch_author_profile(context: ToolContext, author_id: str) -> str:
    return await context.context.tool_executor.execute(
        context,
        partial(_serialized, social_media_mcp.fetch_author_profile, author_id=author_id),
    )


# ── complaints ──────────────────────────────────────────────────────────


# 检索退款、质量和售后等投诉信号，并应用平台及时间筛选。
@function_tool(
    description_override="Search social media posts for complaint-like signals (refunds, quality, after-sales, etc.).",
    use_docstring_info=False,
    strict_mode=False,
    failure_error_function=None,
)
async def search_complaints(
    context: ToolContext,
    query: str,
    platforms: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: SocialLimit = 20,
) -> str:
    return await context.context.tool_executor.execute(
        context,
        partial(
            _serialized,
            social_media_mcp.search_complaints,
            query=query,
            platforms=platforms,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        ),
    )


# ── trending & snapshot ─────────────────────────────────────────────────


# 汇总查询相关帖子的热门词及话题。
@function_tool(
    description_override="Get trending keywords and topics for a query from social media data.",
    use_docstring_info=False,
    strict_mode=False,
    failure_error_function=None,
)
async def get_trending_keywords(
    context: ToolContext,
    query: str,
    platforms: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: SocialLimit = 50,
) -> str:
    return await context.context.tool_executor.execute(
        context,
        partial(
            _serialized,
            social_media_mcp.get_trending_keywords,
            query=query,
            platforms=platforms,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        ),
    )


# 获取包含风险、情绪、关键词和代表帖子的舆情快照。
@function_tool(
    description_override="Get a pre-computed public opinion snapshot with risk level, sentiment, top keywords, and representative posts.",
    use_docstring_info=False,
    strict_mode=False,
    failure_error_function=None,
)
async def get_public_opinion_snapshot(
    context: ToolContext,
    query: str,
    platforms: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: SocialLimit = 200,
) -> str:
    return await context.context.tool_executor.execute(
        context,
        partial(
            _serialized,
            social_media_mcp.get_public_opinion_snapshot,
            query=query,
            platforms=platforms,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        ),
    )


# 同步适配器在取消后仍会完成当前调用，故声明为需要排空线程与回执。
search_posts = register_tool(
    search_posts, domain="social_media", drain_on_cancel=True
)
fetch_thread = register_tool(
    fetch_thread, domain="social_media", drain_on_cancel=True
)
fetch_comments = register_tool(
    fetch_comments, domain="social_media", drain_on_cancel=True
)
fetch_author_profile = register_tool(
    fetch_author_profile, domain="social_media", drain_on_cancel=True
)
search_complaints = register_tool(
    search_complaints, domain="social_media", drain_on_cancel=True
)
get_trending_keywords = register_tool(
    get_trending_keywords, domain="social_media", drain_on_cancel=True
)
get_public_opinion_snapshot = register_tool(
    get_public_opinion_snapshot, domain="social_media", drain_on_cancel=True
)


# ── tool set ────────────────────────────────────────────────────────────

SOCIAL_MEDIA_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "search_posts",
        "fetch_thread",
        "fetch_comments",
        "fetch_author_profile",
        "search_complaints",
        "get_trending_keywords",
        "get_public_opinion_snapshot",
    }
)


# 返回可供角色权限过滤的全部社交媒体工具。
def get_social_media_tools() -> list[BusinessTool]:
    """Apply the social backend effect policy to the shared SDK declarations."""
    tools = [
        search_posts,
        fetch_thread,
        fetch_comments,
        fetch_author_profile,
        search_complaints,
        get_trending_keywords,
        get_public_opinion_snapshot,
    ]

    # The HTTP backend may start a paid Apify Actor even though its route
    # uses GET. Local records require no external effect protection.
    effect: ToolEffect = (
        "protected"
        if social_media_mcp._api_base_url(social_media_mcp._social_config(None))
        else "read"
    )
    return [replace(item, effect=effect) for item in tools]
