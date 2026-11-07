"""Native DBOS call boundaries, without a second execution journal.

Callbacks are live dependencies, not workflow inputs. DBOS persists their return
values. The surrounding business code is re-executed to rebuild Python state.
Never use this to put an Agent loop inside a Step.
"""

from __future__ import annotations

import asyncio
import builtins
from contextvars import ContextVar

from dbos import DBOS

durable_unit: ContextVar[str | None] = ContextVar("durable_unit", default=None)
physical_calls: ContextVar[dict | None] = ContextVar("physical_calls", default=None)
durable_generation: ContextVar[int] = ContextVar("durable_generation", default=0)
event_delivery: ContextVar[set | None] = ContextVar("event_delivery", default=None)
tool_usage: ContextVar[dict | None] = ContextVar("tool_usage", default=None)
cleanup_only: ContextVar[bool] = ContextVar("cleanup_only", default=False)
settling: ContextVar[bool] = ContextVar("settling", default=False)


def in_durable_workflow() -> bool:
    return durable_unit.get() is not None and DBOS.workflow_id is not None


async def checkpoint(name, call):
    """Checkpoint one bounded operation; never pretend nested Steps are durable."""
    if in_durable_workflow() and DBOS.step_id is None and not cleanup_only.get():
        before = set(event_delivery.get() or [])

        async def execute():
            value = await call()
            return {
                "value": value,
                "delivery_ids": sorted((event_delivery.get() or set()) - before),
            }

        saved = await DBOS.run_step_async({"name": name, "preemptible": True}, execute)
        delivery = event_delivery.get()
        if delivery is not None:
            delivery.update(saved["delivery_ids"])
        return saved["value"]
    return await call()


def checkpoint_sync(name, call):
    """Synchronous counterpart for existing owned Vision/RAG worker threads."""
    if in_durable_workflow() and DBOS.step_id is None and not cleanup_only.get():
        return DBOS.run_step({"name": name}, call)
    return call()


def begin_call_sync(name):
    return asyncio.run(begin_call(name)) if in_durable_workflow() else None


def end_call_sync(identity, **kwargs):
    if identity is not None:
        asyncio.run(end_call(identity, **kwargs))


def call_identity(name):
    """Identify physical attempts inside the current native Step, not Hooks."""
    counts = physical_calls.get()
    if counts is None:
        return None
    key = (DBOS.workflow_id, DBOS.step_id)
    counts[key] = counts.get(key, 0) + 1
    return dict(
        role="service",
        name=name,
        task_id=durable_unit.get(),
        research_round=1,
        tool_call_id=f"{key[0]}:{key[1]}:{counts[key]}:{durable_generation.get()}",
        arguments="",
        settlement=settling.get(),
    )


def is_cancellation(error):
    from dbos import error as dbos_error

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


async def begin_call(name):
    from open_deep_research.workflow.execution import observe_execution

    identity = call_identity(name)
    if identity is not None:
        await observe_execution("boundary_started", identity=identity)
    return identity


async def end_call(identity, *, usage=None, status="returned"):
    from open_deep_research.workflow.execution import observe_execution

    if identity is not None:
        await observe_execution(
            "boundary_finished", identity=identity, usage=usage, status=status
        )


class ReplayTranscript:
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

        await checkpoint("por.sdk_transcript", save)

    async def pop_item(self):
        raise RuntimeError("Durable invocations do not resume from SDK sessions")

    async def clear_session(self):
        self.items.clear()
