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

if TYPE_CHECKING:
    from open_deep_research.runtime.tool_executor import ToolExecutor

LOGGER = logging.getLogger(__name__)

# Only the SDK declaration path is verified. Hosted/native search declarations
# carry no local domain, effect or durable invocation, so they are rejected here
# instead of failing later inside the executor.
_DECLARATION_ERROR = (
    "SDK business runtime requires BusinessTool declarations; "
    "hosted/native search is not verified."
)


def _declared(tools: Iterable[Any]) -> list[BusinessTool]:
    """Prove one declaration set before any name, domain or prompt reads it."""
    declared: list[BusinessTool] = []
    for tool in tools:
        if not isinstance(tool, BusinessTool):
            raise TypeError(_DECLARATION_ERROR)
        declared.append(tool)
    return declared


class ToolManager:
    """Own one role's tool discovery, role filtering and SDK binding."""

    def __init__(
        self, role: str, spec: PublicOpinionAgentSpec, config: RunnableConfig
    ) -> None:
        self.role = role
        self.spec = spec
        self.config = config

    async def discover(self) -> list[BusinessTool]:
        """Collect local tools, global MCP and plugin tools, then filter by role.

        This is the one admission boundary: every tool is proven to be an SDK
        ``BusinessTool`` declaration with a unique name and an allowed domain
        before the Agent, its prompt and ``por.role_tools`` can see it.

        The global MCP pass happens exactly once; plugins only add their own
        declared servers and the role-authorized ``load_skill`` declaration.
        """
        configurable = Configuration.from_runnable_config(self.config)
        all_tools = _declared(await get_all_tools(self.config))
        local_tool_names = {tool.name for tool in all_tools}
        plugin_tools, _role_plugins = await load_plugin_tools(
            self.config,
            role=self.role,
            allowed_domains=self.spec.allowed_domains,
            local_tool_names=local_tool_names,
            existing_tool_names=set(local_tool_names),
        )
        all_tools.extend(_declared(plugin_tools))

        selected: list[BusinessTool] = []
        taken: set[str] = set()
        rejected: list[tuple[str, str]] = []
        for available_tool in all_tools:
            if available_tool.name in taken:
                raise ValueError(f"Duplicate business tool name: {available_tool.name}")
            domain = get_tool_domain(available_tool)
            if domain not in self.spec.allowed_domains:
                rejected.append((available_tool.name, domain or "unclassified"))
                continue
            taken.add(available_tool.name)
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

    def bind(
        self, tools: Iterable[BusinessTool], executor: ToolExecutor
    ) -> list[FunctionTool]:
        """Bind admitted declarations to this Run's ToolExecutor.

        Discovery happens before the executor exists (``por.role_tools``), so
        binding stays a separate step: it only replaces the invocation callback
        while the SDK keeps the model-visible name, description and Schema.
        """
        return [
            executor.bind(tool.sdk_tool, drain_on_cancel=tool.drain_on_cancel)
            for tool in tools
        ]

    def _require_enabled(
        self, configurable: Configuration, tools: Iterable[BusinessTool]
    ) -> None:
        """Fail when an explicitly enabled capability did not materialize."""
        names = {tool.name for tool in tools}
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
