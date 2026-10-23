"""Initialize and run the native DBOS executor; no task claims or leases."""

from __future__ import annotations

import argparse
import os
import signal
import time

from dbos import DBOS

from open_deep_research.web.run_data import RunJSON
from open_deep_research.workflow import dbos_workflow
from open_deep_research.workflow.dbos_config import (
    APP,
    ARCHIVE,
    BUSINESS,
    CONTROL,
    RECOVERY,
    VERSION,
    settings,
)
from open_deep_research.workflow.run_facts import facts


def launch(executor_id, *, queues=None):
    assert callable(dbos_workflow.run_workflow)
    cfg = settings()
    facts().verify_policy()
    DBOS(
        config={
            "name": APP,
            "system_database_url": cfg.url,
            "dbos_system_schema": "por_dbos",
            "application_version": VERSION,
            "executor_id": executor_id,
            "serializer": RunJSON(),
            "enable_otlp": False,
            "log_level": "WARN",
            "db_engine_kwargs": {
                "pool_timeout": 3,
                "connect_args": {"connect_timeout": 3},
            },
        }
    )
    DBOS.listen_queues(queues or [CONTROL, BUSINESS, ARCHIVE, RECOVERY])
    DBOS.launch()
    DBOS.register_queue(CONTROL, polling_interval_sec=0.2)
    DBOS.register_queue(
        BUSINESS,
        global_concurrency=cfg.max_active,
        partition_concurrency=cfg.owner_active,
        polling_interval_sec=0.2,
    )
    DBOS.register_queue(ARCHIVE, polling_interval_sec=0.2)
    DBOS.register_queue(
        RECOVERY, worker_concurrency=cfg.max_active, polling_interval_sec=0.2
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initialize-only", action="store_true")
    parser.add_argument("--executor-id", default=os.environ.get("DBOS_EXECUTOR_ID"))
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--reconcile-run")
    parser.add_argument("--owner", default="operator")
    parser.add_argument("--state-version", type=int)
    parser.add_argument("--worker-stopped", action="store_true")
    parser.add_argument("--note", default="")
    args = parser.parse_args()
    if args.initialize_only:
        DBOS.migrate(settings().url, schema="por_dbos")
        facts().initialize()
        facts().close()
        return
    if args.health:
        from sqlalchemy import text

        with facts().engine.connect() as db:
            db.execute(text("SELECT 1"))
        facts().client.list_workflows(limit=1, load_input=False, load_output=False)
        facts().close()
        return
    if args.reconcile_run:
        from open_deep_research.web.durable import get_run_client

        get_run_client().reconcile(
            args.reconcile_run,
            args.owner,
            args.state_version,
            worker_stopped=args.worker_stopped,
            note=args.note,
        )
        facts().close()
        return
    if not args.executor_id:
        parser.error(
            "--executor-id is required; reuse an ID only after the old process is dead"
        )
    launch(args.executor_id)
    stop = False

    def shutdown(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        while not stop:
            time.sleep(0.2)
    finally:
        DBOS.destroy()
        facts().close()


if __name__ == "__main__":
    main()
