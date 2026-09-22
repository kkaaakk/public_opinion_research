"""Tool name conflict resolution via namespace-prefix wrapping.

Multiple MCP servers may expose tools with identical names (e.g., both
DBHub and another server could have a ``search`` tool). This module
provides explicit conflict detection and a safe wrapping strategy that
preserves the original tool's behaviour while giving each tool a unique,
server-scoped name such as ``dbhub__execute_sql``.
"""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool, StructuredTool

logger = logging.getLogger(__name__)

# Characters that are safe in LangChain tool names AND MCP tool names.
# We use double-underscore as the separator: dbhub__execute_sql
_SERVER_SEPARATOR = "__"


def prefixed_tool_name(server_name: str, tool_name: str) -> str:
    """Generate a server-scoped tool name.

    Example:
        >>> prefixed_tool_name("dbhub", "execute_sql")
        'dbhub__execute_sql'
    """
    return f"{server_name}{_SERVER_SEPARATOR}{tool_name}"


def _copy_tool_with_name(tool: BaseTool, *, name: str, description: str) -> BaseTool:
    """Create a safe copy of *tool* with a different *name* and *description*.

    The copy preserves the original coroutine / func, args schema, metadata,
    and return-direct flag.  No internal fields of the original tool are
    mutated — this is important because LangChain / MCP tools may share
    session-level state that must not be altered.
    """
    # Prefer ``model_copy`` (Pydantic v2) when available — it handles
    # private attributes and validators correctly.
    if hasattr(tool, "model_copy"):
        copied: BaseTool = tool.model_copy(
            update={"name": name, "description": description}
        )
        return copied

    # Fallback: reconstruct via StructuredTool constructor.
    # This is safe for the common case (MCP tools are StructuredTool
    # subclasses), but may lose private Pydantic attrs on exotic subclasses.
    return StructuredTool(
        name=name,
        description=description,
        func=getattr(tool, "func", None),
        coroutine=getattr(tool, "coroutine", None),
        args_schema=tool.args_schema,
        metadata=tool.metadata,
        return_direct=getattr(tool, "return_direct", False),
        response_format=getattr(tool, "response_format", "content"),
    )


def wrap_mcp_tools(
    server_name: str,
    tools: list[BaseTool],
    existing_names: set[str],
) -> list[BaseTool]:
    """Namespace *tools* from *server_name* to avoid name collisions.

    Every tool whose original name already appears in *existing_names* is
    renamed to ``{server_name}__{original_name}`` and its description is
    tagged with the server name.  Tools whose names do **not** conflict
    keep their original name — this keeps the prompt short when there are
    no collisions.

    Parameters
    ----------
    server_name:
        Short identifier for the MCP server (e.g. ``"dbhub"``).
    tools:
        LangChain tools returned by the MCP client for this server.
    existing_names:
        Set of tool names already present in the agent's tool collection.

    Returns
    -------
    list[BaseTool]
        The tools, potentially renamed.  **No tools are silently dropped.**
    """
    wrapped: list[BaseTool] = []
    for tool in tools:
        original_name = tool.name
        if original_name in existing_names:
            prefixed = prefixed_tool_name(server_name, original_name)
            tagged_desc = f"[{server_name}] {tool.description or ''}"
            logger.info(
                "MCP tool name conflict: '%s' from server '%s' "
                "→ renaming to '%s'.",
                original_name,
                server_name,
                prefixed,
            )
            renamed = _copy_tool_with_name(
                tool, name=prefixed, description=tagged_desc
            )
            wrapped.append(renamed)
        else:
            wrapped.append(tool)

    return wrapped
