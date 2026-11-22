"""Regression tests for complete public-opinion role-report propagation."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from agents.testing import ScriptedModel, assistant_message
from sdk_model_support import install_sequence
from test_sdk_runtime import _tool

import open_deep_research.runtime.business_agent as business_agent_module
from open_deep_research.state import ResearchReview, agents_reducer
from open_deep_research.workflow import reporting, research


# 构造报告传播测试使用的舆情配置。
def _public_opinion_config(*roles: str) -> dict:
    return {
        "configurable": {
            "thread_id": "role-report-fixture",
            "enabled_business_agents": list(roles),
        }
    }


# 验证精简压缩不会替代正式完整报告。
def test_full_role_report_is_not_replaced_by_compact_notes(monkeypatch) -> None:
    """The producer result carries the complete compressed role report."""
    full_report_body = "A" * 5_000 + "\nCOMPLETE_REPORT_TAIL"

    async def fake_tools(_config, _role, _spec=None):
        return [_tool("public_signal")]

    async def fake_compress(*_args, **_kwargs):
        return full_report_body, {}

    @asynccontextmanager
    async def offline_model(*args, **kwargs):
        yield ScriptedModel([[assistant_message("agent step")]])

    monkeypatch.setattr(business_agent_module, "sdk_model", offline_model)
    monkeypatch.setattr(business_agent_module, "_business_agent_tools", fake_tools)
    monkeypatch.setattr(business_agent_module, "_compress_research", fake_compress)

    result = asyncio.run(
        business_agent_module.run_business_agent(
            state={
                "workflow": {"brief": "brand risk", "round": 1},
                "agents": {},
                "runtime": {"budget": {}},
            },
            config=_public_opinion_config("public_signal"),
            role="public_signal",
        )
    )

    assert full_report_body in result.report
    assert len(result.report) > 1_800
    assert set(result.__dict__) == {
        "role",
        "round",
        "report",
        "replace_report",
        "evidence_collected",
        "budget_delta",
    }


# 验证并行角色报告按角色合并。
def test_parallel_role_reports_merge_by_role() -> None:
    merged = agents_reducer(
        {"public_signal": {"report": "PUBLIC"}},
        {"internal_knowledge": {"report": "INTERNAL"}},
    )
    assert merged["public_signal"]["report"] == "PUBLIC"
    assert merged["internal_knowledge"]["report"] == "INTERNAL"


# 验证风险角色收到完整上游报告。
def test_risk_assessment_uses_full_upstream_reports_and_review(monkeypatch) -> None:
    public_report = "P" * 13_000 + "\nPUBLIC_TAIL"
    requests = install_sequence(monkeypatch, "verified claims and risk register")
    result = asyncio.run(
        research.risk_assessment(
            {
                "workflow": {
                    "brief": "APPROVED_BRIEF",
                    "round": 2,
                    "review": ResearchReview(
                        research_complete=False, unresolved_claims=["REVIEW_GAP"]
                    ),
                },
                "agents": {
                    "public_signal": {"report": public_report},
                    "internal_knowledge": {"report": "INTERNAL_FACTS"},
                },
            },
            {"configurable": {"research_model": "openai:fixture-risk", "research_model_max_tokens": 900}},
        )
    )
    [request] = requests
    assert request.model_ref == "openai:fixture-risk" and request.max_tokens == 900
    assert request.schema_name is None and request.tools == []
    data = request.messages[-1]["content"]
    assert all(text in data for text in (public_report, "INTERNAL_FACTS", "APPROVED_BRIEF", "REVIEW_GAP"))
    assert result["agents"] == {"risk_assessment": {"report": "verified claims and risk register"}}
    assert result["runtime"]["budget"]["model_calls"] == 1
    assert result["runtime"]["budget"]["total_tokens"] == 10


# 验证策略角色收到完整风险报告。
def test_response_strategy_uses_full_risk_report(monkeypatch) -> None:
    risk_report = "R" * 13_000 + "\nRISK_TAIL"
    requests = install_sequence(monkeypatch, "response priorities and monitoring")
    result = asyncio.run(
        research.response_strategy(
            {
                "workflow": {"brief": "APPROVED_BRIEF", "round": 1},
                "agents": {
                    "public_signal": {"report": "PUBLIC_EVIDENCE"},
                    "internal_knowledge": {"report": "INTERNAL_PLAYBOOK"},
                    "risk_assessment": {"report": risk_report},
                },
            },
            {"configurable": {"research_model": "openai:fixture-strategy"}},
        )
    )
    [request] = requests
    assert request.model_ref == "openai:fixture-strategy"
    assert request.schema_name is None and request.tools == []
    data = request.messages[-1]["content"]
    assert all(text in data for text in (risk_report, "PUBLIC_EVIDENCE", "INTERNAL_PLAYBOOK", "APPROVED_BRIEF"))
    assert result["agents"] == {"response_strategy": {"report": "response priorities and monitoring"}}
    assert result["runtime"]["budget"]["model_calls"] == 1


@pytest.mark.parametrize("analyze", [research.risk_assessment, research.response_strategy])
def test_disabled_analysis_does_not_open_a_model(monkeypatch, analyze):
    requests = install_sequence(monkeypatch)
    assert asyncio.run(analyze({}, _public_opinion_config("public_signal"))) == {}
    assert not requests


# 验证综合报告使用上下文容量内的完整角色报告。
def test_final_report_reads_formal_role_reports(monkeypatch) -> None:
    full_report = "F" * 6_000 + "\nREPORT_TAIL"
    requests = install_sequence(monkeypatch, "final report")
    result = asyncio.run(
        reporting.generate_final_report(
            {
                "messages": [],
                "workflow": {"brief": "brand risk"},
                "agents": {"public_signal": {"report": full_report}},
                "report": {},
                "runtime": {"budget": {}},
            },
            {"configurable": {"final_report_model": "openai:fixture-model"}},
        )
    )
    assert "REPORT_TAIL" in requests[0].prompt
    assert result["report"]["final"] == "final report"
