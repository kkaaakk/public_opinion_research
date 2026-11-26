# 把 SDK 原生 MCP 工具声明接入现有 ToolExecutor，并保留可信的本地领域与 Effect 策略。
"""Declare SDK-native MCP tools for the unified business tool executor.

The Agents SDK owns the MCP transport, tool discovery and tool Schema.  This
module only:

* resolves server-scoped names for conflicting tools,
* attaches the trusted local ``domain`` / ``effect`` policy, and
* routes the native MCP call through ``ToolExecutor.execute()`` so budgets,
  role permissions, ``por.agent_tool`` and external-operation receipts remain
  authoritative.

No MCP-specific executor or second registry lives here.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from agents.mcp import MCPServer
from agents.mcp.util import MCPUtil
from agents.tool_context import ToolContext
from jsonschema.validators import validator_for

from mcp import McpError
from open_deep_research.tools import BusinessTool, ToolEffect

logger = logging.getLogger(__name__)

# Double underscore keeps the server scope visible in prompts and journals.
_SERVER_SEPARATOR = "__"


# 生成“服务名__工具名”格式的命名空间名称。
def prefixed_tool_name(server_name: str, tool_name: str) -> str:
    """Generate a server-scoped tool name.

    Example:
        >>> prefixed_tool_name("dbhub", "execute_sql")
        'dbhub__execute_sql'
    """
    return f"{server_name}{_SERVER_SEPARATOR}{tool_name}"


# 为冲突工具分配唯一名称；服务名前缀仍冲突时追加稳定序号。
def allocate_tool_name(
    server_name: str, tool_name: str, taken: set[str]
) -> str:
    """Return a unique model-visible name for one MCP tool.

    Non-conflicting tools keep their original name.  A tool whose name is
    already claimed is namespaced as ``{server}__{tool}``; the rare remaining
    collision receives a numeric suffix instead of silently dropping a tool.
    """
    if tool_name not in taken:
        return tool_name
    base = prefixed_tool_name(server_name, tool_name)
    candidate = base
    index = 2
    while candidate in taken:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


class MCPToolError(RuntimeError):
    """A model-visible MCP failure that must not be reported as success."""


# 构造 SDK 原生 FunctionTool，并把调用接到当前 ToolExecutor。
def build_mcp_tool(
    mcp_tool: Any,
    server: MCPServer,
    *,
    server_name: str,
    server_factory: Callable[[], MCPServer],
    public_name: str,
    domain: str,
    effect: ToolEffect,
    apify_actor: bool = False,
) -> BusinessTool:
    """Convert one SDK MCP tool and attach trusted business policy.

    ``MCPUtil.to_function_tool`` owns the native name, description and Schema
    conversion.  Only the invocation callback is replaced so every call enters
    the existing ``ToolExecutor`` and its durable Step.
    """
    declaration = MCPUtil.to_function_tool(
        mcp_tool,
        server,
        False,
        agent=None,
        # Our thin invoker owns MCP error mapping; do not let the SDK hide a
        # failure behind a model-visible string before the executor records it.
        failure_error_function=None,
        tool_name_override=public_name,
    )
    invoker = _mcp_tool_invoker(
        server_name=server_name,
        server_factory=server_factory,
        mcp_tool_name=mcp_tool.name,
        schema=declaration.params_json_schema,
        apify_actor=apify_actor,
    )
    declared = replace(
        declaration,
        description=declaration.description or public_name,
        on_invoke_tool=invoker,
    )
    return BusinessTool(declared, domain=domain, effect=effect)


# 保留服务方 Schema 校验，然后进入统一 execute 边界。
def _mcp_tool_invoker(
    *,
    server_name: str,
    server_factory: Callable[[], MCPServer],
    mcp_tool_name: str,
    schema: dict[str, Any],
    apify_actor: bool,
) -> Callable[[ToolContext[Any], str], Any]:
    """Validate one MCP argument object, then enter the shared executor.

    ``build_mcp_tool`` replaces the SDK invocation callback, so the SDK's own
    argument validation no longer runs. The service Schema is therefore checked
    here once at declaration time and applied to every call: MCP tools keep the
    same "validate, then admit" order as local business tools.
    """
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    validator = validator_class(schema)

    async def invoke(context: ToolContext[Any], _arguments: str) -> Any:
        record = context.context.results[context.tool_call_id]
        args = record.args
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be a JSON object.")
        validator.validate(args)
        executor = context.context.tool_executor
        if apify_actor:

            async def remember(result):
                if run_id := _apify_run_id(result.structuredContent):
                    await executor.remember_apify_run(context, run_id)

            async def launch():
                return await _call_mcp_tool(
                    server_name,
                    server_factory,
                    mcp_tool_name,
                    args,
                    observe_result=remember,
                )

            async def lookup(run_id):
                result = await _call_mcp_tool(
                    server_name, server_factory, "get-actor-run", {"runId": run_id}
                )
                receipt = (
                    result.get("structured_content")
                    if isinstance(result, dict)
                    else None
                )
                if _apify_run_id(receipt) != run_id or not isinstance(
                    receipt.get("status"), str
                ):
                    raise MCPToolError("Apify did not confirm the original Actor run")
                # The provider supplies its status and storage IDs. The Agent
                # can continue with the existing read-only Dataset tool.
                return result

            return await executor.execute(context, launch, recover_apify=lookup)
        return await executor.execute(
            context,
            _call_mcp_tool,
            server_name,
            server_factory,
            mcp_tool_name,
            args,
        )

    return invoke


def _apify_run_id(receipt):
    run_id = receipt.get("runId") if isinstance(receipt, dict) else None
    return (
        run_id
        if isinstance(run_id, str) and re.fullmatch(r"[A-Za-z0-9]{1,128}", run_id)
        else None
    )


# 每次调用使用独立连接，断连或取消后不会复用失效会话。
async def _call_mcp_tool(
    server_name: str,
    server_factory: Callable[[], MCPServer],
    mcp_tool_name: str,
    args: dict[str, Any],
    *,
    observe_result=None,
) -> Any:
    """Call the remote MCP tool over a fresh SDK connection.

    MCP stdio transports own a subprocess and anyio task group, so connect,
    call and cleanup stay in this coroutine.  The executor's Step still owns
    the durable result and any protected-operation receipt.
    """
    server = server_factory()
    try:
        async with server:
            result = await server.call_tool(mcp_tool_name, args)
            if observe_result is not None:
                await observe_result(result)
    except BaseException as exc:
        interaction = _interaction_required_message(exc)
        if interaction is not None:
            raise MCPToolError(interaction) from exc
        raise
    return _normalise_result(server_name, mcp_tool_name, result)


# 把 MCP 文本或结构化结果转成现有 executor 的文本契约。
def _normalise_result(server_name: str, mcp_tool_name: str, result: Any) -> Any:
    blocks: list[dict[str, Any]] = []
    unsupported_type: str | None = None
    for item in result.content or ():
        item_type = getattr(item, "type", type(item).__name__)
        if item_type == "text":
            blocks.append({"type": "text", "text": item.text})
        else:
            unsupported_type = item_type

    text = "\n".join(block["text"] for block in blocks)
    if result.isError:
        # Error content carries the actionable failure text; it keeps priority
        # over the multimodal rejection, matching the SDK's MCP error policy.
        raise MCPToolError(
            text
            or f"MCP tool '{server_name}.{mcp_tool_name}' returned an error result."
        )
    if unsupported_type is not None:
        raise MCPToolError(
            f"MCP tool '{server_name}.{mcp_tool_name}' returned unsupported "
            f"content type {unsupported_type!r}; "
            "multimodal MCP outputs require a verified content adapter."
        )

    structured = result.structuredContent
    if structured is not None:
        return {"content": blocks, "structured_content": structured}
    return blocks


# 从嵌套异常组中定位 MCP 交互要求，并生成可理解的结果。
def _interaction_required_message(error: BaseException) -> str | None:
    mcp_error = _find_mcp_error(error)
    if mcp_error is None:
        return None
    error_code = getattr(mcp_error.error, "code", None)
    error_data = getattr(mcp_error.error, "data", None) or {}
    if error_code != -32003:
        return None
    message_payload = error_data.get("message", {})
    message = "Required interaction"
    if isinstance(message_payload, dict):
        message = message_payload.get("text") or message
    if url := error_data.get("url"):
        message = f"{message} {url}"
    return message


# 递归查找异常组中的 McpError。
def _find_mcp_error(error: BaseException) -> McpError | None:
    if isinstance(error, McpError):
        return error
    for sub in getattr(error, "exceptions", ()):
        if found := _find_mcp_error(sub):
            return found
    return None
