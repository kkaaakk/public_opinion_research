# 导出业务智能体装配入口与共享 ReAct 运行时。
"""Execution and assembly entry points for public-opinion agents."""

from open_deep_research.runtime.business_agent import run_business_agent
from open_deep_research.runtime.sdk_runtime import (
    AgentRuntimeResult,
    SDKAgentRuntime,
)

__all__ = [
    "AgentRuntimeResult",
    "SDKAgentRuntime",
    "format_private_memory",
    "run_business_agent",
]


def format_private_memory(agent_state):
    """Render one role private memory through the SDK runtime contract."""
    return SDKAgentRuntime.format_private_memory(agent_state)
