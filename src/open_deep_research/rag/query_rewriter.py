"""Question rewriting helpers for local RAG retrieval."""

import json
import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

from langchain_core.messages import HumanMessage

from open_deep_research.budget import invoke_model_with_budget
from open_deep_research.models import (
    ModelConfigurationError,
    create_chat_model,
    resolve_api_key,
)
from open_deep_research.observability.langsmith import trace_span

LOGGER = logging.getLogger(__name__)

DEFAULT_RAG_QUERY_REWRITE_PROMPT = """Rewrite the user's question into one standalone search query for local RAG retrieval.

Rules:
- Preserve exact names, IDs, file paths, JSON paths, database/table names, and technical terms.
- Include enough context for vector search and keyword/BM25 search.
- Do not answer the question.
- Do not add facts that are not present in the question.
- Return only the rewritten query, with no markdown and no explanation.

User question:
{query}
"""


def rewrite_query_with_model(
    query: str,
    *,
    model_name: str,
    max_tokens: int,
    api_key: str | None,
    prompt: str = DEFAULT_RAG_QUERY_REWRITE_PROMPT,
    model_factory: Callable[..., Any] = create_chat_model,
) -> str:
    """Rewrite a query for retrieval, falling back to the original on any failure."""
    original_query = query.strip()
    if not original_query:
        return query

    # The rewrite LLM call is auto-traced by LangChain; this span only adds the
    # bounded stage facts (query lengths) without copying the query text.
    with trace_span(
        "query_rewrite",
        "chain",
        metadata={
            "model": model_name,
            "original_query_length": len(original_query),
        },
    ) as rewrite_run:
        try:
            model = model_factory(
                model=model_name,
                max_tokens=max(1, max_tokens),
                api_key=api_key,
                tags=["langsmith:nostream"],
            )
            response, _budget = invoke_model_with_budget(
                model,
                [HumanMessage(content=prompt.format(query=original_query))],
                observer_model=model_name,
                observer_component="rag_query_rewrite",
            )
        except Exception as exc:
            LOGGER.warning("RAG query rewrite failed; using original query: %s", exc)
            return query

        rewritten_query = clean_rewritten_query(_response_text(response))
        if not _is_usable_rewrite(rewritten_query, original_query):
            return query
        if rewrite_run is not None:
            rewrite_run.add_outputs(
                {
                    "rewritten": True,
                    "rewritten_query_length": len(rewritten_query),
                }
            )
        return rewritten_query


def maybe_rewrite_query_with_model(
    query: str,
    *,
    enabled: bool,
    model_name: str,
    max_tokens: int,
    api_key: str | None,
) -> str:
    """Apply query rewrite when enabled; otherwise return the original query."""
    if not enabled:
        return query
    return rewrite_query_with_model(
        query,
        model_name=model_name,
        max_tokens=max_tokens,
        api_key=api_key,
    )


def api_key_for_model(model_name: str, config: Mapping[str, Any] | None = None) -> str | None:
    """Compatibility wrapper around the unified credential resolver."""
    try:
        return resolve_api_key(model_name, config)
    except ModelConfigurationError:
        return None


def clean_rewritten_query(text: str) -> str:
    """Normalize common LLM wrappers around a rewritten query."""
    stripped = text.strip()
    if not stripped:
        return ""

    stripped = _strip_code_fence(stripped)
    parsed = _try_parse_query_json(stripped)
    if parsed:
        stripped = parsed

    stripped = re.sub(
        r"^\s*(?:rewritten\s+query|query|search\s+query)\s*[:：]\s*",
        "",
        stripped,
        flags=re.IGNORECASE,
    )
    stripped = stripped.strip().strip("\"'")
    return " ".join(stripped.split())


def _response_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                value = item.get("text") or item.get("content")
                if value:
                    parts.append(str(value))
        return "\n".join(parts)
    return str(content)


def _strip_code_fence(text: str) -> str:
    match = re.fullmatch(r"```(?:json|text|markdown)?\s*(.*?)\s*```", text, re.DOTALL)
    return match.group(1).strip() if match else text


def _try_parse_query_json(text: str) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, Mapping):
        return ""
    for key in ("query", "rewritten_query", "search_query"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _is_usable_rewrite(rewritten_query: str, original_query: str) -> bool:
    if not rewritten_query:
        return False
    max_length = max(4000, len(original_query) * 5)
    return len(rewritten_query) <= max_length


def _configurable(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not config:
        return {}
    configurable = config.get("configurable", {})
    return configurable if isinstance(configurable, Mapping) else {}
