"""Run the existing public-opinion business workflow with Python asyncio."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from copy import deepcopy
from functools import partial
from typing import Any, Literal, cast

from langchain_core.runnables import RunnableConfig

from open_deep_research.budget import (
    ModelBudgetExceeded,
    budget_capture,
    capture_budget_usage,
    diff_budget_usage,
)
from open_deep_research.configuration import Configuration
from open_deep_research.state import DeepResearchState
from open_deep_research.workflow import intake, reporting, research
from open_deep_research.workflow.execution import gather_owned
from open_deep_research.workflow.python_state import (
    RESEARCH_ROLES,
    apply_patch,
    copy_state,
    merge_research_patches,
    role_snapshot,
)
from open_deep_research.workflow.state_utils import (
    agents_for_new_run,
    resolve_research_run_id,
)


def failure_kind(error: BaseException) -> str:
    """Classify the original failure without turning partial results into success."""
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


class WorkflowExecutionError(RuntimeError):
    """Expose partial State, exact accounted deltas and all original failures."""

    def __init__(
        self,
        failures: Mapping[str, BaseException],
        state: DeepResearchState,
        initial_budget: dict[str, Any],
    ):
        """Keep diagnostic state without claiming persistence or a resumable run."""
        self.failures = dict(failures)
        self.kinds = {name: failure_kind(exc) for name, exc in failures.items()}
        self.state = deepcopy(state)
        self.budget_usage = diff_budget_usage(
            state.get("runtime", {}).get("budget", {}), initial_budget
        )
        self.context_settlements = {
            name: getattr(exc, "context_settlement", None)
            for name, exc in failures.items()
        }
        self.context_harnesses = {
            name: getattr(exc, "context_harness", None)
            for name, exc in failures.items()
        }
        super().__init__(
            "Workflow failed: "
            + ", ".join(f"{name} ({kind})" for name, kind in self.kinds.items())
        )


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
        with budget_capture() as usage:
            try:
                return await operation(deepcopy(state), config)
            except BaseException as exc:
                original.append(exc)
                raise
            finally:
                captured.update(usage)

    try:
        return await asyncio.wait_for(execute(), timeout=timeout)
    except BaseException as exc:
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


def _account_failure(
    state: DeepResearchState, error: BaseException
) -> dict[str, BaseException]:
    results = getattr(error, "parallel_results", {})
    failures = getattr(error, "parallel_errors", None) or {
        getattr(error, "workflow_step", "workflow"): error
    }
    if results:
        try:
            merge_research_patches(state, results)
        except ValueError:
            # Invalid/conflicting patches are never committed, but the work
            # actually performed still costs usage.
            for patch in results.values():
                apply_patch(
                    state,
                    {"runtime": {"budget": patch.get("runtime", {}).get("budget", {})}},
                )
    for failure in failures.values():
        apply_patch(
            state, {"runtime": {"budget": getattr(failure, "budget_usage", {})}}
        )
    return failures


class PythonWorkflow:
    """Execute intake, joined research rounds, ordered consumers and reporting.

    This object owns no per-run mutable state and offers no Session, streaming,
    checkpoint, approval or crash-resume API. The SDK is the only Agent loop.
    """

    async def ainvoke(
        self,
        input: Mapping[str, Any],
        config: RunnableConfig | None = None,
        *,
        start_at: Literal["intake", "research"] = "intake",
    ) -> DeepResearchState:
        """Run once; optionally accept an already-prepared brief and section plan."""
        config = config or {}
        settings = Configuration.from_runnable_config(config)
        if settings.business_agent_runtime != "openai_agents":
            raise ValueError(
                "Python Workflow requires business_agent_runtime=openai_agents"
            )
        if settings.allow_plan_feedback:
            raise ValueError(
                "Plan approval remains on the LangGraph path until Phase 4"
            )
        if start_at not in {"intake", "research"}:
            raise ValueError("start_at must be intake or research")
        state = copy_state(input)
        initial_budget = deepcopy(state.get("runtime", {}).get("budget", {}))
        timeout = settings.workflow_step_timeout_seconds

        async def call(
            name: str, operation: Callable[..., Coroutine[Any, Any, Any]]
        ) -> Any:
            return await _step(name, operation, state, config, timeout)

        try:
            if start_at == "intake":
                apply_patch(
                    state, await call("enrich_query_images", intake.enrich_query_images)
                )
                decision = await call("clarify_with_user", intake.clarification_step)
                apply_patch(state, decision.update or {})
                if decision.goto == "__end__":
                    return state
                decision = await call(
                    "write_research_brief", intake.research_brief_step
                )
                apply_patch(state, decision.update or {})
                if decision.goto == "plan_report_sections":
                    decision = await call(
                        "plan_report_sections", intake.section_plan_step
                    )
                    apply_patch(state, decision.update or {})

            # Match the original research_phase's new-run boundary: retain private
            # memory and plan, clear prior reports/summaries/tasks/Working Context.
            run_id = resolve_research_run_id(state, config)
            apply_patch(
                state,
                {
                    "workflow": {
                        "type": "override",
                        "value": {
                            "brief": state.get("workflow", {}).get("brief", ""),
                            "round": 1,
                            "review": None,
                            "pending_tasks": [],
                            "completed_tasks": [],
                        },
                    },
                    "agents": {"type": "override", "value": agents_for_new_run(state)},
                    "research": {
                        "type": "override",
                        "value": {"run_id": run_id, "working_contexts": {}},
                    },
                },
            )
            packets: dict[str, DeepResearchState] = {
                role: state for role in RESEARCH_ROLES
            }
            while True:
                patches = await gather_owned(
                    {
                        role: _step(
                            f"{role}_agent",
                            getattr(research, f"{role}_agent"),
                            role_snapshot(packet, role),
                            config,
                            timeout,
                        )
                        for role, packet in packets.items()
                    }
                )
                try:
                    merge_research_patches(state, patches)
                except ValueError as exc:
                    setattr(exc, "parallel_results", patches)
                    raise
                apply_patch(
                    state, await call("research_review", research.research_review)
                )
                packets = {
                    role: cast(DeepResearchState, packet)
                    for role, packet in research.followup_assignments(
                        state, settings
                    ).items()
                }
                if not packets:
                    break

            for role in ("risk_assessment", "response_strategy"):
                patch = await _step(
                    f"{role}_agent",
                    getattr(research, f"{role}_agent"),
                    role_snapshot(state, role),
                    config,
                    timeout,
                )
                apply_patch(state, patch)
            for name in (
                "section_writer",
                "write_final_sections",
                "compile_final_report",
            ):
                operation = getattr(reporting, name)
                if name == "compile_final_report":
                    operation = partial(operation, fail_on_error=True)
                apply_patch(state, await call(name, operation))
            return state
        except BaseException as exc:
            failures = _account_failure(state, exc)
            if isinstance(exc, asyncio.CancelledError):
                setattr(exc, "workflow_state", deepcopy(state))
                setattr(exc, "workflow_failures", failures)
                setattr(
                    exc,
                    "budget_usage",
                    diff_budget_usage(
                        state.get("runtime", {}).get("budget", {}), initial_budget
                    ),
                )
                raise
            raise WorkflowExecutionError(failures, state, initial_budget) from exc
        finally:
            capture_budget_usage(
                diff_budget_usage(
                    state.get("runtime", {}).get("budget", {}), initial_budget
                )
            )
