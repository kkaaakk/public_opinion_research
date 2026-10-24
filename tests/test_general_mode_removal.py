# 验证系统只保留舆情研究流程，旧通用模式及遗留状态和提示词已移除。
"""Tests verifying that general research mode has been fully removed.

Covers:
1. Unique mode: no general/generic/deep_research mode accepted
2. Graph structure: no standalone supervisor/researcher/mode-router nodes
3. Configuration: mode field not required
4. API: mode-free request works
5. State: no ConductResearch or general-mode state fields
"""

from pathlib import Path

import pytest

from open_deep_research.configuration import Configuration
from open_deep_research.state import (
    AgentState,
    PublicOpinionState,
    ResearchQuestion,
)
from open_deep_research.workflow.dbos_workflow import OPERATIONS

# ── 1. Unique mode tests ──────────────────────────────────────────────


# 检查配置和角色只接受当前舆情业务场景。
class TestUniqueMode:
    """Verify the project no longer accepts or depends on general research mode."""

    # 验证默认业务场景为舆情研究。
    def test_configuration_default_is_public_opinion(self):
        """Default business_scenario is public_opinion_risk."""
        config = Configuration()
        assert config.business_scenario == "public_opinion_risk"

    # 验证拒绝旧 general_research 场景。
    def test_general_research_scenario_rejected(self):
        """Creating config with general_research raises ValueError."""
        with pytest.raises(ValueError, match="public_opinion_risk"):
            Configuration(business_scenario="general_research")

    # 验证拒绝旧 generic_research 场景。
    def test_generic_research_scenario_rejected(self):
        """Any non-public-opinion scenario is rejected."""
        with pytest.raises(ValueError):
            Configuration(business_scenario="generic_research")

    # 验证拒绝旧 deep_research 场景。
    def test_deep_research_scenario_rejected(self):
        """deep_research is not a valid business_scenario."""
        with pytest.raises(ValueError):
            Configuration(business_scenario="deep_research")

    # 验证业务角色类型不包含旧通用研究角色。
    def test_general_research_not_in_business_agent_role(self):
        """BusinessAgentRole no longer contains general_research."""
        from open_deep_research.state import BusinessAgentRole

        # Literal type — extract allowed values
        allowed = set(BusinessAgentRole.__args__)
        assert "general_research" not in allowed
        assert "public_signal" in allowed
        assert "risk_assessment" in allowed

    # 验证启用角色列表不再包含通用研究角色。
    def test_enabled_agents_no_general_research(self):
        """enabled_business_agents rejects general_research entries."""
        with pytest.raises(ValueError, match="unsupported"):
            Configuration(enabled_business_agents=["general_research"])


# ── 2. Graph structure tests ──────────────────────────────────────────


# 检查单一业务流程的节点和复核拓扑。
class TestGraphStructure:
    """Native business topology preserves the single public-opinion workflow."""

    def test_no_standalone_supervisor_node(self):
        assert "supervisor" not in OPERATIONS
        assert "research_review" in OPERATIONS

    def test_no_standalone_researcher_node(self):
        assert "researcher" not in OPERATIONS

    def test_no_mode_router(self):
        assert not any("route" in name for name in OPERATIONS)

    def test_no_general_mode_conditional_branching(self):
        source = Path("src/open_deep_research/workflow/dbos_workflow.py").read_text(
            encoding="utf-8"
        )
        assert "business_scenario" not in source
        assert (
            'for name in ("section_writer", "write_final_sections", "compile_final_report"):'
            in source
        )

    def test_public_opinion_nodes_exist(self):
        assert {
            "enrich_query_images",
            "clarify_with_user",
            "write_research_brief",
            "plan_report_sections",
            "research_review",
            "section_writer",
            "write_final_sections",
            "compile_final_report",
        } == set(OPERATIONS)

    def test_research_phase_is_the_only_public_opinion_wrapper(self):
        from open_deep_research.workflow.dbos_workflow import research_workflow

        assert callable(research_workflow)
        assert not any(name.endswith("_supervisor") for name in OPERATIONS)

    def test_public_opinion_agent_topology_has_dynamic_research_review(self):
        source = Path("src/open_deep_research/workflow/dbos_workflow.py").read_text(
            encoding="utf-8"
        )
        assert "for role in RESEARCH_ROLES:" in source
        assert 'packets = reviewed["followup"]' in source
        assert 'for role in ("risk_assessment", "response_strategy"):' in source
        assert "dispatch_followup_research" not in source


# ── 3. Configuration tests ────────────────────────────────────────────


# 检查旧模式开关和调度配置已从有效配置移除。
class TestConfigurationNoMode:
    """Verify creating config does not require a mode field."""

    # 验证配置模型不再声明 mode 字段。
    def test_config_without_mode(self):
        """Configuration can be created without specifying any mode."""
        config = Configuration()
        assert config.business_scenario == "public_opinion_risk"

    # 验证运行配置解析不会恢复旧模式字段。
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

    # 验证旧全局工具领域筛选配置已移除。
    def test_tool_domain_filtering_removed(self):
        """tool_domain_filtering_enabled is no longer a config field."""
        config = Configuration()
        assert not hasattr(config, "tool_domain_filtering_enabled")

    # 验证旧 supervisor 迭代设置已移除。
    def test_supervisor_iteration_settings_removed(self):
        """The removed supervisor loop has no configuration settings left."""
        config = Configuration()
        assert not hasattr(config, "max_researcher_iterations")
        assert not hasattr(config, "max_concurrent_research_units")

    # 验证补充研究轮数仍有安全上限配置。
    def test_dynamic_research_round_safety_setting(self):
        """Dynamic research defaults to two rounds and rejects non-positive limits."""
        assert Configuration().max_research_rounds == 2
        with pytest.raises(ValueError, match="max_research_rounds"):
            Configuration(max_research_rounds=0)


# ── 4. State tests ────────────────────────────────────────────────────


# 检查已移除通用模式的状态结构和字段。
class TestStateCleanup:
    """Verify general-mode state fields are removed."""

    # 验证旧 ConductResearch 工具结构已移除。
    def test_no_conduct_research_class(self):
        """ConductResearch class should not exist in state module."""
        import open_deep_research.state as state_module

        assert not hasattr(state_module, "ConductResearch")

    # 验证旧 SupervisorState 不再导出。
    def test_no_supervisor_state(self):
        """SupervisorState should not exist."""
        import open_deep_research.state as state_module

        assert not hasattr(state_module, "SupervisorState")

    # 验证旧 ResearcherState 不再导出。
    def test_no_researcher_state(self):
        """ResearcherState should not exist."""
        import open_deep_research.state as state_module

        assert not hasattr(state_module, "ResearcherState")

    # 验证旧 ResearcherOutputState 不再导出。
    def test_no_researcher_output_state(self):
        """ResearcherOutputState should not exist."""
        import open_deep_research.state as state_module

        assert not hasattr(state_module, "ResearcherOutputState")

    # 验证主状态不再携带旧 relevant_domains 字段。
    def test_agent_state_no_relevant_domains(self):
        """AgentState should not have relevant_domains field."""
        annotations = AgentState.__annotations__
        assert "relevant_domains" not in annotations

    # 验证主状态不再保存 supervisor 消息通道。
    def test_agent_state_has_no_supervisor_messages(self):
        """AgentState should not retain the unused supervisor message channel."""
        assert "supervisor_messages" not in AgentState.__annotations__

    # 验证舆情状态不再携带旧领域分类字段。
    def test_public_opinion_state_no_relevant_domains(self):
        """PublicOpinionState should not have relevant_domains field."""
        annotations = PublicOpinionState.__annotations__
        assert "relevant_domains" not in annotations

    # 验证结构化研究问题不再返回领域分类。
    def test_research_question_no_relevant_domains(self):
        """ResearchQuestion should not have relevant_domains field."""
        fields = ResearchQuestion.model_fields
        assert "relevant_domains" not in fields


# ── 5. Prompts tests ──────────────────────────────────────────────────


# 检查旧通用提示词已移除，角色提示词由角色规范维护。
class TestPromptsCleanup:
    """Verify general-mode prompts are removed."""

    # 验证旧主研究员提示词已移除。
    def test_no_lead_researcher_prompt(self):
        """lead_researcher_prompt (general supervisor) should not exist."""
        import open_deep_research.prompts as prompts_module

        assert not hasattr(prompts_module, "lead_researcher_prompt")

    # 验证旧通用研究系统提示词已移除。
    def test_no_research_system_prompt(self):
        """research_system_prompt (general researcher) should not exist."""
        import open_deep_research.prompts as prompts_module

        assert not hasattr(prompts_module, "research_system_prompt")

    # 验证旧最终报告提示词已移除。
    def test_no_final_report_generation_prompt(self):
        """final_report_generation_prompt (general) should not exist."""
        import open_deep_research.prompts as prompts_module

        assert not hasattr(prompts_module, "final_report_generation_prompt")

    # 验证舆情提示词属于各角色规范。
    def test_public_opinion_prompts_are_agent_owned(self):
        """The live public-opinion prompts are owned by the four agent specs."""
        import open_deep_research.prompts as prompts_module

        assert not hasattr(prompts_module, "public_opinion_supervisor_prompt")
        assert not hasattr(prompts_module, "public_opinion_researcher_prompt")
        assert hasattr(prompts_module, "public_opinion_final_report_generation_prompt")

    # 验证旧 supervisor 提示词生成器已移除。
    def test_supervisor_prompt_builder_removed(self):
        """The main graph should not retain a dead coordinator prompt builder."""
        import open_deep_research.deep_researcher as deep_researcher_module

        assert not hasattr(deep_researcher_module, "_supervisor_system_prompt")

    # 验证需求转换提示词不再执行工具领域分类。
    def test_transform_prompt_no_domain_classifier(self):
        """transform_messages_into_research_topic_prompt should not reference domain_classifier_section."""
        import open_deep_research.prompts as prompts_module

        prompt = prompts_module.transform_messages_into_research_topic_prompt
        assert "{domain_classifier_section}" not in prompt
        assert "relevant_domains" not in prompt


# ── 6. MCP domain filter tests ────────────────────────────────────────


# 独立加载领域注册模块，供旧接口清理检查。
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
        mcp_dir = (
            pathlib.Path(__file__).resolve().parent.parent
            / "src"
            / "open_deep_research"
            / "mcp"
        )
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


# 检查旧筛选接口清理，同时保留领域注册与分组能力。
class TestDomainFilterCleanup:
    """Verify general-mode domain filtering functions are removed."""

    # 验证旧领域分类提示词生成器已移除。
    def test_no_build_domain_classifier_prompt(self):
        """build_domain_classifier_prompt should not exist."""
        domain_filter = _load_domain_filter_module()
        assert not hasattr(domain_filter, "build_domain_classifier_prompt")

    # 验证旧 get_filtered_tools 接口已移除。
    def test_no_get_filtered_tools(self):
        """get_filtered_tools should not exist."""
        domain_filter = _load_domain_filter_module()
        assert not hasattr(domain_filter, "get_filtered_tools")

    # 验证旧全局领域过滤接口已移除。
    def test_no_filter_tools_by_domain(self):
        """filter_tools_by_domain should not exist."""
        domain_filter = _load_domain_filter_module()
        assert not hasattr(domain_filter, "filter_tools_by_domain")

    # 验证旧自动领域检测接口已移除。
    def test_no_detect_active_domains(self):
        """detect_active_domains should not exist."""
        domain_filter = _load_domain_filter_module()
        assert not hasattr(domain_filter, "detect_active_domains")

    # 验证旧内置工具打标接口已移除。
    def test_no_tag_builtin_tools(self):
        """tag_builtin_tools should not exist."""
        domain_filter = _load_domain_filter_module()
        assert not hasattr(domain_filter, "tag_builtin_tools")

    # 验证领域注册表仍可查询。
    def test_domain_registry_still_exists(self):
        """DOMAIN_REGISTRY must remain for MCP tool management."""
        domain_filter = _load_domain_filter_module()
        assert hasattr(domain_filter, "DOMAIN_REGISTRY")
        assert len(domain_filter.DOMAIN_REGISTRY) > 0

    # 验证工具按领域归组能力仍保留。
    def test_classify_tools_still_exists(self):
        """classify_tools must remain for tool prompt building."""
        domain_filter = _load_domain_filter_module()
        assert hasattr(domain_filter, "classify_tools")
