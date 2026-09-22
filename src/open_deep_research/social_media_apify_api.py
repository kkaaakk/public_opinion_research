"""HTTP adapter that maps Apify Actors to the social media API contract."""

import argparse
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from open_deep_research.social_media.domain import (
    COMPLAINT_TERMS,
    DEFAULT_RISK_RULES,
    NEGATIVE_TERMS,
    POSITIVE_TERMS,
    contains_any,
    heat_level,
    matched_signal_terms,
    negative_query_variants,
    ratio,
    risk_score,
)

load_dotenv()

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9000
DEFAULT_TIMEOUT = 120
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
_POST_CACHE: dict[str, dict[str, Any]] = {}


def search_social_posts(params: Mapping[str, Any]) -> dict[str, Any]:
    """Run configured Apify Actors and return normalized social posts."""
    query = _param(params, "query")
    if not query:
        return _error_payload("search_social_posts", "query is required")

    setup_error = _setup_error()
    if setup_error:
        return setup_error

    platform_names = _platforms(params)
    actor_configs = _selected_actor_configs(platform_names)
    if not actor_configs:
        return _error_payload(
            "search_social_posts",
            "No Apify actor is configured for the requested platforms.",
            setup_required=True,
        )

    limit = _bounded_limit(_param(params, "limit", DEFAULT_LIMIT))
    normalized_posts: list[dict[str, Any]] = []
    actor_errors = []
    for platform, actor_config in actor_configs.items():
        try:
            actor_items = _run_actor(
                actor_config=actor_config,
                query=query,
                platform=platform,
                start_date=_param(params, "start_date"),
                end_date=_param(params, "end_date"),
                limit=limit,
            )
            normalized_posts.extend(
                _normalize_apify_item(item, platform=platform, actor_config=actor_config)
                for item in actor_items
                if isinstance(item, Mapping)
            )
        except Exception as exc:
            actor_errors.append({
                "platform": platform,
                "actor_id": actor_config.get("actor_id"),
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            })

    posts = _filter_posts(
        normalized_posts,
        query=_param(params, "filter_query", query),
        start_date=_param(params, "start_date"),
        end_date=_param(params, "end_date"),
    )
    posts = _dedupe_posts(posts)
    for post in posts:
        _POST_CACHE[post["id"]] = post

    page_posts, next_cursor = _paginate(posts, limit, _param(params, "cursor"))
    return {
        "ok": not actor_errors or bool(page_posts),
        "query": query,
        "returned_count": len(page_posts),
        "total_matches": len(posts),
        "next_cursor": next_cursor,
        "truncated": bool(next_cursor),
        "posts": page_posts,
        "actor_errors": actor_errors,
        "source": _source_summary(actor_configs),
    }


def aggregate_public_sentiment(params: Mapping[str, Any]) -> dict[str, Any]:
    """Aggregate volume, sentiment, trend, and risk from Apify social posts."""
    search_result = search_social_posts({**dict(params), "limit": _param(params, "limit", MAX_LIMIT)})
    if not search_result.get("ok") and not search_result.get("posts"):
        return search_result

    records = search_result.get("posts", [])[:_bounded_limit(_param(params, "limit", MAX_LIMIT))]
    sentiment_counts = Counter(_sentiment(record) for record in records)
    platform_counts = Counter(str(record.get("platform") or "unknown") for record in records)
    daily_counts: dict[str, int] = defaultdict(int)
    total_engagement = 0
    negative_engagement = 0
    complaint_count = 0
    for record in records:
        day = _date_key(record)
        if day:
            daily_counts[day] += 1
        engagement_score = _engagement_score(record)
        total_engagement += engagement_score
        if _sentiment(record) == "negative":
            negative_engagement += engagement_score
        if _is_complaint(record):
            complaint_count += 1

    representative_posts = sorted(records, key=_engagement_score, reverse=True)[:5]
    return {
        "ok": True,
        "query": _param(params, "query"),
        "matched_count": len(records),
        "sentiment_counts": dict(sentiment_counts),
        "negative_ratio": _ratio(sentiment_counts.get("negative", 0), len(records)),
        "platform_counts": dict(platform_counts),
        "daily_counts": dict(sorted(daily_counts.items())),
        "top_keywords": _top_keywords(records),
        "total_engagement": total_engagement,
        "negative_engagement": negative_engagement,
        "complaint_count": complaint_count,
        "risk": _risk_metrics(records, sentiment_counts, total_engagement, complaint_count, negative_engagement),
        "representative_posts": representative_posts,
        "source": search_result.get("source", {}),
        "actor_errors": search_result.get("actor_errors", []),
    }


def get_post_detail(params: Mapping[str, Any]) -> dict[str, Any]:
    """Return one cached or locally persisted normalized post."""
    post_id = _param(params, "post_id")
    if not post_id:
        return _error_payload("get_post_detail", "post_id is required")
    if post_id in _POST_CACHE:
        return {"ok": True, "post": _POST_CACHE[post_id], "source": _cache_source_summary()}
    for post in _load_cached_posts():
        if str(post.get("id") or "") == str(post_id):
            return {"ok": True, "post": post, "source": _cache_source_summary()}
    return _error_payload("get_post_detail", f"Post not found in adapter cache: {post_id}")


def search_complaints(params: Mapping[str, Any]) -> dict[str, Any]:
    """Search complaint-like posts."""
    limit = _bounded_limit(_param(params, "limit", DEFAULT_LIMIT))
    complaints: list[dict[str, Any]] = []
    actor_errors = []
    source = {}
    query_variants = _negative_query_variants(_param(params, "query"), params)
    any_success = False
    for query_variant in query_variants:
        search_result = search_social_posts(
            {
                **dict(params),
                "query": query_variant,
                "filter_query": _param(params, "query"),
                "limit": limit,
            }
        )
        any_success = any_success or bool(search_result.get("ok"))
        source = search_result.get("source") or source
        actor_errors.extend(search_result.get("actor_errors", []))
        for post in search_result.get("posts", []):
            if not _is_negative_evidence(post):
                continue
            complaint = dict(post)
            complaint["matched_query"] = query_variant
            complaint["negative_signals"] = _matched_signal_terms(complaint)
            complaints.append(complaint)

    complaints = _dedupe_posts(complaints)
    page_posts, next_cursor = _paginate(complaints, limit, _param(params, "cursor"))
    return {
        "ok": any_success or bool(page_posts),
        "query": _param(params, "query"),
        "query_variants": query_variants,
        "returned_count": len(page_posts),
        "total_matches": len(complaints),
        "next_cursor": next_cursor,
        "truncated": bool(next_cursor),
        "complaints": page_posts,
        "source": source,
        "actor_errors": actor_errors,
    }


def get_trending_keywords(params: Mapping[str, Any]) -> dict[str, Any]:
    """Return related hot words and negative keywords."""
    search_result = search_social_posts({**dict(params), "limit": _param(params, "limit", MAX_LIMIT)})
    if not search_result.get("ok") and not search_result.get("posts"):
        return search_result
    records = search_result.get("posts", [])
    return {
        "ok": True,
        "query": _param(params, "query"),
        "matched_count": len(records),
        "top_keywords": _top_keywords(records, top_n=30),
        "negative_keywords": _top_keywords(
            [record for record in records if _sentiment(record) == "negative"],
            top_n=20,
        ),
        "source": search_result.get("source", {}),
        "actor_errors": search_result.get("actor_errors", []),
    }


def get_public_opinion_snapshot(params: Mapping[str, Any]) -> dict[str, Any]:
    """Return a decision-ready public-opinion snapshot."""
    aggregate = aggregate_public_sentiment(params)
    if not aggregate.get("ok") and not aggregate.get("representative_posts"):
        return aggregate
    complaints = search_complaints({**dict(params), "limit": 5})
    trending = get_trending_keywords(params)
    risk = aggregate.get("risk") or {}
    return {
        "ok": True,
        "query": _param(params, "query"),
        "summary": {
            "matched_count": aggregate.get("matched_count", 0),
            "risk_level": risk.get("level", "low"),
            "risk_score": risk.get("score", 0),
            "heat_level": risk.get("heat_level", "low"),
            "negative_ratio": aggregate.get("negative_ratio", 0),
            "complaint_count": aggregate.get("complaint_count", 0),
            "total_engagement": aggregate.get("total_engagement", 0),
            "negative_engagement": aggregate.get("negative_engagement", 0),
        },
        "sentiment_counts": aggregate.get("sentiment_counts", {}),
        "platform_counts": aggregate.get("platform_counts", {}),
        "daily_counts": aggregate.get("daily_counts", {}),
        "top_keywords": trending.get("top_keywords", []),
        "negative_keywords": trending.get("negative_keywords", []),
        "representative_posts": aggregate.get("representative_posts", []),
        "representative_complaints": complaints.get("complaints", []),
        "source": aggregate.get("source", {}),
        "actor_errors": aggregate.get("actor_errors", []),
    }


def _run_actor(
    actor_config: Mapping[str, Any],
    query: str,
    platform: str,
    start_date: str | None,
    end_date: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    actor_id = str(actor_config.get("actor_id") or actor_config.get("actor") or "").strip()
    if not actor_id:
        raise ValueError(f"Missing actor_id for platform {platform}")

    actor_input = _render_template(
        actor_config.get("input") or actor_config.get("input_template") or {},
        {
            "query": query,
            "platform": platform,
            "start_date": start_date or "",
            "end_date": end_date or "",
            "limit": limit,
        },
    )
    encoded_actor_id = actor_id.replace("/", "~")
    endpoint = (
        f"https://api.apify.com/v2/acts/{encoded_actor_id}/"
        "run-sync-get-dataset-items?format=json&clean=true"
    )
    timeout = int(os.getenv("APIFY_SOCIAL_RUN_TIMEOUT") or os.getenv("APIFY_TIMEOUT") or DEFAULT_TIMEOUT)
    request = Request(
        endpoint,
        data=json.dumps(actor_input).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {_apify_token()}",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        return [item for item in payload["items"] if isinstance(item, dict)]
    return []


def _normalize_apify_item(
    item: Mapping[str, Any],
    platform: str,
    actor_config: Mapping[str, Any],
) -> dict[str, Any]:
    content = _first_text(
        item,
        [
            "content",
            "text",
            "fullText",
            "description",
            "caption",
            "title",
            "desc",
            "body",
            "message",
        ],
    )
    url = _first_text(item, ["url", "link", "postUrl", "shareUrl", "webpageUrl"])
    external_id = _first_text(
        item,
        [
            "id",
            "postId",
            "tweetId",
            "noteId",
            "awemeId",
            "shortCode",
            "cid",
            "url",
        ],
    )
    if not external_id:
        external_id = hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]

    author = _first_text(
        item,
        [
            "author.nickname",
            "author.name",
            "author.username",
            "author.unique_id",
            "user.nickname",
            "user.name",
            "user.username",
            "user.unique_id",
            "userInfo.nickname",
            "userInfo.name",
            "userInfo.username",
            "author",
            "authorName",
            "userName",
            "username",
            "nickname",
            "ownerUsername",
            "name",
        ],
    )
    author_id = _first_text(
        item,
        [
            "author.uid",
            "author.id",
            "author.secUid",
            "author.sec_uid",
            "author.unique_id",
            "user.uid",
            "user.id",
            "user.secUid",
            "user.unique_id",
            "authorId",
            "userId",
            "uid",
            "secUid",
            "ownerId",
            "username",
        ],
    ) or author
    published_at = _normalize_datetime(
        _first_text(
            item,
            [
                "published_at",
                "createdAt",
                "createTime",
                "create_time",
                "timestamp",
                "date",
                "time",
                "publishTime",
            ],
        )
    )
    engagement = {
        "likes": _first_int(item, ["likes", "likeCount", "diggCount", "favorites", "favoriteCount"]),
        "comments": _first_int(item, ["comments", "commentCount", "replyCount"]),
        "shares": _first_int(item, ["shares", "shareCount", "reposts", "repostCount"]),
        "views": _first_int(item, ["views", "viewCount", "playCount", "videoViewCount"]),
    }
    tags = _normalize_tags(item)
    normalized = {
        "id": f"{platform}-{external_id}",
        "external_id": external_id,
        "platform": platform,
        "type": str(actor_config.get("type") or item.get("type") or "social_post"),
        "author": author,
        "author_id": author_id,
        "title": _first_text(item, ["title", "headline"]),
        "content": content,
        "published_at": published_at,
        "sentiment": _infer_sentiment({**dict(item), "content": content, "tags": tags}),
        "engagement": engagement,
        "engagement_score": sum(engagement.values()),
        "url": url,
        "tags": tags,
        "images": _normalize_media(item),
        "metadata": {
            "actor_id": actor_config.get("actor_id") or actor_config.get("actor"),
            "raw": dict(item),
        },
    }
    normalized["negative_signals"] = _matched_signal_terms(normalized)
    return normalized


def _actor_config() -> dict[str, dict[str, Any]]:
    raw_config = os.getenv("APIFY_SOCIAL_ACTORS") or os.getenv("APIFY_ACTORS") or ""
    if not raw_config.strip():
        return {}
    try:
        parsed = json.loads(raw_config)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    return {
        str(platform).strip().lower(): dict(config)
        for platform, config in parsed.items()
        if isinstance(config, Mapping) and str(platform).strip()
    }


def _selected_actor_configs(platforms: list[str]) -> dict[str, dict[str, Any]]:
    configs = _actor_config()
    if not platforms:
        return configs
    return {
        platform: configs[platform]
        for platform in platforms
        if platform in configs
    }


def _setup_error() -> dict[str, Any] | None:
    missing = []
    if not _apify_token():
        missing.append("APIFY_TOKEN")
    if not _actor_config():
        missing.append("APIFY_SOCIAL_ACTORS")
    if not missing:
        return None
    return {
        "ok": False,
        "operation": "apify_social_api",
        "setup_required": True,
        "missing": missing,
        "message": (
            "Configure APIFY_TOKEN and APIFY_SOCIAL_ACTORS before using the Apify "
            "social API adapter."
        ),
        "example_env": {
            "APIFY_TOKEN": "apify_api_xxx",
            "APIFY_SOCIAL_ACTORS": json.dumps(
                {
                    "xiaohongshu": {
                        "actor_id": "habit.zhou/xiaohongshu-pro-scraper",
                        "input": {
                            "mode": "search",
                            "keywords": ["{query}"],
                            "maxItemsPerInput": "{limit}",
                        },
                    },
                    "douyin": {
                        "actor_id": "sian.agency/douyin-scraper",
                        "input": {
                            "operation": "searchVideo",
                            "keyword": "{query}",
                            "maxPages": 1,
                        },
                    },
                },
                ensure_ascii=False,
            ),
        },
    }


def _apify_token() -> str:
    token = str(os.getenv("APIFY_TOKEN") or "").strip()
    if token in {"your-apify-token", "apify_api_xxx"}:
        return ""
    return token


def _render_template(value: Any, params: Mapping[str, Any]) -> Any:
    if isinstance(value, Mapping):
        return {key: _render_template(item, params) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_template(item, params) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "{limit}":
            return int(params.get("limit") or DEFAULT_LIMIT)
        return value.format_map(defaultdict(str, {key: "" if val is None else val for key, val in params.items()}))
    return value


def _param(params: Mapping[str, Any], key: str, default: Any = None) -> Any:
    value = params.get(key, default)
    if isinstance(value, list):
        return value[0] if value else default
    return default if value is None else value


def _platforms(params: Mapping[str, Any]) -> list[str]:
    value = _param(params, "platforms", "")
    if isinstance(value, list):
        raw_platforms = value
    else:
        raw_platforms = str(value or "").split(",")
    return [str(platform).strip().lower() for platform in raw_platforms if str(platform).strip()]


def _filter_posts(
    posts: list[dict[str, Any]],
    query: str,
    start_date: str | None,
    end_date: str | None,
) -> list[dict[str, Any]]:
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    query_terms = [term.lower() for term in re.split(r"\s+", query or "") if term.strip()]
    filtered = []
    for post in posts:
        post_date = _post_date(post)
        if start and post_date and post_date < start:
            continue
        if end and post_date and post_date > end:
            continue
        haystack = " ".join([
            str(post.get("title") or ""),
            str(post.get("content") or ""),
            str(post.get("author") or ""),
            " ".join(str(tag) for tag in post.get("tags") or []),
        ]).lower()
        if query_terms and not all(term in haystack for term in query_terms):
            continue
        filtered.append(post)
    return sorted(filtered, key=lambda post: (_post_datetime(post) or datetime.min), reverse=True)


def _dedupe_posts(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    deduped = []
    for post in posts:
        key = str(post.get("url") or post.get("id") or _content_hash(post))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(post)
    return deduped


def _paginate(records: list[dict[str, Any]], limit: int, cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
    offset = int(cursor or 0) if str(cursor or "").isdigit() else 0
    page = records[offset:offset + limit]
    next_offset = offset + len(page)
    next_cursor = str(next_offset) if next_offset < len(records) else None
    return page, next_cursor


def _first_text(item: Mapping[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = _deep_get(item, key)
        text = _coerce_text(value)
        if text:
            return text
    return ""


def _coerce_text(value: Any) -> str:
    if value in (None, "", []):
        return ""
    if isinstance(value, Mapping):
        for key in [
            "nickname",
            "name",
            "username",
            "userName",
            "authorName",
            "title",
            "desc",
            "description",
            "content",
            "text",
            "url",
            "shareUrl",
        ]:
            text = _coerce_text(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        for item in value:
            text = _coerce_text(item)
            if text:
                return text
        return ""
    return str(value)


def _first_int(item: Mapping[str, Any], keys: list[str]) -> int:
    for key in keys:
        value = _deep_get(item, key)
        if value in (None, ""):
            continue
        try:
            return int(float(str(value).replace(",", "")))
        except ValueError:
            continue
    return 0


def _deep_get(item: Mapping[str, Any], key: str) -> Any:
    if key in item:
        return item[key]
    current: Any = item
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _normalize_tags(item: Mapping[str, Any]) -> list[str]:
    raw_tags = item.get("tags") or item.get("hashtags") or item.get("keywords") or []
    tags = []
    if isinstance(raw_tags, str):
        tags.extend(tag.strip("# ").strip() for tag in raw_tags.split(","))
    elif isinstance(raw_tags, list):
        for tag in raw_tags:
            if isinstance(tag, Mapping):
                tags.append(str(tag.get("name") or tag.get("text") or "").strip("# "))
            else:
                tags.append(str(tag).strip("# "))
    content = _first_text(item, ["content", "text", "fullText", "description", "caption", "title"])
    tags.extend(match.strip("#") for match in re.findall(r"#([\w\u4e00-\u9fff-]+)", content))
    return sorted({tag for tag in tags if tag})


def _normalize_media(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    media = item.get("images") or item.get("media") or item.get("imageUrls") or item.get("displayUrl") or []
    if isinstance(media, str):
        media = [media]
    if not isinstance(media, list):
        return []
    normalized = []
    for entry in media:
        if isinstance(entry, Mapping):
            url = entry.get("url") or entry.get("src") or entry.get("displayUrl")
            caption = entry.get("caption") or entry.get("alt")
            normalized.append({"url": url, "caption": caption})
        else:
            normalized.append({"url": str(entry), "caption": None})
    return [entry for entry in normalized if entry.get("url")]


def _infer_sentiment(item: Mapping[str, Any]) -> str:
    explicit = str(item.get("sentiment") or item.get("polarity") or "").lower()
    if explicit in {"negative", "positive", "neutral", "unknown"}:
        if explicit != "unknown":
            return explicit
    text = _signal_text(item)
    if _contains_any(text, NEGATIVE_TERMS):
        return "negative"
    if _contains_any(text, POSITIVE_TERMS):
        return "positive"
    return "unknown"


def _normalize_datetime(value: str) -> str:
    if not value:
        return ""
    value = str(value)
    if value.isdigit():
        timestamp = int(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp // 1000
        return datetime.fromtimestamp(timestamp).isoformat()
    return value


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(str(value), "%Y-%m-%d").date()
        except ValueError:
            return None


def _post_datetime(post: Mapping[str, Any]) -> datetime | None:
    value = str(post.get("published_at") or "")
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _post_date(post: Mapping[str, Any]) -> date | None:
    value = _post_datetime(post)
    return value.date() if value else None


def _date_key(post: Mapping[str, Any]) -> str:
    value = _post_date(post)
    return value.isoformat() if value else ""


def _sentiment(post: Mapping[str, Any]) -> str:
    return str(post.get("sentiment") or "unknown").lower()


def _is_complaint(post: Mapping[str, Any]) -> bool:
    return _contains_any(_signal_text(post), COMPLAINT_TERMS)


def _is_negative_evidence(post: Mapping[str, Any]) -> bool:
    return _sentiment(post) == "negative" or _is_complaint(post)


def _signal_text(post: Mapping[str, Any]) -> str:
    return " ".join(
        [
            str(post.get("type") or ""),
            str(post.get("platform") or ""),
            str(post.get("title") or ""),
            str(post.get("content") or post.get("text") or post.get("description") or ""),
            " ".join(str(tag) for tag in post.get("tags") or []),
        ]
    ).lower()


def _contains_any(text: str, terms: list[str]) -> bool:
    return contains_any(text, terms)


def _matched_signal_terms(post: Mapping[str, Any]) -> list[str]:
    return matched_signal_terms(_signal_text(post))


def _negative_query_variants(query: Any, params: Mapping[str, Any]) -> list[str]:
    base_query = str(query or "").strip()
    if not base_query:
        return []
    if not _truthy_param(params, "negative_query_expansion", True):
        return [base_query]
    variant_count = _bounded_int(
        _param(params, "negative_query_variant_count", os.getenv("SOCIAL_MEDIA_NEGATIVE_QUERY_VARIANTS") or 4),
        minimum=1,
        maximum=10,
    )
    return negative_query_variants(base_query, variant_count)


def _truthy_param(params: Mapping[str, Any], key: str, default: bool) -> bool:
    value = _param(params, key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _bounded_int(value: Any, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(str(value)))
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


def _engagement_score(post: Mapping[str, Any]) -> int:
    engagement = post.get("engagement") or {}
    if not isinstance(engagement, Mapping):
        return 0
    return sum(int(engagement.get(key) or 0) for key in ("likes", "comments", "shares", "reposts", "views"))


def _top_keywords(posts: list[Mapping[str, Any]], top_n: int = 20) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for post in posts:
        counter.update(str(tag) for tag in post.get("tags") or [])
    return [{"keyword": keyword, "count": count} for keyword, count in counter.most_common(top_n)]


def _risk_metrics(
    records: list[dict[str, Any]],
    sentiment_counts: Counter[str],
    total_engagement: int,
    complaint_count: int,
    negative_engagement: int,
) -> dict[str, Any]:
    negative_ratio = _ratio(sentiment_counts.get("negative", 0), len(records))
    has_negative_signal = bool(negative_ratio or complaint_count or negative_engagement)
    score = risk_score(
        negative_ratio, complaint_count, negative_engagement, total_engagement
    )
    level = "low"
    reasons = []
    for candidate_level in ("critical", "high", "medium"):
        rule = DEFAULT_RISK_RULES[candidate_level]
        if (
            negative_ratio >= float(rule["min_negative_ratio"])
            or complaint_count >= int(rule["min_complaints"])
            or negative_engagement >= int(rule["min_negative_engagement"])
        ):
            level = candidate_level
            break
    if negative_ratio:
        reasons.append(f"negative_ratio={negative_ratio}")
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
        "rules": DEFAULT_RISK_RULES,
        "reasons": reasons,
    }


def _heat_level(total_engagement: int) -> str:
    return heat_level(total_engagement)


def _ratio(numerator: int, denominator: int) -> float:
    return ratio(numerator, denominator)


def _bounded_limit(value: Any) -> int:
    try:
        return max(1, min(int(value or DEFAULT_LIMIT), MAX_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def _content_hash(post: Mapping[str, Any]) -> str:
    content = f"{post.get('platform')}|{post.get('author')}|{post.get('content')}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _load_cached_posts() -> list[dict[str, Any]]:
    cache_path = os.getenv("APIFY_SOCIAL_CACHE_PATH") or ""
    if not cache_path:
        return []
    path = Path(cache_path)
    if not path.exists():
        return []
    posts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            payload = json.loads(line)
            if isinstance(payload, dict):
                posts.append(payload)
    return posts


def _source_summary(actor_configs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "mode": "apify",
        "actors": {
            platform: config.get("actor_id") or config.get("actor")
            for platform, config in actor_configs.items()
        },
    }


def _cache_source_summary() -> dict[str, Any]:
    return {"mode": "apify_adapter_cache", "cache_size": len(_POST_CACHE)}


def _error_payload(operation: str, error: str, setup_required: bool = False) -> dict[str, Any]:
    return {
        "ok": False,
        "operation": operation,
        "error": error,
        "setup_required": setup_required,
    }


class ApifySocialRequestHandler(BaseHTTPRequestHandler):
    """Serve the social media API contract over HTTP."""

    routes = {
        "/search_social_posts": search_social_posts,
        "/aggregate_public_sentiment": aggregate_public_sentiment,
        "/get_post_detail": get_post_detail,
        "/search_complaints": search_complaints,
        "/get_trending_keywords": get_trending_keywords,
        "/get_public_opinion_snapshot": get_public_opinion_snapshot,
        "/health": lambda params: {"ok": True, "service": "apify_social_api"},
    }

    def do_GET(self) -> None:
        """Dispatch one HTTP GET request to the adapter route table."""
        parsed = urlparse(self.path)
        handler = self.routes.get(parsed.path)
        if handler is None:
            self._send_json(_error_payload("routing", f"Unknown path: {parsed.path}"), status=404)
            return
        params = {key: values[0] if len(values) == 1 else values for key, values in parse_qs(parsed.query).items()}
        try:
            self._send_json(handler(params))
        except Exception as exc:
            self._send_json(
                {
                    "ok": False,
                    "operation": parsed.path.lstrip("/"),
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                },
                status=500,
            )

    def log_message(self, format: str, *args: Any) -> None:
        """Honor the adapter's quiet-mode environment setting."""
        if os.getenv("APIFY_SOCIAL_API_QUIET", "false").lower() in {"1", "true", "yes"}:
            return
        super().log_message(format, *args)

    def _send_json(self, payload: Mapping[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    """Run the Apify social API adapter."""
    parser = argparse.ArgumentParser(description="Run the Apify social API adapter.")
    parser.add_argument("--host", default=os.getenv("APIFY_SOCIAL_API_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("APIFY_SOCIAL_API_PORT", DEFAULT_PORT)),
    )
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), ApifySocialRequestHandler)
    print(  # noqa: T201 - CLI startup status is intentionally written to stdout
        f"Apify social API listening on http://{args.host}:{args.port}", flush=True
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
