# 验证报告归档、历史授权、分页导入及失败和取消运行的阶段输出恢复。
"""Verify report archiving, history authorization, and LangSmith imports."""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from open_deep_research.web import history, server


# 提供隔离的历史存储测试夹具。
@pytest.fixture
def store(monkeypatch):
    archive = Mock()
    archive.save.return_value = 1
    archive.list.return_value = {"items": [], "total": 0, "storage": "neo4j"}
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


# 验证报告交付前完成归档，并可重新打开。
def test_report_is_archived_before_delivery_and_reopens(monkeypatch, store):
    usage = {
        "model_calls": 3,
        "input_tokens": 120,
        "output_tokens": 80,
        "total_tokens": 200,
    }

    # 为归档场景提供可控的图流事件与退出状态。
    class Graph:
        # 检查运行关联配置，并发出完整报告和用量事件。
        async def astream(self, _state, config, **_kwargs):
            self.run_id = config["configurable"]["thread_id"]
            assert config["metadata"]["web_research_mode"] == "fast"
            yield {
                "final_report_generation": {
                    "report": {"final": "# 完整报告\n研究结论"},
                    "runtime": {"budget": usage},
                }
            }

    graph = Graph()
    monkeypatch.setattr(server, "_deep_researcher_factory", lambda _config: graph)
    request = {
        "topic": "品牌质量投诉",
        "mode": "fast",
        "org_context": "品牌背景",
        "rag_enabled": True,
    }
    with TestClient(server.app) as client:
        response = client.post("/api/research", json=request)
        events = stream_events(response)
        record = store.save.call_args.args[0][0]
        assert record.run_id == graph.run_id
        assert record.topic == request["topic"]
        assert record.request["org_context"] == "品牌背景"
        assert record.request["rag_enabled"] is True
        assert record.usage == usage
        assert record.report == "# 完整报告\n研究结论"
        assert any(
            event["type"] == "history_saved" and event["id"] == record.id
            for event in events
        )
        assert events[-1] == {"type": "done"}
        store.get.return_value = record.model_dump()
        reopened = client.get(f"/api/history/{record.id}")
        assert reopened.status_code == 200
        assert reopened.json()["report"] == record.report
        assert reopened.headers["cache-control"] == "no-store"


# 验证归档失败不会丢失已生成报告。
def test_archive_failure_keeps_the_report_available(monkeypatch, store):
    store.save.side_effect = RuntimeError("SECRET_DSN=neo4j://password@internal")

    # 为归档场景提供可控的图流事件与退出状态。
    class Graph:
        # 发出已生成报告，供归档失败后的交付检查。
        async def astream(self, *_args, **_kwargs):
            yield {"final_report_generation": {"report": {"final": "Generated report"}}}

    monkeypatch.setattr(server, "_deep_researcher_factory", lambda _config: Graph())
    with TestClient(server.app) as client:
        response = client.post("/api/research", json={"topic": "品牌投诉"})
    events = stream_events(response)
    assert {"type": "report", "content": "Generated report"} in events
    assert any(event["type"] == "warning" for event in events)
    assert not any(event["type"] in {"error", "history_saved"} for event in events)
    assert "SECRET_DSN" not in response.text


# 验证配置令牌后所有历史接口都执行授权。
@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/history", "get"),
        ("/api/history/record-1", "get"),
        ("/api/history/import/langsmith", "post"),
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


# 验证通过授权的导入请求调用实际归档入口。
def test_authorized_import_delegates_to_the_archive(monkeypatch, store):
    monkeypatch.setattr(server, "WEB_API_TOKEN", "test-only-secret")
    importer = Mock(return_value={"imported": 2, "scanned": 2, "skipped": 0})
    monkeypatch.setattr(server, "import_langsmith_history", importer)
    with TestClient(server.app) as client:
        response = client.post(
            "/api/history/import/langsmith", headers={"X-API-Token": "test-only-secret"}
        )
    assert response.status_code == 200
    assert response.json()["imported"] == 2
    importer.assert_called_once_with(store)


# 构造带状态和输出的 LangSmith 运行替身。
def make_run(**overrides):
    values = dict(
        id="trace-1",
        name="public_opinion_research",
        parent_run_id=None,
        error=None,
        start_time=datetime(2026, 10, 1, 2, tzinfo=timezone.utc),
        end_time=datetime(2026, 10, 1, 3, tzinfo=timezone.utc),
        inputs={
            "messages": [
                {"type": "system", "content": "Instructions"},
                {"type": "human", "content": [{"type": "text", "text": "研究品牌"}]},
            ]
        },
        outputs={
            "report": {"final": "# 原始报告"},
            "research": {"run_id": "run-1"},
            "runtime": {"budget": {"total_tokens": 42, "secret": "do-not-copy"}},
        },
        extra={
            "metadata": {
                "research_model": "deepseek:deepseek-chat",
                "search_api": "tavily",
                "rag_enabled": True,
                "api_key": "do-not-copy",
            }
        },
    )
    values.update(overrides)
    return SimpleNamespace(**values)


# 验证导入规范化研究报告并跳过无关追踪区间。
def test_import_normalizes_reports_and_ignores_non_research_spans():
    record = history.record_from_langsmith(make_run())
    assert record.run_id == "run-1"
    assert record.topic == "研究品牌"
    assert record.langsmith_run_id == "trace-1"
    assert record.request["model"] == "deepseek:deepseek-chat"
    assert record.usage == {"total_tokens": 42}
    assert record.source == "langsmith"
    assert record.status == "completed"
    assert "do-not-copy" not in record.model_dump_json()
    for invalid in (
        make_run(end_time=None),
        make_run(parent_run_id="root"),
        make_run(name="RunnableSequence", outputs={"parsed": "unrelated"}),
    ):
        assert history.record_from_langsmith(invalid) is None


# 验证兼容旧报告字段与无时区时间戳。
def test_legacy_report_and_naive_timestamp_are_supported():
    run = make_run(
        name="LangGraph",
        start_time=datetime(2026, 10, 1),
        outputs={"final_report": "Legacy report"},
        inputs={
            "messages": [
                {"id": ["langchain", "HumanMessage"], "kwargs": {"content": "旧研究"}}
            ]
        },
        extra={
            "metadata": {
                "business_scenario": "public_opinion_risk",
                "thread_id": "old-thread",
            }
        },
    )
    record = history.record_from_langsmith(run)
    assert record.report == "Legacy report"
    assert record.topic == "旧研究"
    assert record.run_id == "old-thread"
    assert record.started_at.endswith("+00:00")


# 验证导入使用 SDK 分页和受限写入批次。
def test_import_uses_sdk_pagination_and_bounded_write_batches():
    client = Mock()
    client.list_runs.return_value = (
        make_run(
            id=f"trace-{index}",
            outputs={
                "report": {"final": "完整报告"},
                "research": {"run_id": f"run-{index}"},
            },
        )
        for index in range(121)
    )
    store = Mock()
    store.save.side_effect = lambda batch: len(batch)
    result = history.import_langsmith_history(store, client=client)
    assert result == {"imported": 121, "scanned": 121, "skipped": 0}
    assert store.save.call_count == 3
    assert "limit" not in client.list_runs.call_args.kwargs
    assert client.list_runs.call_args.kwargs["is_root"] is True
    assert "error" not in client.list_runs.call_args.kwargs


# 验证归档查询参数化且证据统计限制在当前运行。
def test_neo4j_archive_queries_are_parameterized_and_keep_graph_scope():
    driver = Mock()
    driver.execute_query.return_value = SimpleNamespace(records=[])
    store = history.Neo4jHistoryStore(driver)
    record = history.record_from_langsmith(make_run())
    driver.execute_query.return_value = SimpleNamespace(records=[{"imported": 1}])
    assert store.save([record, record]) == 1
    cypher = driver.execute_query.call_args.args[0]
    params = driver.execute_query.call_args.kwargs["parameters_"]
    assert len(params["rows"]) == 1
    assert "ON CREATE SET h += row" in cypher
    assert record.report not in cypher
    assert json.loads(params["rows"][0]["request_json"])["topic"] == record.topic
    driver.execute_query.return_value = SimpleNamespace(
        records=[
            {
                "record": params["rows"][0],
                "sources": 2,
                "evidence": 3,
                "findings": 1,
            }
        ]
    )
    reopened = store.get(record.id)
    assert reopened["report"] == record.report
    assert reopened["graph"]["sources"] == 2
    assert "{run_id: h.run_id}" in driver.execute_query.call_args.args[0]
    driver.execute_query.return_value = SimpleNamespace(
        records=[
            {
                "total": 1,
                "items": [
                    {
                        "id": record.id,
                        "topic": record.topic,
                        "preview": "原始报告",
                        "request_json": "{}",
                        "usage_json": "{}",
                    }
                ],
            }
        ]
    )
    page = store.list(query="a' OR true", limit=5, offset=10)
    assert "report" not in page["items"][0]
    assert driver.execute_query.call_args.kwargs["parameters_"]["query"] == "a' or true"
    assert "a' OR true" not in driver.execute_query.call_args.args[0]


# 验证失败研究导入时保留原始已完成阶段报告。
def test_failed_research_is_imported_with_original_stage_reports():
    root = make_run(
        error="Provider failure with SECRET_DSN",
        outputs={},
        extra={"metadata": {"thread_id": "mooncake-run"}},
        inputs={"messages": [{"type": "human", "content": "月饼研究\n研究要求"}]},
    )
    first = make_run(
        id="stage-1",
        name="public_signal_agent",
        parent_run_id="root",
        outputs={
            "agents": {
                "public_signal": {
                    "report": {"type": "override", "value": "第一轮原始报告"},
                    "memory": ["do-not-copy"],
                }
            }
        },
    )
    second = make_run(
        id="stage-2",
        name="public_signal_agent",
        parent_run_id="root",
        start_time=datetime(2026, 10, 1, 2, 10, tzinfo=timezone.utc),
        outputs={
            "agents": {
                "public_signal": {
                    "report": {"type": "override", "value": "第二轮原始报告"}
                }
            }
        },
    )
    failed = make_run(
        name="response_strategy_agent",
        parent_run_id="root",
        error="SECRET_DSN",
        start_time=datetime(2026, 10, 1, 2, 20, tzinfo=timezone.utc),
        outputs={},
        inputs={"runtime": {"budget": {"total_tokens": 300, "secret": "do-not-copy"}}},
    )
    client = Mock()
    client.list_runs.side_effect = [[root], [failed, second, first]]
    store = Mock()
    store.save.return_value = 1
    result = history.import_langsmith_history(store, client=client)
    record = store.save.call_args.args[0][0]
    assert result == {"imported": 1, "scanned": 1, "skipped": 0}
    assert record.status == "failed"
    assert record.topic == "月饼研究"
    assert record.request["topic"] == "月饼研究\n研究要求"
    assert record.report == ""
    assert [stage.content for stage in record.role_reports] == [
        "第一轮原始报告",
        "第二轮原始报告",
    ]
    assert record.failed_stage == "response_strategy"
    assert record.usage == {"total_tokens": 300}
    assert "SECRET_DSN" not in record.model_dump_json()
    assert "do-not-copy" not in record.model_dump_json()
    assert client.list_runs.call_args.kwargs["trace_id"] == record.langsmith_run_id


# 验证工作流名称变化不会丢弃有最终报告的研究。
def test_renamed_workflow_with_final_report_is_not_filtered_out():
    record = history.record_from_langsmith(make_run(name="old_research_workflow"))
    assert record is not None
    assert record.status == "completed"
    assert record.report == "# 原始报告"


# 验证 SSE 运行失败后保留嵌套角色输出。
def test_failed_sse_run_preserves_nested_role_outputs(monkeypatch, store):
    # 为归档场景提供可控的图流事件与退出状态。
    class Graph:
        # 先产生嵌套角色报告再抛错，检查失败归档保留原始阶段。
        async def astream(self, _state, _config, **kwargs):
            assert kwargs["subgraphs"] is True
            yield (
                ("research_phase:1",),
                {
                    "public_signal_agent": {
                        "agents": {
                            "public_signal": {
                                "report": {
                                    "type": "override",
                                    "value": "已完成的采集报告",
                                }
                            }
                        }
                    }
                },
            )
            raise RuntimeError("SECRET_DSN")

    monkeypatch.setattr(server, "_deep_researcher_factory", lambda _config: Graph())
    with TestClient(server.app) as client:
        response = client.post("/api/research", json={"topic": "月饼研究"})
    record = store.save.call_args.args[0][0]
    assert record.status == "failed"
    assert record.report == ""
    assert record.role_reports[0].content == "已完成的采集报告"
    assert record.usage == {}
    events = stream_events(response)
    assert any(event["type"] == "error" for event in events)
    assert any(
        event["type"] == "history_saved" and event["status"] == "failed"
        for event in events
    )
    assert "SECRET_DSN" not in response.text


# 验证没有最终报告时仍建立未完成历史记录。
def test_no_final_report_still_creates_an_incomplete_history_record(monkeypatch, store):
    # 为归档场景提供可控的图流事件与退出状态。
    class Graph:
        # 只产生角色报告而不输出最终报告，模拟未完成运行。
        async def astream(self, *_args, **_kwargs):
            yield {
                "research_phase": {
                    "agents": {"risk_assessment": {"report": "原始风险报告"}}
                }
            }

    monkeypatch.setattr(server, "_deep_researcher_factory", lambda _config: Graph())
    with TestClient(server.app) as client:
        client.post("/api/research", json={"topic": "月饼研究"})
    record = store.save.call_args.args[0][0]
    assert record.status == "incomplete"
    assert record.role_reports[0].content == "原始风险报告"


# 验证用户取消研究后仍保存历史记录。
def test_cancelled_run_still_creates_a_history_record(monkeypatch, store):
    # 为归档场景提供可控的图流事件与退出状态。
    class Graph:
        # 模拟流中的取消异常，检查取消运行的归档行为。
        async def astream(self, *_args, **_kwargs):
            if False:
                yield {}
            raise asyncio.CancelledError()

    monkeypatch.setattr(server, "_deep_researcher_factory", lambda _config: Graph())
    with TestClient(server.app) as client:
        response = client.post("/api/research", json={"topic": "月饼研究"})
    assert store.save.call_args.args[0][0].status == "cancelled"
    assert any(event["type"] == "history_saved" for event in stream_events(response))


# 验证真实 LangGraph 嵌套失败时已完成阶段仍可恢复。
def test_real_langgraph_nested_failure_retains_finished_stage(monkeypatch, store):
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    # 定义嵌套失败测试所需的最小 LangGraph 状态。
    class State(TypedDict, total=False):
        messages: list
        agents: dict

    # 返回已完成采集阶段的原始报告，供后续失败恢复。
    async def collect_signal(_state):
        return {
            "agents": {
                "public_signal": {
                    "report": {"type": "override", "value": "真实子图阶段输出"}
                }
            }
        }

    # 模拟后续策略角色失败，检查上游成功输出仍保留。
    async def fail_strategy(_state):
        raise RuntimeError("Later agent failure")

    child = StateGraph(State)
    child.add_node("public_signal_agent", collect_signal)
    child.add_node("response_strategy_agent", fail_strategy)
    child.add_edge(START, "public_signal_agent")
    child.add_edge("public_signal_agent", "response_strategy_agent")
    child.add_edge("response_strategy_agent", END)
    child_graph = child.compile()

    # 将状态和配置转发给真实测试子图。
    async def research_phase(state, config):
        return await child_graph.ainvoke(state, config)

    parent = StateGraph(State)
    parent.add_node("research_phase", research_phase)
    parent.add_edge(START, "research_phase")
    parent.add_edge("research_phase", END)
    monkeypatch.setattr(
        server, "_deep_researcher_factory", lambda _config: parent.compile()
    )
    with TestClient(server.app) as client:
        client.post("/api/research", json={"topic": "月饼研究"})
    record = store.save.call_args.args[0][0]
    assert record.status == "failed"
    assert record.role_reports[0].content == "真实子图阶段输出"


# 验证同批导入中的后续失败恢复不会掩盖已有完整报告。
def test_later_failed_resume_cannot_hide_a_complete_report_in_one_import_batch():
    driver = Mock()
    driver.execute_query.return_value = SimpleNamespace(records=[])
    store = history.Neo4jHistoryStore(driver)
    complete = history.record_from_langsmith(make_run())
    failed = complete.model_copy(
        update={
            "report": "",
            "status": "failed",
            "completed_at": "2026-10-08T00:00:00+00:00",
            "role_reports": [
                history.HistoryStage(role="public_signal", content="保留的阶段报告")
            ],
        }
    )
    driver.execute_query.return_value = SimpleNamespace(records=[{"imported": 1}])
    store.save([complete, failed])
    row = driver.execute_query.call_args.kwargs["parameters_"]["rows"][0]
    assert row["report"] == complete.report
    assert row["status"] == "completed"
    assert row["stage_count"] == 1
