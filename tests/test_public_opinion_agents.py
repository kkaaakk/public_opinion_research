"""Tests for public-opinion business-agent encapsulation."""

import asyncio
from types import SimpleNamespace

import open_deep_research.deep_researcher as deep_researcher_module
from open_deep_research.configuration import Configuration
from open_deep_research.public_opinion_agents import (
    PUBLIC_OPINION_AGENT_ORDER,
    PUBLIC_OPINION_AGENT_SPECS,
    get_public_opinion_agent_spec,
)
from open_deep_research.state import (
    Section,
    agent_memories_reducer,
    role_reports_reducer,
)


def test_public_opinion_agent_registry_has_compact_roles() -> None:
    """Public-opinion workflow exposes four compact business agents."""
    assert PUBLIC_OPINION_AGENT_ORDER == (
        "public_signal",
        "internal_knowledge",
        "risk_assessment",
        "response_strategy",
    )
    assert set(PUBLIC_OPINION_AGENT_SPECS) == set(PUBLIC_OPINION_AGENT_ORDER)


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


def test_each_public_opinion_agent_owns_prompt_contract_and_policy() -> None:
    """Each agent spec renders a dedicated prompt with contract and policy sections."""
    for role in PUBLIC_OPINION_AGENT_ORDER:
        spec = get_public_opinion_agent_spec(role)
        prompt = spec.format_system_prompt(
            retrieval_tool_prompt="Role-specific tool whitelist: test.",
            mcp_prompt="",
            date="June 3, 2026",
            organization_context="Test organization context.",
            private_memory_context="Previous private note for this agent.",
        )

        assert spec.role in prompt
        assert spec.display_name in prompt
        assert "<Private Agent Memory>" in prompt
        assert "Previous private note for this agent." in prompt
        assert "<Input Contract>" in prompt
        assert "<Tool Policy>" in prompt
        assert "<Memory Policy>" in prompt
        assert "<Execution Strategy>" in prompt
        assert "<Output Schema>" in prompt
        assert spec.expected_output in prompt


def test_agent_memories_reducer_keeps_private_memory_by_role() -> None:
    """Private agent memory is appended under each role without cross-role mixing."""
    current = {
        "public_signal": [{"content": "old public memory"}],
        "risk_assessment": [{"content": "old risk memory"}],
    }
    update = {
        "public_signal": [{"content": "new public memory"}],
        "response_strategy": {"content": "new response memory"},
    }

    merged = agent_memories_reducer(current, update)

    assert [entry["content"] for entry in merged["public_signal"]] == [
        "old public memory",
        "new public memory",
    ]
    assert [entry["content"] for entry in merged["risk_assessment"]] == [
        "old risk memory",
    ]
    assert [entry["content"] for entry in merged["response_strategy"]] == [
        "new response memory",
    ]


def test_section_writer_uses_full_role_report(monkeypatch) -> None:
    """Formal role reports, rather than compact memory, reach the writer prompt."""

    captured_prompts: list[str] = []

    class CapturingModel:
        def with_config(self, _config):
            return self

        async def ainvoke(self, messages):
            captured_prompts.append(str(messages[0].content))
            return SimpleNamespace(content="section output")

    monkeypatch.setattr(deep_researcher_module, "configurable_model", CapturingModel())
    full_report = "A" * 2_000 + "\nTAIL_EVIDENCE_MUST_REACH_SECTION_WRITER"
    compact_memory = full_report[:1_800] + "\n[truncated]"
    state = {
        "sections": [
            Section(
                name="Risk evidence",
                description="Summarize risk evidence.",
                research=True,
                agent_role="public_signal",
            )
        ],
        "role_reports": {"public_signal": full_report},
        "agent_memories": {
            "public_signal": [
                {
                    "source": "current_public_opinion_run",
                    "content": compact_memory,
                }
            ]
        },
        "budget_usage": {},
    }

    asyncio.run(
        deep_researcher_module.section_writer(
            state,
            {"configurable": {"section_writer_model": "fixture:model"}},
        )
    )

    assert captured_prompts
    assert "TAIL_EVIDENCE_MUST_REACH_SECTION_WRITER" in captured_prompts[0]


def test_private_memory_remains_compact() -> None:
    """Private memory keeps its bounded representation after P0-2."""

    report = "B" * 2_000
    memory = deep_researcher_module._build_agent_private_memory("public_signal", report, [])

    assert len(memory["content"]) <= 1_820
    assert memory["content"].endswith("[truncated]")


def test_role_reports_override_previous_run() -> None:
    """A new research phase replaces prior formal reports instead of merging them."""

    merged = role_reports_reducer(
        {"public_signal": "OLD_REPORT", "risk_assessment": "STALE_REPORT"},
        {
            "type": "override",
            "value": {"public_signal": "NEW_REPORT"},
        },
    )

    assert merged == {"public_signal": "NEW_REPORT"}
