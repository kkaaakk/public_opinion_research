"""Own parallel tasks until every child has returned or completed its cleanup."""

from __future__ import annotations

import asyncio
import builtins
import logging
import time
from collections.abc import Coroutine, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Literal

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

LOGGER = logging.getLogger(__name__)

# Outcomes the tool ledger stores after admission; a call keeps its allowance.
TOOL_CALL_OUTCOMES = frozenset({"returned", "failed", "cancelled"})

# ``admitted`` charged this call once, ``duplicate`` means an earlier attempt
# already recorded the same logical call, ``rejected`` left no row behind.
ToolAdmission = Literal["admitted", "duplicate", "rejected"]


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


def _error_chain_links(error):
    """Visit causes and grouped failures, flagging implicit ``__context__`` links."""
    pending = [(error, True)]
    seen = set()
    while pending:
        exc, explicit = pending.pop()
        if exc is None or id(exc) in seen:
            continue
        seen.add(id(exc))
        yield exc, explicit
        pending.append((exc.__context__, False))
        pending.append((exc.__cause__, True))
        pending.extend((item, explicit) for item in getattr(exc, "exceptions", ()))


def is_execution_control_failure(error):
    """Return whether any error in the causal chain must abort execution.

    Budget/model wrappers and SDK/MCP transports can wrap a lower-level failure.
    Explicit causes are inspected for cancellation too, while implicit contexts
    are not: anyio reports an ordinary MCP timeout as ``McpError`` whose context
    is its internal ``CancelledError``, and that must stay a tool failure.
    """
    from open_deep_research.budget import ModelBudgetExceeded

    for cause, explicit in _error_chain_links(error):
        if isinstance(
            cause,
            (
                ModelBudgetExceeded,
                ExecutionControlError,
                SQLAlchemyError,
                dbos_error.DBOSBaseException,
                dbos_error.DBOSException,
            ),
        ):
            return True
        if explicit and is_cancellation(cause):
            return True
    return False


def failure_kind(error):
    """Classify business failures without publishing exception contents."""
    from open_deep_research.budget import ModelBudgetExceeded
    from open_deep_research.llm.errors import ModelInvocationError

    if any(isinstance(cause, ExecutionControlError) for cause in error_chain(error)):
        return "execution_control_error"
    if any(is_cancellation(cause) for cause in error_chain(error)):
        return "cancelled"
    if any(isinstance(cause, ModelBudgetExceeded) for cause in error_chain(error)):
        return "budget_exhausted"
    if isinstance(
        error, (TimeoutError, asyncio.TimeoutError, dbos_error.DBOSStepTimeoutError)
    ):
        return "timeout"
    if isinstance(error, ModelInvocationError):
        return "model_error"
    if type(error).__name__ == "ToolCallError":
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
    research_round: int = 1
    ordinals: dict = field(default_factory=dict)

    @property
    def execution_id(self):
        return digest((self.stage_id, self.generation))

    def call_id(self, category):
        slot = (DBOS.workflow_id, DBOS.step_id, category)
        ordinal = next(self.ordinals.setdefault(slot, count(1)))
        return digest((slot, ordinal, self.generation))

    def observation_id(self, kind, payload):
        """Name one observation by the business facts it reports.

        The identity must not depend on a Step that no longer exists, so it is
        derived from the stage execution and the reported facts. An identical
        report inside one stage execution is the same fact, and the native
        message idempotency key makes the second write a no-op.
        """
        return digest((self.execution_id, kind, sdk_observation(payload)))

    async def observe(self, kind, payload, *, call_id=None):
        detail = sdk_observation(payload)
        event_id = (
            digest((self.execution_id, kind, call_id))
            if call_id is not None
            else self.observation_id(kind, detail)
        )

        def publish():
            with domain_engine().begin() as db:
                publish_event(
                    db,
                    self.run_id,
                    "sdk_event",
                    event=kind,
                    idempotency_key=event_id,
                    step=self.name,
                    unit_id=self.stage_id,
                    execution_id=self.execution_id,
                    generation=self.generation,
                    **detail,
                )

        # Observations are progress, not business facts: the model, tool and
        # operation ledgers stay authoritative, so a storage blip must not fail
        # the Agent loop. The idempotency key makes the retry harmless.
        try:
            await asyncio.to_thread(publish)
        except SQLAlchemyError:
            LOGGER.warning("Could not publish an SDK observation.", exc_info=True)


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


def start_operation(
    run_id, workflow_id, executor_id, operation_id, tool_call_id, name, fingerprint
):
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
            INSERT INTO por_domain.operations(
                run_id,id,tool_call_id,name,fingerprint,executor_id)
            VALUES (:r,:id,:tool_call,:name,:fingerprint,:executor) ON CONFLICT DO NOTHING
            """),
                dict(
                    r=run_id,
                    id=operation_id,
                    tool_call=tool_call_id,
                    name=name,
                    fingerprint=fingerprint,
                    executor=executor_id,
                ),
            ).rowcount
            != 1
        ):
            # The receipt, not the tool-call status, is what survives a crash
            # here. It proves the effect was admitted, never what it returned.
            raise unconfirmed(
                "External tool already ran for this call; its receipt is authoritative "
                "and the response was not journaled"
            )


def admit_tool_call(
    configurable, run_id, workflow_id, executor_id, call_id, name, search
) -> ToolAdmission:
    """Admit and record one tool call against the Run-wide tool/search ceiling.

    The record is the authority for how often a tool ran, so it is written for
    every admitted call whether or not budget enforcement is enabled; the
    configured ceiling only decides whether the call is allowed. The request row
    lock serializes concurrent Producers. A repeated call identity returns
    ``duplicate`` without charging again, so DBOS recovery of an uncommitted
    ``por.agent_tool`` Step cannot consume a second allowance, and a committed
    row means the call stays charged whatever its later outcome. A rejected call
    leaves no row.

    ``duplicate`` is also the only reliable evidence that an earlier attempt
    reached this call: the Step that would have committed its result is gone, so
    the provider may see the request again. The caller decides what that risk
    means for the tool's declared effect.
    """
    from open_deep_research.budget import can_spend_tool_call

    keys = {"r": run_id, "id": call_id}
    increment = {"tool_calls": 1, "search_calls": int(bool(search))}
    with domain_engine().begin() as db:
        require_live_effect(db, run_id, workflow_id, executor_id)
        if db.scalar(
            text("SELECT 1 FROM por_domain.tool_calls WHERE run_id=:r AND id=:id"),
            keys,
        ):
            return "duplicate"
        calls, searches = db.execute(
            text(
                "SELECT count(*), count(*) FILTER (WHERE search) "
                "FROM por_domain.tool_calls WHERE run_id=:r"
            ),
            {"r": run_id},
        ).one()
        usage = {"tool_calls": int(calls), "search_calls": int(searches)}
        if not can_spend_tool_call(configurable, usage, increment):
            return "rejected"
        db.execute(
            text(
                "INSERT INTO por_domain.tool_calls(run_id, id, name, search) "
                "VALUES (:r, :id, :name, :search)"
            ),
            dict(keys, name=name, search=increment["search_calls"] > 0),
        )
        return "admitted"


def finish_tool_call(run_id, tool_call_id, status):
    """Record one terminal tool-call outcome without releasing its allowance.

    Returns whether a row explains the outcome. A replay of an already settled
    call writes nothing and still returns ``True``; the committed admission row
    stays charged whatever the tool did.
    """
    if status not in TOOL_CALL_OUTCOMES:
        return False
    keys = {"r": run_id, "id": tool_call_id}
    with domain_engine().begin() as db:
        if db.execute(
            text(
                "UPDATE por_domain.tool_calls SET status=:status "
                "WHERE run_id=:r AND id=:id AND status='admitted'"
            ),
            dict(keys, status=status),
        ).rowcount:
            return True
        return bool(
            db.scalar(
                text("SELECT 1 FROM por_domain.tool_calls WHERE run_id=:r AND id=:id"),
                keys,
            )
        )


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
    """Serialize one external commit against application cancellation."""
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
