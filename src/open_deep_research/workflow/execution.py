"""Own parallel tasks until every child has returned or completed its cleanup."""

from __future__ import annotations

import asyncio
import builtins
import time
from collections.abc import Coroutine, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from itertools import count
from typing import Any

from dbos import DBOS
from dbos import error as dbos_error
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.web.run_data import (
    digest,
    publish_event,
    read_request,
    sdk_observation,
)
from open_deep_research.workflow.dbos_config import (
    FACT_WRITE_GRACE_SECONDS,
    domain_engine,
    native_client,
)

# The SDK may finish already owned I/O and evidence after its Workflow is
# cancelled. That cleanup cannot submit more native commands.
owned_cleanup: ContextVar[bool] = ContextVar("owned_cleanup", default=False)


class ExecutionControlError(RuntimeError):
    """Abort SDK execution when an external boundary cannot be confirmed."""

    unknown_outcome: bool = False


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
    if any(is_cancellation(cause) for cause in error_chain(error)):
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


@dataclass
class StageContext:
    """In-memory correlation for budget policy and public SDK events."""

    run_id: str
    stage_id: str
    name: str
    generation: int
    config: Any
    initial_usage: dict
    reserve_report: bool = True
    expires_at: float | None = None
    ordinals: dict = field(default_factory=dict)

    @property
    def execution_id(self):
        return digest((self.stage_id, self.generation))

    def call_id(self, category):
        slot = (DBOS.workflow_id, DBOS.step_id, category)
        ordinal = next(self.ordinals.setdefault(slot, count(1)))
        return digest((slot, ordinal, self.generation))

    async def observe(self, kind, payload, *, call_id=None):
        event_id = (
            digest((self.execution_id, kind, call_id))
            if call_id is not None
            else self.call_id("observation")
        )

        def publish():
            with domain_engine().begin() as db:
                publish_event(
                    db,
                    self.run_id,
                    "sdk_event",
                    event=kind,
                    event_id=event_id,
                    idempotency_key=event_id,
                    step=self.name,
                    unit_id=self.stage_id,
                    execution_id=self.execution_id,
                    generation=self.generation,
                    **sdk_observation(payload),
                )

            return event_id

        if native_step_available() and call_id is None:
            committed = await DBOS.run_step_async(
                {"name": "por.sdk_observation"}, publish
            )
            if kind in {"agent_completed", "agent_failed"}:
                await DBOS.set_event_async("sdk_tail", committed)
        else:
            await asyncio.to_thread(publish)


stage_context: ContextVar[StageContext | None] = ContextVar(
    "stage_context", default=None
)


def unconfirmed(message):
    error = ExecutionControlError(message)
    error.unknown_outcome = True
    return error


def require_live_effect(db, run_id, workflow_id, executor_id):
    """Fence new effects using cancellation intent and the native executor status."""
    request = read_request(run_id, db=db, lock=True)
    if request["cancel_requested"]:
        raise dbos_error.DBOSWorkflowCancelledError("Cancellation requested")
    workflows = native_client().list_workflows(
        workflow_ids=[workflow_id], load_input=False, load_output=False
    )
    if not workflows or workflows[0].status == "CANCELLED":
        raise dbos_error.DBOSWorkflowCancelledError("Workflow is cancelled")
    if (
        workflows[0].status != "PENDING"
        or workflows[0].executor_id != executor_id
        or request["reconciliation"]
    ):
        raise ExecutionControlError("Native executor no longer owns this effect")


def start_operation(run_id, workflow_id, executor_id, operation_id, name, fingerprint):
    with domain_engine().begin() as db:
        require_live_effect(db, run_id, workflow_id, executor_id)
        if db.scalar(
            text("""
            SELECT EXISTS (SELECT 1 FROM por_domain.operations WHERE run_id=:r
                AND (outcome='unknown' OR (outcome IS NULL AND executor_id<>:executor)))
            """),
            {"r": run_id, "executor": executor_id},
        ):
            raise unconfirmed("An earlier executor has an unconfirmed external effect")
        if (
            db.execute(
                text("""
            INSERT INTO por_domain.operations(run_id,id,name,fingerprint,executor_id)
            VALUES (:r,:id,:name,:fingerprint,:executor) ON CONFLICT DO NOTHING
            """),
                dict(
                    r=run_id,
                    id=operation_id,
                    name=name,
                    fingerprint=fingerprint,
                    executor=executor_id,
                ),
            ).rowcount
            != 1
        ):
            raise unconfirmed("External tool was already admitted; inspect its receipt")


def finish_operation(run_id, operation_id, outcome):
    with domain_engine().begin() as db:
        if (
            db.execute(
                text("""
            UPDATE por_domain.operations SET outcome=:outcome
            WHERE run_id=:r AND id=:id AND (outcome IS NULL OR outcome=:outcome)
            """),
                dict(r=run_id, id=operation_id, outcome=outcome),
            ).rowcount
            != 1
        ):
            raise unconfirmed(
                "External tool receipt did not match its original outcome"
            )


async def commit_receipt(write, *args):
    """Allow retry of a physical receipt write after a lost SQL ACK."""

    async def commit():
        deadline = time.monotonic() + FACT_WRITE_GRACE_SECONDS
        while True:
            try:
                await asyncio.to_thread(write, *args)
                return
            except SQLAlchemyError as exc:
                if time.monotonic() >= deadline:
                    raise unconfirmed("Could not confirm the external receipt") from exc
                await asyncio.sleep(0.5)

    _, cancellation = await finish_owned(asyncio.create_task(commit()))
    if cancellation is not None:
        raise cancellation


@asynccontextmanager
async def execution_effect():
    """Serialize Graph and Memory commits with application cancellation."""
    scope = stage_context.get()
    if scope is None:
        yield
        return

    workflow_id, executor_id = DBOS.workflow_id, DBOS.executor_id
    with domain_engine().begin() as db:
        _, cancellation = await owned_io(
            require_live_effect, db, scope.run_id, workflow_id, executor_id
        )
        if cancellation is not None:
            raise cancellation
        yield
