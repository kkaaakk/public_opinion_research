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
import time
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal
from uuid import uuid4

from agents import Agent, FunctionTool, RunConfig, RunContextWrapper, RunHooks, Runner
from agents.items import ModelResponse, TResponseInputItem
from agents.memory import Session
from agents.result import RunResult
from agents.run import CallModelData, ModelInputData
from agents.tool import Tool
from agents.tool_context import ToolContext
from dbos import DBOS
from dbos import error as dbos_error
from dbos_openai_agents import DBOSRunner

from open_deep_research.budget import budget_capture, capture_budget_usage
from open_deep_research.observability.telemetry import (
    annotate,
    correlation,
    observed,
    telemetry_enabled,
)
from open_deep_research.workflow.execution import (
    ExecutionControlError,
    is_cancellation,
    native_step_available,
    owned_cleanup,
    stage_context,
)

ToolStatus = Literal["running", "succeeded", "failed", "cancelled", "unobserved"]


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
        self.results: dict[str, ObservedToolResult] = {}
        self.events: list[tuple[str, str]] = []
        self.model_calls = 0
        self.last_settlement: BatchSettlement | None = None
        self._response_starts: list[str] = []
        self._owned_contexts: set[str] = set()
        self.tool_executor: Any | None = None
        self._batch_lock = asyncio.Lock()
        self._agent: Agent[Any] | None = None
        self._started = False
        self._closing = False
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
                    model_turn=self.model_calls,
                    **metadata,
                ),
                # Sibling tools can finish in a different order after replay.
                # This delivery is idempotent by call ID and uses no native slot.
                call_id=metadata.get("tool_call_id") if kind == "tool_result" else None,
            )

    def _extra_snapshot(self) -> dict[str, Any]:
        """Return invocation-local business state owned by a subclass."""
        return {}

    def _restore_extra(self, state: dict[str, Any]) -> None:
        """Restore invocation-local business state owned by a subclass."""

    def _durable_snapshot(self) -> dict[str, Any]:
        """Freeze data only; never serialize clients, locks or live tasks."""
        state: dict[str, Any] = {
            "model_calls": self.model_calls,
            "_response_starts": copy.deepcopy(self._response_starts),
            "results": {
                key: {
                    k: v
                    for k, v in asdict(value).items()
                    if k not in {"observation", "arguments_json", "args"}
                }
                for key, value in self.results.items()
            },
        }
        state.update(self._extra_snapshot())
        return copy.deepcopy(state)

    def _restore_durable_snapshot(self, state: dict[str, Any]) -> None:
        self.model_calls = state["model_calls"]
        self._response_starts = state["_response_starts"]
        for key, value in state["results"].items():
            if key not in self.results:
                raise ValueError(
                    "Harness snapshot is missing its native Tool Step result"
                )
            for name, field in value.items():
                setattr(self.results[key], name, field)
        self._restore_extra(state)

    async def durable_filter_model_input(self, data: CallModelData[Any]) -> ModelInputData:
        """Checkpoint bounded history compaction and its mutable data.

        The Agent loop stays outside the Step; each Agent model call is
        DBOSRunner's Step. One bounded transaction shares this compaction Step.
        """
        self._check(data.context)

        async def prepare():
            from open_deep_research.runtime.sdk_runtime import _ReasoningStopped

            stop = None
            try:
                with budget_capture() as usage:
                    self._filter_usage = usage
                    try:
                        result = await self.filter_model_input(data)
                    except _ReasoningStopped as exc:
                        stop = str(exc)
                        result = data.model_data
                    finally:
                        self._filter_usage = None
            except BaseException:
                capture_budget_usage(usage)
                raise
            return {
                "input": result.input,
                "instructions": result.instructions,
                "state": self._durable_snapshot(),
                "usage": usage,
                "stop": stop,
            }

        saved = await DBOS.run_step_async(
            {"name": "por.agent_context", "preemptible": True}, prepare
        )
        self._restore_durable_snapshot(saved["state"])
        capture_budget_usage(saved["usage"])
        if saved["stop"] is not None:
            from open_deep_research.runtime.sdk_runtime import _ReasoningStopped

            raise _ReasoningStopped(saved["stop"])
        return ModelInputData(input=saved["input"], instructions=saved["instructions"])

    def _check(self, context: Any) -> None:
        if context is not self:
            raise ValueError("SDK invocation context does not match this harness.")

    async def filter_model_input(self, data: CallModelData[Any]) -> ModelInputData:
        """Return the native input unchanged unless a subclass bounds it."""
        self._check(data.context)
        if self._agent is not None and data.agent is not self._agent:
            raise ValueError(
                "Handoffs/nested roles require a separate SDKContextHarness."
            )
        return data.model_data

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
            model_turn=self.model_calls,
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
        self.events.append((f"tool_{status}", result.tool_call_id))

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
            self.events.append(("tool_start", context.tool_call_id))
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
            self.events.append(("tool_end", context.tool_call_id))
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
        """Record the public lifecycle order of actual model requests."""
        self.model_calls += 1
        self.events.append(("llm_start", str(self.model_calls)))
        self._model_started_at = time.monotonic()
        await self._observe("model_started")

    async def on_llm_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        response: ModelResponse,
    ) -> None:
        """Observe a model response without treating hosted items as local evidence."""
        self.events.append(("llm_end", str(self.model_calls)))
        usage = response.usage
        # SDK zero defaults do not prove a provider supplied token usage.
        tokens = {
            key: getattr(usage, key, None)
            for key in ("input_tokens", "output_tokens", "total_tokens")
        }
        await self._observe(
            "model_completed",
            **({"usage": tokens} if any(tokens.values()) else {}),
            **(
                {"duration_ms": (time.monotonic() - self._model_started_at) * 1000}
                if self._model_started_at is not None
                else {}
            ),
        )
        self._model_started_at = None
        items = response.to_input_items()
        if items:
            self._response_starts.append(self._item_key(items[0]))

    async def on_agent_start(
        self, context: RunContextWrapper[Any], agent: Agent[Any]
    ) -> None:
        """Confirm the invocation boundary before any model or local tool runs."""
        self._check(context.context)
        if self._agent is not None and self._agent is not agent:
            raise ValueError("A Harness supports one Agent per invocation.")
        # Runner supplies the bound Agent here before preparing the first input.
        self._agent = agent
        self.events.append(("agent_start", agent.name))
        self._agent_started_at = time.monotonic()
        await self._observe("agent_started")

    async def on_agent_end(
        self, context: RunContextWrapper[Any], agent: Agent[Any], output: Any
    ) -> None:
        """Observe final output; exit settlement remains the caller's responsibility."""
        self.events.append(("agent_end", agent.name))
        await self._observe(
            "agent_completed",
            model_calls=self.model_calls,
            **(
                {"duration_ms": (time.monotonic() - self._agent_started_at) * 1000}
                if self._agent_started_at is not None
                else {}
            ),
        )

    @staticmethod
    def _item_key(item: dict[str, Any]) -> str:
        return json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)

    def history_units(
        self, items: list[TResponseInputItem]
    ) -> tuple[list[TResponseInputItem], list[tuple[int, list[TResponseInputItem]]]]:
        """Partition at observed SDK response boundaries, keeping reasoning with tools.

        Unknown/reordered protocol histories cannot be safely compacted. Preserve
        them for the caller's explicit capacity error instead of guessing pairs.
        """
        starts = {key: index for index, key in enumerate(self._response_starts)}
        if len(starts) != len(self._response_starts):
            return list(items), []
        boundaries = [
            (index, starts[self._item_key(item)])
            for index, item in enumerate(items)
            if self._item_key(item) in starts
        ]
        if not boundaries:
            return list(items), []
        units = []
        for offset, (start, turn) in enumerate(boundaries):
            end = (
                boundaries[offset + 1][0]
                if offset + 1 < len(boundaries)
                else len(items)
            )
            units.append((turn, list(items[start:end])))
        return list(items[: boundaries[0][0]]), units

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
        self.events.append(("settlement", reason))
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
            try:
                with budget_capture() as usage:
                    result = await self._settle_owned(reason)
            except BaseException:
                capture_budget_usage(usage)
                raise
            return {
                "settlement": asdict(result),
                "state": self._durable_snapshot(),
                "usage": usage,
            }

        saved = await DBOS.run_step_async(
            {"name": "por.agent_settlement", "preemptible": True}, settle
        )
        self._restore_durable_snapshot(saved["state"])
        capture_budget_usage(saved["usage"])
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
            cancelled = is_cancellation(exc)
            reason = "cancelled" if cancelled else "exception"
            if cancelled or isinstance(exc, ExecutionControlError):
                cleanup_token = owned_cleanup.set(True)
            try:
                await self._settle_on_exit(reason)
            except BaseException as cleanup_error:
                self._exit_error_type = type(cleanup_error).__name__
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
]
