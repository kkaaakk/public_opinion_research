"""Public model APIs; load the LangChain backend only for model construction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from open_deep_research.models.providers import (
    DEFAULT_ARK_BASE_URL,
    ModelConfigurationError,
    ModelRef,
    parse_model_ref,
    resolve_api_key,
    resolve_ark_base_url,
    resolve_base_url,
)

if TYPE_CHECKING:
    from open_deep_research.models.factory import (
        configurable_chat_model,
        create_chat_model,
    )

__all__ = [
    "DEFAULT_ARK_BASE_URL",
    "ModelConfigurationError",
    "ModelRef",
    "configurable_chat_model",
    "create_chat_model",
    "parse_model_ref",
    "resolve_api_key",
    "resolve_ark_base_url",
    "resolve_base_url",
    "get_api_key_for_model",
]


def __getattr__(name: str):
    """Preserve public construction exports without loading unused providers."""
    if name in {"configurable_chat_model", "create_chat_model"}:
        from open_deep_research.models import factory

        return getattr(factory, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_api_key_for_model(model_name: str, config: Mapping[str, Any] | None):
    """Compatibility wrapper around the unified credential resolver."""
    try:
        return resolve_api_key(model_name, config)
    except ModelConfigurationError:
        # Test doubles and third-party providers may have no known key mapping.
        # The construction boundary still rejects unknown real providers.
        return None
