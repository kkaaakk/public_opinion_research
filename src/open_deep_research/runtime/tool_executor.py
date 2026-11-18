"""Own the durable lifecycle of every SDK business tool call.

The SDK still owns tool declarations, argument validation and defaults. This
executor owns admission, the ``por.agent_tool`` DBOS Step, committed-result
recovery, in-flight de-duplication and the protected external-effect receipts.
The Context Harness only records and projects the results it is handed back.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, replace
from functools import partial
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Any

from agents import FunctionTool
from agents.exceptions import MCPToolCancellationError
from agents.tool_context import ToolContext
from dbos import DBOS
from dbos import error as dbos_error
from langchain_core.runnables import RunnableConfig
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.budget import (
    budget_capture,
    budget_from_tool_calls,
    budget_usage_with_reason,
    can_spend_tool_call,
    is_over_budget,
)
from open_deep_research.configuration import Configuration
from open_deep_research.mcp.domain_filter import get_tool_domain
from open_deep_research.tools import tool_result_text
from open_deep_research.tools.registry import tool_effect
from open_deep_research.web.run_data import digest
from open_deep_research.workflow.execution import (
    ExecutionControlError,
    commit_receipt,
    finish_operation,
    finish_owned,
    native_step_available,
    owned_io,
    stage_context,
    start_operation,
    unconfirmed,
)

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from open_deep_research.runtime.sdk_context import (
        ObservedToolResult,
        SDKContextHarness,
    )
    from open_deep_research.runtime.sdk_runtime import _RunBudget
    from open_deep_research.tools import BusinessTool

tool_usage: ContextVar[dict | None] = ContextVar("tool_usage", default=None)


class _ToolRejected(Exception):
    """Return an ordinary tool error to the model."""


def _result_text(name: str, output: Any) -> str:
    # Only explicitly defined protocol and business error envelopes affect status.
    envelope = (
        output.get("structured_content", output) if isinstance(output, dict) else output
    )
    if isinstance(output, str):
        try:
            envelope = json.loads(output)
        except ValueError:
            pass
    if isinstance(envelope, dict):
        if envelope.get("status") == "cancelled":
            raise MCPToolCancellationError(tool_result_text(output))
        if (
            envelope.get("isError", False) is not False
            or envelope.get("ok") is False
            or envelope.get("success", True) is not True
            or envelope.get("status", "ok")
            not in ("ok", "success", "succeeded", "completed")
        ):
            raise _ToolRejected(tool_result_text(output))
    if (
        name == "rag_search"
        and isinstance(output, str)
        and output.startswith("Local RAG search failed:")
    ):
        raise _ToolRejected(output)
    return tool_result_text(output)


class ToolExecutor:
    """Share one role's permission, budget and durable tool lifecycle."""

    def __init__(
        self,
        role: str,
        allowed_domains: frozenset[str],
        config: Configuration,
        runtime_config: RunnableConfig,
        budget: _RunBudget,
        tools: dict[str, BusinessTool | BaseTool],
    ):
        self.role = role
        self.allowed_domains = allowed_domains
        self.config = config
        self.runtime_config = runtime_config
        self.budget = budget
        self.tools = tools
        self._tool_calls: dict[str, asyncio.Future[Any]] = {}

    def bind(self, tool: FunctionTool, *, drain_on_cancel: bool = False) -> FunctionTool:
        """Bind an SDK declaration to this executor's durable invocation.

        The SDK keeps the model-visible name, description, Schema, validation
        and defaults; only the invocation callback is replaced.
        """
        return replace(
            tool,
            on_invoke_tool=partial(
                self.invoke, tool, drain_on_cancel=drain_on_cancel
            ),
        )

    async def invoke(
        self,
        tool: FunctionTool,
        context: ToolContext[Any],
        arguments: str,
        *,
        drain_on_cancel: bool = False,
    ) -> Any:
        """Run one SDK tool call through the native ``por.agent_tool`` Step."""
        harness = context.context
        record = harness.start_tool(context)
        call_id = record.tool_call_id
        previous = self._tool_calls.get(call_id)
        completion = None
        if previous is None:
            completion = asyncio.get_running_loop().create_future()
            self._tool_calls[call_id] = completion
        try:
            output = await self._tool_step(
                harness,
                record,
                tool,
                context,
                arguments,
                previous=previous,
                drain_on_cancel=drain_on_cancel,
            )
        except BaseException as exc:
            if completion is not None:
                completion.set_exception(exc)
                completion.exception()
            raise
        if completion is not None:
            completion.set_result(output)
        return output

    async def drain(self, timeout: float) -> bool:
        """Wait for in-flight tool calls; ``False`` means the bounded wait expired."""
        in_flight = [call for call in self._tool_calls.values() if not call.done()]
        if not in_flight:
            return True
        _, live = await asyncio.wait(in_flight, timeout=timeout)
        return not live

    async def _tool_step(
        self,
        harness: SDKContextHarness,
        record: ObservedToolResult,
        tool: FunctionTool,
        context: ToolContext[Any],
        arguments: str,
        *,
        previous: asyncio.Future[Any] | None,
        drain_on_cancel: bool,
    ) -> Any:
        """Execute or replay one call under the ``por.agent_tool`` Step.

        A duplicate call ID still consumes its own Step slot while sharing the
        first result, including a result restored from the journal.
        """

        async def perform() -> Any:
            if previous is not None:
                return await asyncio.shield(previous)
            if harness.closing:
                harness.record_tool(
                    context, "cancelled", None, error_type="CancelledError"
                )
                raise asyncio.CancelledError()
            try:
                self._authorize(tool.name)
                self._admit(tool.name)
                # The original SDK declaration validates arguments and defaults
                # before the tool body admits a protected external operation.
                call = tool.on_invoke_tool(context, arguments)
                if drain_on_cancel:
                    output, _ = await finish_owned(asyncio.ensure_future(call))
                else:
                    output = await call
            except (asyncio.CancelledError, dbos_error.DBOSWorkflowCancelledError):
                harness.record_tool(
                    context, "cancelled", None, error_type="CancelledError"
                )
                raise
            except Exception as exc:
                if isinstance(exc, ExecutionControlError):
                    harness.record_tool(
                        context, "failed", None, error_type=type(exc).__name__
                    )
                    raise
                output = f"Error executing tool: {exc}"
                harness.record_tool(
                    context,
                    "cancelled"
                    if isinstance(exc, MCPToolCancellationError)
                    else "failed",
                    output,
                    error_type=type(exc).__name__,
                )
                return output
            harness.record_tool(context, "succeeded", output)
            return output

        if not native_step_available():
            return await perform()
        executed = False

        async def invoke_step():
            nonlocal executed
            executed = True
            local = {}
            token = tool_usage.set(local)
            try:
                output = await perform()
            finally:
                tool_usage.reset(token)
            return {
                "output": output,
                "record": asdict(harness.results[record.tool_call_id]),
                "budget": local,
            }

        saved = await DBOS.run_step_async(
            {"name": "por.agent_tool", "preemptible": True}, invoke_step
        )
        if not executed:
            harness.restore_tool(saved["record"])
            self.budget.add(saved["budget"])
        return saved["output"]

    def _authorize(self, name: str) -> None:
        # Registration filters each role's tools; re-read the current domain at
        # invocation so a tool cannot keep running after its policy is revoked.
        if get_tool_domain(self.tools[name]) not in self.allowed_domains:
            raise _ToolRejected(
                f"Tool {name!r} is no longer authorized for {self.role}."
            )

    def _admit(self, name: str) -> None:
        """Reserve before validation and before the first await."""
        increment = self._increment(name)
        projected = self.budget.projected()
        if is_over_budget(self.config, projected) or not can_spend_tool_call(
            self.config, projected, increment
        ):
            reason = f"Budget guard skipped {name}: tool or search budget exhausted."
            self.budget.add(budget_usage_with_reason(reason))
            raise _ToolRejected(reason)
        self.budget.add(increment)

    def _increment(self, name: str) -> dict[str, Any]:
        return budget_from_tool_calls([{"name": name}], self.tools)

    def tool_config(self, context: ToolContext[Any]) -> RunnableConfig:
        """Give each invoker its own operation metadata.

        Read this inside the business callable: ``execute`` admits the protected
        operation before running it, so a config built at the tool body's call
        site would miss the operation identity.
        """
        operation_id = context.context.results[context.tool_call_id].operation_id
        if operation_id is None:
            return self.runtime_config
        return {
            **self.runtime_config,
            "metadata": {
                **self.runtime_config.get("metadata", {}),
                "operation_id": operation_id,
                "idempotency_key": operation_id,
            },
        }

    async def execute(
        self,
        context: ToolContext[Any],
        function: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        """Execute validated input and settle any external operation receipt."""
        name, call_id = context.tool_name, context.tool_call_id
        record = context.context.results[call_id]
        scope = stage_context.get()
        operation_id = None
        if scope is not None and tool_effect(self.tools[name]) == "protected":
            operation_id = digest((DBOS.workflow_id, name, call_id))
            try:
                _, cancelled = await owned_io(
                    start_operation,
                    scope.run_id,
                    DBOS.workflow_id,
                    DBOS.executor_id,
                    operation_id,
                    name,
                    digest(record.args),
                )
            except SQLAlchemyError as exc:
                raise ExecutionControlError(
                    "External tool admission storage unavailable"
                ) from exc
            if cancelled is not None:
                await commit_receipt(
                    finish_operation, scope.run_id, operation_id, "not_executed"
                )
                raise cancelled
            record.operation_id = operation_id

        metadata = {
            "role": self.role,
            "name": name,
            "tool_call_id": call_id,
            "invocation_id": record.sdk_run_id,
            "operation_id": operation_id,
        }
        if scope is not None:
            try:
                await scope.observe(
                    "tool",
                    dict(metadata, status="started", **self._increment(name)),
                    call_id=call_id + ":started",
                )
            except BaseException as exc:
                if operation_id is not None:
                    await commit_receipt(
                        finish_operation, scope.run_id, operation_id, "not_executed"
                    )
                if isinstance(exc, SQLAlchemyError):
                    raise ExecutionControlError(
                        "Tool observation storage unavailable"
                    ) from exc
                raise

        record.effect_status = "started"
        try:
            with budget_capture() as captured:
                try:
                    if iscoroutinefunction(function):
                        output = await function(*args, **kwargs)
                    else:
                        # The executor owns this task until the thread and receipt finish.
                        output = await asyncio.to_thread(function, *args, **kwargs)
                finally:
                    self.budget.add(captured)
            result = _result_text(name, output)
        except BaseException as exc:
            if operation_id is not None:
                await commit_receipt(
                    finish_operation, scope.run_id, operation_id, "unknown"
                )
                record.effect_status = "uncertain"
                if isinstance(exc, Exception):
                    raise unconfirmed(
                        "External tool result requires reconciliation"
                    ) from exc
            raise
        if operation_id is not None:
            await commit_receipt(
                finish_operation, scope.run_id, operation_id, "returned"
            )
        record.effect_status = "returned"
        if scope is not None:
            await scope.observe(
                "tool", dict(metadata, status="returned"), call_id=call_id + ":returned"
            )
        return result
