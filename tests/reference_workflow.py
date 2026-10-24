"""Frozen Phase 3/4 behavioral oracle; NEVER a production execution path."""

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
from open_deep_research.workflow.execution import (
    execution_observer,
    gather_owned,
    observe_execution,
)
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


class PlanApprovalRequired(Exception):
    """Return ownership to the worker after the immutable plan has been committed."""


WORKFLOW_VERSION = "python-phase4-v1"
RESUME_CURSORS = frozenset(
    {
        "enrich_query_images",
        "clarify_with_user",
        "write_research_brief",
        "plan_report_sections",
        "approval",
        "research_init",
        "research_agents",
        "research_review",
        "followup",
        "risk_assessment_agent",
        "response_strategy_agent",
        "section_writer",
        "write_final_sections",
        "compile_final_report",
        "done",
    }
)


class PythonWorkflow:
    """Execute intake, joined research rounds, ordered consumers and reporting.

    A durable worker may observe committed boundaries and supply a verified
    cursor. Storage, admission and recovery policy belong to that worker.
    The SDK is the only Agent loop.
    """

    async def ainvoke(
        self,
        input: Mapping[str, Any],
        config: RunnableConfig | None = None,
        *,
        start_at: Literal["intake", "research"] = "intake",
        resume: Mapping[str, Any] | None = None,
    ) -> DeepResearchState:
        """Run once; optionally accept an already-prepared brief and section plan."""
        config = config or {}
        settings = Configuration.from_runnable_config(config)
        if (
            config.get("configurable", {}).get(
                "business_agent_runtime", "openai_agents"
            )
            != "openai_agents"
        ):
            raise ValueError(
                "Python Workflow requires business_agent_runtime=openai_agents"
            )
        if settings.allow_plan_feedback and execution_observer.get() is None:
            raise ValueError(
                "Plan approval requires a durable worker approval observer"
            )
        if start_at not in {"intake", "research"}:
            raise ValueError("start_at must be intake or research")
        state = copy_state(input)
        if resume is not None and (
            resume.get("version") != WORKFLOW_VERSION
            or resume.get("cursor") not in RESUME_CURSORS
        ):
            raise ValueError("Incompatible Workflow checkpoint")
        cursor = (
            str(resume["cursor"])
            if resume
            else ("enrich_query_images" if start_at == "intake" else "research_init")
        )
        initial_budget = deepcopy(state.get("runtime", {}).get("budget", {}))
        timeout = settings.workflow_step_timeout_seconds

        async def begin(name: str) -> None:
            await observe_execution("step_started", step=name, state=deepcopy(state))

        async def commit(name: str, next_cursor: str) -> None:
            nonlocal cursor
            cursor = next_cursor
            await observe_execution(
                "checkpoint",
                step=name,
                state=deepcopy(state),
                cursor=cursor,
                version=WORKFLOW_VERSION,
            )

        async def call(
            name: str, operation: Callable[..., Coroutine[Any, Any, Any]]
        ) -> Any:
            await begin(name)
            return await _step(name, operation, state, config, timeout)

        try:
            if cursor == "enrich_query_images":
                apply_patch(
                    state, await call("enrich_query_images", intake.enrich_query_images)
                )
                await commit("enrich_query_images", "clarify_with_user")
            if cursor == "clarify_with_user":
                decision = await call("clarify_with_user", intake.clarification_step)
                apply_patch(state, decision.update or {})
                await commit(
                    "clarify_with_user",
                    "done" if decision.goto == "__end__" else "write_research_brief",
                )
                if decision.goto == "__end__":
                    return state
            if cursor == "write_research_brief":
                decision = await call(
                    "write_research_brief", intake.research_brief_step
                )
                apply_patch(state, decision.update or {})
                await commit(
                    "write_research_brief",
                    "plan_report_sections"
                    if decision.goto == "plan_report_sections"
                    else "approval",
                )
            if cursor == "plan_report_sections":
                decision = await call("plan_report_sections", intake.section_plan_step)
                apply_patch(state, decision.update or {})
                await commit("plan_report_sections", "approval")
            if cursor == "approval":
                if settings.allow_plan_feedback:
                    await observe_execution("approval_required", state=deepcopy(state))
                    raise PlanApprovalRequired()
                await commit("plan_ready", "research_init")

            # Match the original research_phase's new-run boundary: retain private
            # memory and plan, clear prior reports/summaries/tasks/Working Context.
            if cursor == "research_init":
                await begin("research_init")
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
                        "agents": {
                            "type": "override",
                            "value": agents_for_new_run(state),
                        },
                        "research": {
                            "type": "override",
                            "value": {"run_id": run_id, "working_contexts": {}},
                        },
                    },
                )
                await commit("research_init", "research_agents")
            # A recovered cursor never repeats this destructive new-run boundary.
            while cursor in {"research_agents", "research_review", "followup"}:
                if cursor == "research_agents":
                    packets = (
                        {role: state for role in RESEARCH_ROLES}
                        if state.get("workflow", {}).get("review") is None
                        else research.followup_assignments(state, settings)
                    )
                    await begin("research_agents")
                    patches = await gather_owned(
                        {
                            role: _step(
                                f"{role}_agent",
                                getattr(research, f"{role}_agent"),
                                role_snapshot(cast(DeepResearchState, packet), role),
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
                    await commit("research_agents", "research_review")
                if cursor == "research_review":
                    apply_patch(
                        state, await call("research_review", research.research_review)
                    )
                    await commit("research_review", "followup")
                if cursor == "followup":
                    # followup_assignments returns packets for the NEXT round.
                    packets = research.followup_assignments(state, settings)
                    if not packets:
                        await commit("followup", "risk_assessment_agent")
                    else:
                        # Packet construction is pure and re-computable.
                        await commit("followup", "research_agents")

            for role, next_cursor in (
                ("risk_assessment", "response_strategy_agent"),
                ("response_strategy", "section_writer"),
            ):
                if cursor != f"{role}_agent":
                    continue
                await begin(f"{role}_agent")
                patch = await _step(
                    f"{role}_agent",
                    getattr(research, f"{role}_agent"),
                    role_snapshot(state, role),
                    config,
                    timeout,
                )
                apply_patch(state, patch)
                await commit(f"{role}_agent", next_cursor)
            for name, next_cursor in (
                ("section_writer", "write_final_sections"),
                ("write_final_sections", "compile_final_report"),
                ("compile_final_report", "done"),
            ):
                if cursor != name:
                    continue
                operation = getattr(reporting, name)
                if name == "compile_final_report":
                    operation = partial(operation, fail_on_error=True)
                apply_patch(state, await call(name, operation))
                await commit(name, next_cursor)
            return state
        except PlanApprovalRequired:
            raise
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
