"""Admit SDK tool calls and guard external effects inside por.agent_tool."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Any

from agents.exceptions import MCPToolCancellationError
from agents.tool_context import ToolContext
from dbos import DBOS
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
    owned_io,
    stage_context,
    start_operation,
    unconfirmed,
)

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from open_deep_research.runtime.sdk_runtime import _RunBudget
    from open_deep_research.tools import BusinessTool


class _ToolRejected(Exception):
    """Return an ordinary tool error to the model."""


def format_tool_error(_context: ToolContext[Any], error: Exception) -> str:
    return f"Error executing tool: {error}"


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
    """Share the role's permission and budget policy across all tool calls."""

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

    def _increment(self, name: str) -> dict[str, Any]:
        return budget_from_tool_calls([{"name": name}], self.tools)

    async def invoke(
        self,
        name: str,
        declared_invoke: Callable[[ToolContext[Any], str], Awaitable[str]],
        context: ToolContext[Any],
        arguments: str,
    ) -> str:
        """Admit a call before the SDK validates and runs its declaration.

        Permission and budget are reserved before the first await, so invalid
        input and failed attempts are still accounted for. ``declared_invoke``
        is the SDK's own callback (or the MCP schema adapter): it validates the
        arguments before the tool body enters the external-effect boundary.
        """
        tool = self.tools[name]
        if (
            context.tool_name != name
            or tool.name != name
            or get_tool_domain(tool) not in self.allowed_domains
        ):
            raise _ToolRejected(
                f"Tool {name!r} is no longer authorized for {self.role}."
            )
        increment = self._increment(name)
        projected = self.budget.projected()
        if is_over_budget(self.config, projected) or not can_spend_tool_call(
            self.config, projected, increment
        ):
            reason = f"Budget guard skipped {name}: tool or search budget exhausted."
            self.budget.add(budget_usage_with_reason(reason))
            raise _ToolRejected(reason)
        self.budget.add(increment)
        return await declared_invoke(context, arguments)

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
                        # The Harness owns this task until the thread and receipt finish.
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
