# 将社交媒体 MCP 原语包装为 LangChain 工具，具体调查顺序由模型决定。
"""Thin LangChain @tool wrappers around social_media_mcp functions.

Each @tool exposes one MCP primitive directly to the LLM — no orchestration,
no business logic.  The LLM controls the decision flow in its own ReAct loop.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from open_deep_research import social_media_mcp


# 将工具结果序列化为保留中文的格式化 JSON。
def _to_json(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


# ── search ──────────────────────────────────────────────────────────────

# 按关键词、平台、时间和游标检索帖子，并返回统一 JSON 结果。
@tool(description="Search social media posts across platforms by keyword, date range, and cursor pagination.")
def search_posts(
    query: str,
    platforms: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> str:
    return _to_json(social_media_mcp.search_posts(
        query=query, platforms=platforms,
        start_date=start_date, end_date=end_date,
        limit=limit, cursor=cursor,
    ))


# ── thread & comments ───────────────────────────────────────────────────

# 按帖子 ID 获取完整讨论串并序列化输出。
@tool(description="Fetch the full thread/conversation for a social media post by post ID.")
def fetch_thread(post_id: str) -> str:
    return _to_json(social_media_mcp.fetch_thread(post_id))


# 按帖子 ID 和数量限制读取评论。
@tool(description="Fetch comments for a social media post by post ID.")
def fetch_comments(post_id: str, limit: int = 20) -> str:
    return _to_json(social_media_mcp.fetch_comments(post_id, limit=limit))


# ── author ──────────────────────────────────────────────────────────────

# 查询作者资料及相关发帖历史。
@tool(description="Fetch the profile and post history for a social media author.")
def fetch_author_profile(author_id: str) -> str:
    return _to_json(social_media_mcp.fetch_author_profile(author_id))


# ── complaints ──────────────────────────────────────────────────────────

# 检索退款、质量和售后等投诉信号，并应用平台及时间筛选。
@tool(description="Search social media posts for complaint-like signals (refunds, quality, after-sales, etc.).")
def search_complaints(
    query: str,
    platforms: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 20,
) -> str:
    return _to_json(social_media_mcp.search_complaints(
        query=query, platforms=platforms,
        start_date=start_date, end_date=end_date,
        limit=limit,
    ))


# ── trending & snapshot ─────────────────────────────────────────────────

# 汇总查询相关帖子的热门词及话题。
@tool(description="Get trending keywords and topics for a query from social media data.")
def get_trending_keywords(
    query: str,
    platforms: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
) -> str:
    return _to_json(social_media_mcp.get_trending_keywords(
        query=query, platforms=platforms,
        start_date=start_date, end_date=end_date,
        limit=limit,
    ))


# 获取包含风险、情绪、关键词和代表帖子的舆情快照。
@tool(description="Get a pre-computed public opinion snapshot with risk level, sentiment, top keywords, and representative posts.")
def get_public_opinion_snapshot(
    query: str,
    platforms: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 200,
) -> str:
    return _to_json(social_media_mcp.get_public_opinion_snapshot(
        query=query, platforms=platforms,
        start_date=start_date, end_date=end_date,
        limit=limit,
    ))


# ── tool set ────────────────────────────────────────────────────────────

SOCIAL_MEDIA_TOOL_NAMES: frozenset[str] = frozenset({
    "search_posts",
    "fetch_thread",
    "fetch_comments",
    "fetch_author_profile",
    "search_complaints",
    "get_trending_keywords",
    "get_public_opinion_snapshot",
})


# 返回可供角色权限过滤的全部社交媒体工具。
def get_social_media_tools() -> list:
    """Return all social media LangChain tools."""
    return [
        search_posts,
        fetch_thread,
        fetch_comments,
        fetch_author_profile,
        search_complaints,
        get_trending_keywords,
        get_public_opinion_snapshot,
    ]
