"""SDK context lifecycle for one business Agent invocation.

The SDK owns the agent loop and ToolExecutor owns the durable tool lifecycle.
This harness records the tool results the executor hands back, keeps the native
protocol pairing intact, and provides the single durable seam for bounded
history compaction.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal
from uuid import uuid4

from agents import Agent, FunctionTool, RunConfig, RunContextWrapper, RunHooks, Runner
from agents.exceptions import AgentsException
from agents.items import ModelResponse, TResponseInputItem
from agents.memory import Session
from agents.result import RunResult
from agents.run import CallModelData, ModelInputData
from agents.tool import Tool
from agents.tool_context import ToolContext
from dbos import DBOS
from dbos import error as dbos_error
from dbos_openai_agents import DBOSRunner

from open_deep_research.budget import (
    ModelBudgetExceeded,
    budget_capture,
    budget_from_sdk_usage,
    capture_budget_usage,
    context_pressure_ratio,
    estimate_text_tokens,
)
from open_deep_research.configuration import Configuration
from open_deep_research.llm.context import resolve_context_capacity
from open_deep_research.observability.telemetry import (
    annotate,
    business_span,
    correlation,
    observed,
    telemetry_enabled,
)
from open_deep_research.runtime.compaction import summarize_history
from open_deep_research.web.run_data import digest
from open_deep_research.workflow.execution import (
    ExecutionControlError,
    is_cancellation,
    is_execution_control_failure,
    native_step_available,
    owned_cleanup,
    stage_context,
)

LOGGER = logging.getLogger(__name__)

ToolStatus = Literal["running", "succeeded", "failed", "cancelled", "unobserved"]


class SDKContextLimitExceeded(RuntimeError):
    """The protected SDK input cannot fit safely after compaction."""


class _ReasoningStopped(AgentsException):
    """End SDK reasoning with the SDK's partial run data for the role report."""


@dataclass
class ObservedToolResult:
    """One native tool call and the execution facts the executor reported."""

    sdk_run_id: str
    tool_call_id: str
    tool_name: str
    arguments_json: str
    args: Any
    model_turn: int
    status: ToolStatus = "running"
    observation: Any = None
    error_type: str | None = None
    operation_id: str | None = None
    effect_status: str | None = None


@dataclass(frozen=True)
class BatchSettlement:
    """Report live tool calls without claiming durable execution."""

    reason: str
    in_flight_call_ids: tuple[str, ...]
    error_type: str | None = None

    @property
    def succeeded(self) -> bool:
        """Return whether every observed tool call reached a terminal state."""
        return not (self.in_flight_call_ids or self.error_type)


class SDKContextHarness(RunHooks):
    """Bind exactly one SDK invocation to one research role and task.

    ToolExecutor owns tool execution, the ``por.agent_tool`` Step and
    de-duplication; this harness records what it hands back. Decorated
    FunctionTools bound directly to the SDK remain observable but are never
    presumed successful: their error formatter can hide failures.
    """

    def __init__(
        self,
        *,
        role: str,
        task_id: str,
        research_round: int = 1,
        config: Configuration | None = None,
        runtime_config: Mapping[str, Any] | None = None,
        settlement_timeout: float = 30.0,
    ) -> None:
        """Configure one run-local collector for this role invocation."""
        if settlement_timeout <= 0:
            raise ValueError("Settlement timeout must be positive.")
        self.role = str(role)
        self.task_id = str(task_id)
        self.research_round = max(1, int(research_round or 1))
        self.sdk_run_id = (
            f"SDK_{uuid4().hex}"
            if DBOS.workflow_id is None
            else f"SDK_{DBOS.workflow_id}_{self.role}_{self.task_id}"
        )
        self.settlement_timeout = settlement_timeout
        self.config = config
        self.runtime_config = runtime_config
        self.results: dict[str, ObservedToolResult] = {}
        self.model_turn = 0
        self.last_settlement: BatchSettlement | None = None
        self._response_starts: list[str] = []
        self._compacted_turns: set[int] = set()
        self.rolling_summary = ""
        self.estimated_input_tokens: int | None = None
        self.tool_executor: Any | None = None
        self._started = False
        self._closing = False
        self.model_name: str | None = None
        self._model_started_at: float | None = None
        self._agent_started_at: float | None = None
        self._tool_started_at: dict[str, float] = {}

    async def _observe(self, kind: str, **metadata: Any) -> None:
        """Send metadata at existing SDK/Harness seams; never send content."""
        annotate(**metadata)
        if scope := stage_context.get():
            await scope.observe(
                kind,
                dict(
                    role=self.role,
                    invocation_id=self.sdk_run_id,
                    task_id=self.task_id,
                    research_round=self.research_round,
                    model_turn=self.model_turn,
                    **metadata,
                ),
                # Sibling tools can finish in a different order after replay.
                # This delivery is idempotent by call ID and uses no native slot.
                call_id=metadata.get("tool_call_id") if kind == "tool_result" else None,
            )

    async def durable_filter_model_input(self, data: CallModelData[Any]) -> ModelInputData:
        """Checkpoint the filtered window and summary costs for this turn."""
        self.estimated_input_tokens = None
        try:
            with budget_capture() as usage:
                async def prepare():
                    try:
                        result = await self.filter_model_input(data)
                    except BaseException as exc:
                        exc.budget_usage = usage
                        raise
                    return {
                        "model_input": (
                            None
                            if result is data.model_data
                            else {"input": result.input, "instructions": result.instructions}
                        ),
                        "compacted_turns": sorted(self._compacted_turns),
                        "rolling_summary": self.rolling_summary,
                        "usage": usage,
                    }

                saved = await DBOS.run_step_async(
                    {"name": "por.agent_context", "preemptible": True}, prepare
                )
        except BaseException as exc:
            capture_budget_usage(getattr(exc, "budget_usage", usage))
            raise
        capture_budget_usage(saved["usage"])
        self._compacted_turns = set(saved["compacted_turns"])
        self.rolling_summary = saved["rolling_summary"]
        if saved["model_input"] is None:
            return data.model_data
        return ModelInputData(**saved["model_input"])

    def _check(self, context: Any) -> None:
        if context is not self:
            raise ValueError("SDK invocation context does not match this harness.")

    async def filter_model_input(self, data: CallModelData[Any]) -> ModelInputData:
        """Apply the rolling summary to the model's copy of the SDK input."""
        self._check(data.context)
        return await self._compact_history(data.model_data, data.agent)

    @property
    def closing(self) -> bool:
        """Return whether the invocation has started its exit settlement."""
        return self._closing

    def start_tool(self, context: ToolContext[Any]) -> ObservedToolResult:
        """Return the single observed record for one native tool call."""
        self._check(context.context)
        call_id = context.tool_call_id
        if not call_id:
            raise ValueError("A native tool call ID is required.")
        existing = self.results.get(call_id)
        if (
            existing is not None
            and existing.tool_name == context.tool_name
            and existing.arguments_json == context.tool_arguments
        ):
            return existing
        try:
            args = json.loads(context.tool_arguments)
        except ValueError:
            args = context.tool_arguments
        if existing is not None:
            if existing.tool_name != context.tool_name or json.dumps(
                existing.args, sort_keys=True
            ) != json.dumps(args, sort_keys=True):
                raise ValueError("Tool call ID was reused for a different invocation.")
            return existing
        result = ObservedToolResult(
            sdk_run_id=self.sdk_run_id,
            tool_call_id=call_id,
            tool_name=context.tool_name,
            arguments_json=context.tool_arguments,
            args=args,
            model_turn=self.model_turn,
        )
        self.results[call_id] = result
        return result

    def restore_tool(self, saved: dict[str, Any]) -> None:
        """Restore one committed ``por.agent_tool`` record into this collector."""
        restored = ObservedToolResult(**saved)
        current = self.results.get(restored.tool_call_id)
        if (
            current is None
            or restored.tool_name != current.tool_name
            or restored.arguments_json != current.arguments_json
        ):
            raise ExecutionControlError(
                "Committed tool result does not match this call"
            )
        self.results[restored.tool_call_id] = restored

    def record_tool(
        self,
        context: ToolContext[Any],
        status: ToolStatus,
        observation: Any,
        *,
        error_type: str | None = None,
    ) -> None:
        result = self.start_tool(context)
        if result.status != "running":
            if result.status != status or result.observation != observation:
                raise ValueError("Conflicting duplicate tool result.")
            return
        result.status, result.observation, result.error_type = (
            status,
            copy.deepcopy(observation),
            error_type,
        )

    async def on_tool_start(
        self, context: RunContextWrapper[Any], agent: Agent[Any], tool: Tool
    ) -> None:
        """Observe per-call identity without treating the hook as a batch boundary."""
        self._check(context.context)
        if isinstance(context, ToolContext):
            recorded = self.start_tool(context)
            if recorded.operation_id is not None:
                annotate(
                    operation_id=recorded.operation_id,
                    effect_status=recorded.effect_status,
                )
            annotate(tool_call_id=recorded.tool_call_id)
            self._tool_started_at[context.tool_call_id] = time.monotonic()

    async def on_tool_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool: Tool,
        result: object,
    ) -> None:
        """Mark unwrapped tools unobserved; never infer success from an end hook."""
        if isinstance(context, ToolContext):
            recorded = self.start_tool(context)
            if recorded.operation_id is not None:
                annotate(
                    operation_id=recorded.operation_id,
                    effect_status=recorded.effect_status,
                )
            if recorded.status == "running":
                self.record_tool(context, "unobserved", result)
            started = self._tool_started_at.pop(context.tool_call_id, None)
            await self._observe(
                "tool_result",
                name=recorded.tool_name,
                tool_call_id=recorded.tool_call_id,
                status=recorded.status,
                **(
                    {"duration_ms": (time.monotonic() - started) * 1000}
                    if started is not None
                    else {}
                ),
            )

    async def on_llm_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        """Observe entry to a model turn, before model admission."""
        self.model_turn += 1
        self._model_started_at = time.monotonic()
        await self._observe("model_started")

    async def on_llm_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        response: ModelResponse,
    ) -> None:
        """Project a completed SDK response, including on DBOS replay."""
        usage = budget_from_sdk_usage(response.usage)
        usage["model_calls"] = 1
        capture_budget_usage(usage)
        tokens = {
            key: usage[key]
            for key in ("input_tokens", "output_tokens", "total_tokens")
        }
        started, self._model_started_at = self._model_started_at, None
        if self.config is not None and (items := response.to_input_items()):
            self._response_starts.append(digest(items[0]))
        await self._observe(
            "model_completed",
            **({"usage": tokens} if any(tokens.values()) else {}),
            **(
                {"duration_ms": (time.monotonic() - started) * 1000}
                if started is not None
                else {}
            ),
        )

    async def on_agent_start(
        self, context: RunContextWrapper[Any], agent: Agent[Any]
    ) -> None:
        """Confirm the invocation boundary before any model or local tool runs."""
        self._check(context.context)
        # Responses spans omit the model name when sensitive data is disabled.
        model = agent.model
        for _ in range(4):
            if isinstance(model, str):
                self.model_name = model
                break
            model = getattr(model, "model", None)
        self._agent_started_at = time.monotonic()
        await self._observe("agent_started")

    async def on_agent_end(
        self, context: RunContextWrapper[Any], agent: Agent[Any], output: Any
    ) -> None:
        """Observe final output; exit settlement remains the caller's responsibility."""
        await self._observe(
            "agent_completed",
            model_calls=self.model_turn,
            **(
                {"duration_ms": (time.monotonic() - self._agent_started_at) * 1000}
                if self._agent_started_at is not None
                else {}
            ),
        )

    def history_units(
        self, items: list[TResponseInputItem]
    ) -> tuple[list[TResponseInputItem], list[tuple[int, list[TResponseInputItem]]]]:
        """Partition at observed SDK response boundaries, keeping reasoning with tools.

        Unknown/reordered protocol histories cannot be safely compacted. Preserve
        them for the caller's explicit capacity error instead of guessing pairs.
        """
        starts = {key: index for index, key in enumerate(self._response_starts)}
        if not starts or len(starts) != len(self._response_starts):
            return items, []
        boundaries = [
            (index, turn)
            for index, item in enumerate(items)
            if (turn := starts.get(digest(item))) is not None
        ]
        if not boundaries or [turn for _, turn in boundaries] != list(
            range(len(self._response_starts))
        ):
            return items, []
        units = []
        for offset, (start, turn) in enumerate(boundaries):
            end = (
                boundaries[offset + 1][0]
                if offset + 1 < len(boundaries)
                else len(items)
            )
            units.append((turn, items[start:end]))
        return items[: boundaries[0][0]], units

    @staticmethod
    def can_compact_unit(items: list[TResponseInputItem]) -> bool:
        """Admit complete function-call/output units and their reasoning as one slice."""
        calls = [
            item.get("call_id") for item in items if item.get("type") == "function_call"
        ]
        outputs = [
            item.get("call_id")
            for item in items
            if item.get("type") == "function_call_output"
        ]
        if any(
            not isinstance(call_id, str) or not call_id
            for call_id in [*calls, *outputs]
        ):
            return False
        if sorted(calls) != sorted(outputs) or len(set(calls)) != len(calls):
            return False
        for item in items:
            if item.get("type", "message") not in {
                "message",
                "reasoning",
                "function_call",
                "function_call_output",
            }:
                return False
            if item.get("role") in {"user", "system", "developer"}:
                return False
        return True

    def _estimate(self, data: ModelInputData, agent: Agent[Any]) -> int:
        return estimate_text_tokens(
            json.dumps(
                {
                    "instructions": data.instructions,
                    "input": data.input,
                    "tools": [tool.params_json_schema for tool in agent.tools],
                },
                ensure_ascii=False,
                default=str,
            )
        )

    @staticmethod
    def _with_summary(
        head: list[Any], units: list[Any], summary: str, instructions: Any
    ) -> ModelInputData:
        summary_items = (
            [
                {
                    "role": "user",
                    "content": "<SDK Rolling Summary>\n"
                    + summary
                    + "\n</SDK Rolling Summary>",
                }
            ]
            if summary
            else []
        )
        return ModelInputData(
            input=[
                *head,
                *summary_items,
                *(item for _, unit in units for item in unit),
            ],
            instructions=instructions,
        )

    async def _compact_history(
        self, data: ModelInputData, agent: Agent[Any]
    ) -> ModelInputData:
        config = self.config
        if config is None:
            return data
        # Reapply the committed window to the SDK's original items on each turn.
        head, units = self.history_units(data.input)
        active = [
            (turn, unit) for turn, unit in units if turn not in self._compacted_turns
        ]
        current = (
            self._with_summary(head, active, self.rolling_summary, data.instructions)
            if self.rolling_summary and units
            else data
        )
        capacity = resolve_context_capacity(
            config.research_model, config.context_capacity_tokens
        )
        before = self._estimate(current, agent)
        self.estimated_input_tokens = before
        threshold = config.context_compaction_threshold_ratio
        if context_pressure_ratio(before, capacity) < threshold:
            return current
        recent = max(1, config.recent_raw_steps)
        eligible = [
            (turn, unit)
            for turn, unit in active[:-recent]
            if self.can_compact_unit(unit)
        ]
        # Metrics only: never log prompts, page text, tool output or credentials.
        diagnostics = {
            "estimated_input_tokens": before,
            "context_capacity_tokens": capacity,
            "compaction_threshold_ratio": threshold,
            "recent_raw_steps": recent,
            "eligible_compaction_units": len(eligible),
        }
        if not eligible:
            LOGGER.warning(
                "SDK context capacity exceeded: %s",
                json.dumps(diagnostics, sort_keys=True),
            )
            annotate(**diagnostics)
            raise SDKContextLimitExceeded(
                "Current task, rolling summary or recent native steps exceed "
                "context capacity; they cannot be safely discarded; diagnostics="
                + json.dumps(diagnostics, sort_keys=True)
            )
        removed = {turn for turn, _ in eligible}
        kept = [(turn, unit) for turn, unit in active if turn not in removed]
        # Bound the summary request independently of the business context window.
        # The complete original items and tool observations remain available.
        history_text = json.dumps(
            [item for _, unit in eligible for item in unit],
            ensure_ascii=False,
            default=str,
        )
        if len(history_text) > 30_000:
            history_text = (
                history_text[:15_000]
                + "\n[Middle omitted from summary input; original transcript retained.]\n"
                + history_text[-15_000:]
            )
        with business_span("rolling_compaction", estimated_tokens_before=before):
            try:
                summary, _ = await summarize_history(
                    history_text,
                    previous_summary=self.rolling_summary,
                    task_context=json.dumps(head, ensure_ascii=False, default=str),
                    model_name=config.rolling_compaction_model,
                    config=self.runtime_config,
                    max_tokens=config.rolling_compaction_model_max_tokens,
                    max_retries=config.max_structured_output_retries,
                )
                if estimate_text_tokens(summary) > config.rolling_compaction_model_max_tokens:
                    raise ValueError(
                        "Rolling summary exceeds the configured output bound."
                    )
                candidate = self._with_summary(head, kept, summary, data.instructions)
                after = self._estimate(candidate, agent)
                if (
                    after >= before
                    or context_pressure_ratio(after, capacity) >= threshold
                ):
                    raise ValueError(
                        "Rolling compaction cannot fit the protected request or did not reduce it."
                    )
            except ModelBudgetExceeded as exc:
                # The summary's model admission can consume or exhaust the
                # reasoning allowance. The SDK still owns the original notes.
                raise _ReasoningStopped(str(exc)) from exc
            except Exception as exc:
                if is_execution_control_failure(exc):
                    raise
                await self._observe(
                    "harness",
                    stage="rolling_compact",
                    status="failed",
                    error_type=type(exc).__name__,
                    **diagnostics,
                )
                LOGGER.warning(
                    "SDK rolling compaction failed safely: %s",
                    json.dumps(diagnostics, sort_keys=True),
                )
                # Keep the bounded reason fixed; never copy arbitrary provider
                # messages or prompt content into the archived failure.
                detail = (
                    "candidate cannot fit or did not reduce the protected request"
                    if isinstance(exc, ValueError)
                    else type(exc).__name__
                )
                raise SDKContextLimitExceeded(
                    "Rolling compaction failed safely: "
                    f"{detail}; original history retained; diagnostics="
                    + json.dumps(diagnostics, sort_keys=True)
                ) from exc
            annotate(
                estimated_tokens_after=after,
                removed_turns=len(removed),
                **diagnostics,
            )
            self._compacted_turns.update(removed)
            self.rolling_summary = summary
            self.estimated_input_tokens = after
            await self._observe(
                "harness",
                stage="rolling_compact",
                status="completed",
                removed_turns=len(removed),
            )
            return candidate

    async def settle(self, reason: str) -> BatchSettlement:
        """Drain observed tools without replaying any business tool.

        A late, cancellation-resistant tool can outlive the bounded drain. Its
        record stays live and callers may explicitly settle again once it ends.
        """
        error_type = None
        cancellation = None
        try:
            if self.tool_executor is not None and not await self.tool_executor.drain(
                self.settlement_timeout
            ):
                error_type = "ToolDrainTimeout"
        except asyncio.CancelledError as exc:
            error_type, cancellation = "CancelledError", exc
        except Exception as exc:
            error_type = type(exc).__name__
        settlement = BatchSettlement(
            reason=reason,
            in_flight_call_ids=tuple(
                result.tool_call_id
                for result in self.results.values()
                if result.status == "running"
            ),
            error_type=error_type,
        )
        self.last_settlement = settlement
        await self._observe(
            "harness",
            stage="settlement",
            reason=reason,
            status="settled" if settlement.succeeded else "unresolved",
            in_flight_calls=len(settlement.in_flight_call_ids),
            **({"error_type": settlement.error_type} if settlement.error_type else {}),
        )
        if cancellation is not None:
            raise cancellation
        return settlement

    async def _settle_on_exit(self, reason: str) -> BatchSettlement:
        if not native_step_available():
            return await self._settle_owned(reason)

        async def settle():
            result = await self._settle_owned(reason)
            return {
                "settlement": asdict(result),
                # A Tool Step that raises has no committed tool record to replay.
                "failed_tools": {
                    call_id: {
                        "status": record.status,
                        "error_type": record.error_type,
                        "operation_id": record.operation_id,
                        "effect_status": record.effect_status,
                    }
                    for call_id, record in self.results.items()
                    if record.status in {"failed", "cancelled"} and record.observation is None
                },
            }

        saved = await DBOS.run_step_async(
            {"name": "por.agent_settlement", "preemptible": True}, settle
        )
        for call_id, failed in saved["failed_tools"].items():
            record = self.results[call_id]
            record.status = failed["status"]
            record.error_type = failed["error_type"]
            record.operation_id = failed["operation_id"]
            record.effect_status = failed["effect_status"]
        value = saved["settlement"]
        value["in_flight_call_ids"] = tuple(value["in_flight_call_ids"])
        self.last_settlement = BatchSettlement(**value)
        return self.last_settlement

    async def _settle_owned(self, reason: str) -> BatchSettlement:
        cleanup = asyncio.create_task(self.settle(reason))
        cancelled = False
        while True:
            try:
                settlement = await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError:
                cancelled = True
                if cleanup.cancelled():
                    raise
        if cancelled:
            self.last_settlement = replace(settlement, reason="cancelled")
            raise asyncio.CancelledError()
        return settlement

    @observed("sdk_invocation")
    async def run(
        self,
        agent: Agent[Any],
        input: str | list[TResponseInputItem],
        *,
        run_config: RunConfig | None = None,
        max_turns: int = 10,
        session: Session | None = None,
    ) -> RunResult:
        """Delegate one invocation to Runner and explicitly settle every exit path."""
        if self._started:
            raise ValueError("Create a fresh harness for each SDK invocation.")
        if agent.handoffs:
            raise ValueError("Each invocation requires a single role without handoffs.")
        if any(
            isinstance(tool, FunctionTool) and tool.tool_output_guardrails
            for tool in agent.tools
        ) or any(server.tool_output_guardrails for server in agent.mcp_servers):
            raise ValueError("Tool output guardrails are unsupported by this harness.")
        config = run_config or RunConfig(
            tracing_disabled=not telemetry_enabled(), trace_include_sensitive_data=False
        )
        if config.call_model_input_filter is not None:
            raise ValueError(
                "The Harness requires its own input filter and client-managed history."
            )
        durable = native_step_available()
        config = replace(
            config,
            call_model_input_filter=self.durable_filter_model_input
            if durable
            else self.filter_model_input,
            tracing_disabled=config.tracing_disabled or not telemetry_enabled(),
            trace_include_sensitive_data=False,
        )
        runner = DBOSRunner if durable else Runner
        if durable and session is not None:
            session = InspectionTranscript(session)
        self._started = True
        identity = correlation(
            harness=self,
            role=self.role,
            task_id=self.task_id,
            research_round=self.research_round,
            invocation_id=self.sdk_run_id,
        )
        identity.__enter__()
        cleanup_token = None
        try:
            result = await runner.run(
                agent,
                input,
                context=self,
                hooks=self,
                run_config=config,
                max_turns=max_turns,
                session=session,
            )
            self._closing = True
            settlement = await self._settle_on_exit("completed")
            if not settlement.succeeded:
                raise ExecutionControlError(
                    f"Unsettled SDK tool results: {settlement}"
                )
            return result
        except dbos_error.DBOSWorkflowConflictIDError:
            # A replacement execution owns the journal and effects now.
            self._closing = True
            raise
        except BaseException as exc:
            self._closing = True
            if self._model_started_at is not None:
                # For failed model Steps, the metered error carries the receipt
                # usage. Successful responses are projected by on_llm_end.
                capture_budget_usage(getattr(exc, "budget_usage", None))
            cancelled = is_cancellation(exc)
            reason = "cancelled" if cancelled else "exception"
            if cancelled or isinstance(exc, ExecutionControlError):
                cleanup_token = owned_cleanup.set(True)
            try:
                await self._settle_on_exit(reason)
            except BaseException as cleanup_error:
                if hasattr(exc, "add_note"):
                    exc.add_note(
                        f"Context settlement interrupted: {type(cleanup_error).__name__}"
                    )
            try:
                if self._model_started_at is not None:
                    await self._observe(
                        "model_failed",
                        status=reason,
                        error_type=type(exc).__name__,
                        duration_ms=(time.monotonic() - self._model_started_at) * 1000,
                    )
                await self._observe(
                    "agent_failed",
                    status=reason,
                    error_type=type(exc).__name__,
                )
            except BaseException:
                # Observability must not replace the original execution failure.
                pass
            raise
        finally:
            if cleanup_token is not None:
                owned_cleanup.reset(cleanup_token)
            identity.__exit__(None, None, None)


class InspectionTranscript:
    """Optional inspection transcript; DBOS, not a previous transcript, resumes.

    Each private invocation starts with empty SDK history. Replacing a transcript
    is repeatable even across an add_items ACK loss; it never drives execution.
    """

    def __init__(self, session):
        self.session = session
        self.session_id = session.session_id
        self.items = []

    async def get_items(self, limit=None):
        return []

    async def add_items(self, items):
        self.items.extend(items)

        async def save():
            await self.session.clear_session()
            await self.session.add_items(self.items)

        await DBOS.run_step_async({"name": "por.sdk_transcript"}, save)

    async def pop_item(self):
        raise RuntimeError("Durable invocations do not resume from SDK sessions")

    async def clear_session(self):
        self.items.clear()


__all__ = [
    "BatchSettlement",
    "InspectionTranscript",
    "ObservedToolResult",
    "SDKContextHarness",
    "SDKContextLimitExceeded",
]
