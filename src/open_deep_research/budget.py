"""Budget policy and enforcement for bounded deep research runs.

LangChain owns the model-call facts: ``AIMessage.usage_metadata`` for tokens and the
``on_chat_model_start`` callback lifecycle for real chat-model attempts.  Token
aggregation goes through LangChain's ``add_usage``; this module only adds the
project-owned policy layer: model/tool/search limits, token budgets, warnings,
degradation reasons, and per-node budget deltas for the LangGraph reducers.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Iterator, Mapping, cast

from langchain_core.callbacks import BaseCallbackHandler, UsageMetadataCallbackHandler
from langchain_core.messages import HumanMessage
from langchain_core.messages.ai import UsageMetadata, add_usage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.runnables import RunnableLambda
from langchain_core.runnables.config import ensure_config, merge_configs

from open_deep_research.configuration import Configuration

LOGGER = logging.getLogger(__name__)

BUDGET_COUNTER_FIELDS = ("model_calls", "tool_calls", "search_calls")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
TOKEN_DETAIL_FIELDS = ("input_token_details", "output_token_details")
BUDGET_LIST_FIELDS = ("budget_warnings", "degradation_reasons")
SEARCH_TOOL_NAMES = {"web_search", "rag_search"}
MIN_MODEL_CALLS_PER_RESEARCH_UNIT = 2

# Per-tool-call attribution for model calls executed *inside* a tool (e.g. per-URL
# webpage summarization in ``tavily_search``).  Model attempts and provider tokens
# are recorded here while a capture is active, so the enclosing node can merge one
# delta for the tool.  LangChain's UsageMetadataCallbackHandler cannot replace this:
# it aggregates a whole run per model name and cannot attribute a delta to one tool
# result, which is exactly what the Budget Guard needs mid-run.
_BUDGET_CAPTURE: ContextVar[dict[str, Any] | None] = ContextVar(
    "open_deep_research_budget_capture",
    default=None,
)


def empty_budget_usage() -> dict[str, Any]:
    """Return an empty budget usage payload."""
    usage: dict[str, Any] = {
        field: 0 for field in (*BUDGET_COUNTER_FIELDS, *TOKEN_FIELDS)
    }
    usage["budget_warnings"] = []
    usage["degradation_reasons"] = []
    return usage


def normalize_budget_usage(value: Any) -> dict[str, Any]:
    """Normalize partial or absent budget usage into the canonical dict shape."""
    usage = empty_budget_usage()
    if value is None:
        return usage
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, Mapping):
        return usage

    for field in (*BUDGET_COUNTER_FIELDS, *TOKEN_FIELDS):
        try:
            usage[field] = int(value.get(field, 0) or 0)
        except (TypeError, ValueError):
            usage[field] = 0

    for field in TOKEN_DETAIL_FIELDS:
        details = value.get(field) or {}
        if isinstance(details, Mapping) and details:
            usage[field] = {
                str(key): int(item)
                for key, item in details.items()
                if isinstance(item, int) and not isinstance(item, bool)
            }

    for field in BUDGET_LIST_FIELDS:
        items = value.get(field, []) or []
        if isinstance(items, str):
            items = [items]
        usage[field] = [str(item) for item in items if str(item)]

    return usage


def _usage_metadata_from(usage: Mapping[str, Any]) -> UsageMetadata:
    """Project a normalized budget payload onto LangChain ``UsageMetadata``."""
    metadata: dict[str, Any] = {
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "total_tokens": usage.get("total_tokens", 0) or 0,
    }
    for field in TOKEN_DETAIL_FIELDS:
        details = usage.get(field) or {}
        if isinstance(details, Mapping) and details:
            metadata[field] = dict(details)
    return cast(UsageMetadata, metadata)


def _apply_usage_metadata(usage: dict[str, Any], metadata: UsageMetadata | None) -> None:
    """Write a ``UsageMetadata`` payload back into the flat budget fields."""
    if not metadata:
        return
    for field in TOKEN_FIELDS:
        value = metadata.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            usage[field] = value
    for field in TOKEN_DETAIL_FIELDS:
        details = metadata.get(field) or {}
        if isinstance(details, Mapping) and details:
            usage[field] = dict(details)


def merge_budget_usage(current_value: Any, new_value: Any = None) -> dict[str, Any]:
    """Merge two budget usage payloads: counters sum, tokens go through ``add_usage``."""
    merged = normalize_budget_usage(current_value)
    incoming = normalize_budget_usage(new_value)

    for field in BUDGET_COUNTER_FIELDS:
        merged[field] += incoming[field]

    combined = add_usage(_usage_metadata_from(merged), _usage_metadata_from(incoming))
    _apply_usage_metadata(merged, cast(UsageMetadata, combined))

    for field in BUDGET_LIST_FIELDS:
        seen = set(merged[field])
        for item in incoming[field]:
            if item not in seen:
                merged[field].append(item)
                seen.add(item)

    return merged


def diff_budget_usage(total_value: Any, baseline_value: Any) -> dict[str, Any]:
    """Return the non-negative usage delta between a total payload and baseline."""
    total = normalize_budget_usage(total_value)
    baseline = normalize_budget_usage(baseline_value)
    diff = empty_budget_usage()

    for field in (*BUDGET_COUNTER_FIELDS, *TOKEN_FIELDS):
        diff[field] = max(0, total[field] - baseline[field])

    for field in TOKEN_DETAIL_FIELDS:
        base_details = baseline.get(field) or {}
        total_details = total.get(field) or {}
        diff[field] = {
            str(key): max(0, value - int(base_details.get(key, 0) or 0))
            for key, value in total_details.items()
        }

    for field in BUDGET_LIST_FIELDS:
        baseline_items = set(baseline[field])
        diff[field] = [item for item in total[field] if item not in baseline_items]

    return diff


def start_budget_capture() -> Token:
    """Start capturing nested budget usage in the current async context."""
    return _BUDGET_CAPTURE.set(empty_budget_usage())


def stop_budget_capture(token: Token) -> dict[str, Any]:
    """Stop nested budget capture and return the captured usage."""
    captured_usage = normalize_budget_usage(_BUDGET_CAPTURE.get())
    _BUDGET_CAPTURE.reset(token)
    return captured_usage


@contextmanager
def budget_capture() -> Iterator[dict[str, Any]]:
    """Capture nested usage and always restore the previous ContextVar value.

    The yielded dictionary is updated in place by :func:`capture_budget_usage`,
    so callers can consume the final delta after the ``with`` block.  The low-level
    start/stop API remains available for compatibility.
    """
    token = start_budget_capture()
    captured = _BUDGET_CAPTURE.get()
    assert captured is not None
    try:
        yield captured
    finally:
        stop_budget_capture(token)


def capture_budget_usage(usage: Any) -> None:
    """Record nested budget usage when a capture context is active."""
    captured_usage = _BUDGET_CAPTURE.get()
    if captured_usage is None:
        return
    merged_usage = merge_budget_usage(captured_usage, usage)
    captured_usage.clear()
    captured_usage.update(merged_usage)


def budget_usage_with_reason(reason: str) -> dict[str, Any]:
    """Create a budget payload that only records a degradation reason."""
    usage = empty_budget_usage()
    usage["degradation_reasons"] = [reason]
    return usage


def budget_usage_with_warning(warning: str) -> dict[str, Any]:
    """Create a budget payload that only records a warning."""
    usage = empty_budget_usage()
    usage["budget_warnings"] = [warning]
    return usage


class ModelAttemptCounter(BaseCallbackHandler):
    """Count real model attempts through LangChain's official start lifecycle.

    One ``on_chat_model_start`` (or ``on_llm_start`` for non-chat LLMs) equals one
    underlying model attempt, so ``Runnable.with_retry`` attempts are counted
    naturally without re-implementing any retry logic.  A chat model emits only
    ``on_chat_model_start``, so a single attempt is never counted twice.
    """

    def __init__(self) -> None:
        """Initialize the attempt counter."""
        super().__init__()
        self.starts = 0

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Count one chat-model attempt."""
        self.starts += 1

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Count one non-chat LLM attempt (chat models use ``on_chat_model_start``)."""
        self.starts += 1


def model_accounting_config(
    config: Any = None,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[Any, ModelAttemptCounter, UsageMetadataCallbackHandler]:
    """Return ``(config, attempt_counter, usage_handler)`` for one model invocation.

    Both handlers are LangChain official: ``ModelAttemptCounter`` consumes the
    ``on_chat_model_start`` lifecycle and ``UsageMetadataCallbackHandler`` consumes
    ``AIMessage.usage_metadata`` from every call, including attempts whose
    structured output fails to parse.  A fresh usage handler per invocation keeps
    parallel agents, section writers, and tools isolated from each other.

    ``metadata`` is optional LangSmith correlation metadata (component, structured
    output).  It does not affect token accounting or the provider call.
    """
    counter = ModelAttemptCounter()
    usage_handler = UsageMetadataCallbackHandler()
    overrides: dict[str, Any] = {"callbacks": [counter, usage_handler]}
    if metadata:
        overrides["metadata"] = dict(metadata)
    merged = merge_configs(ensure_config(config), overrides)
    return merged, counter, usage_handler


def _model_langsmith_metadata(
    model_name: str | None = None,
    *,
    component: str | None = None,
    structured_output: bool = False,
) -> dict[str, Any]:
    """Build LangSmith-only metadata for one model invocation (bounded)."""
    from open_deep_research.observability.langsmith import model_metadata

    return model_metadata(
        component=str(component) if component else None,
        structured_output=structured_output,
    )


def budget_from_model_accounting(
    attempts: int,
    usage_handler: UsageMetadataCallbackHandler,
) -> dict[str, Any]:
    """Bridge the official attempt count and usage metadata into one Budget delta.

    Token aggregation is LangChain's ``add_usage`` over the handler's reported
    ``UsageMetadata`` values; missing usage stays zero and is never estimated.
    """
    usage = empty_budget_usage()
    usage["model_calls"] = max(0, int(attempts or 0))
    combined: UsageMetadata | None = None
    for metadata in usage_handler.usage_metadata.values():
        combined = add_usage(combined, metadata)
    _apply_usage_metadata(usage, combined)
    return usage


async def ainvoke_model_with_budget(
    runnable: Any,
    payload: Any,
    *,
    config: Any = None,
    model_name: str | None = None,
    component: str | None = None,
    structured_output: bool = False,
) -> tuple[Any, dict[str, Any]]:
    """Invoke one model runnable and account its budget.

    Returns ``(response, budget_delta)`` where real attempts come from the official
    chat-model start lifecycle and tokens come from the official
    ``UsageMetadataCallbackHandler`` (every provider-reported ``AIMessage``, not only
    the final response).  Nested calls record the delta into the active Budget
    Capture; if the call raises after real attempts, that fact is captured before
    the original exception is re-raised.

    ``model_name``/``component``/``structured_output`` are optional LangSmith
    correlation hints; they do not affect the provider call or token accounting.
    """
    merged, counter, usage_handler = model_accounting_config(
        config,
        metadata=_model_langsmith_metadata(
            model_name,
            component=component,
            structured_output=structured_output,
        ),
    )
    try:
        response = await runnable.ainvoke(payload, config=merged)
    except BaseException:
        capture_budget_usage(
            budget_from_model_accounting(counter.starts, usage_handler)
        )
        raise
    delta = budget_from_model_accounting(counter.starts, usage_handler)
    capture_budget_usage(delta)
    return response, delta


def invoke_model_with_budget(
    runnable: Any,
    payload: Any,
    *,
    config: Any = None,
    model_name: str | None = None,
    component: str | None = None,
    structured_output: bool = False,
) -> tuple[Any, dict[str, Any]]:
    """Invoke one model synchronously and account its budget.

    Mirrors :func:`ainvoke_model_with_budget`, including attempt and usage
    accounting when the call raises.
    """
    merged, counter, usage_handler = model_accounting_config(
        config,
        metadata=_model_langsmith_metadata(
            model_name,
            component=component,
            structured_output=structured_output,
        ),
    )
    try:
        response = runnable.invoke(payload, config=merged)
    except BaseException:
        capture_budget_usage(
            budget_from_model_accounting(counter.starts, usage_handler)
        )
        raise
    delta = budget_from_model_accounting(counter.starts, usage_handler)
    capture_budget_usage(delta)
    return response, delta


def require_valid_structured_output(envelope: Any) -> Any:
    """Validate a LangChain ``include_raw=True`` structured envelope.

    Raises the parsing error (or a ``ValueError`` when nothing was parsed) so that
    a surrounding ``with_retry`` re-invokes the model instead of re-checking the
    same invalid envelope.
    """
    if not isinstance(envelope, Mapping):
        raise ValueError("Structured output did not return a raw/parsed envelope.")
    parsing_error = envelope.get("parsing_error")
    if parsing_error is not None:
        raise parsing_error
    if envelope.get("parsed") is None:
        raise ValueError("Structured output did not contain a parsed result.")
    return envelope


def structured_output_chain(model: Any, schema: Any, *, max_attempts: int = 0) -> Any:
    """Build a structured-output chain that keeps raw usage and retries real calls.

    Composition: ``with_structured_output(include_raw=True)`` (official raw/parsed/
    parsing_error envelope) ``|`` validation, wrapped in ``with_retry`` so the retry
    covers both the model call and parse validation.
    """
    chain = model.with_structured_output(schema, include_raw=True) | RunnableLambda(
        require_valid_structured_output
    )
    if max_attempts and max_attempts > 0:
        chain = chain.with_retry(stop_after_attempt=max_attempts)
    return chain


def budget_from_native_search() -> dict[str, Any]:
    """Create a budget payload for provider-side native web search."""
    usage = empty_budget_usage()
    usage["tool_calls"] = 1
    usage["search_calls"] = 1
    return usage


def budget_from_tool_calls(
    tool_calls: list[dict[str, Any]],
    tools_by_name: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a budget payload for executed tool calls."""
    usage = empty_budget_usage()
    usage["tool_calls"] = len(tool_calls)
    usage["search_calls"] = sum(
        1 for tool_call in tool_calls if is_search_tool_call(tool_call, tools_by_name)
    )
    return usage


def is_search_tool_call(
    tool_call: dict[str, Any],
    tools_by_name: dict[str, Any] | None = None,
) -> bool:
    """Return whether a tool call is a retrieval/search call."""
    name = tool_call.get("name")
    if name in SEARCH_TOOL_NAMES:
        return True

    if not tools_by_name or name not in tools_by_name:
        return False

    tool = tools_by_name[name]
    if isinstance(tool, dict):
        return (
            tool.get("type") in {"search", "web_search_20250305", "web_search_preview"}
            or tool.get("name") in SEARCH_TOOL_NAMES
        )

    metadata = getattr(tool, "metadata", None) or {}
    return metadata.get("type") == "search" or metadata.get("name") in SEARCH_TOOL_NAMES


def can_spend_model_call(
    configurable: Configuration,
    usage: Any,
    *,
    reserve_final_report_call: bool = False,
) -> bool:
    """Return whether another model call can be made."""
    if not configurable.budget_enabled:
        return True
    limit = configurable.max_model_calls
    if limit is None:
        return True

    reserve = 1 if reserve_final_report_call and configurable.reserve_final_report_call else 0
    effective_limit = max(0, limit - reserve)
    return normalize_budget_usage(usage)["model_calls"] < effective_limit


def can_spend_tool_call(
    configurable: Configuration,
    usage: Any,
    increment: dict[str, Any],
) -> bool:
    """Return whether the tool/search budget can absorb the increment."""
    if not configurable.budget_enabled:
        return True

    current = normalize_budget_usage(usage)
    incoming = normalize_budget_usage(increment)
    if (
        configurable.max_tool_calls is not None
        and current["tool_calls"] + incoming["tool_calls"] > configurable.max_tool_calls
    ):
        return False
    if (
        configurable.max_search_calls is not None
        and current["search_calls"] + incoming["search_calls"] > configurable.max_search_calls
    ):
        return False
    return True


def filter_tool_calls_for_budget(
    configurable: Configuration,
    usage: Any,
    tool_calls: list[dict[str, Any]],
    tools_by_name: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split tool calls into executable and skipped calls under the configured budget."""
    if not configurable.budget_enabled:
        return tool_calls, []

    allowed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    projected_usage = normalize_budget_usage(usage)

    for tool_call in tool_calls:
        increment = budget_from_tool_calls([tool_call], tools_by_name)
        if can_spend_tool_call(configurable, projected_usage, increment):
            allowed.append(tool_call)
            projected_usage = merge_budget_usage(projected_usage, increment)
        else:
            skipped.append(tool_call)

    return allowed, skipped


def available_research_unit_slots(configurable: Configuration, usage: Any) -> int | None:
    """Return how many new researcher subgraphs can start, or None when unlimited."""
    if not configurable.budget_enabled or configurable.max_model_calls is None:
        return None

    current = normalize_budget_usage(usage)["model_calls"]
    reserve = 1 if configurable.reserve_final_report_call else 0
    remaining = max(0, configurable.max_model_calls - reserve - current)
    return remaining // MIN_MODEL_CALLS_PER_RESEARCH_UNIT


def is_over_budget(configurable: Configuration, usage: Any) -> bool:
    """Return whether any configured budget has already been exceeded."""
    if not configurable.budget_enabled:
        return False
    normalized = normalize_budget_usage(usage)
    limits = {
        "model_calls": configurable.max_model_calls,
        "tool_calls": configurable.max_tool_calls,
        "search_calls": configurable.max_search_calls,
        "input_tokens": configurable.max_input_tokens,
        "output_tokens": configurable.max_output_tokens,
    }
    return any(limit is not None and normalized[field] >= limit for field, limit in limits.items())


def get_budget_warnings(configurable: Configuration, usage: Any) -> list[str]:
    """Return warning strings for budgets at or above the configured warning ratio."""
    if not configurable.budget_enabled:
        return []

    normalized = normalize_budget_usage(usage)
    warning_ratio = configurable.budget_warning_ratio
    limits = {
        "model_calls": configurable.max_model_calls,
        "tool_calls": configurable.max_tool_calls,
        "search_calls": configurable.max_search_calls,
        "input_tokens": configurable.max_input_tokens,
        "output_tokens": configurable.max_output_tokens,
    }
    warnings = []
    for field, limit in limits.items():
        if limit is None or limit <= 0:
            continue
        if normalized[field] / limit >= warning_ratio:
            warnings.append(f"{field} is at {normalized[field]}/{limit}.")
    return warnings


def estimate_context_tokens(
    messages: Any,
    *,
    tools: list[Any] | None = None,
) -> int:
    """Estimate the tokens the next model request will carry.

    Delegates to LangChain's ``count_tokens_approximately`` over the real message
    objects (never ``str(messages)``) and includes bound tool schemas when the
    model call sends them.  Tools that cannot be converted to a provider schema
    (for example a custom non-BaseTool wrapper) are ignored rather than failing
    the run.
    """
    if not tools:
        return count_tokens_approximately(messages)
    try:
        return count_tokens_approximately(messages, tools=tools)
    except Exception:  # noqa: BLE001 - a tool schema must never break the estimate
        LOGGER.debug("Tool schema is not convertible for token estimation.", exc_info=True)
        return count_tokens_approximately(messages)


def estimate_text_tokens(text: str) -> int:
    """Estimate tokens for one plain text block via LangChain's approximate counter."""
    if not text:
        return 0
    return count_tokens_approximately([HumanMessage(content=text)])


def context_pressure_ratio(estimated_tokens: int, context_window: int | None) -> float:
    """Return how full the next request is relative to the model context window."""
    if not context_window or context_window <= 0 or estimated_tokens <= 0:
        return 0.0
    return estimated_tokens / context_window


def truncate_text_to_token_budget(text: str, token_budget: int) -> tuple[str, bool]:
    """Approximate-truncate text to fit within a token budget.

    The token count comes from LangChain's ``count_tokens_approximately``; the
    character cut is derived from that count instead of a fixed char-per-token
    ratio.
    """
    if token_budget <= 0:
        return "", bool(text)
    total_tokens = estimate_text_tokens(text)
    if total_tokens <= token_budget:
        return text, False
    keep_chars = max(1, int(len(text) * token_budget / total_tokens))
    candidate = text[:keep_chars]
    while keep_chars > 1 and estimate_text_tokens(candidate) > token_budget:
        keep_chars = max(1, keep_chars - max(1, keep_chars // 10))
        candidate = text[:keep_chars]
    return candidate.rstrip(), True


def remaining_input_tokens(configurable: Configuration, usage: Any) -> int | None:
    """Return remaining input tokens, if the user configured that budget."""
    if not configurable.budget_enabled or configurable.max_input_tokens is None:
        return None
    return configurable.max_input_tokens - normalize_budget_usage(usage)["input_tokens"]


def remaining_output_tokens(configurable: Configuration, usage: Any) -> int | None:
    """Return remaining output tokens, if the user configured that budget."""
    if not configurable.budget_enabled or configurable.max_output_tokens is None:
        return None
    return configurable.max_output_tokens - normalize_budget_usage(usage)["output_tokens"]


def format_budget_summary(configurable: Configuration, usage: Any) -> str:
    """Format a concise budget summary for final reports."""
    if not configurable.budget_enabled:
        return ""

    normalized = normalize_budget_usage(usage)
    lines = [
        "### Budget usage",
        (
            f"- Model calls: {normalized['model_calls']}"
            + _format_limit(configurable.max_model_calls)
        ),
        (
            f"- Tool calls: {normalized['tool_calls']}"
            + _format_limit(configurable.max_tool_calls)
        ),
        (
            f"- Search calls: {normalized['search_calls']}"
            + _format_limit(configurable.max_search_calls)
        ),
        (
            f"- Input tokens: {normalized['input_tokens']}"
            + _format_limit(configurable.max_input_tokens)
        ),
        (
            f"- Output tokens: {normalized['output_tokens']}"
            + _format_limit(configurable.max_output_tokens)
        ),
    ]

    lines.extend(_format_token_details(normalized))

    warnings = normalized["budget_warnings"] + get_budget_warnings(configurable, normalized)
    reasons = normalized["degradation_reasons"]
    if warnings:
        lines.append("- Warnings: " + "; ".join(dict.fromkeys(warnings)))
    if reasons:
        lines.append("- Degradation: " + "; ".join(dict.fromkeys(reasons)))

    return "\n".join(lines)


def append_budget_summary(report: str, configurable: Configuration, usage: Any) -> str:
    """Append budget usage details to a final report when budgeting is enabled."""
    summary = format_budget_summary(configurable, usage)
    if not summary:
        return report
    return f"{report.rstrip()}\n\n---\n\n{summary}"


def _format_token_details(normalized: Mapping[str, Any]) -> list[str]:
    """Render cache/reasoning details only when the provider actually reported them."""
    input_details = normalized.get("input_token_details") or {}
    output_details = normalized.get("output_token_details") or {}
    lines = []

    cache_read = input_details.get("cache_read")
    cache_creation = input_details.get("cache_creation")
    if cache_read or cache_creation:
        lines.append(
            "- Cache tokens (read/creation): "
            f"{int(cache_read or 0)} / {int(cache_creation or 0)}"
        )

    reasoning = output_details.get("reasoning")
    if reasoning:
        lines.append(f"- Reasoning tokens: {int(reasoning)}")

    return lines


def _format_limit(limit: int | None) -> str:
    if limit is None:
        return ""
    return f" / {limit}"

