"""Business failure accounting and bounded step ownership; no workflow cursor."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from contextlib import nullcontext
from copy import deepcopy
from typing import Any

from langchain_core.runnables import RunnableConfig

from open_deep_research.budget import (
    ModelBudgetExceeded,
    budget_capture,
)
from open_deep_research.observability.telemetry import annotate, business_span
from open_deep_research.state import DeepResearchState
from open_deep_research.workflow.execution import ExecutionControlError


def failure_kind(error: BaseException) -> str:
    """Classify the original failure without turning partial results into success."""
    cause = error
    for _ in range(8):
        if isinstance(cause, ExecutionControlError):
            return "execution_control_error"
        cause = getattr(cause, "__cause__", None)
        if cause is None:
            break
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ModelBudgetExceeded):
        return "budget_exhausted"
    if type(error).__name__ in {"ResearchGraphSettlementError", "ToolCallError"}:
        return "tool_or_settlement_error"
    if type(error).__module__.startswith(("agents", "openai")):
        return "model_error"
    return "error"


async def _step(
    name: str,
    operation: Callable[..., Coroutine[Any, Any, Any]],
    state: DeepResearchState,
    config: RunnableConfig,
    timeout: float | None,
) -> Any:
    captured: dict[str, Any] = {}
    original: list[BaseException] = []

    async def execute() -> Any:
        span_name = (
            "research_review"
            if name == "research_review"
            else "report_generation"
            if name
            in {"section_writer", "write_final_sections", "compile_final_report"}
            else None
        )
        observation = (
            business_span(span_name, step=name) if span_name else nullcontext()
        )
        with observation, budget_capture() as usage:
            try:
                return await operation(deepcopy(state), config)
            except BaseException as exc:
                original.append(exc)
                raise
            finally:
                captured.update(usage)
                # Existing accounting remains the source; these stage totals
                # are descriptive and must not be summed with child LLM spans.
                annotate(
                    **{
                        key: usage[key]
                        for key in (
                            "model_calls",
                            "input_tokens",
                            "output_tokens",
                            "total_tokens",
                        )
                        if key in usage
                    }
                )

    pending = asyncio.create_task(execute())
    try:
        return await asyncio.wait_for(pending, timeout=timeout)
    except BaseException as exc:
        # A repeated cancellation can interrupt wait_for's cleanup wait. Keep
        # the existing child alive until its own settlement/capture has finished.
        drain = asyncio.gather(pending, return_exceptions=True)
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                pass
        drain.result()
        source = original[-1] if original else exc
        setattr(exc, "workflow_step", name)
        # SDK Runtime already owns all its nested accounting. Ordinary business
        # steps use the existing ambient capture, including failed model calls.
        setattr(exc, "budget_usage", getattr(source, "budget_usage", captured))
        setattr(exc, "context_settlement", getattr(source, "context_settlement", None))
        setattr(exc, "context_harness", getattr(source, "context_harness", None))
        # A business step can own parallel section calls internally. Its public
        # boundary has already totalled those costs; they are not role patches.
        if hasattr(source, "parallel_errors"):
            setattr(exc, "nested_failures", getattr(source, "parallel_errors"))
        for field in ("parallel_results", "parallel_errors"):
            if hasattr(exc, field):
                delattr(exc, field)
        raise


SAFE_STEPS = frozenset(
    {
        "enrich_query_images",
        "clarify_with_user",
        "write_research_brief",
        "plan_report_sections",
        "research_review",
        "section_writer",
        "write_final_sections",
    }
)
