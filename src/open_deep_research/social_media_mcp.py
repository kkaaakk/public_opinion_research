# 提供社交媒体 MCP 工具，可读取本地数据或调用配置的 HTTP 适配服务。
"""Low-level MCP server and local adapters for social media data sources."""

import argparse
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from open_deep_research.social_media.domain import (
    COMPLAINT_TERMS,
    DEFAULT_RISK_RULES,
    NEGATIVE_TERMS,
    POSITIVE_TERMS,
    matched_signal_terms,
    negative_query_variants,
    risk_level,
    risk_score,
)
from open_deep_research.social_media.domain import contains_any as _contains_any
from open_deep_research.social_media.domain import heat_level as _heat_level
from open_deep_research.social_media.domain import ratio as _ratio
from open_deep_research.social_media.records import parse_date as _parse_date
from open_deep_research.social_media.records import parse_datetime

load_dotenv()

MCP_SERVER_NAME = "open-deep-research-social-media"
DEFAULT_DATA_PATHS = ["data/social_media/sample_posts.jsonl"]
DEFAULT_LIMIT = 20
MAX_LIMIT = 200

mcp = FastMCP(
    MCP_SERVER_NAME,
    instructions=(
        "Expose low-level social media tools for platform search, threads, "
        "comments, authors, media extraction, normalization, persistence, and "
        "deduplication. Higher-level business agents should normally call the "
        "social_media_skill tools that compose these primitives."
    ),
)


# 提供 X/Twitter 兼容搜索入口，返回标准帖子。
@mcp.tool(
    name="search_x_posts",
    description="Search X/Twitter-like posts from configured social media data sources.",
)
def search_x_posts(
    query: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Search X/Twitter-like posts."""
    raw_config = _social_config(config)
    platforms = raw_config.get("x_platforms") or ["x", "twitter"]
    return search_social_posts(
        query=query,
        platforms=platforms,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        cursor=cursor,
        config=raw_config,
    )


# 按平台、关键词、时间范围和游标搜索通用社交帖子。
@mcp.tool(
    name="search_posts",
    description=(
        "Search normalized public social/forum/complaint posts across configured "
        "platforms. Prefer this platform-neutral tool over platform-specific search tools."
    ),
)
def search_posts(
    query: str,
    platforms: list[str] | str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Search platform-neutral social media posts."""
    return search_social_posts(
        query=query,
        platforms=platforms,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        cursor=cursor,
        config=config,
    )


# 提供 X/Twitter 兼容入口，按根帖子读取完整讨论串。
@mcp.tool(
    name="fetch_x_thread",
    description="Fetch a thread rooted at a post id, including replies and comments when available.",
)
def fetch_x_thread(
    post_id: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch a normalized X/Twitter-like thread by root post id."""
    try:
        raw_config = _social_config(config)
        records = _load_records(raw_config)
        root = _record_by_id(records, post_id)
        if root is None:
            return {
                "ok": False,
                "operation": "fetch_x_thread",
                "error": f"Post not found: {post_id}",
                "source": _source_summary(raw_config),
            }
        thread_id = str(root.get("thread_id") or root.get("id") or post_id)
        thread_records = [
            record
            for record in records
            if str(record.get("thread_id") or record.get("id") or "") == thread_id
            or str(record.get("parent_id") or "") == post_id
            or str((record.get("metadata") or {}).get("parent_id") or "") == post_id
        ]
        if root not in thread_records:
            thread_records.insert(0, root)
        thread_records = sorted(
            _dedupe_records(thread_records),
            key=lambda record: (_record_datetime(record) or datetime.min),
        )
        return {
            "ok": True,
            "root_post": _post_payload(root, include_metadata=True),
            "thread_id": thread_id,
            "post_count": len(thread_records),
            "posts": [_post_payload(record, include_metadata=True) for record in thread_records],
            "source": _source_summary(raw_config),
        }
    except Exception as exc:
        return _error_payload("fetch_x_thread", exc)


# 按根帖子 ID 获取标准化讨论线程。
@mcp.tool(
    name="fetch_thread",
    description="Fetch a platform-neutral discussion thread rooted at a post id.",
)
def fetch_thread(
    post_id: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch a normalized social discussion thread by root post id."""
    thread = fetch_x_thread(post_id=post_id, config=config)
    if isinstance(thread, dict):
        thread["operation"] = "fetch_thread"
    return thread


# 读取指定帖子的评论并应用数量上限。
@mcp.tool(
    name="fetch_comments",
    description="Fetch comments or replies for a post id.",
)
def fetch_comments(
    post_id: str,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch comments for a normalized social post."""
    try:
        raw_config = _social_config(config)
        records = [
            record
            for record in _load_records(raw_config)
            if str(record.get("parent_id") or "") == post_id
            or str((record.get("metadata") or {}).get("parent_id") or "") == post_id
            or str(record.get("type") or "") in {"comment", "reply"}
            and str(record.get("thread_id") or "") == post_id
        ]
        records = sorted(records, key=lambda record: (_record_datetime(record) or datetime.min))
        page_records, next_cursor = _paginate(records, _bounded_limit(limit), cursor)
        return {
            "ok": True,
            "post_id": post_id,
            "returned_count": len(page_records),
            "total_matches": len(records),
            "next_cursor": next_cursor,
            "comments": [_post_payload(record, include_metadata=True) for record in page_records],
            "source": _source_summary(raw_config),
        }
    except Exception as exc:
        return _error_payload("fetch_comments", exc)


# 根据已有帖子生成轻量作者资料和发帖概况。
@mcp.tool(
    name="fetch_author_profile",
    description="Fetch an author profile summary from configured social records.",
)
def fetch_author_profile(
    author_id: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch a lightweight author profile summary."""
    try:
        raw_config = _social_config(config)
        records = [
            record
            for record in _load_records(raw_config)
            if str(record.get("author_id") or record.get("author") or "").lower()
            == str(author_id).lower()
        ]
        if not records:
            return {
                "ok": False,
                "operation": "fetch_author_profile",
                "error": f"Author not found: {author_id}",
                "source": _source_summary(raw_config),
            }
        platform_counts = Counter(str(record.get("platform") or "unknown") for record in records)
        sentiment_counts = Counter(_sentiment(record) for record in records)
        return {
            "ok": True,
            "author_id": author_id,
            "display_name": records[0].get("author"),
            "platform_counts": dict(platform_counts),
            "sentiment_counts": dict(sentiment_counts),
            "post_count": len(records),
            "total_engagement": sum(_engagement_score(record) for record in records),
            "recent_posts": [_post_payload(record) for record in records[:5]],
            "source": _source_summary(raw_config),
        }
    except Exception as exc:
        return _error_payload("fetch_author_profile", exc)


# 从帖子媒体中提取图片类内容。
@mcp.tool(
    name="extract_images",
    description="Extract image/media URLs and captions from a post record.",
)
def extract_images(
    post_id: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract image-like media from one post."""
    try:
        raw_config = _social_config(config)
        record = _record_by_id(_load_records(raw_config), post_id)
        if record is None:
            return {
                "ok": False,
                "operation": "extract_images",
                "error": f"Post not found: {post_id}",
                "source": _source_summary(raw_config),
            }
        media = record.get("images") or record.get("media") or (record.get("metadata") or {}).get("images") or []
        if isinstance(media, str):
            media = [{"url": media}]
        normalized_media = [
            item if isinstance(item, dict) else {"url": str(item)}
            for item in media
        ]
        return {
            "ok": True,
            "post_id": post_id,
            "image_count": len(normalized_media),
            "images": normalized_media,
            "source": _source_summary(raw_config),
        }
    except Exception as exc:
        return _error_payload("extract_images", exc)


# 提供平台无关的帖子图片媒体提取入口。
@mcp.tool(
    name="extract_post_media",
    description="Extract image/media URLs and captions from a normalized post id.",
)
def extract_post_media(
    post_id: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract image-like media from one platform-neutral post."""
    media = extract_images(post_id=post_id, config=config)
    if isinstance(media, dict):
        media["operation"] = "extract_post_media"
    return media


# 把原始平台帖子转换为统一结构。
@mcp.tool(
    name="normalize_post",
    description="Normalize one raw platform post into the standard social media schema.",
)
def normalize_post(raw_post: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw platform post."""
    try:
        normalized = _normalize_record(raw_post)
        return {
            "ok": True,
            "post": _post_payload(normalized, include_metadata=True),
            "dedupe_key": _record_key(normalized),
        }
    except Exception as exc:
        return _error_payload("normalize_post", exc)


# 将一个标准帖子追加到本地 JSONL 存储。
@mcp.tool(
    name="save_to_database",
    description="Persist one normalized post to the configured JSONL store. Dry-run by default.",
)
def save_to_database(
    post: dict[str, Any],
    dry_run: bool = True,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one normalized post to a local JSONL sink."""
    try:
        raw_config = _social_config(config)
        normalized = _normalize_record(post)
        duplicate = check_duplicate(normalized, config=raw_config)
        write_path = Path(
            raw_config.get("write_path")
            or os.getenv("SOCIAL_MEDIA_WRITE_PATH")
            or "data/social_media/saved_posts.jsonl"
        )
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "would_write_path": str(write_path),
                "duplicate": duplicate,
                "post": _post_payload(normalized, include_metadata=True),
            }
        if duplicate.get("is_duplicate"):
            return {
                "ok": True,
                "saved": False,
                "duplicate": duplicate,
                "write_path": str(write_path),
            }
        write_path.parent.mkdir(parents=True, exist_ok=True)
        with write_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(normalized, ensure_ascii=False) + "\n")
        return {
            "ok": True,
            "saved": True,
            "duplicate": duplicate,
            "write_path": str(write_path),
            "post": _post_payload(normalized, include_metadata=True),
        }
    except Exception as exc:
        return _error_payload("save_to_database", exc)


# 提供通用社交证据持久化入口。
@mcp.tool(
    name="save_social_evidence",
    description="Persist one normalized social evidence record to the configured JSONL store. Dry-run by default.",
)
def save_social_evidence(
    post: dict[str, Any],
    dry_run: bool = True,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one normalized social evidence record."""
    result = save_to_database(post=post, dry_run=dry_run, config=config)
    if isinstance(result, dict):
        result["operation"] = "save_social_evidence"
    return result


# 按 ID、URL 或内容指纹检查证据是否已存在。
@mcp.tool(
    name="check_duplicate",
    description="Check whether a normalized post already exists in configured local sources.",
)
def check_duplicate(
    post: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check duplicate status by id, URL, or content fingerprint."""
    try:
        raw_config = _social_config(config)
        normalized = _normalize_record(post)
        candidate_key = _record_key(normalized)
        for record in _load_records(raw_config):
            if _record_key(record) == candidate_key:
                return {
                    "ok": True,
                    "is_duplicate": True,
                    "dedupe_key": candidate_key,
                    "matched_post": _post_payload(record),
                }
        return {
            "ok": True,
            "is_duplicate": False,
            "dedupe_key": candidate_key,
        }
    except Exception as exc:
        return _error_payload("check_duplicate", exc)


# 按配置来源查询舆情帖子，兼容 HTTP API 和本地文件。
@mcp.tool(
    name="search_social_posts",
    description=(
        "Search normalized public social/forum posts across configured platforms. "
        "Returns matched posts with platform, author, time, sentiment, engagement, "
        "URL, tags, and source metadata."
    ),
)
def search_social_posts(
    query: str,
    platforms: list[str] | str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Search configured public-opinion posts."""
    try:
        raw_config = _raw_config_dict(config)
        if _api_base_url(raw_config):
            return _api_get(
                "search_social_posts",
                {
                    "query": query,
                    "platforms": _platforms_csv(platforms),
                    "start_date": start_date,
                    "end_date": end_date,
                    "limit": _bounded_limit(limit),
                    "cursor": cursor,
                },
                raw_config,
            )

        records = _filtered_records(
            query=query,
            platforms=platforms,
            start_date=start_date,
            end_date=end_date,
            config=raw_config,
        )
        bounded_limit = _bounded_limit(limit)
        page_records, next_cursor = _paginate(records, bounded_limit, cursor)
        return {
            "ok": True,
            "query": query,
            "returned_count": len(page_records),
            "total_matches": len(records),
            "next_cursor": next_cursor,
            "truncated": bool(next_cursor),
            "posts": [_post_payload(record) for record in page_records],
            "source": _source_summary(raw_config),
        }
    except Exception as exc:
        return _error_payload("search_social_posts", exc)


# 聚合检索范围内的情绪、数量和趋势统计。
@mcp.tool(
    name="aggregate_public_sentiment",
    description=(
        "Aggregate public-opinion volume, sentiment, platform distribution, daily "
        "trend, top tags, and representative posts for a query."
    ),
)
def aggregate_public_sentiment(
    query: str,
    platforms: list[str] | str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = MAX_LIMIT,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate public sentiment from configured public-opinion data."""
    try:
        raw_config = _raw_config_dict(config)
        if _api_base_url(raw_config):
            return _api_get(
                "aggregate_public_sentiment",
                {
                    "query": query,
                    "platforms": _platforms_csv(platforms),
                    "start_date": start_date,
                    "end_date": end_date,
                    "limit": _bounded_limit(limit),
                },
                raw_config,
            )

        records = _filtered_records(
            query=query,
            platforms=platforms,
            start_date=start_date,
            end_date=end_date,
            config=raw_config,
        )[:_bounded_limit(limit)]
        sentiment_counts = Counter(_sentiment(record) for record in records)
        platform_counts = Counter(str(record.get("platform") or "unknown") for record in records)
        complaint_count = sum(1 for record in records if _is_complaint(record))
        daily_counts: dict[str, int] = defaultdict(int)
        engagement_total = 0
        negative_engagement = 0
        for record in records:
            day = _date_key(record)
            if day:
                daily_counts[day] += 1
            engagement_score = _engagement_score(record)
            engagement_total += engagement_score
            if _sentiment(record) == "negative":
                negative_engagement += engagement_score

        representative_posts = sorted(
            records,
            key=_engagement_score,
            reverse=True,
        )[:5]
        risk_metrics = _risk_metrics(
            records=records,
            sentiment_counts=sentiment_counts,
            total_engagement=engagement_total,
            complaint_count=complaint_count,
            config=raw_config,
        )
        return {
            "ok": True,
            "query": query,
            "matched_count": len(records),
            "sentiment_counts": dict(sentiment_counts),
            "negative_ratio": _ratio(sentiment_counts.get("negative", 0), len(records)),
            "platform_counts": dict(platform_counts),
            "daily_counts": dict(sorted(daily_counts.items())),
            "top_keywords": _top_keywords(records),
            "total_engagement": engagement_total,
            "negative_engagement": negative_engagement,
            "complaint_count": complaint_count,
            "risk": risk_metrics,
            "representative_posts": [_post_payload(record) for record in representative_posts],
            "source": _source_summary(raw_config),
        }
    except Exception as exc:
        return _error_payload("aggregate_public_sentiment", exc)


# 按记录 ID 获取单条舆情材料。
@mcp.tool(
    name="get_post_detail",
    description="Get one normalized post, complaint, or discussion item by id.",
)
def get_post_detail(
    post_id: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Get a single public-opinion record by id."""
    try:
        raw_config = _raw_config_dict(config)
        if _api_base_url(raw_config):
            return _api_get("get_post_detail", {"post_id": post_id}, raw_config)

        for record in _load_records(raw_config):
            if str(record.get("id") or record.get("post_id") or "") == str(post_id):
                return {
                    "ok": True,
                    "post": _post_payload(record, include_metadata=True),
                    "source": _source_summary(raw_config),
                }
        return {
            "ok": False,
            "operation": "get_post_detail",
            "error": f"Post not found: {post_id}",
            "source": _source_summary(raw_config),
        }
    except Exception as exc:
        return _error_payload("get_post_detail", exc)


# 查询投诉类舆情记录，必要时扩展负面关键词。
@mcp.tool(
    name="search_complaints",
    description=(
        "Search public or internal complaint records. Use this for consumer-rights, "
        "quality, service, safety, refund, delivery, or after-sales complaint signals."
    ),
)
def search_complaints(
    query: str,
    platforms: list[str] | str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Search complaint-like public-opinion records."""
    try:
        raw_config = _raw_config_dict(config)
        if _api_base_url(raw_config):
            return _api_get(
                "search_complaints",
                {
                    "query": query,
                    "platforms": _platforms_csv(platforms),
                    "start_date": start_date,
                    "end_date": end_date,
                    "limit": _bounded_limit(limit),
                    "cursor": cursor,
                    "negative_query_expansion": True,
                    "negative_query_variant_count": _negative_query_variant_count(raw_config),
                },
                raw_config,
            )

        records = []
        query_variants = _negative_query_variants(query, raw_config)
        for query_variant in query_variants:
            for record in _filtered_records(
                query=query_variant,
                platforms=platforms,
                start_date=start_date,
                end_date=end_date,
                config=raw_config,
            ):
                if _is_negative_evidence(record):
                    enriched = dict(record)
                    enriched["matched_query"] = query_variant
                    enriched["negative_signals"] = _matched_signal_terms(enriched)
                    records.append(enriched)
        records = _dedupe_records(records)
        bounded_limit = _bounded_limit(limit)
        page_records, next_cursor = _paginate(records, bounded_limit, cursor)
        return {
            "ok": True,
            "query": query,
            "query_variants": query_variants,
            "returned_count": len(page_records),
            "total_matches": len(records),
            "next_cursor": next_cursor,
            "truncated": bool(next_cursor),
            "complaints": [_post_payload(record) for record in page_records],
            "source": _source_summary(raw_config),
        }
    except Exception as exc:
        return _error_payload("search_complaints", exc)


# 汇总检索记录中的热门词和相关话题。
@mcp.tool(
    name="get_trending_keywords",
    description=(
        "Return related hot words, complaint themes, negative keywords, and platform "
        "terms from configured public-opinion records."
    ),
)
def get_trending_keywords(
    query: str,
    platforms: list[str] | str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = MAX_LIMIT,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Get top related terms from configured public-opinion records."""
    try:
        raw_config = _raw_config_dict(config)
        if _api_base_url(raw_config):
            return _api_get(
                "get_trending_keywords",
                {
                    "query": query,
                    "platforms": _platforms_csv(platforms),
                    "start_date": start_date,
                    "end_date": end_date,
                    "limit": _bounded_limit(limit),
                },
                raw_config,
            )

        records = _filtered_records(
            query=query,
            platforms=platforms,
            start_date=start_date,
            end_date=end_date,
            config=raw_config,
        )[:_bounded_limit(limit)]
        return {
            "ok": True,
            "query": query,
            "matched_count": len(records),
            "top_keywords": _top_keywords(records, top_n=30),
            "negative_keywords": _top_keywords(
                [record for record in records if _sentiment(record) == "negative"],
                top_n=20,
            ),
            "source": _source_summary(raw_config),
        }
    except Exception as exc:
        return _error_payload("get_trending_keywords", exc)


# 构造包含风险、情绪、热度和代表性记录的精简监测快照。
@mcp.tool(
    name="get_public_opinion_snapshot",
    description=(
        "Return a decision-ready public-opinion snapshot including matched volume, "
        "sentiment, complaints, risk level, top keywords, and representative posts."
    ),
)
def get_public_opinion_snapshot(
    query: str,
    platforms: list[str] | str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = MAX_LIMIT,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a compact monitoring snapshot for a public-opinion query."""
    try:
        raw_config = _raw_config_dict(config)
        if _api_base_url(raw_config):
            return _api_get(
                "get_public_opinion_snapshot",
                {
                    "query": query,
                    "platforms": _platforms_csv(platforms),
                    "start_date": start_date,
                    "end_date": end_date,
                    "limit": _bounded_limit(limit),
                },
                raw_config,
            )

        # Build snapshot from internal helpers (not from other @mcp.tool functions)
        records = _filtered_records(
            query=query,
            platforms=platforms,
            start_date=start_date,
            end_date=end_date,
            config=raw_config,
        )[:_bounded_limit(limit)]

        sentiment_counts = Counter(_sentiment(r) for r in records)
        platform_counts = Counter(str(r.get("platform") or "unknown") for r in records)
        complaint_count = sum(1 for r in records if _is_complaint(r))
        daily_counts: dict[str, int] = defaultdict(int)
        engagement_total = 0
        negative_engagement = 0
        for r in records:
            day = _date_key(r)
            if day:
                daily_counts[day] += 1
            score = _engagement_score(r)
            engagement_total += score
            if _sentiment(r) == "negative":
                negative_engagement += score

        risk = _risk_metrics(
            records=records,
            sentiment_counts=sentiment_counts,
            total_engagement=engagement_total,
            complaint_count=complaint_count,
            config=raw_config,
        )
        negative_ratio_val = _ratio(sentiment_counts.get("negative", 0), len(records))

        # Complaint records (filtered, bounded to 5)
        complaint_records = [
            r for r in records
            if _is_complaint(r) or _sentiment(r) == "negative"
        ][:5]

        # Trending keywords from records
        keywords_all = _top_keywords(records)
        negative_keywords = _top_keywords([
            r for r in records
            if _sentiment(r) == "negative" or _is_complaint(r)
        ])

        representative = sorted(records, key=_engagement_score, reverse=True)[:5]

        return {
            "ok": True,
            "query": query,
            "summary": {
                "matched_count": len(records),
                "risk_level": risk.get("level", "low"),
                "risk_score": risk.get("score", 0),
                "heat_level": risk.get("heat_level", "low"),
                "negative_ratio": negative_ratio_val,
                "complaint_count": complaint_count,
                "total_engagement": engagement_total,
                "negative_engagement": negative_engagement,
            },
            "sentiment_counts": dict(sentiment_counts),
            "platform_counts": dict(platform_counts),
            "daily_counts": dict(sorted(daily_counts.items())),
            "top_keywords": keywords_all,
            "negative_keywords": negative_keywords,
            "representative_posts": [_post_payload(r) for r in representative],
            "representative_complaints": [_post_payload(r) for r in complaint_records],
            "source": _source_summary(raw_config),
        }
    except Exception as exc:
        return _error_payload("get_public_opinion_snapshot", exc)


# 兼容配置模型和映射，转换为可处理字典。
def _raw_config_dict(config: Mapping[str, Any] | Any | None) -> dict[str, Any]:
    if config is None:
        raw: dict[str, Any] = {}
    elif hasattr(config, "model_dump"):
        raw = dict(config.model_dump(exclude_none=True))
    else:
        raw = dict(config)
    configurable = raw.get("configurable")
    if isinstance(configurable, Mapping):
        raw = dict(configurable)
    return {key: value for key, value in raw.items() if value is not None}


# 合并显式配置和兼容环境变量，确定文件、HTTP 来源及风险规则。
def _social_config(config: Mapping[str, Any] | Any | None) -> dict[str, Any]:
    raw = _raw_config_dict(config)
    if "data_paths" not in raw:
        data_paths = (
            raw.get("social_media_data_paths")
            or os.getenv("SOCIAL_MEDIA_DATA_PATHS")
            or os.getenv("PUBLIC_OPINION_DATA_PATHS")
        )
        if data_paths:
            raw["data_paths"] = data_paths
    if "api_base_url" not in raw and "data_paths" not in raw:
        api_base_url = (
            raw.get("social_media_api_base_url")
            or os.getenv("SOCIAL_MEDIA_API_BASE_URL")
            or os.getenv("PUBLIC_OPINION_API_BASE_URL")
        )
        if api_base_url:
            raw["api_base_url"] = api_base_url
    if "api_key" not in raw:
        api_key = (
            raw.get("social_media_api_key")
            or os.getenv("SOCIAL_MEDIA_API_KEY")
            or os.getenv("PUBLIC_OPINION_API_KEY")
        )
        if api_key:
            raw["api_key"] = api_key
    if "entity_aliases" not in raw:
        aliases = (
            raw.get("social_media_entity_aliases")
            or os.getenv("SOCIAL_MEDIA_ENTITY_ALIASES")
            or os.getenv("PUBLIC_OPINION_ENTITY_ALIASES")
        )
        if aliases:
            raw["entity_aliases"] = aliases
    if "risk_rules" not in raw:
        risk_rules = (
            raw.get("social_media_risk_rules")
            or os.getenv("SOCIAL_MEDIA_RISK_RULES")
            or os.getenv("PUBLIC_OPINION_RISK_RULES")
        )
        if risk_rules:
            raw["risk_rules"] = risk_rules
    return raw


# 按配置、环境变量、默认值的顺序读取单项设置。
def _config_value(config: Mapping[str, Any], key: str, env_name: str, default: Any = None) -> Any:
    return config.get(key) or os.getenv(env_name) or default


# 解析并规范化社交媒体 HTTP API 根地址。
def _api_base_url(config: Mapping[str, Any]) -> str:
    if "api_base_url" in config:
        return str(config.get("api_base_url") or "").rstrip("/")
    if config.get("data_paths") or config.get("social_media_data_paths"):
        return ""
    return str(
        os.getenv("SOCIAL_MEDIA_API_BASE_URL")
        or os.getenv("PUBLIC_OPINION_API_BASE_URL")
        or ""
    ).rstrip("/")


# 发送配置认证信息的 HTTP GET 请求，并解析统一响应。
def _api_get(endpoint: str, params: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    base_url = _api_base_url(config)
    query_params = {
        key: value
        for key, value in params.items()
        if value not in (None, "", [])
    }
    url = f"{base_url.rstrip('/')}/{endpoint}?{urlencode(query_params, doseq=True)}"
    headers = {"Accept": "application/json"}
    api_key = (
        config.get("api_key")
        or os.getenv("SOCIAL_MEDIA_API_KEY")
        or os.getenv("PUBLIC_OPINION_API_KEY")
    )
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout = float(
        config.get("api_timeout")
        or os.getenv("SOCIAL_MEDIA_API_TIMEOUT")
        or os.getenv("PUBLIC_OPINION_API_TIMEOUT")
        or 20
    )
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, dict):
        return payload
    return {"ok": True, "data": payload}


# 遍历配置数据源加载并归一化舆情记录。
def _load_records(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for data_path in _data_paths(config):
        path = Path(data_path)
        if not path.exists():
            continue
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.suffix.lower() in {".json", ".jsonl"}:
                    records.extend(_load_records_from_file(child))
        elif path.suffix.lower() in {".json", ".jsonl"}:
            records.extend(_load_records_from_file(path))
    return _dedupe_records([_normalize_record(record) for record in records])


# 解析本地数据路径列表，兼容环境变量形式。
def _data_paths(config: Mapping[str, Any]) -> list[str]:
    configured_paths = _config_value(
        config,
        "data_paths",
        "SOCIAL_MEDIA_DATA_PATHS",
        os.getenv("PUBLIC_OPINION_DATA_PATHS") or DEFAULT_DATA_PATHS,
    )
    if isinstance(configured_paths, list):
        return [str(path).strip() for path in configured_paths if str(path).strip()]
    return [
        path.strip()
        for path in str(configured_paths).split(",")
        if path.strip()
    ]


# 从支持的文件格式读取原始记录。
def _load_records_from_file(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped:
                payload = json.loads(stripped)
                if isinstance(payload, dict):
                    records.append(payload)
        return records

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        return [record for record in payload["records"] if isinstance(record, dict)]
    return []


# 统一帖子身份、文本、来源、时间、情绪和互动字段。
def _normalize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(record)
    normalized["id"] = str(record.get("id") or record.get("post_id") or record.get("url") or "")
    normalized["platform"] = str(record.get("platform") or record.get("source") or "unknown")
    normalized["content"] = str(record.get("content") or record.get("text") or record.get("summary") or "")
    normalized["published_at"] = str(record.get("published_at") or record.get("created_at") or "")
    normalized["sentiment"] = _sentiment(record)
    return normalized


# 从配置来源查找指定 ID 的记录。
def _record_by_id(records: list[Mapping[str, Any]], post_id: str) -> Mapping[str, Any] | None:
    for record in records:
        if str(record.get("id") or record.get("post_id") or "") == str(post_id):
            return record
    return None


# 按查询词、平台与日期筛选本地标准记录。
def _filtered_records(
    query: str,
    platforms: list[str] | str | None,
    start_date: str | None,
    end_date: str | None,
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    records = _load_records(config)
    platform_set = _platform_set(platforms)
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    query_groups = _query_groups(query, config)

    filtered = []
    for record in records:
        if platform_set and str(record.get("platform", "")).lower() not in platform_set:
            continue
        record_date = _record_date(record)
        if start and record_date and record_date < start:
            continue
        if end and record_date and record_date > end:
            continue
        if query_groups and not _matches_query(record, query_groups):
            continue
        filtered.append(record)

    return sorted(filtered, key=lambda record: (_record_datetime(record) or datetime.min), reverse=True)


# 将查询拆分为用于本地匹配的词项。
def _query_terms(query: str) -> list[str]:
    terms = [term.strip().lower() for term in re.split(r"\s+", query or "") if term.strip()]
    return terms or [str(query).strip().lower()] if str(query).strip() else []


# 按实体别名扩展词组，同组别名用于匹配同一实体。
def _query_groups(query: str, config: Mapping[str, Any]) -> list[set[str]]:
    aliases = _entity_aliases(config)
    stripped_query = str(query or "").strip().lower()
    for canonical_name, alias_values in aliases.items():
        alias_set = {canonical_name.lower(), *{str(alias).lower() for alias in alias_values}}
        if stripped_query in alias_set:
            return [alias_set]

    groups = []
    for term in _query_terms(query):
        group = {term}
        for canonical_name, alias_values in aliases.items():
            alias_set = {canonical_name.lower(), *{str(alias).lower() for alias in alias_values}}
            if term in alias_set:
                group.update(alias_set)
        groups.append(group)
    return groups


# 用查询词组及别名判断帖子是否符合查询。
def _matches_query(record: Mapping[str, Any], query_groups: list[set[str]]) -> bool:
    haystack = " ".join(
        [
            str(record.get("title") or ""),
            str(record.get("content") or ""),
            str(record.get("author") or ""),
            str(record.get("platform") or ""),
            " ".join(str(tag) for tag in _tags(record)),
            json.dumps(record.get("metadata") or {}, ensure_ascii=False),
        ]
    ).lower()
    return all(any(term in haystack for term in group) for group in query_groups)


# 读取并归一化品牌或实体的别名配置。
def _entity_aliases(config: Mapping[str, Any]) -> dict[str, list[str]]:
    raw_aliases = (
        config.get("entity_aliases")
        or os.getenv("SOCIAL_MEDIA_ENTITY_ALIASES")
        or os.getenv("PUBLIC_OPINION_ENTITY_ALIASES")
        or {}
    )
    if isinstance(raw_aliases, str):
        if not raw_aliases.strip():
            return {}
        try:
            raw_aliases = json.loads(raw_aliases)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw_aliases, Mapping):
        return {}
    aliases: dict[str, list[str]] = {}
    for canonical_name, alias_values in raw_aliases.items():
        if isinstance(alias_values, str):
            aliases[str(canonical_name)] = [
                alias.strip()
                for alias in alias_values.split(",")
                if alias.strip()
            ]
        elif isinstance(alias_values, list):
            aliases[str(canonical_name)] = [
                str(alias).strip()
                for alias in alias_values
                if str(alias).strip()
            ]
    return aliases


# 将平台字符串或序列转换为标准集合。
def _platform_set(platforms: list[str] | str | None) -> set[str]:
    if not platforms:
        return set()
    if isinstance(platforms, str):
        raw_platforms = platforms.split(",")
    else:
        raw_platforms = platforms
    return {str(platform).strip().lower() for platform in raw_platforms if str(platform).strip()}


# 把平台集合整理为 HTTP 接口接受的逗号分隔参数。
def _platforms_csv(platforms: list[str] | str | None) -> str | None:
    if platforms is None:
        return None
    if isinstance(platforms, str):
        return platforms
    return ",".join(str(platform) for platform in platforms)


# 从记录解析发布时间对象。
def _record_datetime(record: Mapping[str, Any]) -> datetime | None:
    value = str(record.get("published_at") or record.get("created_at") or "")
    return parse_datetime(value)


# 取得记录所属日期，用于时间筛选与聚合。
def _record_date(record: Mapping[str, Any]) -> date | None:
    record_datetime = _record_datetime(record)
    return record_datetime.date() if record_datetime else None


# 生成趋势统计中的日期分组键。
def _date_key(record: Mapping[str, Any]) -> str:
    record_date = _record_date(record)
    return record_date.isoformat() if record_date else ""


# 读取并规范化记录情绪类别。
def _sentiment(record: Mapping[str, Any]) -> str:
    sentiment = str(record.get("sentiment") or record.get("polarity") or "unknown").lower()
    if sentiment in {"negative", "neg", "\u8d1f\u9762"}:
        return "negative"
    if sentiment in {"positive", "pos", "\u6b63\u9762"}:
        return "positive"
    if sentiment in {"neutral", "neu", "\u4e2d\u6027"}:
        return "neutral"
    text = _signal_text(record)
    if _contains_any(text, NEGATIVE_TERMS):
        return "negative"
    if _contains_any(text, POSITIVE_TERMS):
        return "positive"
    return "unknown"


# 判断记录是否命中投诉领域信号。
def _is_complaint(record: Mapping[str, Any]) -> bool:
    return _contains_any(_signal_text(record), COMPLAINT_TERMS)


# 综合情绪及投诉词判断记录是否属于负面证据。
def _is_negative_evidence(record: Mapping[str, Any]) -> bool:
    return _sentiment(record) == "negative" or _is_complaint(record)


# 合并风险规则需要检查的正文和标签文本。
def _signal_text(record: Mapping[str, Any]) -> str:
    return " ".join(
        [
            str(record.get("type") or ""),
            str(record.get("category") or ""),
            str(record.get("platform") or ""),
            str(record.get("source_type") or ""),
            str(record.get("title") or ""),
            str(record.get("content") or record.get("text") or record.get("summary") or ""),
            " ".join(str(tag) for tag in _tags(record)),
            json.dumps(record.get("metadata") or {}, ensure_ascii=False),
        ]
    ).lower()


# 复用共享规则提取负面与投诉信号词。
def _matched_signal_terms(record: Mapping[str, Any]) -> list[str]:
    return matched_signal_terms(_signal_text(record))


# 基于共享负面词表生成补充搜索语句。
def _negative_query_variants(query: str, config: Mapping[str, Any]) -> list[str]:
    return negative_query_variants(query, _negative_query_variant_count(config))


# 解析补充负面查询数量，并限制为 1 到 10 条。
def _negative_query_variant_count(config: Mapping[str, Any]) -> int:
    raw_value = (
        config.get("negative_query_variant_count")
        or config.get("social_media_negative_query_variants")
        or os.getenv("SOCIAL_MEDIA_NEGATIVE_QUERY_VARIANTS")
        or os.getenv("PUBLIC_OPINION_NEGATIVE_QUERY_VARIANTS")
        or 4
    )
    try:
        parsed = int(float(str(raw_value)))
    except ValueError:
        parsed = 4
    return max(1, min(10, parsed))


# 累加帖子互动指标，供排序和热度评估使用。
def _engagement_score(record: Mapping[str, Any]) -> int:
    engagement = record.get("engagement") or {}
    if isinstance(engagement, Mapping):
        return sum(
            int(engagement.get(key) or 0)
            for key in ("likes", "comments", "shares", "reposts", "views")
        )
    try:
        return int(engagement)
    except (TypeError, ValueError):
        return 0


# 从记录提取规范化标签列表。
def _tags(record: Mapping[str, Any]) -> list[str]:
    tags = record.get("tags") or record.get("keywords") or []
    if isinstance(tags, str):
        return [tag.strip() for tag in tags.split(",") if tag.strip()]
    if isinstance(tags, list):
        return [str(tag).strip() for tag in tags if str(tag).strip()]
    return []


# 统计结果中的高频关键词和标签。
def _top_keywords(records: list[Mapping[str, Any]], top_n: int = 20) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for record in records:
        counter.update(_tags(record))
        metadata = record.get("metadata") or {}
        if isinstance(metadata, Mapping):
            keywords = metadata.get("keywords") or []
            if isinstance(keywords, str):
                counter.update(keyword.strip() for keyword in keywords.split(",") if keyword.strip())
            elif isinstance(keywords, list):
                counter.update(str(keyword).strip() for keyword in keywords if str(keyword).strip())
    return [
        {"keyword": keyword, "count": count}
        for keyword, count in counter.most_common(top_n)
    ]


# 将内部标准记录转换为对外帖子响应结构。
def _post_payload(record: Mapping[str, Any], include_metadata: bool = False) -> dict[str, Any]:
    payload = {
        "id": record.get("id"),
        "platform": record.get("platform"),
        "author": record.get("author"),
        "title": record.get("title"),
        "content": record.get("content"),
        "published_at": record.get("published_at"),
        "sentiment": _sentiment(record),
        "engagement": record.get("engagement") or {},
        "engagement_score": _engagement_score(record),
        "url": record.get("url"),
        "tags": _tags(record),
        "type": record.get("type") or record.get("source_type"),
    }
    if record.get("matched_query"):
        payload["matched_query"] = record.get("matched_query")
    if record.get("negative_signals"):
        payload["negative_signals"] = record.get("negative_signals")
    if include_metadata:
        payload["metadata"] = record.get("metadata") or {}
        payload["raw"] = dict(record)
    return payload


# 按稳定记录键去重，减少重复证据对统计的影响。
def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped = []
    seen = set()
    for record in records:
        key = _record_key(record)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


# 根据记录身份、来源或内容生成去重键。
def _record_key(record: Mapping[str, Any]) -> str:
    explicit_key = record.get("id") or record.get("url")
    if explicit_key:
        return str(explicit_key)
    content_fingerprint = "|".join(
        [
            str(record.get("platform") or ""),
            str(record.get("author") or ""),
            str(record.get("published_at") or ""),
            str(record.get("content") or "")[:500],
        ]
    )
    return hashlib.sha256(content_fingerprint.encode("utf-8")).hexdigest()


# 按当前游标和页大小截取记录，生成下一页游标。
def _paginate(
    records: list[dict[str, Any]],
    limit: int,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    offset = _cursor_offset(cursor)
    page_records = records[offset:offset + limit]
    next_offset = offset + len(page_records)
    next_cursor = str(next_offset) if next_offset < len(records) else None
    return page_records, next_cursor


# 解析分页游标为合法偏移量。
def _cursor_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(cursor))
    except ValueError:
        return 0


# 按风险规则汇总负面、投诉及传播等统计并确定风险等级。
def _risk_metrics(
    records: list[Mapping[str, Any]],
    sentiment_counts: Counter[str],
    total_engagement: int,
    complaint_count: int,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    matched_count = len(records)
    negative_ratio = _ratio(sentiment_counts.get("negative", 0), matched_count)
    negative_engagement = sum(
        _engagement_score(record)
        for record in records
        if _sentiment(record) == "negative"
    )
    has_negative_signal = bool(negative_ratio or complaint_count or negative_engagement)
    score = risk_score(
        negative_ratio, complaint_count, negative_engagement, total_engagement
    )
    rules = _risk_rules(config)
    level = risk_level(negative_ratio, complaint_count, negative_engagement, rules)
    reasons = []

    if negative_ratio:
        reasons.append(f"negative_ratio={negative_ratio}")
    if total_engagement:
        reasons.append(f"total_engagement={total_engagement}")
    if complaint_count:
        reasons.append(f"complaint_count={complaint_count}")
    if negative_engagement:
        reasons.append(f"negative_engagement={negative_engagement}")
    if total_engagement and not has_negative_signal:
        reasons.append("high_heat_without_negative_signal")

    return {
        "level": level,
        "score": score,
        "heat_level": _heat_level(total_engagement),
        "negative_ratio": negative_ratio,
        "negative_engagement": negative_engagement,
        "complaint_count": complaint_count,
        "rules": rules,
        "reasons": reasons,
    }


# 合并默认风险规则与已知等级的自定义覆盖，格式错误时使用默认规则。
def _risk_rules(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw_rules = (
        config.get("risk_rules")
        or os.getenv("SOCIAL_MEDIA_RISK_RULES")
        or os.getenv("PUBLIC_OPINION_RISK_RULES")
        or DEFAULT_RISK_RULES
    )
    if isinstance(raw_rules, str):
        try:
            raw_rules = json.loads(raw_rules)
        except json.JSONDecodeError:
            raw_rules = DEFAULT_RISK_RULES
    if not isinstance(raw_rules, Mapping):
        return DEFAULT_RISK_RULES
    merged_rules = dict(DEFAULT_RISK_RULES)
    for level, rule in raw_rules.items():
        if isinstance(rule, Mapping) and level in merged_rules:
            merged_rules[level] = {**merged_rules[level], **dict(rule)}
    return merged_rules


# 将请求结果数量约束到允许范围。
def _bounded_limit(limit: int) -> int:
    return max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))


# 说明当前使用 HTTP API 还是本地文件，并列出来源路径或根地址。
def _source_summary(config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "mode": "http_api" if _api_base_url(config) else "local_files",
        "data_paths": _data_paths(config),
        "api_base_url": _api_base_url(config) or None,
    }


# 将服务或数据错误整理为统一工具返回结构。
def _error_payload(operation: str, exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "operation": operation,
        "error_type": exc.__class__.__name__,
        "error": str(exc),
    }


# 启动社交媒体 MCP 服务并注册工具。
def main() -> None:
    """Run the social media MCP server."""
    parser = argparse.ArgumentParser(description="Run the social media MCP server.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default=os.getenv("SOCIAL_MEDIA_MCP_TRANSPORT") or os.getenv("PUBLIC_OPINION_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument("--host", default=os.getenv("SOCIAL_MEDIA_MCP_HOST") or os.getenv("PUBLIC_OPINION_MCP_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("SOCIAL_MEDIA_MCP_PORT") or os.getenv("PUBLIC_OPINION_MCP_PORT", "8001")),
    )
    args = parser.parse_args()

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
