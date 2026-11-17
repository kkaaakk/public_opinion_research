# 为单次 Agent 调用提供增量滚动摘要，不保存第二份完整会话历史。
"""Incremental rolling compaction for one SDK Agent invocation."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from open_deep_research.budget import ainvoke_model_with_budget, structured_output_chain


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
    model: Any,
    model_name: str,
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
    structured = structured_output_chain(
        model, RollingCompactOutput, max_attempts=max_retries
    )
    response, budget_usage = await ainvoke_model_with_budget(
        structured,
        [HumanMessage(content=prompt)],
        model_name=model_name,
        structured_output=True,
        component="rolling_compact",
    )
    summary = _coerce_output(response["parsed"]).rolling_summary.strip()
    if not summary:
        raise ValueError("Rolling compaction returned an empty summary.")
    return summary, budget_usage


# 兼容模型对象、字典和普通响应，统一为滚动压缩输出模型。
def _coerce_output(response: Any) -> RollingCompactOutput:
    if isinstance(response, RollingCompactOutput):
        return response
    if isinstance(response, dict):
        return RollingCompactOutput.model_validate(response)
    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        return RollingCompactOutput.model_validate(model_dump())
    return RollingCompactOutput(rolling_summary=str(getattr(response, "content", "") or ""))


__all__ = ["RollingCompactOutput", "summarize_history"]
