"""Public Opinion Research adapter using only DeerFlow's public extension API."""

from collections.abc import Mapping
from typing import Any

from deerflow_extension_api import (
    ActionContext,
    BackendAction,
    ExtensionRegistry,
    ModelTool,
    PluginContribution,
    ToolContext,
    extension,
)
from opinion_business import get_status, ping

__version__ = "0.1.0"
NAMESPACE = "public-opinion.phase0"

__all__ = ["NAMESPACE", "__version__", "install"]


@extension(api="0.2.7", name="opinion")
def install(registry: ExtensionRegistry, config: Mapping[str, Any]) -> None:
    """Register a bounded offline tool and a read-only status action."""
    if set(config) - {"enabled"}:
        raise ValueError("Unknown opinion extension configuration field")
    enabled = config.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")

    async def opinion_ping(payload: Mapping[str, Any], context: ToolContext) -> dict[str, str]:
        if set(payload) != {"text"}:
            raise ValueError("opinion_ping requires only text")
        return ping(payload["text"])

    async def status(payload: Mapping[str, Any], context: ActionContext) -> dict[str, Any]:
        if payload:
            raise ValueError("status takes no arguments")
        return {
            "namespace": NAMESPACE,
            "enabled": enabled,
            "extension_version": __version__,
            "business": get_status(),
        }

    contribution = PluginContribution(
        namespace=NAMESPACE,
        title="舆情业务连接检查 / Public opinion connectivity check",
        description="Offline Phase 0 package diagnostic; business research capabilities are not migrated yet.",
        enabled=enabled,
        backend=(BackendAction("status", status),),
        tools=(
            ModelTool(
                "opinion_ping",
                "Check the public opinion business package connection by echoing 1 to 200 characters. "
                "This offline diagnostic does not perform research, sentiment analysis or risk assessment.",
                {
                    "type": "object",
                    "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 200}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                opinion_ping,
            ),
        ),
    )
    if registry.plugin(contribution) is not True:
        raise RuntimeError("Opinion extension requires a host supporting PluginContribution")
