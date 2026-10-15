# 注册舆情角色规范，集中提供职责、领域权限和兼容渠道视图。
"""Registry for compact public-opinion business agents."""

from open_deep_research.public_opinion_agents.base import PublicOpinionAgentSpec
from open_deep_research.public_opinion_agents.internal_knowledge import (
    AGENT as INTERNAL_KNOWLEDGE_AGENT,
)
from open_deep_research.public_opinion_agents.public_signal import (
    AGENT as PUBLIC_SIGNAL_AGENT,
)
from open_deep_research.public_opinion_agents.response_strategy import (
    AGENT as RESPONSE_STRATEGY_AGENT,
)
from open_deep_research.public_opinion_agents.risk_assessment import (
    AGENT as RISK_ASSESSMENT_AGENT,
)

PUBLIC_OPINION_AGENT_ORDER = (
    "public_signal",
    "internal_knowledge",
    "risk_assessment",
    "response_strategy",
)

PUBLIC_OPINION_AGENT_SPECS: dict[str, PublicOpinionAgentSpec] = {
    PUBLIC_SIGNAL_AGENT.role: PUBLIC_SIGNAL_AGENT,
    INTERNAL_KNOWLEDGE_AGENT.role: INTERNAL_KNOWLEDGE_AGENT,
    RISK_ASSESSMENT_AGENT.role: RISK_ASSESSMENT_AGENT,
    RESPONSE_STRATEGY_AGENT.role: RESPONSE_STRATEGY_AGENT,
}


# 按角色名返回规范定义，供运行时装配智能体。
def get_public_opinion_agent_spec(role: str) -> PublicOpinionAgentSpec:
    """Return one public-opinion agent spec by role."""
    normalized_role = str(role).strip().lower()
    if normalized_role not in PUBLIC_OPINION_AGENT_SPECS:
        supported = ", ".join(PUBLIC_OPINION_AGENT_ORDER)
        raise KeyError(f"Unsupported public-opinion agent role: {role}. Supported roles: {supported}")
    return PUBLIC_OPINION_AGENT_SPECS[normalized_role]


# 返回角色与预期输出的映射，兼容已有调用方。
def public_opinion_role_expectations() -> dict[str, str]:
    """Return role-to-expected-output mapping for compatibility."""
    return {
        role: PUBLIC_OPINION_AGENT_SPECS[role].expected_output
        for role in PUBLIC_OPINION_AGENT_ORDER
    }


# 返回角色明确允许访问的工具领域，作为权限判断依据。
def public_opinion_role_domains() -> dict[str, frozenset[str]]:
    """Return the explicit role-to-tool-domain mapping."""
    return {
        role: PUBLIC_OPINION_AGENT_SPECS[role].allowed_domains
        for role in PUBLIC_OPINION_AGENT_ORDER
    }


# 从领域权限生成旧版渠道视图，仅用于兼容展示。
def public_opinion_role_channels() -> dict[str, frozenset[str]]:
    """Return a deprecated channel view derived from domain permissions.

    Domain permissions remain the only authorization source. This compatibility
    view is retained for callers that still display the former channel names.
    """
    return {
        role: frozenset(
            channel
            for domain, channel in (
                ("web_search", "web"),
                ("rag", "rag"),
                ("social_media", "mcp"),
            )
            if domain in PUBLIC_OPINION_AGENT_SPECS[role].allowed_domains
        )
        for role in PUBLIC_OPINION_AGENT_ORDER
    }
