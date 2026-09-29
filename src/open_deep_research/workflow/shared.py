"""Shared model binding and correlation metadata for workflow stages."""

from collections.abc import Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig

from open_deep_research.models import configurable_chat_model
from open_deep_research.observability import invocation_metadata

configurable_model = configurable_chat_model()

def _with_correlation_metadata(
    config: RunnableConfig | None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a copied config with bounded LangSmith correlation metadata.

    The base config is never mutated, and the metadata is derived only from
    identifiers, never from graph state or user content.
    """
    merged: dict[str, Any] = dict(config) if isinstance(config, Mapping) else {}
    metadata: dict[str, Any] = {}
    existing = merged.get("metadata")
    if isinstance(existing, Mapping):
        metadata.update(existing)
    metadata.update(invocation_metadata(merged))
    if extra:
        metadata.update({str(key): value for key, value in extra.items() if value})
    merged["metadata"] = metadata
    return merged
