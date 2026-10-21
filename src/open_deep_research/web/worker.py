"""Run database-delivered attempts with the existing Python Workflow.

The queue schedules whole business runs; PythonWorkflow alone decides business
order. No HTTP subscriber owns execution, cancellation or archive compensation.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from agents import SQLiteSession
from sqlalchemy import select

from open_deep_research.configuration import Configuration
from open_deep_research.runtime.sdk_runtime import SDK_RUNTIME_VERSION
from open_deep_research.web.history import (
    HistoryStage,
    close_history_store,
    get_history_store,
    history_record,
)
from open_deep_research.web.run_store import (
    SDK_VERSION,
    LeaseLost,
    RunConflict,
    RunStore,
    digest,
    timestamp,
)
from open_deep_research.workflow.execution import execution_observer
from open_deep_research.workflow.python_workflow import (
    WORKFLOW_VERSION,
    PlanApprovalRequired,
    PythonWorkflow,
    WorkflowExecutionError,
)

LOGGER = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_run_store() -> RunStore:
    """Read operator-only deployment settings; never accept a database URL in HTTP."""
    url = os.environ.get("RUN_DATABASE_URL", "")
    if not url:
        raise RunConflict("RUN_DATABASE_URL is required to enable durable Runs")
    return RunStore(
        url,
        max_active=int(os.environ.get("RUN_MAX_ACTIVE", "4")),
        owner_active=int(os.environ.get("RUN_OWNER_ACTIVE", "2")),
        queue_capacity=int(os.environ.get("RUN_QUEUE_CAPACITY", "1000")),
        lease_seconds=float(os.environ.get("RUN_LEASE_SECONDS", "60")),
        approval_seconds=float(os.environ.get("RUN_APPROVAL_SECONDS", "86400")),
    )


def close_run_store() -> None:
    """Release a lazily initialized process-local pool."""
    if get_run_store.cache_info().currsize:
        get_run_store().close()
    get_run_store.cache_clear()


class RunWorker:
    """Own claimed tasks, lease renewal, cancellation and archive projections."""

    def __init__(
        self,
        store: RunStore,
        *,
        concurrency: int = 4,
        timeout: float = 3600,
        poll_seconds: float = 1,
        session_path: str | None = None,
    ) -> None:
        """Bound local resources in addition to database-enforced global slots."""
        if concurrency < 1 or timeout <= 0 or poll_seconds <= 0:
            raise ValueError("Worker limits must be positive")
        self.store = store
        self.concurrency = concurrency
        self.timeout = timeout
        self.poll_seconds = poll_seconds
        self.session_path = session_path
        self.worker_id = f"{os.getpid()}-{uuid4().hex}"
        self.stopping = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()

    async def execute(self, run: dict) -> None:
        """Consume one fenced delivery without retrying business work on failure."""
        run_id, attempt_id = run["run_id"], run["attempt_id"]
        sessions = []

        async def observer(kind, **facts):
            if kind == "sdk_session":
                await asyncio.to_thread(
                    self.store.progress, run_id, attempt_id, "agent_started", **facts
                )
                if self.session_path is None:
                    return None
                # Session is private to one role invocation, never a new role's
                # memory source or a Workflow/side-effect recovery mechanism.
                session = await asyncio.to_thread(
                    SQLiteSession,
                    f"{run['owner']}:{run_id}:{attempt_id}:{facts['role']}:{facts['invocation_id']}",
                    self.session_path,
                )
                sessions.append(session)
                return session
            if kind in {"tool_started", "tool_finished"}:
                return await asyncio.to_thread(
                    self.store.operation,
                    run_id,
                    attempt_id,
                    facts["identity"],
                    status="started" if kind == "tool_started" else facts["status"],
                )
            return await asyncio.to_thread(
                self.store.progress, run_id, attempt_id, kind, **facts
            )

        async def workflow():
            token = execution_observer.set(observer)
            try:
                effective = Configuration.from_runnable_config(
                    run["data"]["config"]
                ).model_dump(mode="json")
                changed = [
                    name
                    for name, value in effective.items()
                    if digest(value)
                    != run["data"]["configuration_fingerprints"].get(name)
                ]
                if (
                    run["data"]["workflow_version"] != WORKFLOW_VERSION
                    or run["data"]["sdk_version"] != SDK_VERSION
                    or run["data"]["runtime_version"] != SDK_RUNTIME_VERSION
                    or version("openai-agents") != SDK_VERSION
                    or digest(effective) != run["data"]["environment_fingerprint"]
                ):
                    error = RunConflict(
                        "Accepted runtime version is unavailable; drain with the original worker"
                    )
                    error.configuration_changed = changed
                    raise error
                return await PythonWorkflow().ainvoke(
                    run["data"]["state"],
                    run["data"]["config"],
                    resume=dict(version=WORKFLOW_VERSION, cursor=run["data"]["cursor"]),
                )
            finally:
                execution_observer.reset(token)

        task = asyncio.create_task(workflow(), name=f"run-{run_id}")
        timed_out = False
        ownership_lost = False
        cancel_sent = False
        started = time.monotonic()
        interval = min(10.0, self.store.policy["lease_seconds"] / 3)
        try:
            while not task.done():
                try:
                    await asyncio.wait({task}, timeout=interval)
                    if task.done():
                        break
                    status = await asyncio.to_thread(
                        self.store.heartbeat, run_id, attempt_id
                    )
                except Exception:
                    # A database outage cannot authorize more work. Intent and
                    # checkpoint writes fail closed; the expired row is audited
                    # on restart, never silently delivered to another Agent.
                    ownership_lost = True
                    status = "cancelling"
                timed_out = timed_out or time.monotonic() - started >= self.timeout
                if (
                    status == "cancelling" or timed_out or self.stopping.is_set()
                ) and not cancel_sent:
                    task.cancel()
                    cancel_sent = True
            result = await task
        except PlanApprovalRequired:
            return
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError) and not task.done():
                task.cancel()
                # Keep a single cancellation signal while the original SDK
                # performs its bounded exit settlement.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                try:
                    task.result()
                except BaseException as inner:
                    exc = inner
            state = getattr(
                exc, "workflow_state", getattr(exc, "state", run["data"]["state"])
            )
            failures = (
                exc.failures
                if isinstance(exc, WorkflowExecutionError)
                else getattr(exc, "workflow_failures", {"worker": exc})
            )
            settlements = [
                getattr(failure, "context_settlement", None)
                for failure in failures.values()
            ]
            settled = all(item is None or item.succeeded for item in settlements)
            kinds = getattr(exc, "kinds", {})
            error = dict(
                kind="timeout"
                if timed_out
                else "cancelled"
                if cancel_sent
                else "workflow_failed",
                steps=list(failures),
                kinds=kinds,
                usage_complete=settled,
            )
            if hasattr(exc, "configuration_changed"):
                error.update(
                    kind="configuration_changed", fields=exc.configuration_changed
                )
            if not ownership_lost:
                try:
                    unresolved = await asyncio.to_thread(
                        self._unresolved_operations, run_id, attempt_id
                    )
                    settled = settled and not unresolved
                    error["usage_complete"] = settled
                    await asyncio.to_thread(
                        self.store.finish,
                        run_id,
                        attempt_id,
                        "failed" if timed_out or not cancel_sent else "cancelled",
                        state,
                        error=error,
                        settled=settled,
                    )
                except Exception:
                    LOGGER.warning(
                        "Run settlement awaits durable reconciliation: %s", run_id
                    )
            return
        else:
            try:
                settled = not await asyncio.to_thread(
                    self._unresolved_operations, run_id, attempt_id
                )
                await asyncio.to_thread(
                    self.store.finish,
                    run_id,
                    attempt_id,
                    "completed" if result.get("report", {}).get("final") else "failed",
                    result,
                    error=None
                    if result.get("report", {}).get("final")
                    else {"kind": "no_report"},
                    settled=settled,
                )
            except LeaseLost:
                LOGGER.warning("Discarded stale completion: %s", run_id)
            except Exception:
                LOGGER.warning(
                    "Run completion awaits durable reconciliation: %s", run_id
                )
        finally:
            for session in sessions:
                try:
                    await asyncio.to_thread(session.close)
                except Exception:
                    LOGGER.warning("SDK Session cleanup failed for Run %s", run_id)

    def _unresolved_operations(self, run_id: str, attempt_id: str) -> bool:
        with self.store.engine.connect() as db:
            return (
                db.execute(
                    select(self.store.operations.c.operation_id)
                    .where(
                        self.store.operations.c.run_id == run_id,
                        self.store.operations.c.attempt_id == attempt_id,
                        self.store.operations.c.status.in_(["started", "uncertain"]),
                    )
                    .limit(1)
                ).first()
                is not None
            )

    async def archive(self) -> None:
        """Retry the idempotent Neo4j projection without re-executing research."""
        for run in await asyncio.to_thread(self.store.archive_candidates):
            data = run["data"]
            record = history_record(
                run_id=run["run_id"],
                request=data["request"],
                report=data["state"].get("report", {}).get("final", ""),
                usage=data["usage"],
                started_at=timestamp(run["created_at"]),
                completed_at=data["terminated_at"],
                source="python_worker",
                status=run["status"]
                if run["status"] in {"completed", "failed", "cancelled"}
                else "incomplete",
                role_reports=[
                    HistoryStage.model_validate(stage) for stage in data["stages"]
                ],
                error_message="Research requires review." if data["error"] else "",
                failed_stage=data["active_step"],
            )
            record.owner = run["owner"]
            record.research_session_id = run["session_id"]
            record.run_state_version = run["version"]
            try:
                await asyncio.to_thread(lambda: get_history_store().save([record]))
                await asyncio.to_thread(
                    self.store.archive_result, run["run_id"], run["version"], record.id
                )
            except Exception:
                await asyncio.to_thread(
                    self.store.archive_result, run["run_id"], run["version"], None
                )

    async def tick(self) -> None:
        """Reconcile leases and claim only as many tasks as this process can own."""
        await asyncio.to_thread(self.store.reconcile)
        for task in [task for task in self.tasks if task.done()]:
            self.tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                LOGGER.warning(
                    "Worker task failed outside its durable execution boundary"
                )
        while len(self.tasks) < self.concurrency and not self.stopping.is_set():
            run = await asyncio.to_thread(self.store.claim, self.worker_id)
            if run is None:
                break
            self.tasks.add(asyncio.create_task(self.execute(run)))

    async def serve(self) -> None:
        """Poll durable work and drain children on an explicit process shutdown."""
        try:
            while not self.stopping.is_set():
                try:
                    await self.tick()
                    await self.archive()
                except Exception:
                    LOGGER.warning(
                        "Worker database/archive unavailable; retrying control operations"
                    )
                try:
                    await asyncio.wait_for(self.stopping.wait(), self.poll_seconds)
                except TimeoutError:
                    pass
        finally:
            self.stopping.set()
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)


def main() -> None:
    """Start a separate worker process using operator environment configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
    )
    parser.add_argument("--session-path", default=None)
    parser.add_argument("--initialize-only", action="store_true")
    parser.add_argument("--reconcile-run")
    parser.add_argument("--owner", default="operator")
    parser.add_argument("--state-version", type=int, default=0)
    parser.add_argument("--worker-stopped", action="store_true")
    parser.add_argument("--note", default="")
    args = parser.parse_args()
    # Like the Web entrypoint, only a deliberately started process reads .env.
    from dotenv import load_dotenv

    load_dotenv()
    if args.concurrency is None:
        args.concurrency = int(os.environ.get("RUN_WORKER_CONCURRENCY", "4"))
    if args.timeout is None:
        args.timeout = float(os.environ.get("RUN_TIMEOUT_SECONDS", "3600"))
    if args.session_path is None:
        args.session_path = os.environ.get("RUN_SDK_SESSION_PATH")
    logging.basicConfig(level=logging.INFO)
    store = get_run_store()
    if args.initialize_only or args.reconcile_run:
        try:
            if args.reconcile_run:
                store.release_quarantine(
                    args.reconcile_run,
                    args.owner,
                    args.state_version,
                    worker_stopped=args.worker_stopped,
                    note=args.note,
                )
        finally:
            close_run_store()
        return
    if args.session_path:
        Path(args.session_path).resolve().parent.mkdir(parents=True, exist_ok=True)

    async def run():
        worker = RunWorker(
            store,
            concurrency=args.concurrency,
            timeout=args.timeout,
            session_path=args.session_path,
        )
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(
                signum, lambda _s, _f: loop.call_soon_threadsafe(worker.stopping.set)
            )
        await worker.serve()

    try:
        asyncio.run(run())
    finally:
        from open_deep_research.research_graph.store import (
            clear_research_graph_store_cache,
        )

        clear_research_graph_store_cache()
        close_history_store()
        close_run_store()


if __name__ == "__main__":
    main()
