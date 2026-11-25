"""Offline protocol, config and source-boundary contracts for native execution."""

import ast
import asyncio
from inspect import unwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import HumanMessage

from open_deep_research.state import ResearchReview, ResearchTask
from open_deep_research.web.run_data import (
    RunJSON,
    dumps,
    legacy_projection,
    public_payload,
)
from open_deep_research.workflow import dbos_workflow
from open_deep_research.workflow.dbos_config import ExecutionSettings
from open_deep_research.workflow.execution import (
    execution_effect,
    owned_io,
    stage_context,
)


def test_terminal_relay_drains_prefix_and_delivers_one_done_per_generation(monkeypatch):
    async def run():
        def terminal(status, generation, *, close):
            return {
                "type": "_terminal",
                "generation": generation,
                "close": close,
                "frames": list(
                    dbos_workflow.terminal_frames(
                        {
                            "status": status,
                            "settlement": "settled",
                            "usage": {},
                            "error": {},
                        }
                    )
                ),
            }

        failed = terminal("failed", 0, close=False)
        success = terminal("completed", 1, close=True)
        recv = AsyncMock(
            side_effect=[
                {
                    "type": "run_status",
                    "generation": 0,
                    "status": "awaiting_approval",
                    "plan_version": "v1",
                },
                failed,
                {"type": "status", "generation": 0, "message": "first committed tail"},
                None,
                failed,
                {"type": "retry_started", "generation": 1},
                {"type": "status", "generation": 0, "message": "obsolete"},
                success,
                {"type": "status", "generation": 1, "message": "second committed tail"},
                None,
            ]
        )
        receipts, stream = [], []
        monkeypatch.setattr(dbos_workflow, "_interaction_now", lambda: 1)
        monkeypatch.setattr(dbos_workflow.DBOS, "recv_async", recv)
        monkeypatch.setattr(
            dbos_workflow.DBOS,
            "write_stream_async",
            AsyncMock(side_effect=lambda _key, frame: stream.append(frame)),
        )
        monkeypatch.setattr(
            dbos_workflow.DBOS,
            "set_event_async",
            AsyncMock(side_effect=lambda _key, receipt: receipts.append(dict(receipt))),
        )
        close = AsyncMock()
        monkeypatch.setattr(dbos_workflow.DBOS, "close_stream_async", close)

        await unwrap(dbos_workflow.events_workflow)("run")

        assert receipts[0]["pause"] == {"phase": "awaiting_approval", "version": "v1"}
        done = [(seq, f) for seq, f in enumerate(stream, 1) if f["type"] == "done"]
        assert [(f["generation"], f["status"]) for _, f in done] == [
            (0, "failed"),
            (1, "completed"),
        ]
        for seq, frame in done:
            generation = frame["generation"]
            assert (
                next(
                    i
                    for i, f in enumerate(stream, 1)
                    if f.get("message", "").endswith("committed tail")
                    and f["generation"] == generation
                )
                < seq
            )
            assert {
                "generation": generation,
                "offset": seq,
                "terminal": seq,
                "pause": None,
            } in receipts
        assert not any(f.get("message") == "obsolete" for f in stream)
        assert [c.kwargs["timeout_seconds"] for c in recv.await_args_list] == [
            86400,
            86400,
            0,
            0,
            86400,
            86400,
            86400,
            86400,
            0,
            0,
        ]
        close.assert_awaited_once_with("events")

    asyncio.run(run())


@pytest.mark.parametrize(
    "value",
    [
        HumanMessage(content="hello"),
        ResearchReview(research_complete=True),
        ResearchTask(
            task_id="t",
            target_role="public_signal",
            objective="o",
            evidence_needed="e",
            reason="r",
        ),
    ],
)
def test_json_serializer_only_restores_known_domain_types(value):
    result = RunJSON().deserialize(RunJSON().serialize(value))
    assert type(result) is type(value)
    assert result == value


@pytest.mark.parametrize("value", [object(), lambda: None, Path("private")])
def test_serializer_refuses_arbitrary_objects(value):
    with pytest.raises(TypeError):
        dumps(value)


def test_archived_stage_keeps_report_and_effect_metadata():
    db = Mock()
    db.scalar.return_value = "por_archive_native_recovery.units"
    db.execute.return_value.mappings.return_value = [
        {
            "id": "run:role",
            "name": "public_signal_agent",
            "artifact": dumps(
                {
                    "patch": {
                        "agents": {
                            "public_signal": {"report": {"value": "wrapped report"}},
                            "internal_knowledge": {"report": "plain report"},
                            "risk_assessment": {"report": None},
                        },
                        "report": {"final": "original final report"},
                    }
                }
            ),
            "usage": dumps({"total_tokens": 23}),
            "error": dumps(None),
            "settled": True,
            "costs": [],
            "unknown_effect": True,
        },
    ]

    view, usage, unresolved = legacy_projection(db, "run")

    assert view == {
        "completed_steps": ["public_signal_agent"],
        "legacy_report": "original final report",
        "legacy_roles": [
            {
                "role": "internal_knowledge",
                "content": "plain report",
                "stage_id": "run:role",
                "completed_at": "",
            },
            {
                "role": "public_signal",
                "content": "wrapped report",
                "stage_id": "run:role",
                "completed_at": "",
            },
        ],
    }
    assert usage["total_tokens"] == 23
    assert unresolved is True


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///runs.db",
        "sqlite:///:memory:",
        "mysql+pymysql://local/runs",
        "postgresql://local/runs",
        "",
    ],
)
def test_durable_execution_requires_postgresql_psycopg(url):
    with pytest.raises(ValueError):
        ExecutionSettings(url)


@pytest.mark.parametrize(
    "field", ["max_active", "owner_active", "queue_capacity", "approval_seconds"]
)
def test_resource_policy_is_positive(field):
    with pytest.raises(ValueError):
        ExecutionSettings("postgresql+psycopg://local/test", **{field: 0})


def test_public_reports_are_redacted_at_egress(monkeypatch):
    monkeypatch.setenv("EXAMPLE_API_KEY", "fixture-secret-123456")
    data = public_payload(
        {
            "report": "fixture-secret-123456 bearer token123 password=hidden",
            "stages": ["api_key=abc"],
        }
    )
    assert "fixture-secret-123456" not in str(data)
    assert "token123" not in str(data)
    assert "hidden" not in str(data)
    assert "abc" not in str(data)


def test_owned_commit_drains_cancellation_before_return():
    import threading

    entered = threading.Event()
    finish = threading.Event()

    def commit():
        entered.set()
        finish.wait(3)
        return "receipt"

    async def run():
        pending = asyncio.create_task(owned_io(commit))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        pending.cancel()
        await asyncio.sleep(0.01)
        assert not pending.done()
        finish.set()
        value, cancel = await pending
        assert value == "receipt"
        assert isinstance(cancel, asyncio.CancelledError)

    asyncio.run(run())


def test_effect_guard_checks_only_the_durable_commit(monkeypatch):
    from contextlib import contextmanager

    from dbos import DBOS

    from open_deep_research.workflow import execution

    events = []
    db = object()

    @contextmanager
    def begin():
        events.append("enter")
        yield db
        events.append("exit")

    def require(connection, run_id, workflow_id, executor):
        assert connection is db and run_id == "run" and workflow_id == "wf"
        assert executor == "executor"
        events.append("native_check")

    monkeypatch.setattr(DBOS, "workflow_id", "wf")
    monkeypatch.setattr(DBOS, "executor_id", "executor")
    monkeypatch.setattr(
        execution, "domain_engine", lambda: SimpleNamespace(begin=begin)
    )
    monkeypatch.setattr(execution, "require_live_effect", require)

    async def run():
        async with execution_effect():
            events.append("standalone")
        token = stage_context.set(SimpleNamespace(run_id="run"))
        try:
            async with execution_effect():
                events.append("write")
        finally:
            stage_context.reset(token)

    asyncio.run(run())
    assert events == ["standalone", "enter", "native_check", "write", "exit"]


def test_native_production_chain_does_not_import_old_execution_or_experimental_runner():
    root = Path(__file__).parents[1] / "src/open_deep_research"
    files = [
        "web/durable.py",
        "web/executor.py",
        "web/runs.py",
        "workflow/dbos_workflow.py",
        "workflow/domain_schema.py",
    ]
    for name in files:
        tree = ast.parse((root / name).read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not any(
                    x in module
                    for x in (
                        "python_workflow",
                        "run_store",
                        "web.worker",
                        "dbos_openai_agents",
                        "experiments",
                    )
                )
    assert not (root / "workflow/run_facts.py").exists()
    assert not (root / "web/worker.py").exists()
    assert not (root / "web/run_store.py").exists()
    assert not (root / "workflow/python_workflow.py").exists()


@pytest.mark.parametrize("advertised", [None, "1"])
def test_run_body_limit_counts_actual_bytes_before_admission(monkeypatch, advertised):
    from fastapi.testclient import TestClient

    from open_deep_research.web import server

    monkeypatch.setattr(server, "WEB_API_TOKEN", "body-test-token")
    headers = {
        "Authorization": "Bearer body-test-token",
        "Content-Type": "application/json",
    }
    if advertised is not None:
        headers["Content-Length"] = advertised
    with TestClient(server.app) as http:
        packet = b'{"topic":"' + b"x" * 70000 + b'"}'
        response = http.post(
            "/api/runs",
            content=iter([packet]) if advertised is None else packet,
            headers=headers,
        )
        assert response.status_code == 413
