"""Unified chat-model construction for Open Deep Research."""

from langchain_core.runnables import RunnableConfig

from open_deep_research.models.factory import (
    DEFAULT_ARK_BASE_URL,
    ModelConfigurationError,
    ModelRef,
    configurable_chat_model,
    create_chat_model,
    parse_model_ref,
    resolve_api_key,
    resolve_ark_base_url,
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
    "get_api_key_for_model",
]


def get_api_key_for_model(model_name: str, config: RunnableConfig):
    """Compatibility wrapper around the unified credential resolver."""
    try:
        return resolve_api_key(model_name, config)
    except ModelConfigurationError:
        # Test doubles and third-party providers may not have a known
        # credential mapping. Model creation remains strict and will reject an
        # unknown real provider at the authoritative factory boundary.
        return None
