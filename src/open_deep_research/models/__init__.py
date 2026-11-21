"""Public provider configuration API."""

from __future__ import annotations

from typing import Any, Mapping

from open_deep_research.models.providers import (
    DEFAULT_ARK_BASE_URL,
    ModelConfigurationError,
    ModelRef,
    parse_model_ref,
    resolve_api_key,
    resolve_ark_base_url,
    resolve_base_url,
)

__all__ = [
    "DEFAULT_ARK_BASE_URL",
    "ModelConfigurationError",
    "ModelRef",
    "parse_model_ref",
    "resolve_api_key",
    "resolve_ark_base_url",
    "resolve_base_url",
    "get_api_key_for_model",
]


def get_api_key_for_model(model_name: str, config: Mapping[str, Any] | None):
    """Compatibility wrapper around the unified credential resolver."""
    try:
        return resolve_api_key(model_name, config)
    except ModelConfigurationError:
        # Test doubles and third-party providers may have no known key mapping.
        # The construction boundary still rejects unknown real providers.
        return None
