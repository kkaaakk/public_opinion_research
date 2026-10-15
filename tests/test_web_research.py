# 验证前端传入的 RAG 选择实际影响研究流的检索配置。
"""Verify the Web UI can run research with or without local RAG."""

import json

import pytest
from fastapi.testclient import TestClient

from open_deep_research.configuration import Configuration
from open_deep_research.runtime import business_agent
from open_deep_research.web import server


# 验证启用或关闭 RAG 时图调用收到对应设置。
@pytest.mark.parametrize("rag_enabled", [False, True])
def test_research_stream_respects_rag_selection(monkeypatch, rag_enabled) -> None:
    """A web-only run skips the RAG-only agent and delivers the final report."""
    monkeypatch.delenv("RAG_ENABLED", raising=False)
    monkeypatch.delenv("ENABLED_BUSINESS_AGENTS", raising=False)
    monkeypatch.setattr(server, "WEB_API_TOKEN", "")

    # 阻止被禁用的内部知识角色意外初始化工具。
    async def unexpected_tools(*_args, **_kwargs):
        raise AssertionError("The disabled internal agent must not initialize tools")

    monkeypatch.setattr(business_agent, "_business_agent_tools", unexpected_tools)

    # 检查传入 RAG 配置并产生固定研究结果的图替身。
    class ResearchGraph:
        # 校验 RAG 开关及角色启用列表，再产生预设研究结果。
        async def astream(self, initial_state, config, **_kwargs):
            configurable = Configuration.from_runnable_config(config)
            assert configurable.rag_enabled is rag_enabled
            assert {"public_signal", "risk_assessment", "response_strategy"}.issubset(
                configurable.enabled_business_agents
            )
            if rag_enabled:
                assert "internal_knowledge" in configurable.enabled_business_agents
            else:
                # Exercise the real agent guard: an enabled internal agent would
                # try to build unavailable RAG tools and fail this stream.
                result = await business_agent.run_business_agent(
                    role="internal_knowledge", state=initial_state, config=config
                )
                assert result == {}
            yield {"final_report_generation": {"report": {"final": "Fixture report"}}}

    monkeypatch.setattr(server, "_deep_researcher_factory", lambda _config: ResearchGraph())
    with TestClient(server.app) as client:
        response = client.post(
            "/api/research",
            json={"topic": "Analyze a customer-service incident", "rag_enabled": rag_enabled},
        )

    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert not any(event["type"] == "error" for event in events)
    assert {"type": "report", "content": "Fixture report"} in events
    assert events[-1] == {"type": "done"}
