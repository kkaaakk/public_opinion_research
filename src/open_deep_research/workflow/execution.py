"""Own parallel tasks until every child has returned or completed its cleanup."""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import Coroutine, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from dbos import DBOS
from dbos import error as dbos_error

# The SDK may finish already owned I/O and evidence after its Workflow is
# cancelled. That cleanup cannot submit more native commands.
owned_cleanup: ContextVar[bool] = ContextVar("owned_cleanup", default=False)


class ExecutionControlError(RuntimeError):
    """Abort SDK execution when durable permission/storage cannot be confirmed."""


def native_step_available() -> bool:
    """A Step inside another Step cannot create an independent checkpoint."""
    return (
        DBOS.workflow_id is not None
        and DBOS.step_id is None
        and not owned_cleanup.get()
    )


def error_chain(error):
    """Visit causes and grouped errors once, including SDK-wrapped failures."""
    pending = [error]
    seen = set()
    while pending:
        exc = pending.pop()
        if exc is None or id(exc) in seen:
            continue
        seen.add(id(exc))
        yield exc
        pending.extend((exc.__context__, exc.__cause__))
        pending.extend(getattr(exc, "exceptions", ()))


def is_cancellation(error):
    if isinstance(error, getattr(builtins, "BaseExceptionGroup", ())):
        return any(is_cancellation(item) for item in error.exceptions)
    return isinstance(
        error,
        (
            asyncio.CancelledError,
            dbos_error.DBOSWorkflowCancelledError,
            dbos_error.DBOSAwaitedWorkflowCancelledError,
        ),
    )


def failure_kind(error):
    """Classify business failures without publishing exception contents."""
    from open_deep_research.budget import ModelBudgetExceeded
    from open_deep_research.llm.errors import ModelInvocationError

    if any(isinstance(cause, ExecutionControlError) for cause in error_chain(error)):
        return "execution_control_error"
    if is_cancellation(error):
        return "cancelled"
    if isinstance(
        error, (TimeoutError, asyncio.TimeoutError, dbos_error.DBOSStepTimeoutError)
    ):
        return "timeout"
    if isinstance(error, ModelBudgetExceeded):
        return "budget_exhausted"
    if isinstance(error, ModelInvocationError):
        return "model_error"
    if type(error).__name__ in {"ResearchGraphSettlementError", "ToolCallError"}:
        return "tool_or_settlement_error"
    if type(error).__module__.startswith(("agents", "openai")):
        return "model_error"
    return "error"


@asynccontextmanager
async def stage_timeout(seconds):
    """Apply the optional per-stage timeout to the live coroutine and its I/O."""
    if seconds is None:
        yield
        return
    task = asyncio.current_task()
    cancellations = task.cancelling() if hasattr(task, "cancelling") else 0
    timed_out = False

    def expire():
        nonlocal timed_out
        timed_out = True
        task.cancel()

    deadline = asyncio.get_running_loop().call_later(seconds, expire)
    try:
        yield
    except asyncio.CancelledError as exc:
        if timed_out:
            remaining = task.uncancel() if hasattr(task, "uncancel") else cancellations
            if remaining <= cancellations:
                raise TimeoutError("Business stage timed out") from exc
        raise
    finally:
        deadline.cancel()


async def gather_owned(calls: Mapping[str, Coroutine[Any, Any, Any]]) -> dict[str, Any]:
    """Cancel siblings on failure and shield their drain against repeated cancellation.

    There is no retry here. SDK/tool timeouts and bounded settlement remain owned
    by those layers. Preserve all outcomes on the original exception for exact
    accounting, including simultaneous failures and successful siblings.
    """
    results: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}

    async def collect(name: str, call: Coroutine[Any, Any, Any]) -> None:
        try:
            results[name] = await call
        except BaseException as exc:
            errors[name] = exc

    tasks = [
        asyncio.create_task(collect(name, call), name=name)
        for name, call in calls.items()
    ]
    cancellation = None
    pending = set(tasks)
    try:
        while pending and not errors:
            _, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
    except asyncio.CancelledError as exc:
        cancellation = exc
    finally:
        # Issue cancellation once. Repeated parent cancellation must not interrupt
        # the child's already-running SDK exit settlement.
        for task in pending:
            if not task.done():
                task.cancel()
        drain = asyncio.gather(*tasks, return_exceptions=True)
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
        drain.result()

    ordered_results = {name: results[name] for name in calls if name in results}
    ordered_errors = {name: errors[name] for name in calls if name in errors}
    if cancellation is not None or ordered_errors:
        # A sibling cancelled because another step failed must not hide the
        # initiating failure merely because that sibling comes first by name.
        if cancellation is not None:
            failure: BaseException = cancellation
        else:
            failure = next(
                (
                    error
                    for error in ordered_errors.values()
                    if not isinstance(error, asyncio.CancelledError)
                ),
                next(iter(ordered_errors.values())),
            )
        setattr(failure, "parallel_results", ordered_results)
        setattr(failure, "parallel_errors", ordered_errors)
        raise failure
    return ordered_results


async def finish_owned(task):
    """Wait for an already started receipt write or synchronous I/O to finish."""
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    return task.result(), cancellation


async def owned_io(operation, *args, **kwargs):
    """Drain a real control commit even if its asyncio awaiter is cancelled."""
    return await finish_owned(
        asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    )


@asynccontextmanager
async def execution_effect():
    """Hold an optional business permission across a Graph/Memory commit."""
    from open_deep_research.workflow.run_facts import current_unit

    unit = current_unit.get()
    if unit is not None and getattr(unit.control_error, "unknown_outcome", False):
        raise unit.control_error
    guard = unit.repo.effect_guard(unit.id, unit.permit) if unit is not None else None
    if guard is not None:
        _, cancelled = await owned_io(guard.__enter__)
        if cancelled is not None:
            try:
                await owned_io(guard.__exit__, None, None, None)
            finally:
                raise cancelled
    try:
        yield
    finally:
        if guard is not None:
            _, cancelled = await owned_io(guard.__exit__, None, None, None)
            if cancelled is not None:
                raise cancelled
