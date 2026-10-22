"""Run the DBOS POC against a private SQLite journal, never production entries."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import common
from agents import Agent
from agents.testing import ScriptedModel, assistant_message
from dbos import DBOS, DBOSClient, DBOSPortableJSONSerializer, SetWorkflowID
from dbos_openai_agents import DBOSRunner


@DBOS.step(preemptible=True)
async def business_boundary(value):
    return await common.segment(value)


@DBOS.step(preemptible=True)
async def restart_gate(directory):
    await common.fault_gate(directory, "checkpoint")


@DBOS.workflow()
async def research_run(value):
    while value["cursor"] not in {"done", "interrupted", "cancelled"}:
        value = await business_boundary(value)
        if value["cursor"] == "approval":
            await DBOS.set_event_async("plan", {"version": value["plan_version"]})
            if value.get("require_approval"):
                decision = await DBOS.recv_async("approval", timeout_seconds=120)
                if decision != {
                    "version": value["plan_version"],
                    "decision": "approve",
                }:
                    return {
                        **value,
                        "cursor": "cancelled",
                        "error": "invalid or expired approval",
                    }
            value = {**value, "cursor": "research_init"}
        if value["cursor"] == "research_review":
            # Executing this step proves the previous boundary was already
            # checkpointed by DBOS before the fault is injected.
            await restart_gate(value["directory"])
    return value


@DBOS.workflow()
async def bare_native_probe():
    model = ScriptedModel([[assistant_message("offline DBOSRunner response")]])
    result = await DBOSRunner.run(Agent(name="probe", model=model), "offline input")
    return result.final_output


@DBOS.workflow()
async def harness_native_probe(directory):
    from open_deep_research.runtime import sdk_context
    from open_deep_research.workflow.python_workflow import PythonWorkflow

    patch, state, cfg, *_ = common.install(
        directory, "normal", "memory", "native-probe"
    )
    patch.setattr(sdk_context, "Runner", DBOSRunner)
    try:
        await PythonWorkflow().ainvoke(state, cfg, start_at="research")
    except Exception as exc:
        # Preserve the actual failure, including the business wrapper's causes.
        details = [f"{type(exc).__name__}: {exc}"]
        for failure in getattr(exc, "failures", {}).values():
            details.append(f"{type(failure).__name__}: {failure}")
        return {"compatible": False, "failure": details}
    finally:
        patch.undo()
    return {"compatible": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=["start", "recover", "approve", "cancel", "probe"]
    )
    parser.add_argument("directory", type=Path)
    parser.add_argument("--run-id", default="poc-run")
    parser.add_argument("--scenario", default="normal")
    parser.add_argument("--unguarded", action="store_true")
    parser.add_argument("--approval", action="store_true")
    parser.add_argument("--graph", choices=["memory", "neo4j"], default="memory")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{(args.directory / 'dbos.sqlite').resolve().as_posix()}"
    if args.mode in {"approve", "cancel"}:
        client = DBOSClient(system_database_url=url, application_name="por-engine-poc")
        if args.mode == "cancel":
            client.cancel_workflow(args.run_id)
        else:
            plan = json.loads(
                (args.directory / "plan.json").read_text(encoding="utf-8")
            )
            client.send(
                args.run_id,
                {"version": plan["version"], "decision": "approve"},
                topic="approval",
                idempotency_key="poc-approval",
            )
        return
    config = dict(
        name="por-engine-poc",
        system_database_url=url,
        application_version="phase4-5ca71c12-eval-v1",
        executor_id="por-evaluation",
        enable_otlp=False,
        log_level="WARN",
    )
    if args.mode != "probe":
        config["serializer"] = DBOSPortableJSONSerializer()
    DBOS(config=config)
    DBOS.launch()
    DBOS.register_queue("research", global_concurrency=2, worker_concurrency=2)
    try:
        if args.mode == "probe":

            async def probes():
                return {
                    "bare": await bare_native_probe(),
                    "harness": await harness_native_probe(str(args.directory)),
                }

            output = asyncio.run(probes())
        elif args.mode == "start":
            value = common.packet(
                args.directory,
                args.run_id,
                scenario=args.scenario,
                guarded=not args.unguarded,
                graph=args.graph,
            )
            value["require_approval"] = args.approval
            with SetWorkflowID(args.run_id):
                handle = DBOS.enqueue_workflow("research", research_run, value)
            output = handle.get_result()
        else:
            # A process with the same version and executor ID performs genuine
            # startup recovery; no private DBOS recovery method is called.
            output = DBOS.retrieve_workflow(args.run_id).get_result()
        (args.directory / "result.json").write_text(
            json.dumps(output), encoding="utf-8"
        )
    finally:
        DBOS.destroy()


if __name__ == "__main__":
    main()
