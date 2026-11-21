"""Regression tests for complete public-opinion role-report propagation."""

import asyncio
from contextlib import asynccontextmanager

from agents.testing import ScriptedModel, assistant_message
from sdk_model_support import install_sequence
from test_sdk_runtime import _tool

import open_deep_research.runtime.business_agent as business_agent_module
from open_deep_research.state import Section, agents_reducer
from open_deep_research.workflow import reporting


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
        return full_report_body, [], {}

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
def test_risk_assessment_assignment_uses_full_upstream_reports() -> None:
    public_report = "P" * 13_000 + "\nPUBLIC_TAIL"
    assignment = business_agent_module._build_business_agent_assignment(
        {
            "workflow": {"brief": "brand risk", "round": 1},
            "agents": {
                "public_signal": {"report": public_report},
                "internal_knowledge": {"report": "INTERNAL_FACTS"},
            },
        },
        "risk_assessment",
    )
    assert "PUBLIC_TAIL" in assignment
    assert "INTERNAL_FACTS" in assignment


# 验证策略角色收到完整风险报告。
def test_response_strategy_assignment_uses_full_risk_report() -> None:
    risk_report = "R" * 13_000 + "\nRISK_TAIL"
    assignment = business_agent_module._build_business_agent_assignment(
        {
            "workflow": {"brief": "brand risk", "round": 1},
            "agents": {
                "public_signal": {"report": "PUBLIC"},
                "internal_knowledge": {"report": "INTERNAL"},
                "risk_assessment": {"report": risk_report},
            },
        },
        "response_strategy",
    )
    assert "RISK_TAIL" in assignment


# 验证章节写作使用完整正式报告。
def test_section_writer_uses_full_role_report(monkeypatch) -> None:
    full_report = "S" * 5_000 + "\nSECTION_TAIL"
    requests = install_sequence(monkeypatch, "section body")
    result = asyncio.run(
        reporting.section_writer(
            {
                "report": {
                    "sections": [
                        Section(
                            name="Risk",
                            description="risk evidence",
                            research=True,
                            agent_role="public_signal",
                        )
                    ],
                    "completed_sections": [],
                },
                "agents": {"public_signal": {"report": full_report}},
                "runtime": {"budget": {}},
            },
            {"configurable": {"section_writer_model": "openai:fixture-model"}},
        )
    )
    assert "SECTION_TAIL" in requests[0].prompt
    assert result["report"]["completed_sections"][0].content == "section body"


# 验证最终报告兜底使用完整角色报告。
def test_final_report_fallback_reads_formal_role_reports(monkeypatch) -> None:
    full_report = "F" * 6_000 + "\nFALLBACK_TAIL"
    requests = install_sequence(monkeypatch, "final report")
    result = asyncio.run(
        reporting._fallback_report_generation(
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
    assert "FALLBACK_TAIL" in requests[0].prompt
    assert result["report"]["final"] == "final report"
