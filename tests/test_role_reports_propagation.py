"""Regression tests for complete public-opinion role-report propagation."""

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage

import open_deep_research.deep_researcher as deep_researcher_module
import open_deep_research.runtime.business_agent as business_agent_module
from open_deep_research.state import ResearchReview, Section, agents_reducer


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

    async def fake_tools(_config, _role, _spec=None):
        return [SimpleNamespace(name="web_search")]

    async def fake_compress(*_args, **_kwargs):
        return full_report_body, [], {}

    monkeypatch.setattr(business_agent_module, "_CONFIGURABLE_MODEL", FakeModel())
    monkeypatch.setattr(business_agent_module, "_business_agent_tools", fake_tools)
    monkeypatch.setattr(business_agent_module, "_compress_research", fake_compress)

    result = asyncio.run(
        business_agent_module.run_business_agent(
            state={
                "workflow": {"brief": "brand risk", "round": 1},
                "agents": {},
                "research": {"working_contexts": {}},
                "runtime": {"budget": {}},
            },
            config=_public_opinion_config("public_signal"),
            role="public_signal",
        )
    )

    formal_report = result["agents"]["public_signal"]["report"]
    private_memory = result["agents"]["public_signal"]["memory"][0]["content"]
    assert full_report_body in formal_report
    assert len(formal_report) > 1_800
    assert len(private_memory) < len(formal_report)
    assert "COMPLETE_REPORT_TAIL" not in private_memory


def test_business_agent_preserves_private_memory_and_rolling_summary(monkeypatch) -> None:
    """The assembly entry keeps agent-owned dynamic context and summary state."""
    captured_messages = []

    class CapturingModel:
        def bind_tools(self, _tools):
            return self

        def with_retry(self, **_kwargs):
            return self

        def with_config(self, _config):
            return self

        async def ainvoke(self, messages):
            captured_messages.extend(messages)
            return AIMessage(content="done")

    async def fake_tools(_config, _role, _spec=None):
        return [SimpleNamespace(name="web_search")]

    async def fake_compress(*_args, **_kwargs):
        return "report", [], {"model_calls": 1}

    monkeypatch.setattr(business_agent_module, "_CONFIGURABLE_MODEL", CapturingModel())
    monkeypatch.setattr(business_agent_module, "_business_agent_tools", fake_tools)
    monkeypatch.setattr(business_agent_module, "_compress_research", fake_compress)

    result = asyncio.run(
        business_agent_module.run_business_agent(
            role="public_signal",
            state={
                "workflow": {"brief": "brand risk", "round": 1},
                "agents": {
                    "public_signal": {
                        "memory": [{"content": "PRIVATE_SENTINEL"}],
                        "rolling_summary": "PRIOR_SUMMARY",
                    }
                },
                "research": {"working_contexts": {}},
                "runtime": {"budget": {}},
            },
            config=_public_opinion_config("public_signal"),
        )
    )

    assert sum("PRIVATE_SENTINEL" in str(message.content) for message in captured_messages) == 1
    assert result["agents"]["public_signal"]["rolling_summary"] == "PRIOR_SUMMARY"
    assert result["runtime"]["budget"]["model_calls"] >= 1


def test_parallel_role_reports_merge_by_role() -> None:
    """Concurrent partial updates retain every role's complete report."""
    merged = agents_reducer(
        {}, {"public_signal": {"report": "PUBLIC_SIGNAL_FULL"}},
    )
    merged = agents_reducer(
        merged,
        {"internal_knowledge": {"report": "INTERNAL_KNOWLEDGE_FULL"}},
    )

    assert merged == {
        "public_signal": {"report": "PUBLIC_SIGNAL_FULL"},
        "internal_knowledge": {"report": "INTERNAL_KNOWLEDGE_FULL"},
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

    async def fake_agent(*, state, config, role):
        del config
        seen_states[role] = {
            name: value.get("report", "")
            for name, value in state.get("agents", {}).items()
        }
        return {
            "agents": {role: {"report": reports[role], "memory": [{"content": reports[role][:1_800]}]}},
            "runtime": {"budget": {}},
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

    monkeypatch.setattr(deep_researcher_module, "run_business_agent", fake_agent)
    monkeypatch.setattr(deep_researcher_module, "configurable_model", FakeReviewModel())
    result = asyncio.run(
        deep_researcher_module.public_opinion_subgraph.ainvoke(
            {
                "messages": [],
                "workflow": {"brief": "brand risk", "round": 1, "review": None,
                             "pending_tasks": [], "completed_tasks": []},
                "agents": {},
                "research": {"working_contexts": {}},
                "report": {},
                "runtime": {"budget": {}, "metrics": {}},
            },
            _public_opinion_config(*reports),
        )
    )

    assert {role: value["report"] for role, value in result["agents"].items()} == reports
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
    prompt = business_agent_module._build_business_agent_assignment(
        {
            "workflow": {"brief": "brand risk", "round": 1},
            "agents": {
                "public_signal": {"report": public_signal_report, "memory": [
                    {"content": public_signal_report[:1_800] + "\n[truncated]"}
                ]},
                "internal_knowledge": {"report": "INTERNAL_FACTS", "memory": []},
            },
        },
        "risk_assessment",
    )

    assert "CRITICAL_EVIDENCE_AT_END" in prompt


def test_response_strategy_assignment_uses_full_risk_report() -> None:
    """Response strategy receives the complete risk assessment output."""
    risk_report = "R" * 1_800 + "HIGH_PRIORITY_RESPONSE_ACTION"
    prompt = business_agent_module._build_business_agent_assignment(
        {
            "workflow": {"brief": "brand risk", "round": 1},
            "agents": {
                "public_signal": {"report": "PUBLIC_SIGNAL_FULL"},
                "internal_knowledge": {"report": "INTERNAL_FACTS"},
                "risk_assessment": {"report": risk_report, "memory": [
                    {"content": risk_report[:1_800] + "\n[truncated]"}
                ]},
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
                "report": {"sections": [
                    Section(
                        name="Risk evidence",
                        description="Summarize risk evidence.",
                        research=True,
                        agent_role="risk_assessment",
                    )
                ]},
                "agents": {
                    "risk_assessment": {"report": full_report, "memory": [
                        {"content": full_report[:1_800] + "\n[truncated]"}
                    ]}
                },
                "runtime": {"budget": {}},
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
            assert payload["agents"] == {}
            return {
                "workflow": payload["workflow"],
                "agents": {
                    "public_signal": {"report": full_report, "memory": [
                        {"content": full_report[:1_800] + "\n[truncated]"}
                    ]}
                },
                "research": payload["research"],
                "runtime": {"budget": {}, "metrics": {}},
            }

    monkeypatch.setattr(deep_researcher_module, "public_opinion_subgraph", FakeSubgraph())

    result = asyncio.run(
        deep_researcher_module.research_phase(
            {
                "messages": [],
                "workflow": {"brief": "brand risk"},
                "agents": {},
                "research": {},
                "report": {},
                "runtime": {"budget": {}},
            },
            _public_opinion_config("public_signal"),
        )
    )

    agents_update = result["agents"]
    assert agents_update["type"] == "override"
    assert agents_update["value"]["public_signal"]["report"] == full_report
    assert len(agents_update["value"]["public_signal"]["report"]) == 4_021
    assert agents_update["value"]["public_signal"]["memory"][0]["content"].endswith(
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
                    "agents": {
                        "risk_assessment": {"report": full_report, "memory": [
                            {"content": "compact memory only"}
                        ]}
                    },
                    "messages": [],
                    "workflow": {"brief": "brand risk"},
                    "runtime": {"budget": {}},
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
