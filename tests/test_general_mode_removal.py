"""Tests verifying that general research mode has been fully removed.

Covers:
1. Unique mode: no general/generic/deep_research mode accepted
2. Graph structure: no standalone supervisor/researcher/mode-router nodes
3. Configuration: mode field not required
4. API: mode-free request works
5. State: no ConductResearch or general-mode state fields
"""

import pytest

from open_deep_research.configuration import Configuration
from open_deep_research.deep_researcher import (
    deep_researcher_graph,
    public_opinion_builder,
)
from open_deep_research.state import (
    AgentState,
    PublicOpinionState,
    ResearchQuestion,
)

# ── 1. Unique mode tests ──────────────────────────────────────────────


class TestUniqueMode:
    """Verify the project no longer accepts or depends on general research mode."""

    def test_configuration_default_is_public_opinion(self):
        """Default business_scenario is public_opinion_risk."""
        config = Configuration()
        assert config.business_scenario == "public_opinion_risk"

    def test_general_research_scenario_rejected(self):
        """Creating config with general_research raises ValueError."""
        with pytest.raises(ValueError, match="public_opinion_risk"):
            Configuration(business_scenario="general_research")

    def test_generic_research_scenario_rejected(self):
        """Any non-public-opinion scenario is rejected."""
        with pytest.raises(ValueError):
            Configuration(business_scenario="generic_research")

    def test_deep_research_scenario_rejected(self):
        """deep_research is not a valid business_scenario."""
        with pytest.raises(ValueError):
            Configuration(business_scenario="deep_research")

    def test_general_research_not_in_business_agent_role(self):
        """BusinessAgentRole no longer contains general_research."""
        from open_deep_research.state import BusinessAgentRole
        # Literal type — extract allowed values
        allowed = set(BusinessAgentRole.__args__)
        assert "general_research" not in allowed
        assert "public_signal" in allowed
        assert "risk_assessment" in allowed

    def test_enabled_agents_no_general_research(self):
        """enabled_business_agents rejects general_research entries."""
        with pytest.raises(ValueError, match="unsupported"):
            Configuration(enabled_business_agents=["general_research"])


# ── 2. Graph structure tests ──────────────────────────────────────────


class TestGraphStructure:
    """Verify the main graph no longer contains general-mode nodes."""

    def test_no_standalone_supervisor_node(self):
        """Graph should not have a standalone 'supervisor' node (old general-mode).

        The public-opinion subgraph is entered through the research_phase node;
        there is no supervisor agent in the current workflow.
        """
        node_names = set(deep_researcher_graph.get_graph().nodes)
        assert "supervisor" not in node_names
        assert "research_phase" in node_names

    def test_no_standalone_researcher_node(self):
        """Graph should not have a standalone 'researcher' node (old general-mode).
        
        The old general-mode had separate supervisor/researcher nodes.
        Now research_phase delegates directly to public_opinion_subgraph.
        """
        node_names = set(deep_researcher_graph.get_graph().nodes)
        assert "researcher" not in node_names
        # No separate researcher node exists; PO agents run inside research_phase

    def test_no_mode_router(self):
        """Graph should not contain any mode-routing conditional edges."""
        node_names = set(deep_researcher_graph.get_graph().nodes)
        for name in node_names:
            assert "route" not in str(name).lower() or "enrich" in str(name).lower()

    def test_no_general_mode_conditional_branching(self):
        """Graph edges should not branch based on business_scenario/mode."""
        edges = deep_researcher_graph.get_graph().edges
        # The public-opinion research phase now hands its formal reports to the
        # dedicated section_writer node before final-section writing.
        for edge in edges:
            if edge.source == "research_phase":
                assert edge.target == "section_writer"

    def test_public_opinion_nodes_exist(self):
        """Graph should contain the public-opinion workflow nodes."""
        node_names = set(deep_researcher_graph.get_graph().nodes)
        expected = {
            "enrich_query_images",
            "clarify_with_user",
            "write_research_brief",
            "plan_report_sections",
            "research_phase",
            "section_writer",
            "write_final_sections",
            "compile_final_report",
        }
        assert expected.issubset(node_names), f"Missing nodes: {expected - node_names}"

    def test_research_phase_is_the_only_public_opinion_wrapper(self):
        """The main graph exposes the state-conversion phase under its real name."""
        node_names = set(deep_researcher_graph.get_graph().nodes)
        assert "research_phase" in node_names
        assert not {name for name in node_names if name.endswith("_supervisor")}

    def test_public_opinion_agent_topology_has_dynamic_research_review(self):
        """The subgraph has a review loop without a supervisor or dispatch node."""
        edges = {
            (edge.source, edge.target)
            for edge in public_opinion_builder.compile().get_graph().edges
        }
        assert {
            ("__start__", "public_signal_agent"),
            ("__start__", "internal_knowledge_agent"),
            ("public_signal_agent", "research_review"),
            ("internal_knowledge_agent", "research_review"),
            ("research_review", "public_signal_agent"),
            ("research_review", "internal_knowledge_agent"),
            ("research_review", "risk_assessment_agent"),
            ("risk_assessment_agent", "response_strategy_agent"),
            ("response_strategy_agent", "__end__"),
        }.issubset(edges)
        assert "dispatch_followup_research" not in public_opinion_builder.nodes


# ── 3. Configuration tests ────────────────────────────────────────────


class TestConfigurationNoMode:
    """Verify creating config does not require a mode field."""

    def test_config_without_mode(self):
        """Configuration can be created without specifying any mode."""
        config = Configuration()
        assert config.business_scenario == "public_opinion_risk"

    def test_config_from_runnable_config_no_mode(self):
        """from_runnable_config works without mode in configurable."""
        runnable_config = {
            "configurable": {
                "research_model": "openai:gpt-4.1",
                "search_api": "tavily",
            }
        }
        config = Configuration.from_runnable_config(runnable_config)
        assert config.business_scenario == "public_opinion_risk"

    def test_tool_domain_filtering_removed(self):
        """tool_domain_filtering_enabled is no longer a config field."""
        config = Configuration()
        assert not hasattr(config, "tool_domain_filtering_enabled")

    def test_supervisor_iteration_settings_removed(self):
        """The removed supervisor loop has no configuration settings left."""
        config = Configuration()
        assert not hasattr(config, "max_researcher_iterations")
        assert not hasattr(config, "max_concurrent_research_units")

    def test_dynamic_research_round_safety_setting(self):
        """Dynamic research defaults to two rounds and rejects non-positive limits."""
        assert Configuration().max_research_rounds == 2
        with pytest.raises(ValueError, match="max_research_rounds"):
            Configuration(max_research_rounds=0)


# ── 4. State tests ────────────────────────────────────────────────────


class TestStateCleanup:
    """Verify general-mode state fields are removed."""

    def test_no_conduct_research_class(self):
        """ConductResearch class should not exist in state module."""
        import open_deep_research.state as state_module
        assert not hasattr(state_module, "ConductResearch")

    def test_no_supervisor_state(self):
        """SupervisorState should not exist."""
        import open_deep_research.state as state_module
        assert not hasattr(state_module, "SupervisorState")

    def test_no_researcher_state(self):
        """ResearcherState should not exist."""
        import open_deep_research.state as state_module
        assert not hasattr(state_module, "ResearcherState")

    def test_no_researcher_output_state(self):
        """ResearcherOutputState should not exist."""
        import open_deep_research.state as state_module
        assert not hasattr(state_module, "ResearcherOutputState")

    def test_agent_state_no_relevant_domains(self):
        """AgentState should not have relevant_domains field."""
        annotations = AgentState.__annotations__
        assert "relevant_domains" not in annotations

    def test_agent_state_has_no_supervisor_messages(self):
        """AgentState should not retain the unused supervisor message channel."""
        assert "supervisor_messages" not in AgentState.__annotations__

    def test_public_opinion_state_no_relevant_domains(self):
        """PublicOpinionState should not have relevant_domains field."""
        annotations = PublicOpinionState.__annotations__
        assert "relevant_domains" not in annotations

    def test_research_question_no_relevant_domains(self):
        """ResearchQuestion should not have relevant_domains field."""
        fields = ResearchQuestion.model_fields
        assert "relevant_domains" not in fields


# ── 5. Prompts tests ──────────────────────────────────────────────────


class TestPromptsCleanup:
    """Verify general-mode prompts are removed."""

    def test_no_lead_researcher_prompt(self):
        """lead_researcher_prompt (general supervisor) should not exist."""
        import open_deep_research.prompts as prompts_module
        assert not hasattr(prompts_module, "lead_researcher_prompt")

    def test_no_research_system_prompt(self):
        """research_system_prompt (general researcher) should not exist."""
        import open_deep_research.prompts as prompts_module
        assert not hasattr(prompts_module, "research_system_prompt")

    def test_no_final_report_generation_prompt(self):
        """final_report_generation_prompt (general) should not exist."""
        import open_deep_research.prompts as prompts_module
        assert not hasattr(prompts_module, "final_report_generation_prompt")

    def test_public_opinion_prompts_are_agent_owned(self):
        """The live public-opinion prompts are owned by the four agent specs."""
        import open_deep_research.prompts as prompts_module
        assert not hasattr(prompts_module, "public_opinion_supervisor_prompt")
        assert not hasattr(prompts_module, "public_opinion_researcher_prompt")
        assert hasattr(prompts_module, "public_opinion_final_report_generation_prompt")

    def test_supervisor_prompt_builder_removed(self):
        """The main graph should not retain a dead coordinator prompt builder."""
        import open_deep_research.deep_researcher as deep_researcher_module
        assert not hasattr(deep_researcher_module, "_supervisor_system_prompt")

    def test_transform_prompt_no_domain_classifier(self):
        """transform_messages_into_research_topic_prompt should not reference domain_classifier_section."""
        import open_deep_research.prompts as prompts_module
        prompt = prompts_module.transform_messages_into_research_topic_prompt
        assert "{domain_classifier_section}" not in prompt
        assert "relevant_domains" not in prompt


# ── 6. MCP domain filter tests ────────────────────────────────────────


def _load_domain_filter_module():
    """Load domain_filter directly, bypassing mcp/__init__.py (which may fail
    due to langchain_mcp_adapters version issues in some environments)."""
    import importlib
    import pathlib
    import sys
    import types

    # Save and clear any cached mcp modules
    saved = {}
    for key in list(sys.modules):
        if key.startswith("open_deep_research.mcp"):
            saved[key] = sys.modules.pop(key)
    try:
        # Create a minimal mcp package stub that doesn't run __init__.py
        mcp_dir = pathlib.Path(__file__).resolve().parent.parent / "src" / "open_deep_research" / "mcp"
        mcp_pkg = types.ModuleType("open_deep_research.mcp")
        mcp_pkg.__path__ = [str(mcp_dir)]
        mcp_pkg.__package__ = "open_deep_research.mcp"
        sys.modules["open_deep_research.mcp"] = mcp_pkg
        mod = importlib.import_module("open_deep_research.mcp.domain_filter")
        return mod
    finally:
        # Restore original modules
        sys.modules.pop("open_deep_research.mcp", None)
        sys.modules.pop("open_deep_research.mcp.domain_filter", None)
        sys.modules.update(saved)


class TestDomainFilterCleanup:
    """Verify general-mode domain filtering functions are removed."""

    def test_no_build_domain_classifier_prompt(self):
        """build_domain_classifier_prompt should not exist."""
        domain_filter = _load_domain_filter_module()
        assert not hasattr(domain_filter, "build_domain_classifier_prompt")

    def test_no_get_filtered_tools(self):
        """get_filtered_tools should not exist."""
        domain_filter = _load_domain_filter_module()
        assert not hasattr(domain_filter, "get_filtered_tools")

    def test_no_filter_tools_by_domain(self):
        """filter_tools_by_domain should not exist."""
        domain_filter = _load_domain_filter_module()
        assert not hasattr(domain_filter, "filter_tools_by_domain")

    def test_no_detect_active_domains(self):
        """detect_active_domains should not exist."""
        domain_filter = _load_domain_filter_module()
        assert not hasattr(domain_filter, "detect_active_domains")

    def test_no_tag_builtin_tools(self):
        """tag_builtin_tools should not exist."""
        domain_filter = _load_domain_filter_module()
        assert not hasattr(domain_filter, "tag_builtin_tools")

    def test_domain_registry_still_exists(self):
        """DOMAIN_REGISTRY must remain for MCP tool management."""
        domain_filter = _load_domain_filter_module()
        assert hasattr(domain_filter, "DOMAIN_REGISTRY")
        assert len(domain_filter.DOMAIN_REGISTRY) > 0

    def test_classify_tools_still_exists(self):
        """classify_tools must remain for tool prompt building."""
        domain_filter = _load_domain_filter_module()
        assert hasattr(domain_filter, "classify_tools")
