# 验证精简舆情角色规范、私有记忆和完整报告的状态传播。
"""Tests for public-opinion business-agent encapsulation."""

import asyncio

from sdk_model_support import install_sequence

from open_deep_research.configuration import Configuration
from open_deep_research.public_opinion_agents import (
    PUBLIC_OPINION_AGENT_ORDER,
    PUBLIC_OPINION_AGENT_SPECS,
    get_public_opinion_agent_spec,
)
from open_deep_research.state import (
    Section,
    agents_reducer,
)
from open_deep_research.workflow import reporting


# 验证注册表只包含当前精简角色。
def test_public_opinion_agent_registry_has_compact_roles() -> None:
    """Only the research roles have SDK Agent specs."""
    assert PUBLIC_OPINION_AGENT_ORDER == (
        "public_signal",
        "internal_knowledge",
    )
    assert set(PUBLIC_OPINION_AGENT_SPECS) == set(PUBLIC_OPINION_AGENT_ORDER)


# 验证历史角色配置映射到当前角色体系。
def test_legacy_public_opinion_roles_are_mapped_to_compact_roles() -> None:
    """Old seven-agent role configs remain compatible."""
    config = Configuration(
        business_scenario="public_opinion_risk",
        enabled_business_agents=[
            "news_intelligence",
            "social_sentiment",
            "internal_knowledge",
            "fact_verification",
            "competitor_impact",
            "compliance_risk",
            "pr_strategy",
        ],
    )

    assert config.enabled_business_agents == [
        "public_signal",
        "internal_knowledge",
        "risk_assessment",
        "response_strategy",
    ]


# 验证每个角色拥有独立提示词、契约及工具策略。
def test_each_public_opinion_agent_owns_prompt_contract_and_policy() -> None:
    """Each agent spec renders a dedicated prompt with contract and policy sections."""
    for role in PUBLIC_OPINION_AGENT_ORDER:
        spec = get_public_opinion_agent_spec(role)
        prompt = spec.format_system_prompt(
            retrieval_tool_prompt="Role-specific tool whitelist: test.",
            mcp_prompt="",
            date="June 3, 2026",
            organization_context="Test organization context.",
        )

        assert spec.role in prompt
        assert spec.display_name in prompt
        assert "<Private Agent Memory>" not in prompt
        assert "<Input Contract>" in prompt
        assert "<Tool Policy>" in prompt
        assert "<Report Policy>" in prompt
        assert "<Execution Strategy>" in prompt
        assert "<Output Schema>" in prompt
        assert spec.expected_output in prompt


# 验证角色状态合并保持每个角色的报告隔离。
def test_agents_reducer_keeps_reports_by_role() -> None:
    """Role updates cannot overwrite another role's report."""
    current = {
        "public_signal": {"report": "old public report"},
        "risk_assessment": {"report": "old risk report"},
    }
    update = {
        "public_signal": {"report": "new public report"},
        "response_strategy": {"report": "new response report"},
    }

    merged = agents_reducer(current, update)

    assert merged["public_signal"]["report"] == (
        "old public report\n\n--- Additional role research report ---\nnew public report"
    )
    assert merged["risk_assessment"]["report"] == "old risk report"
    assert merged["response_strategy"]["report"] == "new response report"


# 验证章节写作使用完整正式角色报告。
def test_section_writer_uses_full_role_report(monkeypatch) -> None:
    """Formal role reports, rather than compact memory, reach the writer prompt."""

    requests = install_sequence(monkeypatch, "section output")
    full_report = "A" * 2_000 + "\nTAIL_EVIDENCE_MUST_REACH_SECTION_WRITER"
    state = {
        "report": {
            "sections": [
                Section(
                    name="Risk evidence",
                    description="Summarize risk evidence.",
                    research=True,
                    agent_role="public_signal",
                )
            ]
        },
        "agents": {
            "public_signal": {"report": full_report}
        },
        "runtime": {"budget": {}},
    }

    asyncio.run(
        reporting.section_writer(
            state,
            {
                "configurable": {
                    "section_writer_model": "openai:fixture-model",
                                }
            },
        )
    )

    assert requests
    assert "TAIL_EVIDENCE_MUST_REACH_SECTION_WRITER" in requests[0].prompt

def test_role_reports_override_previous_run() -> None:
    """A new research phase replaces prior formal reports instead of merging them."""

    merged = agents_reducer(
        {
            "public_signal": {"report": "OLD_REPORT"},
            "risk_assessment": {"report": "STALE_REPORT"},
        },
        {
            "type": "override",
            "value": {"public_signal": {"report": "NEW_REPORT"}},
        },
    )

    assert merged == {"public_signal": {"report": "NEW_REPORT"}}
