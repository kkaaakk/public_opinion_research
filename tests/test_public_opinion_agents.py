# 验证精简舆情角色规范、私有记忆和完整报告的状态传播。
"""Tests for public-opinion business-agent encapsulation."""

import asyncio
from types import SimpleNamespace

import open_deep_research.deep_researcher as deep_researcher_module
import open_deep_research.workflow.shared as workflow_shared
from open_deep_research.configuration import Configuration
from open_deep_research.public_opinion_agents import (
    PUBLIC_OPINION_AGENT_ORDER,
    PUBLIC_OPINION_AGENT_SPECS,
    get_public_opinion_agent_spec,
)
from open_deep_research.runtime import AgentRuntime
from open_deep_research.state import (
    Section,
    agents_reducer,
)


# 验证注册表只包含当前精简角色。
def test_public_opinion_agent_registry_has_compact_roles() -> None:
    """Public-opinion workflow exposes four compact business agents."""
    assert PUBLIC_OPINION_AGENT_ORDER == (
        "public_signal",
        "internal_knowledge",
        "risk_assessment",
        "response_strategy",
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
        assert "<Memory Policy>" in prompt
        assert "<Execution Strategy>" in prompt
        assert "<Output Schema>" in prompt
        assert spec.expected_output in prompt


# 验证角色状态合并保持私有记忆隔离。
def test_agents_reducer_keeps_private_memory_by_role() -> None:
    """Private agent memory is appended under each role without cross-role mixing."""
    current = {
        "public_signal": {"memory": [{"content": "old public memory"}]},
        "risk_assessment": {"memory": [{"content": "old risk memory"}]},
    }
    update = {
        "public_signal": {"memory": [{"content": "new public memory"}]},
        "response_strategy": {"memory": [{"content": "new response memory"}]},
    }

    merged = agents_reducer(current, update)

    assert [entry["content"] for entry in merged["public_signal"]["memory"]] == [
        "old public memory",
        "new public memory",
    ]
    assert [entry["content"] for entry in merged["risk_assessment"]["memory"]] == [
        "old risk memory",
    ]
    assert [entry["content"] for entry in merged["response_strategy"]["memory"]] == [
        "new response memory",
    ]


# 验证章节写作使用完整正式角色报告。
def test_section_writer_uses_full_role_report(monkeypatch) -> None:
    """Formal role reports, rather than compact memory, reach the writer prompt."""

    captured_prompts: list[str] = []

    # 记录输入提示或消息，并返回固定报告以检查证据传递。
    class CapturingModel:
        # 接收运行配置并返回测试模型，兼容真实调用链。
        def with_config(self, _config):
            return self

        # 记录模型实际输入，并返回预设章节或报告消息。
        async def ainvoke(self, messages, config=None):
            captured_prompts.append(str(messages[0].content))
            return SimpleNamespace(content="section output")

    monkeypatch.setattr(workflow_shared, "configurable_model", CapturingModel())
    full_report = "A" * 2_000 + "\nTAIL_EVIDENCE_MUST_REACH_SECTION_WRITER"
    compact_memory = full_report[:1_800] + "\n[truncated]"
    state = {
        "report": {"sections": [Section(
                name="Risk evidence",
                description="Summarize risk evidence.",
                research=True,
                agent_role="public_signal",
            )]},
        "agents": {
            "public_signal": {"report": full_report, "memory": [
                {
                    "source": "current_public_opinion_run",
                    "content": compact_memory,
                }
            ]}
        },
        "runtime": {"budget": {}},
    }

    asyncio.run(
        deep_researcher_module.section_writer(
            state,
            {"configurable": {"section_writer_model": "fixture:model", "research_graph_enabled": False}},
        )
    )

    assert captured_prompts
    assert "TAIL_EVIDENCE_MUST_REACH_SECTION_WRITER" in captured_prompts[0]


# 验证短期记忆受大小限制。
def test_private_memory_remains_compact() -> None:
    """Private memory keeps its bounded representation after P0-2."""

    report = "B" * 2_000
    memory = AgentRuntime.build_private_memory("public_signal", report, [])

    assert len(memory["content"]) <= 1_820
    assert memory["content"].endswith("[truncated]")


# 验证新运行报告按规则替换上一运行输出。
def test_role_reports_override_previous_run() -> None:
    """A new research phase replaces prior formal reports instead of merging them."""

    merged = agents_reducer(
        {"public_signal": {"report": "OLD_REPORT"}, "risk_assessment": {"report": "STALE_REPORT"}},
        {
            "type": "override",
            "value": {"public_signal": {"report": "NEW_REPORT"}},
        },
    )

    assert merged == {"public_signal": {"report": "NEW_REPORT"}}
