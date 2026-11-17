"""Business policy attached to official SDK FunctionTool declarations.

The SDK owns tool declarations: ``@function_tool`` produces the model-visible
name, description, JSON Schema, argument validation and defaults. This module
only records the domain and external-effect policy the business Registry and
ToolExecutor need.
"""

import json
from dataclasses import dataclass
from typing import Any, Literal

from agents import FunctionTool

ToolEffect = Literal["read", "idempotent", "protected"]


@dataclass(frozen=True)
class BusinessTool:
    """An official SDK tool plus the business metadata kept outside it."""

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


def tool_result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(
        isinstance(block, dict) and block.get("type") == "text" for block in value
    ):
        return "\n".join(str(block.get("text", "")) for block in value)
    return json.dumps(value, ensure_ascii=False, default=str)
