# Social Media Skill

The social media layer uses a two-level design:

```text
social_media_skill
  calls social_media_mcp
    search_x_posts()
    fetch_x_thread()
    fetch_comments()
    fetch_author_profile()
    extract_images()
    normalize_post()
    save_to_database()
    check_duplicate()
```

`social_media_mcp` is the low-level platform/data adapter. It can run as an MCP server, but the
public-opinion agents normally use `social_media_skill` directly as in-process LangChain tools.

`social_media_skill` is the business layer. It composes low-level MCP primitives into agent-facing
tools:

```text
social_media_search
social_media_snapshot
social_complaint_scan
social_media_context_bundle
```

## Agent Usage

In `public_opinion_risk` mode, these roles can use the skill tools:

- `public_signal`
- `risk_assessment`

The skill tools call `social_media_mcp` internally, so the agent does not need to call each raw
platform primitive by hand.

## Local Data Mode

By default, low-level social media tools read:

```text
data/social_media/sample_posts.jsonl
```

Override with:

```powershell
$env:SOCIAL_MEDIA_DATA_PATHS="data/social_media/sample_posts.jsonl,C:\data\more_posts.jsonl"
```

Each record can use this schema:

```json
{
  "id": "x-root-001",
  "thread_id": "x-root-001",
  "parent_id": null,
  "platform": "x",
  "type": "social_post",
  "author": "ev_watch",
  "author_id": "ev_watch",
  "title": "Battery range complaint",
  "content": "Post content",
  "published_at": "2026-06-02T10:20:00",
  "sentiment": "negative",
  "engagement": {
    "likes": 10,
    "comments": 2,
    "shares": 1,
    "views": 500
  },
  "url": "https://example.com/x/root-001",
  "tags": ["battery", "complaint"],
  "images": [{"url": "https://example.com/image.png", "caption": "screenshot"}],
  "metadata": {
    "keywords": ["service", "quality"]
  }
}
```

## Optional MCP Server

Run the low-level MCP server only if another process or non-Python client needs the raw tools:

```powershell
$env:PYTHONPATH="src"
python -m open_deep_research.social_media_mcp --transport streamable-http --host 127.0.0.1 --port 8001
```

If installed, the script entry is:

```powershell
social-media-mcp --transport streamable-http --host 127.0.0.1 --port 8001
```

## HTTP Proxy Mode

Set a base URL to proxy low-level calls to a real social media data service:

```powershell
$env:SOCIAL_MEDIA_API_BASE_URL="https://your-social-data-service.example.com"
$env:SOCIAL_MEDIA_API_KEY="optional-token"
```

The low-level adapter can proxy:

```text
GET /search_social_posts
GET /aggregate_public_sentiment
GET /get_post_detail
GET /search_complaints
GET /get_trending_keywords
GET /get_public_opinion_snapshot
```

## Entity Aliases

```powershell
$env:SOCIAL_MEDIA_ENTITY_ALIASES='{"Acme EV":["A-EV","Acme electric vehicle"]}'
```

Alias groups help the skill match brand or product variants consistently.
