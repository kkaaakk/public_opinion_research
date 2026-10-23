"""Offline protocol, config and source-boundary contracts for native execution."""

import ast
import asyncio
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from open_deep_research.state import ResearchReview, ResearchTask, Section
from open_deep_research.web.run_data import (
    RunJSON,
    dumps,
    public_payload,
)
from open_deep_research.workflow.dbos_config import ExecutionSettings
from open_deep_research.workflow.execution import (
    execution_effect,
    execution_observer,
    owned_io,
)


@pytest.mark.parametrize(
    "value",
    [
        HumanMessage(content="hello"),
        Section(name="A", description="B"),
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


def test_effect_guard_remains_optional_for_legacy_and_is_owned():
    events = []

    class Guard:
        def __enter__(self):
            events.append("enter")

        def __exit__(self, *_):
            events.append("exit")

    async def observer(kind, **kw):
        assert kind == "effect_guard"
        return Guard()

    async def run():
        async with execution_effect():
            events.append("legacy")
        token = execution_observer.set(observer)
        try:
            async with execution_effect():
                events.append("write")
        finally:
            execution_observer.reset(token)

    asyncio.run(run())
    assert events == ["legacy", "enter", "write", "exit"]


def test_native_production_chain_does_not_import_old_execution_or_experimental_runner():
    root = Path(__file__).parents[1] / "src/open_deep_research"
    files = [
        "web/durable.py",
        "web/executor.py",
        "web/runs.py",
        "workflow/dbos_workflow.py",
        "workflow/run_facts.py",
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
