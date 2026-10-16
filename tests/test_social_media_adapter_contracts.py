"""Protect shared social rules and the distinct MCP/Apify data contracts."""

import asyncio
import io
import json
from collections import Counter
from urllib.parse import parse_qs, urlparse

import pytest

import open_deep_research.social_media_apify_api as apify_api
import open_deep_research.social_media_mcp as social_mcp
from open_deep_research.social_media.domain import DEFAULT_RISK_RULES


@pytest.mark.parametrize("adapter", [social_mcp, apify_api])
@pytest.mark.parametrize(
    ("negative_count", "complaints", "negative_engagement", "expected_level"),
    [
        (0, 0, 0, "low"),
        (24, 0, 0, "low"),
        (25, 0, 0, "medium"),
        (44, 0, 0, "medium"),
        (45, 0, 0, "high"),
        (59, 0, 0, "high"),
        (60, 0, 0, "critical"),
        (0, 1, 0, "medium"),
        (0, 3, 0, "high"),
        (0, 10, 0, "critical"),
        (1, 0, 799, "low"),
        (1, 0, 800, "medium"),
        (1, 0, 3000, "high"),
        (1, 0, 10000, "critical"),
    ],
)
def test_risk_thresholds_preserve_priority_and_inclusive_boundaries(
    adapter, negative_count, complaints, negative_engagement, expected_level
):
    records = [
        {"sentiment": "negative" if index < negative_count else "positive"}
        for index in range(100)
    ]
    records[0]["engagement"] = {"views": negative_engagement}
    counts = Counter(record["sentiment"] for record in records)
    if adapter is social_mcp:
        risk = adapter._risk_metrics(
            records,
            counts,
            negative_engagement,
            complaints,
            {"risk_rules": DEFAULT_RISK_RULES},
        )
    else:
        risk = adapter._risk_metrics(
            records, counts, negative_engagement, complaints, negative_engagement
        )

    assert risk["level"] == expected_level
    assert risk["negative_ratio"] == negative_count / 100
    assert risk["complaint_count"] == complaints


def test_risk_config_and_reason_fields_remain_adapter_specific():
    records = [{"sentiment": "positive", "engagement": {"views": 5000}}]
    counts = Counter({"positive": 1})
    mcp_risk = social_mcp._risk_metrics(
        records,
        counts,
        5000,
        0,
        {"risk_rules": {"critical": {"min_complaints": 0}}},
    )
    apify_risk = apify_api._risk_metrics(records, counts, 5000, 0, 0)

    assert mcp_risk["level"] == "critical"
    assert apify_risk["level"] == "low"
    assert mcp_risk["reasons"] == [
        "total_engagement=5000",
        "high_heat_without_negative_signal",
    ]
    assert apify_risk["reasons"] == ["high_heat_without_negative_signal"]
    assert mcp_risk["score"] == apify_risk["score"] == 1


@pytest.mark.parametrize("adapter", [social_mcp, apify_api])
@pytest.mark.parametrize(
    ("start_date", "end_date", "expected_ids"),
    [
        ("2026-06-02", "2026-06-02", ["same-day-late", "same-day", "unknown"]),
        (
            "2026-06-02T23:00:00+08:00",
            "2026-06-02T01:00:00Z",
            ["same-day-late", "same-day", "unknown"],
        ),
        (
            "invalid",
            "invalid",
            ["after", "same-day-late", "same-day", "before", "unknown"],
        ),
        (None, "2026-06-01", ["before", "unknown"]),
    ],
)
def test_date_filter_keeps_inclusive_wall_dates_and_undated_evidence(
    monkeypatch, adapter, start_date, end_date, expected_ids
):
    records = [
        {"id": "before", "published_at": "2026-06-01T23:00:00Z"},
        {"id": "same-day", "published_at": "2026-06-02T01:00:00+08:00"},
        {"id": "unknown", "published_at": "invalid"},
        {"id": "after", "published_at": "2026-06-03"},
        {"id": "same-day-late", "published_at": "2026-06-02T23:00:00-05:00"},
    ]
    if adapter is social_mcp:
        monkeypatch.setattr(adapter, "_load_records", lambda _config: records)
        filtered = adapter._filtered_records("", None, start_date, end_date, {})
    else:
        filtered = adapter._filter_posts(records, "", start_date, end_date)

    assert [record["id"] for record in filtered] == expected_ids


def test_mcp_created_at_fallback_does_not_change_apify_normalized_contract():
    record = {"created_at": "2026-06-02T10:00:00Z"}

    assert social_mcp._date_key(record) == "2026-06-02"
    assert apify_api._date_key(record) == ""


@pytest.mark.parametrize(
    ("cursor", "mcp_ids", "apify_ids"),
    [
        (None, [0, 1], [0, 1]),
        ("2", [2, 3], [2, 3]),
        ("-2", [0, 1], [0, 1]),
        ("+2", [2, 3], [0, 1]),
        (" 2 ", [2, 3], [0, 1]),
        ("invalid", [0, 1], [0, 1]),
    ],
)
def test_cursor_parsing_preserves_protocol_specific_acceptance(
    cursor, mcp_ids, apify_ids
):
    records = [{"id": index} for index in range(5)]
    mcp_page, mcp_next = social_mcp._paginate(records, 2, cursor)
    apify_page, apify_next = apify_api._paginate(records, 2, cursor)

    assert [record["id"] for record in mcp_page] == mcp_ids
    assert [record["id"] for record in apify_page] == apify_ids
    assert mcp_next == str(mcp_ids[-1] + 1)
    assert apify_next == str(apify_ids[-1] + 1)


def test_deduplication_preserves_source_identity_precedence():
    same_id = [{"id": "one", "url": "a"}, {"id": "one", "url": "b"}]
    same_url = [{"id": "one", "url": "a"}, {"id": "two", "url": "a"}]

    assert social_mcp._dedupe_records(same_id) == same_id[:1]
    assert apify_api._dedupe_posts(same_id) == same_id
    assert social_mcp._dedupe_records(same_url) == same_url
    assert apify_api._dedupe_posts(same_url) == same_url[:1]


def test_sentiment_and_metadata_detection_remain_source_specific():
    short_sentiment = {"sentiment": "neg"}
    metadata_signal = {"metadata": {"description": "refund"}}

    assert social_mcp._sentiment(short_sentiment) == "negative"
    assert apify_api._sentiment(short_sentiment) == "neg"
    assert social_mcp._is_complaint(metadata_signal) is True
    assert apify_api._is_complaint(metadata_signal) is False


def test_mcp_registration_keeps_public_and_compatibility_tool_contracts():
    registered = {tool.name: tool for tool in asyncio.run(social_mcp.mcp.list_tools())}

    assert set(registered) == {
        "search_x_posts",
        "search_posts",
        "fetch_x_thread",
        "fetch_thread",
        "fetch_comments",
        "fetch_author_profile",
        "extract_images",
        "extract_post_media",
        "normalize_post",
        "save_to_database",
        "save_social_evidence",
        "check_duplicate",
        "search_social_posts",
        "aggregate_public_sentiment",
        "get_post_detail",
        "search_complaints",
        "get_trending_keywords",
        "get_public_opinion_snapshot",
    }
    assert registered["search_posts"].inputSchema["required"] == ["query"]
    assert (
        registered["save_social_evidence"].inputSchema["properties"]["dry_run"][
            "default"
        ]
        is True
    )


def test_mcp_http_bridge_preserves_query_authentication_and_response(monkeypatch):
    requests = []

    def fake_urlopen(request, *, timeout):
        requests.append((request, timeout))
        return io.BytesIO(
            json.dumps({"ok": True, "posts": [{"id": "fixture"}]}).encode()
        )

    monkeypatch.setattr(social_mcp, "urlopen", fake_urlopen)
    result = social_mcp.search_social_posts(
        "品牌 refund",
        platforms=["x", "weibo"],
        start_date="2026-06-02",
        limit=2,
        cursor="2",
        config={
            "api_base_url": "http://fixture.invalid/",
            "api_key": "fixture-token",
            "api_timeout": 3,
        },
    )
    request, timeout = requests[0]
    parsed = urlparse(request.full_url)

    assert result == {"ok": True, "posts": [{"id": "fixture"}]}
    assert parsed.path == "/search_social_posts"
    assert parse_qs(parsed.query) == {
        "query": ["品牌 refund"],
        "platforms": ["x,weibo"],
        "start_date": ["2026-06-02"],
        "limit": ["2"],
        "cursor": ["2"],
    }
    assert request.get_header("Authorization") == "Bearer fixture-token"
    assert timeout == 3


def test_apify_api_search_normalizes_filters_deduplicates_and_caches_without_network(
    monkeypatch,
):
    actor_requests = []
    raw_post = {
        "tweetId": "fixture",
        "fullText": "brand refund #battery",
        "createdAt": "2026-06-02T01:00:00+08:00",
        "favoriteCount": "1,000",
        "url": "https://fixture.invalid/post/one",
    }

    def fake_run_actor(**kwargs):
        actor_requests.append(kwargs)
        return [
            raw_post,
            dict(raw_post),
            {**raw_post, "tweetId": "old", "url": "old", "createdAt": "2026-06-01"},
        ]

    monkeypatch.setattr(apify_api, "_setup_error", lambda: None)
    monkeypatch.setattr(
        apify_api,
        "_selected_actor_configs",
        lambda _platforms: {"x": {"actor_id": "fixture/actor"}},
    )
    monkeypatch.setattr(apify_api, "_run_actor", fake_run_actor)
    monkeypatch.setattr(apify_api, "_POST_CACHE", {})
    result = apify_api.ApifySocialRequestHandler.routes["/search_social_posts"](
        {
            "query": "brand",
            "start_date": "2026-06-02",
            "end_date": "2026-06-02",
            "limit": "2",
        }
    )

    assert actor_requests[0]["query"] == "brand"
    assert actor_requests[0]["limit"] == 2
    assert result["ok"] is True
    assert result["total_matches"] == result["returned_count"] == 1
    assert result["next_cursor"] is None
    post = result["posts"][0]
    assert post["id"] == "x-fixture"
    assert post["sentiment"] == "negative"
    assert post["engagement"]["likes"] == 1000
    assert post["tags"] == ["battery"]
    assert apify_api.get_post_detail({"post_id": post["id"]})["post"] == post
    assert apify_api.ApifySocialRequestHandler.routes["/health"]({})["ok"] is True
