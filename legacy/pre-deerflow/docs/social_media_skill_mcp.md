# Social Media Tools

Direct MCP tool exposure for the deep research agent. No intermediate orchestration layer — the LLM calls MCP primitives directly and makes decisions in its own ReAct loop.

```text
LangChain @tool (social_media/tools.py)   ← thin wrapper: json.dumps(result)
  ├─ search_posts
  ├─ fetch_thread
  ├─ fetch_comments
  ├─ fetch_author_profile
  ├─ search_complaints
  ├─ get_trending_keywords
  └─ get_public_opinion_snapshot
       │  direct Python call
       ▼
Social Media MCP Module (social_media_mcp.py)
  │  read / proxy
       ▼
JSONL files / HTTP API / external services
```

## Responsibilities

- `social_media/tools.py` — thin LangChain `@tool` wrappers. Each wraps one MCP function, returns `json.dumps(result)`. No orchestration, no business logic.
- `social_media_mcp.py` — data adapter. Reads local JSON/JSONL or proxies to an HTTP service. Also doubles as an MCP server (FastMCP) for external clients.
- `web_search` — public web/news channel (Tavily / DuckDuckGo / native search).
- `rag_search` — internal knowledge base channel.

## Agent Usage

In `business_scenario=public_opinion_risk`, agents with the `mcp` channel (`public_signal`, `risk_assessment`) see these tools directly:

```text
search_posts
fetch_thread
fetch_comments
search_complaints
fetch_author_profile
get_trending_keywords
get_public_opinion_snapshot
```

The LLM controls the decision flow in its ReAct loop — search posts, check sentiment, fetch comments if negative, search complaints if needed, compute risk from results. No pre-canned orchestration.

## Local Mock Data

```text
SOCIAL_MEDIA_DATA_PATHS=data/social_media/sample_posts.jsonl
```

Records use a common schema: `platform`, `id`, `author`, `published_at`, `content`, `sentiment`, `engagement`, `url`, `tags`, `images`, `metadata`.

## Optional MCP Server

Run the MCP server for external MCP clients:

```bash
python -m open_deep_research.social_media_mcp --transport streamable-http --host 127.0.0.1 --port 8001
```

Or via package script: `social-media-mcp --transport streamable-http --host 127.0.0.1 --port 8001`

Then configure:

```python
{
    "configurable": {
        "mcp_config": {
            "url": "http://127.0.0.1:8001",
            "tools": ["search_posts", "fetch_thread", "fetch_comments", ...],
            "auth_required": False
        }
    }
}
```

## Replacing The Mock Provider

To connect a real social-data provider, either:

- Set `SOCIAL_MEDIA_API_BASE_URL` and optionally `SOCIAL_MEDIA_API_KEY`; `social_media_mcp` will proxy supported endpoints such as `search_posts` and `aggregate_public_sentiment`.
- Replace the local adapter functions in `social_media_mcp.py` with platform adapters for Weibo, Xiaohongshu, Douyin, Zhihu, Black Cat Complaint, or an internal monitoring vendor.

## Apify Adapter

Apify should be connected through the local adapter service instead of assigning
`SOCIAL_MEDIA_API_BASE_URL` directly to `https://api.apify.com/v2`.

Reason: Apify Actor endpoints are actor-specific, while `social_media_mcp` expects
a stable business API contract:

```text
/search_social_posts
/aggregate_public_sentiment
/get_post_detail
/search_complaints
/get_trending_keywords
/get_public_opinion_snapshot
```

The adapter in `open_deep_research.social_media_apify_api` translates those stable
endpoints into Apify Actor runs and normalizes the returned dataset items into the
project's `SocialPost` schema.

### Start The Adapter

```bash
python -m open_deep_research.social_media_apify_api --host 127.0.0.1 --port 9000
```

or:

```bash
apify-social-api --host 127.0.0.1 --port 9000
```

Then point the social media MCP layer at it:

```text
SOCIAL_MEDIA_API_BASE_URL=http://127.0.0.1:9000
```

### Required Apify Settings

```text
APIFY_TOKEN=your-apify-token
APIFY_SOCIAL_ACTORS={"x":{"actor_id":"apidojo/tweet-scraper","input":{"searchTerms":["{query}"],"maxItems":"{limit}"}}}
```

`APIFY_SOCIAL_ACTORS` is a JSON object keyed by platform. Each platform config needs:

- `actor_id`: Apify Actor id, such as `apidojo/tweet-scraper`.
- `input`: Actor input template. Supported placeholders are `{query}`, `{platform}`, `{start_date}`, `{end_date}`, and `{limit}`.

Example with multiple platforms:

```json
{
  "x": {
    "actor_id": "apidojo/tweet-scraper",
    "input": {
      "searchTerms": ["{query}"],
      "maxItems": "{limit}"
    }
  },
  "weibo": {
    "actor_id": "sian.agency/weibo-scraper",
    "input": {
      "keyword": "{query}",
      "maxItems": "{limit}"
    }
  }
}
```

Actor input schemas vary by Actor. Choose the Actor in Apify Store, inspect its input
schema, then map this template to that schema. The adapter handles output
normalization, aggregation, risk scoring, and the stable endpoint contract.

### Verify The Adapter

```bash
curl "http://127.0.0.1:9000/health"
curl "http://127.0.0.1:9000/search_social_posts?query=battery&platforms=x&limit=5"
curl "http://127.0.0.1:9000/get_public_opinion_snapshot?query=battery&platforms=x&limit=20"
```

If `APIFY_TOKEN` or `APIFY_SOCIAL_ACTORS` is missing, the adapter returns
`setup_required=true` with the missing settings instead of silently falling back.
