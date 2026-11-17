# 通过服务名前缀解决多个 MCP 服务的工具重名问题。
"""Namespace MCP tools and adapt their results to the SDK execution path.

Multiple MCP servers may expose tools with identical names (e.g., both
DBHub and another server could have a ``search`` tool). This module
provides explicit conflict detection and a safe wrapping strategy that
preserves the original tool's behaviour while giving each tool a unique,
server-scoped name such as ``dbhub__execute_sql``.
"""

from __future__ import annotations

import logging
from typing import Any

from agents.tool_context import ToolContext
from jsonschema.validators import validator_for
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, ToolException

from open_deep_research.tools import BusinessTool, tool_result_text

logger = logging.getLogger(__name__)

# Characters that are safe in LangChain tool names AND MCP tool names.
# We use double-underscore as the separator: dbhub__execute_sql
_SERVER_SEPARATOR = "__"


# 生成“服务名__工具名”格式的命名空间名称。
def prefixed_tool_name(server_name: str, tool_name: str) -> str:
    """Generate a server-scoped tool name.

    Example:
        >>> prefixed_tool_name("dbhub", "execute_sql")
        'dbhub__execute_sql'
    """
    return f"{server_name}{_SERVER_SEPARATOR}{tool_name}"


# 为冲突工具加服务名前缀，并维护已占用名称集合以避免后续冲突。
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
                "MCP tool name conflict: '%s' from server '%s' → renaming to '%s'.",
                original_name,
                server_name,
                prefixed,
            )
            renamed = tool.model_copy(
                update={"name": prefixed, "description": tagged_desc}
            )
            wrapped.append(renamed)
        else:
            wrapped.append(tool)

    return wrapped


def tool_schema(tool: BusinessTool | BaseTool) -> dict[str, Any]:
    """Read the declaration used for both the role journal and SDK registration."""
    if isinstance(tool, BusinessTool):
        return tool.sdk_tool.params_json_schema
    schema = tool.tool_call_schema
    return schema if isinstance(schema, dict) else schema.model_json_schema()


def mcp_tool_invoker(tool: BaseTool, schema: dict[str, Any]):
    """Validate MCP's advertised schema before entering the protected call."""
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    validator = validator_class(schema)

    async def invoke(context: ToolContext[Any], _: str) -> Any:
        args = context.context.results[context.tool_call_id].args
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be a JSON object.")
        validator.validate(args)
        return await context.context.tool_executor.execute(
            context, _call_mcp_tool, tool, context, args
        )

    return invoke


async def _call_mcp_tool(
    tool: BaseTool, context: ToolContext[Any], args: dict[str, Any]
) -> Any:
    # MCP's LangChain adapter exposes structured content in the message artifact.
    output = await tool.ainvoke(
        {
            "type": "tool_call",
            "id": context.tool_call_id,
            "name": context.tool_name,
            "args": args,
        },
        context.context.tool_executor.tool_config(context),
    )
    if not isinstance(output, ToolMessage):
        return output
    if output.status == "error":
        raise ToolException(tool_result_text(output.content))

    content = output.content
    if isinstance(content, list) and any(
        not isinstance(block, dict) or block.get("type") != "text" for block in content
    ):
        raise ToolException(
            "SDK business tools currently support text/structured results; "
            "multimodal MCP outputs require a verified content adapter."
        )
    artifact = output.artifact
    structured = (
        artifact.get("structured_content")
        if isinstance(artifact, dict)
        else getattr(artifact, "structured_content", None)
    )
    return (
        {"content": content, "structured_content": structured}
        if structured is not None
        else content
    )
