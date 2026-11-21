# 为单次 Agent 调用提供增量滚动摘要，不保存第二份完整会话历史。
"""Incremental rolling compaction for one SDK Agent invocation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from open_deep_research.llm.generation import generate_structured


# 承载滚动压缩输出的唯一结构化模型。
class RollingCompactOutput(BaseModel):
    """Summary of older, already-closed agent history."""

    rolling_summary: str = Field(description="Merged summary of compacted history.")


# 只汇总调用方选定的旧历史片段，并合并进上一份摘要。
async def summarize_history(
    compactable_text: str,
    *,
    previous_summary: str,
    task_context: str,
    model_name: str,
    config: Mapping[str, Any] | None = None,
    max_tokens: int | None = None,
    max_retries: int = 3,
) -> tuple[str, dict[str, Any]]:
    """Summarize one admitted history slice and merge it into the summary."""
    prompt = (
        "You are the incremental history compactor for one research agent. "
        "Summarize only the newly compactable reasoning and tool progress below and "
        "merge it into the previous rolling summary. Do not invent evidence, URLs, "
        "IDs, or research conclusions. Keep source URLs, concrete dates, numeric "
        "findings and unresolved questions verbatim when present. Do not call tools.\n\n"
        f"Current task:\n{task_context}\n\n"
        f"Previous rolling summary:\n{previous_summary or 'None'}\n\n"
        f"New compactable steps:\n{compactable_text}\n\n"
        "Return RollingCompactOutput."
    )
    response, budget_usage = await generate_structured(
        model_name,
        prompt,
        RollingCompactOutput,
        config=config,
        max_tokens=max_tokens,
        max_attempts=max_retries,
        component="rolling_compact",
    )
    summary = response.rolling_summary.strip()
    if not summary:
        raise ValueError("Rolling compaction returned an empty summary.")
    return summary, budget_usage


__all__ = ["RollingCompactOutput", "summarize_history"]
