"""Discover, filter and bind the tools for one business role.

``ToolManager`` is the single assembly point between tool declarations and the
SDK Agent. Discovery runs inside ``por.role_tools`` before the durable Executor
exists; binding happens afterwards, so a resumed Run reuses the catalog it
already admitted instead of reinterpreting tool definitions.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from agents import FunctionTool
from langchain_core.runnables import RunnableConfig

from open_deep_research.configuration import Configuration
from open_deep_research.mcp.domain_filter import get_tool_domain
from open_deep_research.public_opinion_agents.base import PublicOpinionAgentSpec
from open_deep_research.tools import BusinessTool
from open_deep_research.tools.plugins import load_plugin_tools
from open_deep_research.tools.registry import get_all_tools, rag_requested
from open_deep_research.workflow.state_utils import tool_name

if TYPE_CHECKING:
    from open_deep_research.runtime.tool_executor import ToolExecutor

LOGGER = logging.getLogger(__name__)


class ToolManager:
    """Own one role's tool discovery, role filtering and SDK binding."""

    def __init__(
        self, role: str, spec: PublicOpinionAgentSpec, config: RunnableConfig
    ) -> None:
        self.role = role
        self.spec = spec
        self.config = config

    async def discover(self) -> list[Any]:
        """Collect local tools, global MCP and plugin tools, then filter by role.

        The global MCP pass happens exactly once; plugins only add their own
        declared servers and the role-authorized ``load_skill`` declaration.
        """
        configurable = Configuration.from_runnable_config(self.config)
        all_tools = list(await get_all_tools(self.config))
        local_tool_names = {tool_name(tool) for tool in all_tools}
        plugin_tools, _role_plugins = await load_plugin_tools(
            self.config,
            role=self.role,
            allowed_domains=self.spec.allowed_domains,
            local_tool_names=local_tool_names,
            existing_tool_names=set(local_tool_names),
        )
        all_tools.extend(plugin_tools)

        selected: list[Any] = []
        taken: set[str] = set()
        rejected: list[tuple[str, str]] = []
        for available_tool in all_tools:
            name = tool_name(available_tool)
            domain = get_tool_domain(available_tool)
            if domain not in self.spec.allowed_domains:
                rejected.append((name, domain or "unclassified"))
                continue
            if name in taken:
                raise ValueError(f"Duplicate business tool name: {name}")
            taken.add(name)
            selected.append(available_tool)
        LOGGER.debug(
            "Public-opinion agent %s: allowed_domains=%s tools_before=%d "
            "tools_after=%d rejected=%s",
            self.role,
            sorted(self.spec.allowed_domains),
            len(all_tools),
            len(selected),
            rejected,
        )
        self._require_enabled(configurable, selected)
        return selected

    def index(self, tools: Iterable[Any]) -> dict[str, BusinessTool]:
        """Validate the admitted catalog and index it by model-visible name."""
        indexed: dict[str, BusinessTool] = {}
        for tool in tools:
            if not isinstance(tool, BusinessTool):
                raise TypeError(
                    "SDK business runtime requires BusinessTool declarations; "
                    "hosted/native search is not verified."
                )
            if tool.name in indexed:
                raise ValueError(f"Duplicate business tool name: {tool.name}")
            if get_tool_domain(tool) not in self.spec.allowed_domains:
                raise ValueError(
                    f"Tool {tool.name!r} is outside {self.role}'s allowed domains."
                )
            indexed[tool.name] = tool
        return indexed

    def bind(
        self, tools: Iterable[BusinessTool], executor: ToolExecutor
    ) -> list[FunctionTool]:
        """Bind admitted declarations to this Run's ToolExecutor."""
        return [
            executor.bind(tool.sdk_tool, drain_on_cancel=tool.drain_on_cancel)
            for tool in tools
        ]

    def _require_enabled(
        self, configurable: Configuration, tools: Iterable[Any]
    ) -> None:
        """Fail when an explicitly enabled capability did not materialize."""
        names = {tool_name(tool) for tool in tools}
        if (
            "rag" in self.spec.allowed_domains
            and rag_requested(configurable)
            and "rag_search" not in names
        ):
            raise ValueError(
                f"Role {self.role!r} requires rag_search because rag_enabled=true with "
                f"retrieval_mode={configurable.retrieval_mode.value}, but the RAG "
                "tool is unavailable."
            )


__all__ = ["ToolManager"]
