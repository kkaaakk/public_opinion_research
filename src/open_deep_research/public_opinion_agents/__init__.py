"""Encapsulated public-opinion business agents."""

from open_deep_research.public_opinion_agents.base import PublicOpinionAgentSpec
from open_deep_research.public_opinion_agents.registry import (
    PUBLIC_OPINION_AGENT_ORDER,
    PUBLIC_OPINION_AGENT_SPECS,
    get_public_opinion_agent_spec,
    public_opinion_role_channels,
    public_opinion_role_domains,
    public_opinion_role_expectations,
)

__all__ = [
    "PUBLIC_OPINION_AGENT_ORDER",
    "PUBLIC_OPINION_AGENT_SPECS",
    "PublicOpinionAgentSpec",
    "get_public_opinion_agent_spec",
    "public_opinion_role_channels",
    "public_opinion_role_domains",
    "public_opinion_role_expectations",
]
