"""Own one native DBOS executor process; no custom worker scheduler."""

from __future__ import annotations

import env  # isort: skip
import argparse
import json
import os
import time
from pathlib import Path

import engine
from codec import NativeJSON
from dbos import DBOS, DBOSClient


def launch(executor):
    assert callable(engine.control_workflow), (
        "Native workflows must register before launch"
    )
    DBOS(
        config=dict(
            name=env.APP_NAME,
            system_database_url=env.DB_URL,
            application_version=env.VERSION,
            executor_id=executor,
            serializer=NativeJSON(),
            enable_otlp=False,
            log_level="WARN",
            db_engine_kwargs={"connect_args": {"connect_timeout": 2}},
        )
    )
    DBOS.listen_queues(
        os.environ.get("NATIVE_LISTEN_QUEUES", "control,business,recovery").split(",")
    )
    DBOS.launch()
    DBOS.register_queue("recovery", worker_concurrency=2, polling_interval_sec=0.1)
    DBOS.register_queue("control", worker_concurrency=32, polling_interval_sec=0.1)
    DBOS.register_queue(
        "business",
        global_concurrency=2,
        partition_concurrency=1,
        worker_concurrency=2,
        polling_interval_sec=0.1,
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["run", "recover", "serve"])
    p.add_argument("directory", type=Path)
    p.add_argument("--run-id", required=True)
    p.add_argument("--executor", default="native-local")
    p.add_argument("--scenario", default="normal")
    p.add_argument("--route", default="A")
    p.add_argument("--approval", action="store_true")
    p.add_argument("--approval-seconds", type=float, default=120)
    p.add_argument("--probes", action="store_true")
    a = p.parse_args()
    if a.route == "B":
        from runtime_ext import ExtensibleSDKRuntime

        assert callable(ExtensibleSDKRuntime)
    if a.probes:
        __import__("probe")
    a.directory.mkdir(parents=True, exist_ok=True)
    launch(a.executor)
    client = DBOSClient(
        system_database_url=env.DB_URL,
        application_name=env.APP_NAME,
        serializer=NativeJSON(),
    )
    try:
        if a.mode == "serve":
            (a.directory / "executor_ready").write_text(a.executor)
            while not (a.directory / "stop").exists():
                time.sleep(0.2)
            return
        if a.mode == "run":
            request = {
                "run_id": a.run_id,
                "owner": "alice",
                "topic": "brand recall",
                "directory": str(a.directory.resolve()),
                "scenario": a.scenario,
                "route": a.route,
                "approval": a.approval,
                "approval_seconds": a.approval_seconds,
            }
            handle = client.enqueue(
                {
                    "workflow_name": "native_control",
                    "queue_name": "control",
                    "workflow_id": a.run_id,
                    "app_version": env.VERSION,
                    "authenticated_user": "alice",
                },
                request,
            )
        else:
            handle = client.retrieve_workflow(a.run_id)
        try:
            result = handle.get_result()
        except Exception as exc:
            result = {"error": type(exc).__name__, "message": str(exc)}
        (a.directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
        status = client.retrieve_workflow(a.run_id).get_status()
        (a.directory / "status.txt").write_text(status.status)
    finally:
        client.destroy()
        DBOS.destroy()


if __name__ == "__main__":
    main()
