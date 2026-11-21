# 验证报告归档、历史授权、分页及失败和取消运行的阶段输出恢复。
"""Verify PostgreSQL report archiving and history authorization."""

import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from open_deep_research.web import history, server


# 提供隔离的历史存储测试夹具。
@pytest.fixture
def store(monkeypatch):
    archive = Mock()
    archive.save.return_value = 1
    archive.list.return_value = {"items": [], "total": 0, "storage": "postgres"}
    archive.get.return_value = None
    monkeypatch.setattr(server, "get_history_store", lambda: archive)
    monkeypatch.setattr(server, "WEB_API_TOKEN", "")
    return archive


# 解析测试响应中的 SSE 数据事件。
def stream_events(response):
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


# 验证归档完成后的报告可通过 History 重新打开。
@pytest.mark.integration
def test_saved_report_reopens_in_history(native_case, monkeypatch):
    case = native_case
    run = case.create(owner="operator")
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    record = case.history.get(result["history_id"], owner="operator")
    assert record["report"] == result["report"]
    assert record["usage"] == {
        k: result["usage"][k]
        for k in ("model_calls", "input_tokens", "output_tokens", "total_tokens")
    }
    assert record["run_id"] == run["run_id"]
    assert len(record["role_reports"]) == 4
    monkeypatch.setattr(server, "get_history_store", lambda: case.history)
    monkeypatch.setattr(server, "WEB_API_TOKEN", "history-test-token")
    with TestClient(server.app) as client:
        reopened = client.get(
            "/api/history/" + record["id"],
            headers={"Authorization": "Bearer history-test-token"},
        )
        assert reopened.status_code == 200
        assert reopened.json()["report"] == record["report"]
        assert reopened.headers["cache-control"] == "no-store"
    events = case.client.read_events(run["run_id"], "operator", 0, 1000)
    assert any(e["type"] == "history_saved" for e in events)
    assert any(e["type"] == "done" for e in events)


# 验证归档失败不会丢失已生成报告。
@pytest.mark.integration
def test_archive_failure_keeps_the_report_available(native_case, monkeypatch):
    import threading

    case = native_case
    outage = threading.Event()
    outage.set()
    original = case.history.save

    def save(records):
        if outage.is_set():
            raise history.HistoryUnavailable("SECRET_DSN=postgres://password@internal")
        return original(records)

    monkeypatch.setattr(case.history, "save", save)
    run = case.create()
    result = case.wait(run)
    assert result["status"] == "completed"
    assert result["report"]
    case.wait(
        run,
        lambda r: any(
            e["type"] == "archive_pending"
            for e in case.client.read_events(run["run_id"], "alice", 0, 1000)
        ),
    )
    assert "SECRET_DSN" not in json.dumps(
        case.client.read_events(run["run_id"], "alice", 0, 1000)
    )
    before = len(case.models)
    outage.clear()
    archived = case.wait(run, lambda r: r["archive_status"] == "saved")
    assert archived["report"] == result["report"]
    assert archived["usage"] == result["usage"]
    assert len(case.models) == before == 2


# 验证配置令牌后所有历史接口都执行授权。
@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/history", "get"),
        ("/api/history/record-1", "get"),
    ],
)
def test_all_history_routes_require_the_configured_token(
    monkeypatch, store, path, method
):
    monkeypatch.setattr(server, "WEB_API_TOKEN", "test-only-secret")
    with TestClient(server.app) as client:
        assert getattr(client, method)(path).status_code == 401
        assert (
            getattr(client, method)(
                path, headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
    assert not store.mock_calls


# 验证历史搜索、分页与服务错误响应。
def test_history_search_pagination_and_errors(monkeypatch, store):
    with TestClient(server.app) as client:
        response = client.get(
            "/api/history", params={"q": "投诉", "limit": 5, "offset": 10}
        )
        assert response.status_code == 200
        store.list.assert_called_once_with(query="投诉", limit=5, offset=10)
        assert client.get("/api/history/missing").status_code == 404
        assert client.get("/api/history?limit=0").status_code == 400
        assert client.get("/api/history?offset=-1").status_code == 400
        store.list.side_effect = history.HistoryUnavailable("研究历史暂时无法访问。")
        unavailable = client.get("/api/history")
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"] == "研究历史暂时无法访问。"


# 验证历史记录模型保留前端需要的字段。
def test_history_record_exposes_stable_archive_fields():
    record = history.history_record(
        run_id="run-1",
        request={"topic": "投诉\n第二行", "model": "fixture:model"},
        report="report body",
        usage={"model_calls": 2, "total_tokens": 10},
        started_at="2026-10-01T02:00:00+00:00",
        status="completed",
        role_reports=[history.HistoryStage(role="public_signal", content="signal")],
    )
    payload = record.model_dump()
    assert payload["topic"] == "投诉"
    assert payload["id"].startswith("HISTORY_")
    assert payload["report"] == "report body"
    assert payload["request"] == {"topic": "投诉\n第二行", "model": "fixture:model"}
    assert payload["usage"] == {"model_calls": 2, "total_tokens": 10}


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
    run = case.create()
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    record = case.history.get(result["history_id"], owner="alice")
    assert record["status"] == "incomplete"
    assert result["status"] == "interrupted"
    assert result["settlement"] == "unresolved"
    assert result["retry_safe"] is False
    assert record["report"] == ""
    assert {s["role"] for s in record["role_reports"]} == {
        "public_signal",
        "internal_knowledge",
    }
    assert record["usage"] == {
        k: result["usage"][k]
        for k in (
            "model_calls",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "usage_complete",
        )
    }
    events = case.client.read_events(run["run_id"], "alice", 0, 1000)
    assert any(e["type"] == "error" for e in events)
    assert any(e["type"] == "history_saved" for e in events)
    assert "SECRET_DSN" not in json.dumps(events)


# 验证没有最终报告时仍建立未完成历史记录。
@pytest.mark.integration
def test_no_final_report_still_creates_an_incomplete_history_record(
    native_case, monkeypatch
):
    from open_deep_research.workflow import reporting

    case = native_case

    async def fail(*args, **kwargs):
        raise RuntimeError("Final report generation failed")

    monkeypatch.setattr(reporting, "generate_final_report", fail)
    result = case.wait(case.create(), lambda r: r["archive_status"] == "saved")
    record = case.history.get(result["history_id"], owner="alice")
    assert record["status"] == "failed"
    assert result["status"] == "failed"
    assert result["settlement"] == "settled"
    assert result["retry_safe"] is True
    assert not record["report"]
    assert len(record["role_reports"]) == 4
    assert len(case.models) == 2
    assert case.calls == {"public_signal": 1, "internal_knowledge": 1}
    assert record["usage"] == {
        k: result["usage"][k]
        for k in ("model_calls", "input_tokens", "output_tokens", "total_tokens")
    }


# 验证用户取消研究后仍保存历史记录。
@pytest.mark.integration
@pytest.mark.parametrize("native_case", ["cancel"], indirect=True)
def test_cancelled_run_still_creates_a_history_record(native_case):
    import time

    case = native_case
    run = case.create()
    deadline = time.monotonic() + 15
    while not case.blocked.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert case.blocked.is_set()
    case.client.cancel(run["run_id"], "alice")
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    record = case.history.get(result["history_id"], owner="alice")
    assert record["status"] == result["status"] == "cancelled"
    assert record["usage"] == {
        k: result["usage"][k]
        for k in (
            "model_calls",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "usage_complete",
        )
    }
    assert result["usage"]["usage_complete"] is False
    assert all(rt.harness.last_settlement.succeeded for rt in case.runtimes)
    assert any(
        e["type"] == "history_saved"
        for e in case.client.read_events(run["run_id"], "alice", 0, 1000)
    )


@pytest.mark.integration
@pytest.mark.parametrize("failed_role", ["risk_assessment", "response_strategy"])
def test_analysis_provider_failure_preserves_usage_and_completed_history(
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
    run = case.create()
    result = case.wait(run, lambda r: r["archive_status"] == "saved")
    record = case.history.get(result["history_id"], owner="alice")
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
    assert calls == (["risk_assessment", "response_strategy"] if failed_role == "response_strategy" else ["risk_assessment"])
    stage = next(
        unit for unit in stage_observations(run["run_id"])
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
    assert record["usage"] == {
        k: result["usage"][k]
        for k in ("model_calls", "input_tokens", "output_tokens", "total_tokens", "usage_complete")
    }
    assert len(case.models) == 2
