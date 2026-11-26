"""Own the durable lifecycle of every SDK business tool call.

The SDK still owns tool declarations, argument validation and defaults. This
executor owns admission, the ``por.agent_tool`` DBOS Step, committed-result
recovery, in-flight de-duplication and the protected external-effect receipts.
The Context Harness only records and projects the results it is handed back.

Recovery follows the declared ``effect``, because a committed Step is the only
proof that a call finished:

* a committed ``por.agent_tool`` record is replayed without running the tool;
* an uncommitted Step may run the tool again. ``read`` and ``idempotent`` tools
  accept that: the ledger keeps one logical call, so no allowance is charged
  twice, while the provider may receive the request twice. The duplicate is
  reported as a budget warning instead of being hidden;
* ``protected`` tools consult the existing Operation. An Apify Actor reference
  can be queried again. An operation without proof of absence or a reference
  that can be resolved is fenced.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from functools import partial
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Any, NoReturn

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
    budget_usage_with_warning,
    can_spend_tool_call,
)
from open_deep_research.configuration import Configuration
from open_deep_research.mcp.domain_filter import get_tool_domain
from open_deep_research.tools import tool_result_text
from open_deep_research.tools.registry import tool_effect
from open_deep_research.web.run_data import digest
from open_deep_research.workflow.execution import (
    ExecutionControlError,
    admit_tool_call,
    commit_receipt,
    finish_operation,
    finish_owned,
    finish_tool_call,
    is_cancellation,
    is_execution_control_failure,
    native_step_available,
    owned_io,
    record_apify_run,
    stage_context,
    start_operation,
    unconfirmed,
)

if TYPE_CHECKING:
    from open_deep_research.runtime.sdk_context import (
        ObservedToolResult,
        SDKContextHarness,
    )
    from open_deep_research.runtime.sdk_runtime import _RunBudget
    from open_deep_research.tools import BusinessTool

tool_usage: ContextVar[dict | None] = ContextVar("tool_usage", default=None)

LOGGER = logging.getLogger(__name__)

# The Harness reports what the SDK observed; the ledger stores the durable
# outcome. ``unobserved`` and ``running`` have no outcome and never settle a
# call, so they are absent here instead of defaulting to success.
LEDGER_STATUS = {
    "succeeded": "returned",
    "failed": "failed",
    "cancelled": "cancelled",
}


class _ToolRejected(Exception):
    """Return an ordinary tool error to the model."""


@dataclass(frozen=True)
class _Invocation:
    """One SDK tool call: the facts every Step of that call needs.

    Grouping them keeps the Step, the replay and the drain paths from threading
    seven arguments through each other.
    """

    harness: SDKContextHarness
    record: ObservedToolResult
    tool: FunctionTool
    context: ToolContext[Any]
    arguments: str
    previous: asyncio.Future[Any] | None
    drain_on_cancel: bool


def _result_text(name: str, output: Any) -> str:
    # MCP result errors are checked by the MCP wrapper. Its structured_content
    # contains business data, such as the state of an already started Actor.
    envelope = output
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
        tools: dict[str, BusinessTool],
    ):
        self.role = role
        self.allowed_domains = allowed_domains
        self.config = config
        self.runtime_config = runtime_config
        self.budget = budget
        self.tools = tools
        self._tool_calls: dict[str, asyncio.Future[Any]] = {}
        self._step_lock = asyncio.Lock()
        self._step_failure: BaseException | None = None

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
        previous = self._tool_calls.get(record.tool_call_id)
        completion = None
        if previous is None:
            completion = asyncio.get_running_loop().create_future()
            self._tool_calls[record.tool_call_id] = completion
        call = _Invocation(
            harness, record, tool, context, arguments, previous, drain_on_cancel
        )
        try:
            if native_step_available():
                # DBOSRunner orders step starts. Wait for each commit as well:
                # native rewind would discard later successes in the same batch.
                async with self._step_lock:
                    if self._step_failure is not None:
                        harness.close_unexecuted(
                            record, type(self._step_failure).__name__
                        )
                        raise self._step_failure
                    try:
                        output = await self._step(call)
                    except BaseException as exc:
                        self._step_failure = exc
                        raise
            else:
                output = await self._perform(call)
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

    async def _step(self, call: _Invocation) -> Any:
        """Execute or replay the call under the ``por.agent_tool`` Step.

        A duplicate call ID still consumes its own Step slot while sharing the
        first result, including a result restored from the journal.
        """
        executed = False

        async def invoke_step():
            nonlocal executed
            executed = True
            local: dict[str, Any] = {}
            token = tool_usage.set(local)
            try:
                output = await self._perform(call)
            except BaseException as exc:
                if call.previous is None:
                    exc.budget_usage = local
                raise
            finally:
                tool_usage.reset(token)
            return {
                "output": output,
                "record": asdict(call.harness.results[call.record.tool_call_id]),
                "budget": local,
            }

        try:
            saved = await DBOS.run_step_async(
                {"name": "por.agent_tool", "preemptible": True}, invoke_step
            )
        except BaseException as exc:
            # Live execution already applied the local delta. Only a replay of
            # the owning call has to restore it from the error receipt.
            if not executed and call.previous is None:
                self.budget.add(getattr(exc, "budget_usage", {}))
                # This Step never reached ``_perform``, so the tool body did not
                # run: no result, no allowance and no external effect. The record
                # must not stay observed as running, because the SDK cancelled
                # this invocation and will never finish the call.
                call.harness.close_unexecuted(call.record, type(exc).__name__)
            raise
        if not executed:
            call.harness.restore_tool(saved["record"])
            self.budget.add(saved["budget"])
        return saved["output"]

    async def _perform(self, call: _Invocation) -> Any:
        """Admit, run and settle one call; never replayed by this function."""
        harness, record = call.harness, call.record
        if call.previous is not None:
            return await asyncio.shield(call.previous)
        # From here on the call may reach the tool body, so settlement must not
        # close its record as never executed even if this coroutine is killed.
        harness.mark_executing(record.tool_call_id)
        admitted = False
        try:
            if harness.closing:
                harness.record_tool(
                    call.context, "cancelled", None, error_type="CancelledError"
                )
                raise asyncio.CancelledError()
            try:
                self._authorize(record.tool_name)
                await self._admit(record)
                admitted = True
                # The original SDK declaration validates arguments and defaults
                # before the tool body admits a protected external operation.
                invoked = call.tool.on_invoke_tool(call.context, call.arguments)
                if call.drain_on_cancel:
                    output, _ = await finish_owned(asyncio.ensure_future(invoked))
                else:
                    output = await invoked
            except (asyncio.CancelledError, dbos_error.DBOSWorkflowCancelledError):
                harness.record_tool(
                    call.context, "cancelled", None, error_type="CancelledError"
                )
                raise
            except BaseException as exc:
                # Wrapped budget, storage, DBOS and cancellation failures must
                # stop the Agent loop instead of becoming tool failure text.
                if is_execution_control_failure(exc):
                    harness.record_tool(
                        call.context,
                        "cancelled" if is_cancellation(exc) else "failed",
                        None,
                        error_type=type(exc).__name__,
                    )
                    raise
                if not isinstance(exc, Exception):
                    raise
                output = f"Error executing tool: {exc}"
                harness.record_tool(
                    call.context,
                    "cancelled"
                    if isinstance(exc, MCPToolCancellationError)
                    else "failed",
                    output,
                    error_type=type(exc).__name__,
                )
                return output
            harness.record_tool(call.context, "succeeded", output)
            return output
        finally:
            # Only an admitted call has a ledger row to settle.
            if admitted:
                await self._settle_call(record)

    def _authorize(self, name: str) -> None:
        # Registration filters each role's tools; re-read the current domain at
        # invocation so a tool cannot keep running after its policy is revoked.
        tool = self.tools.get(name)
        if tool is None or get_tool_domain(tool) not in self.allowed_domains:
            raise _ToolRejected(
                f"Tool {name!r} is no longer authorized for {self.role}."
            )

    def _skip(self, name: str) -> NoReturn:
        reason = f"Budget guard skipped {name}: tool or search budget exhausted."
        self.budget.add(budget_usage_with_reason(reason))
        raise _ToolRejected(reason)

    async def _admit(self, record: ObservedToolResult) -> None:
        """Charge and record one logical call before its arguments are validated."""
        name = record.tool_name
        increment = budget_from_tool_calls([{"name": name}], self.tools)
        scope = stage_context.get()
        if scope is None:
            if not can_spend_tool_call(self.config, self.budget.projected(), increment):
                self._skip(name)
        else:
            # Every admitted call is recorded, whether or not budget enforcement
            # is on. Concurrent Producers share the Run's State snapshot, so only
            # this atomic Run-level admission can enforce the configured ceiling.
            try:
                outcome, cancellation = await owned_io(
                    admit_tool_call,
                    self.config,
                    scope.run_id,
                    DBOS.workflow_id,
                    DBOS.executor_id,
                    self.ledger_id(record.tool_call_id),
                    name,
                    increment["search_calls"] > 0,
                )
            except SQLAlchemyError as exc:
                raise ExecutionControlError(
                    "Tool admission storage unavailable"
                ) from exc
            if cancellation is not None:
                raise cancellation
            if outcome == "rejected":
                self._skip(name)
            if outcome == "duplicate" and tool_effect(self.tools[name]) != "protected":
                # An earlier attempt already paid for this logical call, so the
                # allowance is not charged twice. Its uncommitted Step is exactly
                # the window in which the provider may have been called twice.
                self.budget.add(
                    budget_usage_with_warning(
                        f"{name} re-ran after an uncommitted tool step; one logical "
                        "tool call is recorded but the provider may have received it twice."
                    )
                )
        self.budget.add(increment)

    @staticmethod
    def ledger_id(call_id: str) -> str:
        """Derive the Run-scoped ledger identity of one SDK tool call.

        Stored in ``por_domain.tool_calls`` and ``por_domain.operations``, so a
        pending Run keeps resolving the same receipts.
        """
        return digest((DBOS.workflow_id, call_id))

    @staticmethod
    def operation_id(record: ObservedToolResult) -> str:
        """Derive the external-operation identity of one SDK tool call.

        Persisted in ``por_domain.operations``; changing the derivation would
        orphan the receipts of an in-flight Run, so it stays fixed.
        """
        return digest((DBOS.workflow_id, record.tool_name, record.tool_call_id))

    async def _settle_call(self, record: ObservedToolResult) -> None:
        """Persist the ledger outcome; a bookkeeping error never masks the tool.

        The committed ``por.agent_tool`` result stays authoritative, so this
        record cannot change the tool's own success, failure or cancellation.
        """
        scope = stage_context.get()
        status = LEDGER_STATUS.get(record.status)
        if scope is None or status is None:
            return
        try:
            recorded, _ = await owned_io(
                finish_tool_call,
                scope.run_id,
                self.ledger_id(record.tool_call_id),
                status,
            )
        except Exception:  # noqa: BLE001 - the journal owns the tool outcome
            LOGGER.warning("Could not record tool call outcome.", exc_info=True)
            return
        if not recorded:
            LOGGER.warning("Tool call receipt was missing when its outcome settled.")

    def tool_config(self, context: ToolContext[Any]) -> RunnableConfig:
        """Give each invoker its own operation or idempotency metadata.

        Read this inside the business callable: ``execute`` admits the protected
        operation before running it, so a config built at the tool body's call
        site would miss the operation identity.

        A protected call carries the identity of its Operation row. An
        idempotent call has no receipt; it receives the same derived identity as
        its idempotency key, so a retry can reuse it instead of inventing a
        second one.
        """
        record = context.context.results[context.tool_call_id]
        tool = self.tools.get(record.tool_name)
        if record.operation_id is not None:
            keys = {
                "operation_id": record.operation_id,
                "idempotency_key": record.operation_id,
            }
        elif tool is not None and tool_effect(tool) == "idempotent":
            keys = {"idempotency_key": self.operation_id(record)}
        else:
            return self.runtime_config
        return {
            **self.runtime_config,
            "metadata": {
                **self.runtime_config.get("metadata", {}),
                **keys,
            },
        }

    async def remember_apify_run(self, context: ToolContext[Any], actor_run_id: str):
        scope = stage_context.get()
        record = context.context.results[context.tool_call_id]
        if scope is not None and record.operation_id is not None:
            await commit_receipt(
                record_apify_run, scope.run_id, record.operation_id, actor_run_id
            )

    async def execute(
        self,
        context: ToolContext[Any],
        function: Callable[..., Any],
        /,
        *args: Any,
        recover_apify: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> str:
        """Execute validated input and settle any external operation receipt."""
        name, call_id = context.tool_name, context.tool_call_id
        record = context.context.results[call_id]
        scope = stage_context.get()
        tool = self.tools.get(name)
        # An unlisted tool cannot prove it is safe, so it keeps the strongest
        # protection instead of silently skipping its receipt.
        protected = tool is None or tool_effect(tool) == "protected"
        operation_id = None
        actor_run_id = None
        if scope is not None and protected:
            operation_id = self.operation_id(record)
            try:
                actor_run_id, cancelled = await owned_io(
                    start_operation,
                    scope.run_id,
                    DBOS.workflow_id,
                    DBOS.executor_id,
                    operation_id,
                    self.ledger_id(call_id),
                    name,
                    digest(record.args),
                )
            except SQLAlchemyError as exc:
                raise ExecutionControlError(
                    "External tool admission storage unavailable"
                ) from exc
            if cancelled is not None:
                if actor_run_id is None:
                    await commit_receipt(
                        finish_operation, scope.run_id, operation_id, "not_executed"
                    )
                raise cancelled
            if actor_run_id is not None and recover_apify is None:
                raise unconfirmed("Apify receipt cannot be resolved by this tool")
            record.operation_id = operation_id

        metadata = {
            "role": self.role,
            "name": name,
            "tool_call_id": call_id,
            "invocation_id": record.sdk_run_id,
            "operation_id": operation_id,
        }
        if actor_run_id is not None:
            metadata["reason"] = "Reading the original Apify Actor run"
        if scope is not None:
            try:
                await scope.observe(
                    "tool",
                    dict(
                        metadata,
                        status="started",
                        **budget_from_tool_calls([{"name": name}], self.tools),
                    ),
                    call_id=call_id + ":started",
                )
            except BaseException as exc:
                if operation_id is not None and actor_run_id is None:
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
                    if actor_run_id is not None:
                        output = await recover_apify(actor_run_id)
                    elif iscoroutinefunction(function):
                        output = await function(*args, **kwargs)
                    else:
                        # The executor owns this task until the thread and receipt finish.
                        output = await asyncio.to_thread(function, *args, **kwargs)
                finally:
                    self.budget.add(captured)
            result = _result_text(name, output)
        except BaseException as exc:
            if operation_id is not None:
                if actor_run_id is None:
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
                finish_operation,
                scope.run_id,
                operation_id,
                "returned",
                *((actor_run_id,) if actor_run_id else ()),
            )
        record.effect_status = "returned"
        if scope is not None:
            await scope.observe(
                "tool", dict(metadata, status="returned"), call_id=call_id + ":returned"
            )
        return result
