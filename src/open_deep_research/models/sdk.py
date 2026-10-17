"""Opt-in official SDK models sharing the project's provider/key configuration."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Literal
from urllib.parse import urlparse

from agents import Model, OpenAIChatCompletionsModel, OpenAIResponsesModel
from openai import AsyncOpenAI

from open_deep_research.models.factory import (
    ModelConfigurationError,
    parse_model_ref,
    resolve_api_key,
    resolve_ark_base_url,
)

SDKModelAPI = Literal["responses", "chat_completions"]
_COMPATIBLE_ENDPOINTS = {
    "deepseek": ("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
    "groq": ("GROQ_API_BASE", "https://api.groq.com/openai/v1"),
}


@asynccontextmanager
async def sdk_model(
    model_ref: str,
    *,
    config: Mapping[str, Any] | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    api: SDKModelAPI | None = None,
    client: AsyncOpenAI | None = None,
) -> AsyncIterator[Model]:
    """Yield an official Responses/Chat Completions model and close owned clients.

    Explicit clients remain caller-owned. Unsupported providers fail before any
    request; their existing LangChain configuration remains available on the
    production path. ModelSettings belong on Agent/RunConfig, not a second loop.
    """
    ref = parse_model_ref(model_ref)
    if ref.provider not in {"openai", "volcengine", *_COMPATIBLE_ENDPOINTS}:
        raise ModelConfigurationError(
            f"Phase 0 has no verified SDK adapter for {ref.provider!r}; "
            "supply an official SDK Model directly or use the existing runtime."
        )
    model_api = api or ("responses" if ref.provider == "openai" else "chat_completions")
    if model_api not in {"responses", "chat_completions"}:
        raise ModelConfigurationError(
            "SDK model API must be responses or chat_completions."
        )
    model_class = (
        OpenAIResponsesModel if model_api == "responses" else OpenAIChatCompletionsModel
    )
    if client is not None:
        yield model_class(model=ref.model, openai_client=client)
        return
    credential = api_key or resolve_api_key(ref, config)
    if not credential:
        raise ModelConfigurationError(
            f"A configured credential is required for {ref.provider!r}."
        )
    configurable = config or {}
    nested = configurable.get("configurable")
    if isinstance(nested, Mapping):
        configurable = nested
    endpoint = base_url
    if ref.provider == "volcengine":
        endpoint = resolve_ark_base_url(config, base_url)
    elif ref.provider in _COMPATIBLE_ENDPOINTS:
        env_name, default_url = _COMPATIBLE_ENDPOINTS[ref.provider]
        endpoint = (
            base_url or configurable.get(env_name) or os.getenv(env_name) or default_url
        )
    elif endpoint is None:
        endpoint = configurable.get("OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    if endpoint is not None:
        endpoint = str(endpoint).strip().rstrip("/")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ModelConfigurationError(
                "Model base URL must be an absolute http(s) URL."
            )
    async with AsyncOpenAI(api_key=credential, base_url=endpoint) as owned_client:
        yield model_class(model=ref.model, openai_client=owned_client)
