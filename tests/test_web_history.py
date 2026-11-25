"""Owner-scoped history over native Run status, delivery and reports."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call

import pytest
from dbos import DBOSClient
from dbos import error as dbos_error
from fastapi.testclient import TestClient

from open_deep_research.web import durable, runs, server
from open_deep_research.web.run_data import dumps
from open_deep_research.workflow import dbos_config

HEADERS = {"Authorization": "Bearer history-test-token"}
PREVIOUS_VERSION = "por-datasource-stage-v1"


@pytest.fixture
def history_views(monkeypatch):
    engine = MagicMock()
    native = Mock(spec=DBOSClient)
    monkeypatch.setattr(durable, "domain_engine", lambda: engine)
    monkeypatch.setattr(dbos_config, "domain_engine", lambda: engine)
    monkeypatch.setattr(durable, "native_client", lambda: native)
    client = durable.RunClient()
    monkeypatch.setattr(runs, "get_run_client", lambda: client)
    monkeypatch.setattr(server, "WEB_API_TOKEN", "history-test-token")
    return native, engine.connect.return_value.__enter__.return_value


def workflow(run_id, version=dbos_config.VERSION):
    return SimpleNamespace(
        workflow_id=run_id,
        status="SUCCESS",
        created_at=1000,
        completed_at=2000,
        updated_at=3000,
        app_version=version,
    )


def test_history_page_reads_only_delivered_terminal_status(history_views):
    native, db = history_views
    native.list_workflows.return_value = [
        workflow("previous", PREVIOUS_VERSION),
        workflow("unknown"),
        workflow("settling"),
        workflow("retrying"),
        workflow("other-owner"),
        workflow("next-page"),
    ]
    db.execute.return_value = [
        SimpleNamespace(
            id=run_id,
            topic=f"Topic {run_id}",
            version=version,
            generation=gen,
            session="a" * 32,
            rag_enabled=True,
        )
        for run_id, version, gen in (
            ("previous", PREVIOUS_VERSION, 0),
            ("unknown", dbos_config.VERSION, 0),
            ("settling", dbos_config.VERSION, 0),
            ("retrying", dbos_config.VERSION, 1),
            ("next-page", dbos_config.VERSION, 0),
        )
    ]
    receipts = {
        "previous:events": {"offset": 4, "terminal": True, "generation": 0},
        "unknown:events": {"offset": 7, "terminal": 6, "generation": 0},
        "settling:events": {"offset": 2, "generation": 0},
        "retrying:events": {"offset": 5, "terminal": 5, "generation": 0},
        "next-page:events": {"offset": 2, "terminal": 2, "generation": 0},
    }
    native.get_event.side_effect = (
        lambda wf, key, _: receipts[wf] if key == "delivery" else None
    )
    native.read_stream_offset.side_effect = [
        {"type": "done", "status": "cancelled", "generation": 0},
        {"type": "done", "status": "interrupted", "generation": 0},
        {"type": "done", "status": "completed", "generation": 0},
    ]

    http = TestClient(server.app)
    response = http.get("/api/history?limit=2&offset=10&q=Topic", headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "items": [
            {
                "run_id": run_id,
                "topic": f"Topic {run_id}",
                "research_session_id": "a" * 32,
                "rag_enabled": True,
                "status": status,
                "started_at": "1970-01-01T00:00:01+00:00",
                "completed_at": "1970-01-01T00:00:02+00:00",
            }
            for run_id, status in (
                ("previous", "cancelled"),
                ("unknown", "interrupted"),
            )
        ],
        "has_more": True,
        "next_offset": 12,
    }
    native.list_workflows.assert_called_once_with(
        name="por.run",
        has_parent=False,
        user="operator",
        status=sorted(durable.NATIVE_TERMINAL),
        load_input=False,
        load_output=False,
        sort_desc=True,
        limit=50,
        offset=10,
    )
    db.execute.assert_called_once()
    query, params = db.execute.call_args.args
    assert "r.owner = :owner" in str(query)
    assert params == {
        "owner": "operator",
        "ids": [
            "previous",
            "unknown",
            "settling",
            "retrying",
            "other-owner",
            "next-page",
        ],
        "query": "Topic",
    }
    assert native.get_event.call_args_list == [
        call(wf, "delivery", 0) for wf in receipts
    ]
    assert native.read_stream_offset.call_args_list == [
        call("previous:events", "events", 3, timeout_seconds=0),
        call("unknown:events", "events", 5, timeout_seconds=0),
        call("next-page:events", "events", 1, timeout_seconds=0),
    ]
    native.read_stream.assert_not_called()
    native.retrieve_workflow.assert_not_called()

    assert http.get("/api/history?limit=0", headers=HEADERS).status_code == 400
    assert http.get("/api/history?offset=-1", headers=HEADERS).status_code == 400
    assert http.get("/api/history?status=running", headers=HEADERS).status_code == 400
    native.list_workflows.side_effect = dbos_error.DBOSQueryTimeoutError(3)
    assert http.get("/api/history", headers=HEADERS).status_code == 503


def test_history_detail_matches_run_and_checks_owner_before_native_reads(
    history_views, monkeypatch
):
    native, db = history_views
    native.list_workflows.return_value = [workflow("run", PREVIOUS_VERSION)]
    native.get_event.side_effect = lambda _id, key, _timeout: (
        {"offset": 6, "terminal": True, "generation": 0} if key == "delivery" else None
    )
    stored = {
        "id": "run",
        "owner": "operator",
        "session": "session",
        "cancel_requested": False,
        "reconciliation": None,
        "value": dumps(
            {
                "version": PREVIOUS_VERSION,
                "request": {"topic": "Brand recall", "org_context": "Operator input"},
                "config": {"configurable": {}},
                "initial": {},
                "created": 1,
            }
        ),
    }
    row = db.execute.return_value.mappings.return_value.first
    row.return_value = stored
    monkeypatch.setattr(durable, "retry_generation", lambda *args: 0)
    monkeypatch.setattr(
        durable, "model_bill", lambda *args: ({"model_calls": 2}, 0, True)
    )
    monkeypatch.setattr(
        durable, "tool_ledger", lambda *args: {"tool_calls": 3, "search_calls": 1}
    )
    effects = Mock(return_value=(False, True))
    monkeypatch.setattr(durable, "external_effects", effects)
    monkeypatch.setattr(durable, "stage_errors", lambda *args: [])
    reports = [{"role": "public_signal", "content": "Published role result"}]
    stage_result = Mock(
        side_effect=lambda *_: {
            "report": "Native report",
            "role_reports": reports,
            "completed_steps": [],
            "current_step": "",
            "stage_usage": {},
        }
    )
    monkeypatch.setattr(durable, "stage_results", stage_result)

    http = TestClient(server.app)
    for path in ("/api/history", "/api/history/run"):
        assert http.get(path).status_code == 401
        assert (
            http.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        )
    native.list_workflows.assert_not_called()

    response = http.get("/api/history/run", headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    record = response.json()
    assert record == http.get("/api/runs/run", headers=HEADERS).json()
    assert record["native_status"] == "SUCCESS" and record["status"] == "interrupted"
    assert record["settlement"] == "unresolved" and record["events_complete"]
    assert record["report"] == "Native report" and record["role_reports"] == reports
    assert record["usage"]["model_calls"] == 2 and record["usage"]["search_calls"] == 1
    assert record["request"]["org_context"] == "Operator input"
    assert "owner" not in record
    stage_result.assert_called_with(native, "run")

    effects.return_value = (True, False)
    monkeypatch.setattr(durable, "db_time", lambda _db: 3)
    assert http.get("/api/history/run", headers=HEADERS).status_code == 404
    native.reset_mock()
    row.return_value = dict(stored, owner="another-user")
    assert http.get("/api/history/run", headers=HEADERS).status_code == 404
    native.list_workflows.assert_not_called()
    monkeypatch.setattr(server, "WEB_API_TOKEN", "")
    assert http.get("/api/history", headers=HEADERS).status_code == 503


def reopen_history(case, run_id, monkeypatch):
    monkeypatch.setattr(runs, "get_run_client", lambda: case.client)
    monkeypatch.setattr(server, "WEB_API_TOKEN", "history-test-token")
    http = TestClient(server.app)
    response = http.get(f"/api/history/{run_id}", headers=HEADERS)
    assert response.status_code == 200
    page = http.get("/api/history", headers=HEADERS).json()
    assert [(item["run_id"], item["status"]) for item in page["items"]] == [
        (run_id, response.json()["status"])
    ]
    return response.json()


@pytest.mark.integration
def test_persisted_report_reopens_in_history(native_case, monkeypatch):
    case = native_case
    run = case.create(owner="operator")
    result = case.wait(run, lambda r: r["events_complete"])
    record = reopen_history(case, run["run_id"], monkeypatch)
    assert record["report"] == result["report"]
    assert record["usage"] == result["usage"]
    assert record["run_id"] == run["run_id"]
    assert len(record["role_reports"]) == 4
    events = case.client.read_events(run["run_id"], "operator", 0, 1000)
    assert [e["status"] for e in events if e["type"] == "done"] == ["completed"]


# 验证失败 SSE 运行保留已完成角色输出。
@pytest.mark.integration
def test_failed_sse_run_preserves_nested_role_outputs(native_case, monkeypatch):
    from open_deep_research import budget
    from open_deep_research.workflow.execution import stage_context, unconfirmed

    case = native_case
    finish = budget.finish_model_attempt

    def lose_receipt(run_id, call_id, usage):
        stage = stage_context.get()
        if stage is not None and stage.name == "risk_assessment_agent":
            raise unconfirmed("SECRET_DSN")
        return finish(run_id, call_id, usage)

    monkeypatch.setattr(budget, "finish_model_attempt", lose_receipt)
    run = case.create(owner="operator")
    result = case.wait(run, lambda r: r["events_complete"])
    record = reopen_history(case, result["run_id"], monkeypatch)
    assert record["status"] == "interrupted"
    assert result["status"] == "interrupted"
    assert result["settlement"] == "unresolved"
    assert result["retry_safe"] is False
    assert record["report"] == ""
    assert {s["role"] for s in record["role_reports"]} == {
        "public_signal",
        "internal_knowledge",
    }
    assert record["usage"] == result["usage"]
    events = case.client.read_events(run["run_id"], "operator", 0, 1000)
    assert any(e["type"] == "error" for e in events)
    assert [e["status"] for e in events if e["type"] == "done"] == ["interrupted"]
    assert "SECRET_DSN" not in json.dumps(events)


# 验证报告生成失败后仍可读取阶段结果。
@pytest.mark.integration
def test_failed_report_is_readable_in_history(native_case, monkeypatch):
    from open_deep_research.workflow import reporting

    case = native_case

    async def fail(*args, **kwargs):
        raise RuntimeError("Final report generation failed")

    monkeypatch.setattr(reporting, "generate_final_report", fail)
    result = case.wait(case.create(owner="operator"), lambda r: r["events_complete"])
    record = reopen_history(case, result["run_id"], monkeypatch)
    assert record["status"] == "failed"
    assert result["status"] == "failed"
    assert result["settlement"] == "settled"
    assert result["retry_safe"] is True
    assert not record["report"]
    assert len(record["role_reports"]) == 4
    assert len(case.models) == 2
    assert case.calls == {"public_signal": 1, "internal_knowledge": 1}
    assert record["usage"] == result["usage"]


# 验证用户取消研究后仍可按 Run ID 查询历史。
@pytest.mark.integration
@pytest.mark.parametrize("native_case", ["cancel"], indirect=True)
def test_cancelled_run_is_readable_in_history(native_case, monkeypatch):
    import time

    case = native_case
    run = case.create(owner="operator")
    deadline = time.monotonic() + 15
    while not case.blocked.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert case.blocked.is_set()
    case.client.cancel(run["run_id"], "operator")
    result = case.wait(run, lambda r: r["events_complete"])
    record = reopen_history(case, result["run_id"], monkeypatch)
    assert record["status"] == result["status"] == "cancelled"
    assert record["usage"] == result["usage"]
    assert result["usage"]["usage_complete"] is False
    assert all(rt.harness.last_settlement.succeeded for rt in case.runtimes)
    events = case.client.read_events(run["run_id"], "operator", 0, 1000)
    assert [e["status"] for e in events if e["type"] == "done"] == ["cancelled"]


@pytest.mark.integration
@pytest.mark.parametrize("failed_role", ["risk_assessment", "response_strategy"])
def test_analysis_provider_failure_preserves_usage_and_role_reports(
    native_case, monkeypatch, failed_role
):
    from dbos_support import stage_observations
    from sdk_model_support import install_inference
    from sqlalchemy import text

    from open_deep_research.prompts import (
        response_strategy_prompt,
        risk_assessment_prompt,
    )
    from open_deep_research.web.run_data import loads
    from open_deep_research.workflow.dbos_config import domain_engine

    case = native_case
    calls = []

    def fail(request):
        if request.prompt == risk_assessment_prompt:
            role = "risk_assessment"
        elif request.prompt == response_strategy_prompt:
            role = "response_strategy"
        else:
            return None
        calls.append(role)
        if role == failed_role:
            raise RuntimeError("Synthetic analysis provider failure")

    install_inference(monkeypatch, fail)
    run = case.create(owner="operator")
    result = case.wait(run, lambda r: r["events_complete"])
    record = reopen_history(case, result["run_id"], monkeypatch)
    assert record["status"] == result["status"] == "failed"
    assert result["settlement"] == "settled"
    assert result["error"]["kind"] == "model_error"
    assert result["current_step"] == result["error"]["step"] == failed_role + "_agent"
    assert result["retry_safe"] is False
    assert not record["report"]
    expected = {"public_signal", "internal_knowledge"}
    if failed_role == "response_strategy":
        expected.add("risk_assessment")
    assert {s["role"] for s in record["role_reports"]} == expected
    assert calls == (
        ["risk_assessment", "response_strategy"]
        if failed_role == "response_strategy"
        else ["risk_assessment"]
    )
    stage = next(
        unit
        for unit in stage_observations(run["run_id"])
        if unit["name"] == failed_role + "_agent"
    )
    assert stage["usage"]["model_calls"] == 1
    with domain_engine().connect() as connection:
        receipt = loads(
            connection.execute(
                text(
                    "SELECT usage FROM por_domain.model_attempts "
                    "WHERE run_id=:run AND stage_id=:stage"
                ),
                {"run": run["run_id"], "stage": stage["id"]},
            ).scalar_one()
        )
    assert receipt["model_calls"] == 1 and receipt["usage_complete"] is False
    assert result["usage"]["usage_complete"] is False
    assert record["usage"] == result["usage"]
    assert len(case.models) == 2
