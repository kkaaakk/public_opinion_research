"""Run the agent-as-tool experiment against a real local DBOS system database."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from agents import FunctionTool
from agents.exceptions import UserError
from agents.tool_context import ToolContext
from dbos import DBOS, DBOSClient, SetWorkflowID
from dbos._error import DBOSAwaitedWorkflowCancelledError
from dbos_openai_agents import DBOSRunner
from manager_agent_sdk import (
    WAIT_SECONDS,
    Hooks,
    OrdinaryFailure,
    Probe,
    build_manager,
    business_call,
    configure_offline,
    observe_exit,
    run_config,
    setup_pair,
)
from sqlalchemy.engine import URL, make_url

APP = "manager-agent-settlement-lab"
VERSION = "manager-agent-settlement-v1"
CRASH_ID = "manager-crash-and-recover"
ABRUPT_EXIT_CODE = 73
WORKER_TIMEOUT = 45
POSTGRES_ENV = "MANAGER_AGENT_LAB_POSTGRES_URL"
PROBE: Probe


def database(directory: Path, purpose: str) -> str:
    if not (dsn := os.environ.get(POSTGRES_ENV)):
        return URL.create(
            "sqlite", database=str(directory / f"{purpose}.sqlite")
        ).render_as_string()
    url = make_url(dsn)
    if (
        url.drivername != "postgresql+psycopg"
        or url.host != "127.0.0.1"
        or url.database != "por_manager_sdk_lab"
        or url.query
    ):
        raise ValueError(
            f"{POSTGRES_ENV} must point to the dedicated loopback lab database"
        )
    return dsn


def por_serializer():
    from open_deep_research.web.run_data import RunJSON

    return RunJSON()


def boot(dsn: str, schema: str, *, default_serializer=False) -> None:
    config = {
        "name": APP,
        "application_version": VERSION,
        "system_database_url": dsn,
        "dbos_system_schema": schema,
        "enable_otlp": False,
        "use_listen_notify": False,
        "console_log_level": "CRITICAL",
    }
    if not default_serializer:
        config["serializer"] = por_serializer()
    DBOS(config=config)
    DBOS.launch()


def durable_agent_tool(tool: FunctionTool, *, preemptible: bool) -> FunctionTool:
    original = tool.on_invoke_tool
    step_name = f"lab.{tool.name}"

    async def invoke(context: ToolContext, arguments: str):
        async def body():
            PROBE.emit("step-start", context.tool_call_id, step=DBOS.step_id)
            try:
                return await original(context, arguments)
            finally:
                PROBE.emit("step-finally", context.tool_call_id, step=DBOS.step_id)

        # DBOS persists the string result. ToolContext and the callback stay in memory.
        return await DBOS.run_step_async(
            {"name": step_name, "preemptible": preemptible}, body
        )

    return replace(tool, on_invoke_tool=invoke)


@DBOS.workflow(name="lab.manager")
async def manager_workflow(preemptible: bool) -> dict:
    manager = build_manager(PROBE, child_config=run_config())
    manager.tools = [
        durable_agent_tool(tool, preemptible=preemptible) for tool in manager.tools
    ]
    result = await DBOSRunner.run(
        manager, "manager", context=PROBE, hooks=Hooks(PROBE), run_config=run_config()
    )
    return {
        "output": result.final_output,
        "sdk_requests": result.context_wrapper.usage.requests,
    }


def start(workflow_id: str, *, preemptible: bool) -> asyncio.Task:
    with SetWorkflowID(workflow_id):
        return asyncio.create_task(manager_workflow(preemptible))


def journal_rows(rows: list[dict]) -> list[dict]:
    return [
        dict(
            id=row["function_id"],
            name=row["function_name"],
            error=row["error"] is not None,
        )
        for row in rows
    ]


async def live_trial(mode: str, trace: Path | None) -> None:
    from open_deep_research.workflow.execution import (
        ExecutionControlError,
        is_execution_control_failure,
        unconfirmed,
    )

    global PROBE
    probe = PROBE = Probe(mode, trace)
    raises = mode in {"exception", "control-error", "default-serializer-error"}
    setup_pair(probe, failure=raises)
    if mode == "control-error":
        probe.failures["public-tool"] = unconfirmed
    if raises or mode in {"python-cancel", "dbos-preemptible"}:
        probe.held_tool_cleanup.add("knowledge-tool")
    if mode in {"python-cancel", "dbos-preemptible"}:
        probe.held_tool_cleanup.add("public-tool")
    task = start(mode, preemptible=mode == "dbos-preemptible")
    try:
        await probe.wait("tool-start", "public-tool")
        await probe.wait("tool-start", "knowledge-tool")
        assert probe.snapshot("overlap")["tool"] == ["knowledge-tool", "public-tool"]
        assert all(
            row["step"] is not None
            for row in probe.events
            if row["kind"] == "tool-start"
        )
        if mode == "normal":
            probe.release("knowledge-tool")
            await probe.wait("tool-hook-end", "call-knowledge")
            assert not task.done() and probe.pending()["tool"] == ["public-tool"]
            probe.release("public-tool")
            assert await observe_exit(probe, task) == {
                "output": "done:manager",
                "sdk_requests": 6,
            }
        elif raises:
            probe.release("public-tool")
            if mode == "default-serializer-error":
                error = await observe_exit(probe, task, AttributeError)
                assert "Can't pickle local object" in str(error)
            else:
                error = await observe_exit(probe, task, UserError)
                if mode == "control-error":
                    assert isinstance(error.__cause__, ExecutionControlError)
                    assert (
                        error.__cause__.unknown_outcome
                        and is_execution_control_failure(error)
                    )
                else:
                    assert isinstance(error.__cause__, OrdinaryFailure)
            await probe.wait("tool-finally", "knowledge-tool")
            assert probe.pending()["tool"] == ["knowledge-tool"]
            assert probe.seen("step-finally", "call-public")
        elif mode == "python-cancel":
            task.cancel()
            await observe_exit(probe, task, asyncio.CancelledError)
            await probe.wait("step-finally", "call-public")
            await probe.wait("step-finally", "call-knowledge")
            assert probe.snapshot("step-finally-seen")["tool"] == [
                "knowledge-tool",
                "public-tool",
            ]
        else:
            await DBOS.cancel_workflow_async(mode)
            assert (await DBOS.get_workflow_status_async(mode)).status == "CANCELLED"
            if mode == "dbos-nonpreemptible":
                assert not task.done() and not any(
                    probe.seen("tool-cancel", name)
                    for name in ("public-tool", "knowledge-tool")
                )
                probe.snapshot("native-cancelled-while-body-running")
                probe.release("public-tool")
                probe.release("knowledge-tool")
            await observe_exit(probe, task, DBOSAwaitedWorkflowCancelledError)
            for name in ("public-tool", "knowledge-tool"):
                await probe.wait("tool-finally", name)
                assert probe.seen("tool-cancel", name) == (mode == "dbos-preemptible")
            if mode == "dbos-preemptible":
                assert probe.pending()["tool"] == ["knowledge-tool", "public-tool"]
        for call in ("call-public", "call-knowledge"):
            await probe.wait("step-finally", call)
        await probe.finish()
        status = (await DBOS.get_workflow_status_async(mode)).status
        native_steps = await DBOS.list_workflow_steps_async(mode)
        steps = journal_rows(native_steps)
        assert (
            status
            == {
                "normal": "SUCCESS",
                "exception": "ERROR",
                "control-error": "ERROR",
                "python-cancel": "PENDING",
                "dbos-preemptible": "CANCELLED",
                "dbos-nonpreemptible": "CANCELLED",
                "default-serializer-error": "ERROR",
            }[mode]
        ), (mode, status)
        child_rows = [row for row in steps if row["name"] != "_model_call_step"]
        if mode == "normal":
            assert len(steps) == 4
            assert [(row["id"], row["name"], row["error"]) for row in child_rows] == [
                (2, "lab.public_agent", False),
                (3, "lab.knowledge_agent", False),
            ]
        elif mode in {"exception", "control-error"}:
            assert child_rows == [{"id": 2, "name": "lab.public_agent", "error": True}]
            saved_error = next(
                row["error"] for row in native_steps if row["function_id"] == 2
            )
            assert type(saved_error) is RuntimeError and str(saved_error) == "UserError"
            assert not is_execution_control_failure(saved_error)
            probe.emit(
                "journal-error",
                mode,
                error=type(saved_error).__name__,
                original_type=str(saved_error),
                unknown_outcome=getattr(saved_error, "unknown_outcome", None),
            )
        else:
            assert not child_rows
        probe.emit("journal", mode, status=status, steps=steps)
        print(f"PASS 10-{mode} status={status} steps={steps}", flush=True)
    finally:
        await probe.finish()
        # Raw Python cancellation leaves a pending lab journal. Close it before any restart.
        if (
            status := await DBOS.get_workflow_status_async(mode)
        ) and status.status == "PENDING":
            await DBOS.cancel_workflow_async(mode)


async def live_trials(trace: Path | None) -> None:
    for mode in (
        "normal",
        "exception",
        "control-error",
        "python-cancel",
        "dbos-preemptible",
        "dbos-nonpreemptible",
    ):
        await live_trial(mode, trace)


async def crash_worker(directory: Path, phase: str, schema: str) -> None:
    global PROBE
    probe = PROBE = Probe(phase, directory / "worker.jsonl")
    setup_pair(probe)
    # An actual reissued model call can produce new inner call IDs for the same work.
    probe.program["public"][0].insert(
        0, business_call("normal", "public-before-crash", phase)
    )
    if phase == "after-crash":
        probe.release("public-tool")
        probe.release("knowledge-tool")
    boot(database(directory, "crash"), schema)
    if phase == "before-crash":
        task = start(CRASH_ID, preemptible=False)
        await probe.wait("tool-start", "public-tool")
        await probe.wait("tool-start", "knowledge-tool")
        assert probe.snapshot("overlap")["tool"] == ["knowledge-tool", "public-tool"]
        await probe.wait("tool-exit", "public-before-crash")
        probe.release("knowledge-tool")
        await probe.wait("tool-hook-end", "call-knowledge")
        (directory / "ready").touch()
        while not (directory / "abort").exists():
            assert not task.done(), "The Public Step ended before the abort barrier"
            await asyncio.sleep(0.05)
        # Run in the actual interpreter, including when Windows uses a venv redirector.
        # os._exit bypasses Python finally blocks, DBOS.destroy and asyncio shutdown.
        probe.emit("process-abort", CRASH_ID, code=ABRUPT_EXIT_CODE)
        os._exit(ABRUPT_EXIT_CODE)
    handle = await DBOS.retrieve_workflow_async(CRASH_ID)
    result = await asyncio.wait_for(handle.get_result(), WORKER_TIMEOUT - 5)
    await probe.finish()
    probe.emit("recovered", CRASH_ID, result=result)


def spawn(directory: Path, schema: str, phase: str) -> subprocess.Popen:
    with (directory / f"{phase}.log").open("w", encoding="utf-8") as output:
        return subprocess.Popen(
            [
                sys.executable,
                "-u",
                str(Path(__file__).resolve()),
                "--worker",
                phase,
                "--directory",
                str(directory),
                "--schema",
                schema,
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
        )


def wait_ready(directory: Path, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + WORKER_TIMEOUT
    while not (directory / "ready").exists():
        if process.poll() is not None or time.monotonic() >= deadline:
            detail = (directory / "before-crash.log").read_text(encoding="utf-8")
            raise AssertionError(
                f"Crash worker did not reach the journal barrier. {detail}"
            )
        time.sleep(0.05)


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(WAIT_SECONDS)


def crash_recovery(directory: Path, schema: str, trace: Path | None) -> None:
    first = spawn(directory, schema, "before-crash")
    client = None
    try:
        wait_ready(directory, first)
        client = DBOSClient(
            system_database_url=database(directory, "crash"),
            dbos_system_schema=schema,
            serializer=por_serializer(),
        )
        handle = client.retrieve_workflow(CRASH_ID)
        before = client.list_workflow_steps(CRASH_ID)
        assert handle.get_status().status == "PENDING"
        assert journal_rows(before) == [
            {"id": 1, "name": "_model_call_step", "error": False},
            {"id": 3, "name": "lab.knowledge_agent", "error": False},
        ]
        (directory / "abort").touch()
        assert first.wait(WAIT_SECONDS) == ABRUPT_EXIT_CODE
        assert handle.get_status().status == "PENDING"
        second = spawn(directory, schema, "after-crash")
        try:
            if second.wait(WORKER_TIMEOUT) != 0:
                raise AssertionError(
                    (directory / "after-crash.log").read_text(encoding="utf-8")
                )
        finally:
            stop(second)
        after = client.list_workflow_steps(CRASH_ID)
        assert handle.get_status().status == "SUCCESS"
        assert [row for row in after if row["function_id"] in (1, 3)] == before
        assert journal_rows(after) == [
            {"id": 1, "name": "_model_call_step", "error": False},
            {"id": 2, "name": "lab.public_agent", "error": False},
            {"id": 3, "name": "lab.knowledge_agent", "error": False},
            {"id": 4, "name": "_model_call_step", "error": False},
        ]
        # The cached Knowledge result is present in Manager history, but its old Usage mutation
        # and child callbacks were never replayed into the fresh process.
        assert handle.get_result() == {"output": "done:manager", "sdk_requests": 4}
        events = [
            json.loads(line)
            for line in (directory / "worker.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]

        def entries(phase, kind, name):
            return [
                event
                for event in events
                if (event["case"], event["kind"], event["name"]) == (phase, kind, name)
            ]

        before_pids = {
            event["pid"] for event in events if event["case"] == "before-crash"
        }
        after_pids = {
            event["pid"] for event in events if event["case"] == "after-crash"
        }
        assert len(before_pids) == len(after_pids) == 1 and before_pids.isdisjoint(
            after_pids
        )
        assert not entries("before-crash", "tool-finally", "public-tool")
        assert not entries("before-crash", "step-finally", "call-public")
        assert not entries("after-crash", "step-start", "call-knowledge")
        assert not entries("after-crash", "model", "knowledge")
        assert entries("after-crash", "tool-hook-end", "call-knowledge")
        assert len([event for event in events if event["kind"] == "model"]) == 7
        assert len(entries("after-crash", "model", "manager")) == 1
        assert len(entries("after-crash", "model", "public")) == 2
        for phase in ("before-crash", "after-crash"):
            assert len(entries(phase, "tool-exit", "public-before-crash")) == 1
        assert [
            entries(phase, "tool-start", "public-before-crash")[0]["native_id"]
            for phase in ("before-crash", "after-crash")
        ] == ["before-crash", "after-crash"]
        manager = entries("after-crash", "model", "manager")[0]
        assert {item["call_id"]: item["output"] for item in manager["outputs"]} == {
            "call-public": "done:public",
            "call-knowledge": "done:knowledge",
        }
        result_event = dict(
            case="crash-recovery",
            kind="journal",
            before=journal_rows(before),
            after=journal_rows(after),
            result=handle.get_result(),
            status=handle.get_status().status,
        )
        if trace is not None:
            with trace.open("a", encoding="utf-8") as output:
                for event in [*events, result_event]:
                    output.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(f"PASS 10-crash-recovery {result_event}", flush=True)
    finally:
        if first.poll() is None and (directory / "ready").exists():
            (directory / "abort").touch()
            try:
                first.wait(WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        stop(first)
        if client is not None:
            client.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, help="append a JSON line per observation")
    parser.add_argument(
        "--worker", choices=("before-crash", "after-crash"), help=argparse.SUPPRESS
    )
    parser.add_argument("--directory", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--schema", help=argparse.SUPPRESS)
    args = parser.parse_args()
    configure_offline()
    if args.worker:
        try:
            asyncio.run(crash_worker(args.directory, args.worker, args.schema))
        finally:
            DBOS.destroy()
        return

    with TemporaryDirectory(prefix="por-manager-sdk-") as temporary:
        directory = Path(temporary).resolve()
        schema_prefix = f"manager_lab_{uuid4().hex[:12]}"
        boot(database(directory, "live"), f"{schema_prefix}_live")
        try:
            asyncio.run(live_trials(args.trace))
        finally:
            DBOS.destroy()
        crash_recovery(directory, f"{schema_prefix}_crash", args.trace)
        boot(
            database(directory, "default"),
            f"{schema_prefix}_default",
            default_serializer=True,
        )
        try:
            asyncio.run(live_trial("default-serializer-error", args.trace))
        finally:
            DBOS.destroy()
    print("8 DBOS observations passed", flush=True)


if __name__ == "__main__":
    main()
