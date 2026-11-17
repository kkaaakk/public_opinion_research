"""SDK tool declarations and the policy needed by business roles."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from inspect import iscoroutinefunction
from typing import Any, Literal

from agents import FunctionTool, function_tool
from agents.tool_context import ToolContext

ToolEffect = Literal["read", "idempotent", "protected"]


@dataclass(frozen=True)
class BusinessTool:
    sdk_tool: FunctionTool
    domain: str
    effect: ToolEffect = "protected"
    drain_on_cancel: bool = False

    @property
    def name(self) -> str:
        return self.sdk_tool.name

    @property
    def description(self) -> str:
        return self.sdk_tool.description


def python_tool(
    *,
    domain: str,
    description: str,
    effect: ToolEffect = "protected",
    name: str | None = None,
):
    """Declare a function whose first argument is the SDK ToolContext."""

    def declare(function: Callable[..., Any]) -> BusinessTool:
        # The SDK reads the original signature. The typed callback starts
        # external effects only after SDK validation has succeeded.
        @wraps(function)
        async def invoke(context: ToolContext[Any], *args: Any, **kwargs: Any) -> Any:
            return await context.context.tool_executor.execute(
                context, function, context, *args, **kwargs
            )

        sdk_tool = function_tool(
            invoke,
            name_override=name,
            description_override=description,
            use_docstring_info=False,
            strict_mode=False,
            failure_error_function=None,
        )
        # Match the catalog stored by existing por.role_tools steps.
        sdk_tool.params_json_schema.update(title=sdk_tool.name, description=description)
        return BusinessTool(sdk_tool, domain, effect, not iscoroutinefunction(function))

    return declare


def tool_result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(
        isinstance(block, dict) and block.get("type") == "text" for block in value
    ):
        return "\n".join(str(block.get("text", "")) for block in value)
    return json.dumps(value, ensure_ascii=False, default=str)
