"""Regression tests for complete public-opinion role-report propagation."""

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage

import open_deep_research.deep_researcher as deep_researcher_module
from open_deep_research.state import ResearchReview, Section, role_reports_reducer


def _public_opinion_config(*roles: str) -> dict:
    return {
        "configurable": {
            "agent_observer_enabled": False,
            "enabled_business_agents": list(roles),
        }
    }


def test_full_role_report_is_not_replaced_by_compact_memory(monkeypatch) -> None:
    """A long formal report stays complete while its private memory is bounded."""
    full_report_body = "A" * 5_000 + "\nCOMPLETE_REPORT_TAIL"

    class FakeModel:
        def bind_tools(self, _tools):
            return self

        def with_retry(self, **_kwargs):
            return self

        def with_config(self, _config):
            return self

        async def ainvoke(self, _messages):
            return AIMessage(content="agent step")

    async def fake_tools(_config, _role):
        return [SimpleNamespace(name="web_search")]

    async def fake_compress(_state, _config):
        return {
            "compressed_research": full_report_body,
            "raw_notes": [],
            "budget_usage": {},
        }

    monkeypatch.setattr(deep_researcher_module, "configurable_model", FakeModel())
    monkeypatch.setattr(deep_researcher_module, "_business_agent_tools", fake_tools)
    monkeypatch.setattr(deep_researcher_module, "compress_research", fake_compress)

    result = asyncio.run(
        deep_researcher_module._run_public_opinion_agent(
            {
                "research_brief": "brand risk",
                "role_reports": {},
                "agent_memories": {},
                "budget_usage": {},
            },
            _public_opinion_config("public_signal"),
            "public_signal",
        )
    )

    formal_report = result["role_reports"]["public_signal"]
    private_memory = result["agent_memories"]["public_signal"][0]["content"]
    assert full_report_body in formal_report
    assert len(formal_report) > 1_800
    assert len(private_memory) < len(formal_report)
    assert "COMPLETE_REPORT_TAIL" not in private_memory


def test_parallel_role_reports_merge_by_role() -> None:
    """Concurrent partial updates retain every role's complete report."""
    merged = role_reports_reducer(
        {},
        {"public_signal": "PUBLIC_SIGNAL_FULL"},
    )
    merged = role_reports_reducer(
        merged,
        {"internal_knowledge": "INTERNAL_KNOWLEDGE_FULL"},
    )

    assert merged == {
        "public_signal": "PUBLIC_SIGNAL_FULL",
        "internal_knowledge": "INTERNAL_KNOWLEDGE_FULL",
    }


def test_public_opinion_subgraph_keeps_full_reports_for_downstream_agents(monkeypatch) -> None:
    """The compiled subgraph applies the report reducer between agent stages."""
    reports = {
        "public_signal": "P" * 2_000 + "PUBLIC_SIGNAL_TAIL",
        "internal_knowledge": "I" * 2_000 + "INTERNAL_KNOWLEDGE_TAIL",
        "risk_assessment": "R" * 2_000 + "RISK_ASSESSMENT_TAIL",
        "response_strategy": "S" * 2_000 + "RESPONSE_STRATEGY_TAIL",
    }
    seen_states: dict[str, dict[str, str]] = {}

    async def fake_agent(state, _config, role):
        seen_states[role] = dict(state.get("role_reports", {}))
        return {
            "role_reports": {role: reports[role]},
            "agent_memories": {role: [{"content": reports[role][:1_800]}]},
            "notes": [],
            "raw_notes": [],
            "budget_usage": {},
        }

    class FakeReviewModel:
        def with_structured_output(self, _schema):
            return self

        def with_retry(self, **_kwargs):
            return self

        def with_config(self, _config):
            return self

        async def ainvoke(self, _messages):
            return ResearchReview(research_complete=True)

    monkeypatch.setattr(deep_researcher_module, "_run_public_opinion_agent", fake_agent)
    monkeypatch.setattr(deep_researcher_module, "configurable_model", FakeReviewModel())
    result = asyncio.run(
        deep_researcher_module.public_opinion_subgraph.ainvoke(
            {
                "messages": [],
                "research_brief": "brand risk",
                "role_reports": {},
                "agent_memories": {},
                "notes": [],
                "raw_notes": [],
                "budget_usage": {},
                "research_round": 1,
                "research_mode": "initial",
                "research_review": None,
                "current_research_tasks": [],
                "completed_research_tasks": [],
            },
            _public_opinion_config(*reports),
        )
    )

    assert result["role_reports"] == reports
    assert seen_states["risk_assessment"]["public_signal"] == reports["public_signal"]
    assert seen_states["risk_assessment"]["internal_knowledge"] == reports[
        "internal_knowledge"
    ]
    assert seen_states["response_strategy"]["risk_assessment"] == reports[
        "risk_assessment"
    ]


def test_risk_assessment_assignment_uses_full_upstream_reports() -> None:
    """Risk assessment receives evidence after the memory truncation boundary."""
    public_signal_report = "A" * 1_800 + "CRITICAL_EVIDENCE_AT_END"
    prompt = deep_researcher_module._build_public_opinion_agent_assignment(
        {
            "research_brief": "brand risk",
            "role_reports": {
                "public_signal": public_signal_report,
                "internal_knowledge": "INTERNAL_FACTS",
            },
            "agent_memories": {
                "public_signal": [
                    {"content": public_signal_report[:1_800] + "\n[truncated]"}
                ]
            },
        },
        "risk_assessment",
    )

    assert "CRITICAL_EVIDENCE_AT_END" in prompt


def test_response_strategy_assignment_uses_full_risk_report() -> None:
    """Response strategy receives the complete risk assessment output."""
    risk_report = "R" * 1_800 + "HIGH_PRIORITY_RESPONSE_ACTION"
    prompt = deep_researcher_module._build_public_opinion_agent_assignment(
        {
            "research_brief": "brand risk",
            "role_reports": {
                "public_signal": "PUBLIC_SIGNAL_FULL",
                "internal_knowledge": "INTERNAL_FACTS",
                "risk_assessment": risk_report,
            },
            "agent_memories": {
                "risk_assessment": [
                    {"content": risk_report[:1_800] + "\n[truncated]"}
                ]
            },
        },
        "response_strategy",
    )

    assert "HIGH_PRIORITY_RESPONSE_ACTION" in prompt


def test_section_writer_prefers_full_role_report_over_memory(monkeypatch) -> None:
    """Section writing uses formal role reports even when memory omits the tail."""
    captured_prompts: list[str] = []

    class CapturingModel:
        def with_config(self, _config):
            return self

        async def ainvoke(self, messages):
            captured_prompts.append(str(messages[0].content))
            return SimpleNamespace(content="section output")

    full_report = "A" * 3_000 + "FINAL_RISK_EVIDENCE"
    monkeypatch.setattr(deep_researcher_module, "configurable_model", CapturingModel())

    asyncio.run(
        deep_researcher_module.section_writer(
            {
                "sections": [
                    Section(
                        name="Risk evidence",
                        description="Summarize risk evidence.",
                        research=True,
                        agent_role="risk_assessment",
                    )
                ],
                "role_reports": {"risk_assessment": full_report},
                "agent_memories": {
                    "risk_assessment": [
                        {"content": full_report[:1_800] + "\n[truncated]"}
                    ]
                },
                "budget_usage": {},
            },
            {"configurable": {"section_writer_model": "fixture:model"}},
        )
    )

    assert captured_prompts
    assert "FINAL_RISK_EVIDENCE" in captured_prompts[0]


def test_research_phase_propagates_subgraph_role_reports(monkeypatch) -> None:
    """The research wrapper forwards both formal reports and compact memories."""
    full_report = "P" * 4_000 + "\nSUBGRAPH_REPORT_TAIL"

    class FakeSubgraph:
        async def ainvoke(self, payload, _config):
            assert payload["role_reports"] == {}
            return {
                "role_reports": {"public_signal": full_report},
                "agent_memories": {
                    "public_signal": [{"content": full_report[:1_800] + "\n[truncated]"}]
                },
                "notes": [],
                "raw_notes": [],
                "budget_usage": {},
            }

    monkeypatch.setattr(deep_researcher_module, "public_opinion_subgraph", FakeSubgraph())

    result = asyncio.run(
        deep_researcher_module.research_phase(
            {
                "messages": [],
                "research_brief": "brand risk",
                "agent_memories": {},
                "budget_usage": {},
            },
            _public_opinion_config("public_signal"),
        )
    )

    role_reports_update = result["role_reports"]
    assert role_reports_update["type"] == "override"
    assert role_reports_update["value"]["public_signal"] == full_report
    assert len(role_reports_update["value"]["public_signal"]) == 4_021
    assert result["agent_memories"]["value"]["public_signal"][0]["content"].endswith(
        "[truncated]"
    )


def test_final_report_fallback_reads_formal_role_reports(monkeypatch) -> None:
    """The final-report fallback also receives the complete formal reports."""
    captured_prompts: list[str] = []

    class CapturingModel:
        def with_config(self, _config):
            return self

        async def ainvoke(self, messages):
            captured_prompts.append(str(messages[0].content))
            return SimpleNamespace(content="final report")

    monkeypatch.setattr(deep_researcher_module, "configurable_model", CapturingModel())
    full_report = "A" * 3_000 + "FINAL_REPORT_EVIDENCE"
    asyncio.run(
        deep_researcher_module._fallback_report_generation(
            {
                "role_reports": {"risk_assessment": full_report},
                "agent_memories": {
                    "risk_assessment": [{"content": "compact memory only"}]
                },
                "notes": ["legacy note without the final evidence"],
                "messages": [],
                "research_brief": "brand risk",
                "budget_usage": {},
            },
            {
                "configurable": {
                    "final_report_model": "fixture:model",
                    "rag_memory_write_enabled": False,
                }
            },
        )
    )

    assert captured_prompts
    assert "FINAL_REPORT_EVIDENCE" in captured_prompts[0]
