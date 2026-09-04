"""Message lifecycle utilities for Micro and Rolling Compaction."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from open_deep_research.budget import budget_from_model_response, estimate_tokens
from open_deep_research.observability import observe_model_ainvoke
from open_deep_research.research_graph.models import (
    RollingCompactOutput,
    WriteReceipt,
)


@dataclass
class CompactionResult:
    """Result of deterministic Micro Compact."""

    messages: list[Any]
    compacted_tool_call_ids: list[str] = field(default_factory=list)
    tokens_removed: int = 0


@dataclass
class RollingCompactionResult:
    """Result of one incremental Rolling Compact."""

    messages: list[Any]
    rolling_summary: str
    budget_usage: dict[str, Any] = field(default_factory=dict)
    tokens_removed: int = 0


def micro_compact_messages(
    messages: Iterable[Any],
    receipts: dict[str, WriteReceipt | str],
    *,
    recent_raw_steps: int = 3,
) -> CompactionResult:
    """Replace old persisted ToolMessage bodies with small receipts.

    The corresponding AI message and ``tool_call_id`` are retained, so the
    provider's tool-call pairing remains valid.  Results without a successful
    persistence receipt are never removed.
    """
    items = list(messages)
    keep_ids = _recent_tool_call_ids(items, recent_raw_steps)
    compacted: list[Any] = []
    compacted_ids: list[str] = []
    removed = 0
    for message in items:
        tool_call_id = str(getattr(message, "tool_call_id", "") or "")
        if (
            isinstance(message, ToolMessage)
            and tool_call_id in receipts
            and tool_call_id not in keep_ids
        ):
            receipt = receipts[tool_call_id]
            receipt_text = receipt.as_text() if isinstance(receipt, WriteReceipt) else str(receipt)
            old_text = str(getattr(message, "content", "") or "")
            replacement = _replace_message_content(message, receipt_text)
            compacted.append(replacement)
            compacted_ids.append(tool_call_id)
            removed += max(0, estimate_tokens(old_text) - estimate_tokens(receipt_text))
        else:
            compacted.append(message)
    return CompactionResult(
        messages=compacted,
        compacted_tool_call_ids=compacted_ids,
        tokens_removed=removed,
    )


def context_token_estimate(messages: Iterable[Any], extra_context: str = "") -> int:
    """Estimate the model input size without serializing provider metadata."""
    text = extra_context + "\n" + "\n".join(
        str(getattr(message, "content", message) or "") for message in messages
    )
    return estimate_tokens(text)


def should_rolling_compact(
    messages: Iterable[Any],
    *,
    extra_context: str = "",
    model_context_capacity: int | None,
    threshold_ratio: float = 0.75,
) -> bool:
    """Return whether context capacity, not research budget, crossed the trigger."""
    if not model_context_capacity or model_context_capacity <= 0:
        return False
    return context_token_estimate(messages, extra_context) >= int(
        model_context_capacity * threshold_ratio
    )


async def rolling_compact(
    messages: Iterable[Any],
    *,
    previous_summary: str,
    protected_context: str,
    model: Any,
    model_name: str,
    max_retries: int = 3,
    recent_raw_steps: int = 3,
) -> RollingCompactionResult:
    """Incrementally summarize only newly compactable history.

    The previous summary is the only old summary supplied.  Older raw steps are
    replaced as complete AI/tool units; current task/protected context and the
    newest raw steps remain outside the summary.
    """
    items = list(messages)
    keep_start = _recent_step_start(items, recent_raw_steps)
    if keep_start is None or keep_start <= 1:
        return RollingCompactionResult(
            messages=items,
            rolling_summary=previous_summary,
        )
    compactable = items[1:keep_start]
    compactable_text = _render_messages(compactable)
    prompt = (
        "You are the incremental rolling context compactor for a ReAct harness. "
        "Summarize only the newly compactable reasoning/progress below and merge it "
        "into the previous rolling summary. Do not invent evidence, URLs, IDs, or "
        "research conclusions. Durable evidence lives in the Research Graph; retain "
        "important graph IDs and unresolved questions when present. Do not call tools.\n\n"
        f"Protected context:\n{protected_context}\n\n"
        f"Previous rolling summary:\n{previous_summary or 'None'}\n\n"
        f"New compactable steps:\n{compactable_text}\n\n"
        "Return RollingCompactOutput."
    )
    structured = model
    if hasattr(structured, "with_structured_output"):
        structured = structured.with_structured_output(RollingCompactOutput)
    if hasattr(structured, "with_retry"):
        structured = structured.with_retry(stop_after_attempt=max_retries)
    response = await observe_model_ainvoke(
        structured,
        [HumanMessage(content=prompt)],
        observer_model=model_name,
        observer_structured_output=True,
        observer_component="rolling_compact",
    )
    output = _coerce_rolling_output(response)
    summary = output.rolling_summary.strip()
    replacement = HumanMessage(
        content=(
            "[Rolling Research Harness Summary]\n"
            f"{summary}\n"
            "[Older ReAct steps compacted; durable provenance remains in the Research Graph.]"
        )
    )
    kept = items[keep_start:]
    result_messages = [items[0], replacement, *kept]
    return RollingCompactionResult(
        messages=result_messages,
        rolling_summary=summary,
        budget_usage=budget_from_model_response(response),
        tokens_removed=max(0, estimate_tokens(compactable_text) - estimate_tokens(summary)),
    )


def render_protected_context(
    *,
    current_task: str,
    working_context: str,
    relevant_subgraph: str,
    rolling_summary: str,
) -> str:
    """Build the non-history context block that rolling compaction protects."""
    return json.dumps(
        {
            "current_task": current_task,
            "working_context": working_context,
            "relevant_subgraph": relevant_subgraph,
            "rolling_summary": rolling_summary,
        },
        ensure_ascii=False,
    )


def _replace_message_content(message: Any, content: str) -> Any:
    model_copy = getattr(message, "model_copy", None)
    if callable(model_copy):
        return model_copy(update={"content": content})
    if isinstance(message, dict):
        replacement = dict(message)
        replacement["content"] = content
        return replacement
    return message


def _recent_tool_call_ids(messages: list[Any], recent_raw_steps: int) -> set[str]:
    ai_indices = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, AIMessage) and getattr(message, "tool_calls", None)
    ]
    recent = max(0, int(recent_raw_steps))
    keep_ai_indices = set(ai_indices[-recent:]) if recent else set()
    keep_ids: set[str] = set()
    for index in keep_ai_indices:
        for call in getattr(messages[index], "tool_calls", []) or []:
            if isinstance(call, dict) and call.get("id"):
                keep_ids.add(str(call["id"]))
    return keep_ids


def _recent_step_start(messages: list[Any], recent_raw_steps: int) -> int | None:
    starts = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, AIMessage) and getattr(message, "tool_calls", None)
    ]
    if not starts:
        return None
    kept_starts = starts[-max(0, int(recent_raw_steps)) :]
    if not kept_starts:
        return len(messages)
    return kept_starts[0]


def _render_messages(messages: Iterable[Any], *, max_each: int = 5000, max_total: int = 30_000) -> str:
    parts: list[str] = []
    total = 0
    for index, message in enumerate(messages, start=1):
        content = str(getattr(message, "content", message) or "")
        if len(content) > max_each:
            content = content[:max_each].rstrip() + "…"
        part = f"{index}. {getattr(message, 'type', type(message).__name__)}: {content}"
        if total + len(part) > max_total:
            break
        parts.append(part)
        total += len(part)
    return "\n".join(parts) or "None"


def _coerce_rolling_output(response: Any) -> RollingCompactOutput:
    if isinstance(response, RollingCompactOutput):
        return response
    if isinstance(response, dict):
        return RollingCompactOutput.model_validate(response)
    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        return RollingCompactOutput.model_validate(model_dump())
    content = getattr(response, "content", "")
    return RollingCompactOutput(rolling_summary=str(content or ""))


__all__ = [
    "CompactionResult",
    "RollingCompactionResult",
    "context_token_estimate",
    "micro_compact_messages",
    "render_protected_context",
    "rolling_compact",
    "should_rolling_compact",
]
