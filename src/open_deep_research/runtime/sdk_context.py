"""Opt-in Agents SDK boundary for the existing ResearchWorkspace strategies.

Runner owns the agent loop. This adapter observes local tools, commits completed
evidence batches at the model-input seam, and substitutes only persisted text
outputs. The SDK's native calls, reasoning items, and output pairing stay intact.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal
from uuid import uuid4

from agents import Agent, FunctionTool, RunConfig, RunContextWrapper, RunHooks, Runner
from agents.exceptions import MCPToolCancellationError
from agents.items import ModelResponse, TResponseInputItem
from agents.mcp import MCPToolCustomDataContext
from agents.memory import Session
from agents.result import RunResult
from agents.run import CallModelData, ModelInputData
from agents.tool import Tool, default_tool_error_function
from agents.tool_context import ToolContext
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from open_deep_research.budget import merge_budget_usage
from open_deep_research.research_graph.compaction import micro_compact_messages
from open_deep_research.research_graph.schema import stable_id
from open_deep_research.research_graph.workspace import ResearchWorkspace, ToolBatchItem
from open_deep_research.workflow.execution import (
    ExecutionControlError,
    execution_effect,
)

ToolStatus = Literal["running", "succeeded", "failed", "cancelled", "unobserved"]
DEFAULT_EVIDENCE_TOOLS = frozenset({"web_search", "rag_search"})


@dataclass
class ObservedToolResult:
    """Keep native and graph-scoped identities plus explicit execution facts."""

    run_id: str
    sdk_run_id: str
    role: str
    task_id: str
    tool_name: str
    tool_call_id: str
    graph_call_id: str
    arguments_json: str
    args: Any
    model_turn: int
    status: ToolStatus = "running"
    observation: Any = None
    error_type: str | None = None
    consumed: bool = False


@dataclass(frozen=True)
class BatchSettlement:
    """Report remaining evidence and live calls without claiming durable execution."""

    reason: str
    pending_call_ids: tuple[str, ...]
    in_flight_call_ids: tuple[str, ...]
    error_type: str | None = None

    @property
    def succeeded(self) -> bool:
        """Return whether all observed terminal results have been accounted for."""
        return not (self.pending_call_ids or self.in_flight_call_ids or self.error_type)


class ResearchGraphSettlementError(RuntimeError):
    """Signal uncommitted evidence after an otherwise completed SDK run."""


class SDKContextHarness(RunHooks):
    """Bind exactly one SDK invocation to one research run, role, and task.

    Use ``function_tool`` for raw local invokers. Plain decorated FunctionTools
    remain observable but are never presumed successful: their error formatter
    can hide failures. For local MCP, supply ``mcp_result`` and ``tool_error`` as
    the server's public custom-data extractor and failure formatter respectively.
    Hosted tools have no local execution result and are excluded from ingestion.
    """

    def __init__(
        self,
        workspace: ResearchWorkspace,
        *,
        evidence_tools: frozenset[str] = DEFAULT_EVIDENCE_TOOLS,
        consumer_refresh_every: int = 0,
        update_consumer_context: bool = False,
        context_max_chars: int = 16_000,
        settlement_timeout: float = 30.0,
    ) -> None:
        """Configure a run-local collector; zero consumer refresh means first call only."""
        # Standard mode reuses the same execution collector and exit drain, but
        # neither retrieves graph context nor submits evidence.
        if workspace.run_id == stable_id("RUN", workspace.role):
            raise ValueError(
                "Supply an explicit research run_id when creating the workspace."
            )
        if (
            context_max_chars < 1024
            or settlement_timeout <= 0
            or consumer_refresh_every < 0
        ):
            raise ValueError(
                "Invalid context bound, refresh interval, or settlement timeout."
            )
        self.workspace = workspace
        self.scope = workspace.scope.model_copy(deep=True)
        self.sdk_run_id = uuid4().hex
        self.evidence_tools = frozenset(evidence_tools)
        self.consumer_refresh_every = consumer_refresh_every
        self.update_consumer_context = update_consumer_context
        self.context_max_chars = context_max_chars
        self.settlement_timeout = settlement_timeout
        self.results: dict[str, ObservedToolResult] = {}
        self.events: list[tuple[str, str]] = []
        self.model_calls = 0
        self.budget_usage: dict[str, Any] = {}
        self.last_settlement: BatchSettlement | None = None
        self._compacted: dict[str, str] = {}
        self._needs_context_update: set[str] = set()
        self._batch_errors: dict[str, str] = {}
        self._context_text = ""
        self._retrieved_signature = ""
        self._response_starts: list[str] = []
        self._owned_contexts: set[str] = set()
        self._active: set[asyncio.Task[Any]] = set()
        self._batch_lock = asyncio.Lock()
        self._agent: Agent[Any] | None = None
        self._started = False
        self._closing = False

    def _check(self, context: Any) -> None:
        if (
            context is not self
            or self.workspace.scope != self.scope
            or self.workspace.run_id != self.scope.run_id
            or self.workspace.role != self.scope.role
            or self.workspace.task.task_id != self.scope.task_id
        ):
            raise ValueError(
                "Research run/role/task context does not match this SDK invocation."
            )

    def _start(self, context: ToolContext[Any]) -> ObservedToolResult:
        self._check(context.context)
        call_id = context.tool_call_id
        if not call_id:
            raise ValueError("A native tool call ID is required.")
        try:
            args = json.loads(context.tool_arguments)
        except ValueError:
            args = context.tool_arguments
        existing = self.results.get(call_id)
        if existing is not None:
            if existing.tool_name != context.tool_name or json.dumps(
                existing.args, sort_keys=True
            ) != json.dumps(args, sort_keys=True):
                raise ValueError("Tool call ID was reused for a different invocation.")
            return existing
        result = ObservedToolResult(
            run_id=self.scope.run_id,
            sdk_run_id=self.sdk_run_id,
            role=self.scope.role,
            task_id=self.scope.task_id,
            tool_name=context.tool_name,
            tool_call_id=call_id,
            graph_call_id=stable_id(
                "CALL",
                self.scope.run_id,
                self.scope.role,
                self.scope.task_id,
                self.sdk_run_id,
                call_id,
            ),
            arguments_json=context.tool_arguments,
            args=args,
            model_turn=self.model_calls,
        )
        self.results[call_id] = result
        return result

    def _record(
        self,
        context: ToolContext[Any],
        status: ToolStatus,
        observation: Any,
        *,
        error_type: str | None = None,
    ) -> None:
        result = self._start(context)
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
        if self.workspace.transcript is not None:
            self.workspace.transcript.append(
                "sdk_tool_result",
                {
                    "sdk_run_id": self.sdk_run_id,
                    "role": self.scope.role,
                    "task_id": self.scope.task_id,
                    "tool_call_id": result.tool_call_id,
                    "graph_call_id": result.graph_call_id,
                    "tool_name": result.tool_name,
                    "args": result.args,
                    "status": status,
                    "observation": observation,
                    "error_type": error_type,
                },
            )

    def function_tool(
        self,
        *,
        name: str,
        description: str,
        params_json_schema: dict[str, Any],
        invoke: Callable[[ToolContext[Any], str], Awaitable[Any]],
        is_success: Callable[[Any], bool] | None = None,
        errors_as_results: bool = True,
        strict_json_schema: bool = True,
        error_formatter: Callable[[ToolContext[Any], Exception], str] | None = None,
    ) -> FunctionTool:
        """Observe a raw invoker before error formatting can obscure its status.

        The invoker owns argument validation (e.g. an existing BaseTool.ainvoke).
        Tools returning business error envelopes must supply ``is_success``.
        Cancellation always propagates. No SDK private fields are modified.
        """

        async def observed(context: ToolContext[Any], arguments: str) -> Any:
            self._start(context)
            if self._closing:
                self._record(context, "cancelled", None, error_type="CancelledError")
                raise asyncio.CancelledError()
            current = asyncio.current_task()
            if current is not None:
                self._active.add(current)
            try:
                output = await invoke(context, arguments)
                success = is_success(output) if is_success is not None else True
            except asyncio.CancelledError:
                self._record(context, "cancelled", None, error_type="CancelledError")
                raise
            except Exception as exc:
                if isinstance(exc, ExecutionControlError):
                    self._record(context, "failed", None, error_type=type(exc).__name__)
                    raise
                output = (
                    error_formatter(context, exc)
                    if error_formatter is not None
                    else default_tool_error_function(context, exc)
                )
                self._record(
                    context,
                    "cancelled"
                    if isinstance(exc, MCPToolCancellationError)
                    else "failed",
                    output,
                    error_type=type(exc).__name__,
                )
                if not errors_as_results:
                    raise
                return output
            else:
                self._record(context, "succeeded" if success else "failed", output)
                return output
            finally:
                if current is not None:
                    self._active.discard(current)

        return FunctionTool(
            name=name,
            description=description,
            params_json_schema=copy.deepcopy(params_json_schema),
            on_invoke_tool=observed,
            strict_json_schema=strict_json_schema,
        )

    def mcp_result(self, data: MCPToolCustomDataContext) -> dict[str, str]:
        """Collect local MCP status before the SDK discards the isError flag."""
        context = data.run_context
        if not isinstance(context, ToolContext):
            raise TypeError("Local MCP collection requires the SDK ToolContext.")
        observation: Any = (
            dict(data.structured_content)
            if data.structured_content is not None
            else None
        )
        if data.is_error or observation is None:
            output = data.tool_output
            blocks = output if isinstance(output, list) else [output]
            observation = "\n".join(
                str(block.get("text", ""))
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
        self._record(context, "failed" if data.is_error else "succeeded", observation)
        return {"research_sdk_run_id": self.sdk_run_id}

    def tool_error(self, context: RunContextWrapper[Any], error: Exception) -> str:
        """Preserve local MCP/function failure status when formatting a tool error."""
        output = default_tool_error_function(context, error)
        if isinstance(context, ToolContext):
            self._record(
                context,
                "cancelled"
                if isinstance(error, MCPToolCancellationError)
                else "failed",
                output,
                error_type=type(error).__name__,
            )
        return output

    async def on_tool_start(
        self, context: RunContextWrapper[Any], agent: Agent[Any], tool: Tool
    ) -> None:
        """Observe per-call identity without treating the hook as a batch boundary."""
        self._check(context.context)
        if isinstance(context, ToolContext):
            self._start(context)
            self.events.append(("tool_start", context.tool_call_id))

    async def on_tool_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool: Tool,
        result: object,
    ) -> None:
        """Mark unwrapped tools unobserved; never infer success from an end hook."""
        if isinstance(context, ToolContext):
            recorded = self._start(context)
            if recorded.status == "running":
                self._record(context, "unobserved", result)
            self.events.append(("tool_end", context.tool_call_id))

    async def on_llm_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        """Record the public lifecycle order of actual model requests."""
        self.events.append(("llm_start", str(self.model_calls)))

    async def on_llm_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        response: ModelResponse,
    ) -> None:
        """Observe a model response without treating hosted items as local evidence."""
        self.events.append(("llm_end", str(self.model_calls)))
        items = response.to_input_items()
        if items:
            self._response_starts.append(self._item_key(items[0]))

    async def on_agent_start(
        self, context: RunContextWrapper[Any], agent: Agent[Any]
    ) -> None:
        """Confirm the invocation boundary before any model or local tool runs."""
        self._check(context.context)
        self.events.append(("agent_start", agent.name))

    async def on_agent_end(
        self, context: RunContextWrapper[Any], agent: Agent[Any], output: Any
    ) -> None:
        """Observe final output; exit settlement remains the caller's responsibility."""
        self.events.append(("agent_end", agent.name))

    def _message_view(self) -> list[Any]:
        """Build a LangChain view only for strategy compaction, never for the provider."""
        messages: list[Any] = [HumanMessage(content=self.workspace.task.objective)]
        turns = sorted({result.model_turn for result in self.results.values()})
        for turn in turns:
            results = [
                result
                for result in self.results.values()
                if result.model_turn == turn and result.status != "running"
            ]
            if not results:
                continue
            messages.append(
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": result.tool_name,
                            "id": result.graph_call_id,
                            "args": result.args
                            if isinstance(result.args, dict)
                            else {},
                        }
                        for result in results
                    ],
                )
            )
            messages.extend(
                ToolMessage(
                    content=result.observation
                    if isinstance(result.observation, str)
                    else json.dumps(
                        result.observation, ensure_ascii=False, default=str
                    ),
                    tool_call_id=result.graph_call_id,
                    name=result.tool_name,
                )
                for result in results
            )
        return messages

    async def _flush(self) -> bool:
        async with self._batch_lock:
            pending = [
                result
                for result in self.results.values()
                if not result.consumed and result.status != "running"
            ]
            evidence = []
            for result in pending:
                if (
                    self.workspace.strategy.is_producer
                    and result.status == "succeeded"
                    and result.tool_name in self.evidence_tools
                ):
                    evidence.append(result)
                else:
                    result.consumed = True
            if not evidence:
                if pending and self.workspace.strategy.is_producer:
                    compacted = micro_compact_messages(
                        self._message_view(),
                        dict(self.workspace.receipts),
                        recent_raw_steps=int(
                            getattr(self.workspace.configurable, "recent_raw_steps", 3)
                        ),
                    )
                    self._remember_compaction(compacted.messages)
                return False
            updated = False
            for turn in sorted({result.model_turn for result in evidence}):
                # Preserve the original SDK model batch identity across retries,
                # including when later model turns add more completed tools.
                group = [result for result in evidence if result.model_turn == turn]
                updated = await self._flush_evidence(group) or updated
            return updated

    async def _flush_evidence(self, evidence: list[ObservedToolResult]) -> bool:
        self.events.append(("batch_start", str(len(evidence))))
        previous_receipts = dict(self.workspace.receipts)
        context_calls_before = int(
            self.workspace.metrics.as_dict().get("context_manager_calls", 0) or 0
        )
        call_ids = {result.tool_call_id for result in evidence}
        try:
            async with execution_effect():
                outcome = await self.workspace.ingest(
                    self._message_view(),
                    [
                        ToolBatchItem(
                            result.tool_name,
                            result.graph_call_id,
                            result.args,
                            result.observation,
                            True,
                        )
                        for result in evidence
                    ],
                )
            if not outcome.succeeded:
                self._batch_errors.update(
                    {
                        call_id: outcome.error_type or "ResearchGraphError"
                        for call_id in call_ids
                    }
                )
                if (
                    int(
                        self.workspace.metrics.as_dict().get("context_manager_calls", 0)
                        or 0
                    )
                    <= context_calls_before
                ):
                    self._needs_context_update.update(call_ids)
                self.workspace.receipts.clear()
                self.workspace.receipts.update(previous_receipts)
                self.events.append(("batch_failed", str(len(evidence))))
                return False
        except BaseException:
            if (
                int(
                    self.workspace.metrics.as_dict().get("context_manager_calls", 0)
                    or 0
                )
                <= context_calls_before
            ):
                self._needs_context_update.update(call_ids)
            self.workspace.receipts.clear()
            self.workspace.receipts.update(previous_receipts)
            raise
        if (
            call_ids & self._needs_context_update
            and int(
                self.workspace.metrics.as_dict().get("context_manager_calls", 0) or 0
            )
            <= context_calls_before
        ):
            # A failed context update cannot be acknowledged by the legacy
            # URL-cache fast path, which does not rebuild Working Context.
            self.workspace.receipts.clear()
            self.workspace.receipts.update(previous_receipts)
            self.events.append(("batch_recovery_incomplete", str(len(evidence))))
            return False
        self._needs_context_update.difference_update(call_ids)
        for call_id in call_ids:
            self._batch_errors.pop(call_id, None)
        self.budget_usage = merge_budget_usage(self.budget_usage, outcome.budget_usage)
        self._remember_compaction(outcome.messages)
        for result in evidence:
            result.consumed = True
        self.events.append(("batch_end", str(len(evidence))))
        return True

    def _remember_compaction(self, messages: list[Any]) -> None:
        by_graph_id = {result.graph_call_id: result for result in self.results.values()}
        for message in messages:
            if (
                isinstance(message, ToolMessage)
                and message.tool_call_id in self.workspace.receipts
            ):
                original = by_graph_id.get(message.tool_call_id)
                if (
                    original is not None
                    and original.status == "succeeded"
                    and original.tool_name in self.evidence_tools
                ):
                    receipt = self.workspace.receipts[message.tool_call_id]
                    referenced = set(
                        [
                            *receipt.source_ids,
                            *receipt.evidence_ids,
                            *receipt.claim_ids,
                            *receipt.finding_ids,
                            *receipt.duplicate_source_ids,
                        ]
                    )
                    # Legacy URL-only dedup can acknowledge a fresh, unwritten
                    # Source ID. Keep raw output unless the write receipt proves
                    # every reference; do not publish a fabricated graph pointer.
                    if not referenced.issubset(receipt.node_ids):
                        self._compacted.pop(original.tool_call_id, None)
                        self.events.append(
                            ("compaction_deferred", original.tool_call_id)
                        )
                        continue
                    self._compacted[original.tool_call_id] = str(message.content)

    async def filter_model_input(self, data: CallModelData[Any]) -> ModelInputData:
        """Commit a complete tool batch, then inject one bounded current context."""
        self._check(data.context)
        if self._agent is not None and data.agent is not self._agent:
            raise ValueError(
                "Handoffs/nested roles require a separate SDKContextHarness."
            )
        self.events.append(("input_filter", str(self.model_calls + 1)))
        updated = await self._flush()
        if not self.workspace.strategy.graph_enabled:
            self.model_calls += 1
            return data.model_data
        # Business roles request per-turn refresh; standalone Phase 0 callers
        # retain the explicit refresh interval and retrieval-only default.
        refresh_due = bool(
            self.model_calls
            and self.consumer_refresh_every
            and self.model_calls % self.consumer_refresh_every == 0
        )
        if not self._context_text or updated or refresh_due:
            self.workspace.context_for(query_suffix="SDK current task")
        signature = self.workspace.relevant_subgraph.model_dump_json(exclude={"query"})
        changed = signature != self._retrieved_signature
        refresh = changed or refresh_due
        if (
            not self.workspace.strategy.is_producer
            and self.update_consumer_context
            and self.workspace.relevant_subgraph.nodes
            and changed
        ):
            await self.workspace.update_working_context([])
        self._retrieved_signature = signature
        if not self._context_text or updated or refresh:
            body = self.workspace.protected_context(include_summary=False)
            identity = f"run={self.scope.run_id} sdk_run={self.sdk_run_id} role={self.scope.role} task={self.scope.task_id}\n"
            bound = self.context_max_chars - len(identity) - 160
            if bound <= 0:
                raise ValueError(
                    "Research identity exceeds the configured context bound."
                )
            self._context_text = (
                "<SDK Research Context>\n"
                + identity
                + body[:bound]
                + ("\n[Research context bounded]" if len(body) > bound else "")
                + "\n</SDK Research Context>"
            )
            self._owned_contexts.add(self._context_text)
        items = []
        for item in data.model_data.input:
            if (
                item.get("role") == "user"
                and isinstance(item.get("content"), str)
                and item["content"] in self._owned_contexts
            ):
                continue
            if (
                item.get("type") == "function_call_output"
                and isinstance(item.get("output"), str)
                and item.get("call_id") in self._compacted
            ):
                item = {**item, "output": self._compacted[item["call_id"]]}
            items.append(item)
        self.model_calls += 1
        return ModelInputData(
            input=[{"role": "user", "content": self._context_text}, *items],
            instructions=data.model_data.instructions,
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

    def can_compact_unit(self, items: list[TResponseInputItem]) -> bool:
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
        if self.workspace.strategy.is_producer:
            for call_id in calls:
                result = self.results.get(call_id)
                if (
                    result is None
                    or result.status != "succeeded"
                    or not result.consumed
                ):
                    return False
                if result.tool_name in self.evidence_tools:
                    receipt = self.workspace.receipts.get(result.graph_call_id)
                    if receipt is None or not receipt.has_complete_node_references:
                        return False
        return True

    async def settle(self, reason: str) -> BatchSettlement:
        """Drain observed tools and evidence without replaying any business tool.

        A late, cancellation-resistant tool can outlive the bounded drain. Its
        record stays live and callers may explicitly settle again once it ends.
        """
        error_type = None
        cancellation = None
        try:
            if self._active:
                _, live = await asyncio.wait(
                    self._active, timeout=self.settlement_timeout
                )
                if live:
                    error_type = "ToolDrainTimeout"
            await asyncio.wait_for(self._flush(), timeout=self.settlement_timeout)
        except asyncio.CancelledError as exc:
            error_type, cancellation = "CancelledError", exc
        except Exception as exc:
            error_type = type(exc).__name__
        settlement = BatchSettlement(
            reason=reason,
            pending_call_ids=tuple(
                result.tool_call_id
                for result in self.results.values()
                if not result.consumed and result.status != "running"
            ),
            in_flight_call_ids=tuple(
                result.tool_call_id
                for result in self.results.values()
                if result.status == "running"
            ),
            error_type=error_type or next(iter(self._batch_errors.values()), None),
        )
        self.last_settlement = settlement
        self.events.append(("settlement", reason))
        if cancellation is not None:
            raise cancellation
        return settlement

    async def _settle_on_exit(self, reason: str) -> BatchSettlement:
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
            raise ValueError(
                "Phase 0 supports one role per invocation, without handoffs."
            )
        if any(
            isinstance(tool, FunctionTool) and tool.tool_output_guardrails
            for tool in agent.tools
        ) or any(server.tool_output_guardrails for server in agent.mcp_servers):
            raise ValueError(
                "Evidence status after output guardrails is outside the Phase 0 contract."
            )
        config = run_config or RunConfig(
            tracing_disabled=True, trace_include_sensitive_data=False
        )
        if config.call_model_input_filter is not None:
            raise ValueError(
                "Phase 0 requires its own filter and full client-managed input."
            )
        config = replace(config, call_model_input_filter=self.filter_model_input)
        self._agent, self._started = agent, True
        try:
            result = await Runner.run(
                agent,
                input,
                context=self,
                hooks=self,
                run_config=config,
                max_turns=max_turns,
                session=session,
            )
        except BaseException as exc:
            self._closing = True
            reason = (
                "cancelled" if isinstance(exc, asyncio.CancelledError) else "exception"
            )
            try:
                await self._settle_on_exit(reason)
            except BaseException as cleanup_error:
                if hasattr(exc, "add_note"):
                    exc.add_note(
                        f"Context settlement interrupted: {type(cleanup_error).__name__}"
                    )
            raise
        self._closing = True
        settlement = await self._settle_on_exit("completed")
        if not settlement.succeeded:
            raise ResearchGraphSettlementError(
                f"Unsettled Research Graph results: {settlement}"
            )
        return result
