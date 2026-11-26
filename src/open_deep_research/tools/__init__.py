"""Business declarations for official SDK FunctionTool implementations.

The SDK owns tool declarations: ``@function_tool`` produces the model-visible
name, description, JSON Schema, argument validation and defaults. This module
adds the domain and external-effect policy the business assembly and
``ToolExecutor`` need, and lets a business function run inside that executor
without calling it explicitly.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from inspect import iscoroutinefunction, signature
from typing import Any, Literal, get_origin, get_type_hints

from agents import FunctionTool, function_tool
from agents.tool_context import ToolContext

ToolEffect = Literal["read", "idempotent", "protected"]


class ToolNotExecuted(RuntimeError):
    """The provider confirmed the external operation never started.

    This is an ordinary tool failure, not an execution-control failure. The
    Agent loop may show the provider's rejection to the model and let it correct
    its arguments; the operation receipt is settled as ``not_executed``.
    """


@dataclass(frozen=True)
class BusinessTool:
    """An official SDK tool plus the business metadata kept outside it."""

    sdk_tool: FunctionTool
    domain: str
    effect: ToolEffect = "protected"
    drain_on_cancel: bool = False
    legacy_protected: bool = False

    @property
    def name(self) -> str:
        return self.sdk_tool.name

    @property
    def description(self) -> str:
        return self.sdk_tool.description


def register_tool(
    tool: FunctionTool,
    *,
    domain: str,
    effect: ToolEffect = "protected",
    drain_on_cancel: bool = False,
) -> BusinessTool:
    """Pair an SDK declaration with its domain, effect and cancel policy.

    ``drain_on_cancel`` marks tools whose business callable is synchronous: a
    cancelled SDK call must still wait for that thread and its receipt.
    """
    # Match the catalog stored by existing por.role_tools steps.
    tool.params_json_schema.update(title=tool.name, description=tool.description)
    return BusinessTool(tool, domain, effect, drain_on_cancel)


def business_tool(
    *,
    domain: str,
    effect: ToolEffect = "protected",
    drain_on_cancel: bool = False,
    **sdk_options: Any,
) -> Callable[[Callable[..., Any]], BusinessTool]:
    """Declare one SDK tool whose body runs inside the shared ToolExecutor.

    The decorated function must take ``ToolContext`` first; the remaining
    parameters keep the official SDK name, description, JSON Schema,
    validation and defaults. The wrapper passes the same context to
    ``ToolExecutor.execute`` after the SDK validated the arguments, so a
    business function only implements business logic.

    A synchronous business function is always drained on cancellation because
    its thread and external receipt outlive the cancelled coroutine.
    """
    options = {
        "use_docstring_info": False,
        "strict_mode": False,
        "failure_error_function": None,
        **sdk_options,
    }

    def declare(function: Callable[..., Any]) -> BusinessTool:
        parameters = list(signature(function).parameters.values())
        annotations = get_type_hints(function, include_extras=True)
        first_parameter = parameters[0] if parameters else None
        context_annotation = (
            annotations.get(first_parameter.name) if first_parameter else None
        )
        if (get_origin(context_annotation) or context_annotation) is not ToolContext:
            raise TypeError(
                f"business_tool {function.__name__} must take ToolContext as "
                "its first parameter."
            )

        @wraps(function)
        async def invoke(
            context: ToolContext[Any], /, *args: Any, **kwargs: Any
        ) -> Any:
            return await context.context.tool_executor.execute(
                context, function, context, *args, **kwargs
            )

        # The SDK derives the Schema from this declaration. Resolved hints keep
        # the business function's own module responsible for its annotations.
        invoke.__annotations__ = annotations
        return register_tool(
            function_tool(invoke, **options),
            domain=domain,
            effect=effect,
            drain_on_cancel=drain_on_cancel or not iscoroutinefunction(function),
        )

    return declare


def tool_result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(
        isinstance(block, dict) and block.get("type") == "text" for block in value
    ):
        return "\n".join(str(block.get("text", "")) for block in value)
    return json.dumps(value, ensure_ascii=False, default=str)
