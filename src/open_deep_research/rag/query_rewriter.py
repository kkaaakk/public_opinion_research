# 可选使用模型改写检索查询，失败或输出不合格时保留原问题。
"""Question rewriting helpers for local RAG retrieval."""

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from open_deep_research.budget import ModelBudgetExceeded
from open_deep_research.llm.generation import generate_text_sync
from open_deep_research.models import (
    ModelConfigurationError,
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


# 调用改写模型并清理输出；任一失败时回退到原查询。
def rewrite_query_with_model(
    query: str,
    *,
    model_name: str,
    max_tokens: int,
    api_key: str | None,
    config: Mapping[str, Any] | None = None,
    prompt: str = DEFAULT_RAG_QUERY_REWRITE_PROMPT,
) -> str:
    """Rewrite a query for retrieval, falling back to the original on any failure."""
    original_query = query.strip()
    if not original_query:
        return query

    # Record bounded stage facts without copying the query text.
    with trace_span(
        "query_rewrite",
        "chain",
        metadata={
            "model": model_name,
            "original_query_length": len(original_query),
        },
    ) as rewrite_run:
        try:
            response, _budget = generate_text_sync(
                model_name,
                prompt.format(query=original_query),
                max_tokens=max(1, max_tokens),
                api_key=api_key,
                config=config,
                component="rag_query_rewrite",
            )
        except Exception as exc:
            from open_deep_research.workflow.execution import (
                is_execution_control_failure,
            )

            if is_execution_control_failure(exc) and not isinstance(exc, ModelBudgetExceeded):
                raise
            LOGGER.warning(
                "RAG query rewrite failed; using original query (degraded): %s", exc
            )
            return query

        rewritten_query = clean_rewritten_query(response)
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


# 根据开关决定是否改写查询，关闭时直接返回原文。
def maybe_rewrite_query_with_model(
    query: str,
    *,
    enabled: bool,
    model_name: str,
    max_tokens: int,
    api_key: str | None,
    config: Mapping[str, Any] | None = None,
) -> str:
    """Apply query rewrite when enabled; otherwise return the original query."""
    if not enabled:
        return query
    return rewrite_query_with_model(
        query,
        model_name=model_name,
        max_tokens=max_tokens,
        api_key=api_key,
        config=config,
    )


# 兼容旧接口，委托统一模型工厂解析凭据。
def api_key_for_model(model_name: str, config: Mapping[str, Any] | None = None) -> str | None:
    """Compatibility wrapper around the unified credential resolver."""
    try:
        return resolve_api_key(model_name, config)
    except ModelConfigurationError:
        return None


# 去除代码围栏、JSON 包装和常见前缀，提取实际检索语句。
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


# 去除模型输出外围的 Markdown 代码围栏。
def _strip_code_fence(text: str) -> str:
    match = re.fullmatch(r"```(?:json|text|markdown)?\s*(.*?)\s*```", text, re.DOTALL)
    return match.group(1).strip() if match else text


# 尝试从 JSON 输出中取得查询字段，兼容结构化改写结果。
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


# 检查改写文本是否可作为有效查询，过滤空白或异常输出。
def _is_usable_rewrite(rewritten_query: str, original_query: str) -> bool:
    if not rewritten_query:
        return False
    max_length = max(4000, len(original_query) * 5)
    return len(rewritten_query) <= max_length
