# 导出业务智能体装配入口与 SDK 运行时。
"""Execution and assembly entry points for public-opinion agents."""

from open_deep_research.runtime.business_agent import run_business_agent
from open_deep_research.runtime.sdk_runtime import (
    AgentRuntimeResult,
    SDKAgentRuntime,
)

__all__ = [
    "AgentRuntimeResult",
    "SDKAgentRuntime",
    "run_business_agent",
]
