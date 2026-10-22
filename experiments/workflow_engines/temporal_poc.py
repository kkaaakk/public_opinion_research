"""Run unchanged SDK/Harness business work as Temporal Activities."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import common
from temporal_workflow import ResearchRun
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker


async def heartbeats():
    while True:
        activity.heartbeat()
        await asyncio.sleep(0.25)


@activity.defn(name="phase4_segment")
async def business_boundary(value: dict) -> dict:
    beat = asyncio.create_task(heartbeats())
    common.record(
        value["directory"],
        "activity_attempt",
        {"cursor": value["cursor"], "attempt": activity.info().attempt},
    )
    try:
        return await common.segment(value)
    finally:
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)


@activity.defn(name="phase4_gate")
async def restart_gate(directory: str) -> None:
    beat = asyncio.create_task(heartbeats())
    try:
        await common.fault_gate(directory, "checkpoint")
    finally:
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)


async def main(args):
    client = await Client.connect(args.address)
    handle = client.get_workflow_handle(args.run_id)
    if args.mode == "cancel":
        await handle.cancel()
        return
    if args.mode == "approve":
        version = await handle.query(ResearchRun.pending_plan)
        assert version, "no durable approval is pending"
        assert await handle.execute_update(ResearchRun.approve, version) == "approved"
        return
    async with Worker(
        client,
        task_queue=args.run_id,
        workflows=[ResearchRun],
        activities=[business_boundary, restart_gate],
        max_concurrent_activities=2,
    ):
        if args.mode == "start":
            value = common.packet(
                args.directory,
                args.run_id,
                scenario=args.scenario,
                guarded=not args.unguarded,
                graph=args.graph,
            )
            value["require_approval"] = args.approval
            handle = await client.start_workflow(
                ResearchRun.run, value, id=args.run_id, task_queue=args.run_id
            )
        result = await handle.result()
        (args.directory / "result.json").write_text(
            json.dumps(result), encoding="utf-8"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["start", "recover", "approve", "cancel"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--address", default="127.0.0.1:17233")
    parser.add_argument("--run-id", default="poc-run")
    parser.add_argument("--scenario", default="normal")
    parser.add_argument("--unguarded", action="store_true")
    parser.add_argument("--approval", action="store_true")
    parser.add_argument("--graph", choices=["memory", "neo4j"], default="memory")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    asyncio.run(main(args))
