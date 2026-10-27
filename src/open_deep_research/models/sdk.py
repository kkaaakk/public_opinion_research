"""Opt-in official SDK models sharing the project's provider/key configuration."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Literal

from agents import Model, OpenAIChatCompletionsModel, OpenAIResponsesModel
from openai import AsyncOpenAI

from open_deep_research.models.providers import (
    ModelConfigurationError,
    parse_model_ref,
    resolve_api_key,
    resolve_base_url,
)

SDKModelAPI = Literal["responses", "chat_completions"]


@asynccontextmanager
async def sdk_model(
    model_ref: str,
    *,
    config: Mapping[str, Any] | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    api: SDKModelAPI | None = None,
    client: AsyncOpenAI | None = None,
    max_retries: int | None = None,
) -> AsyncIterator[Model]:
    """Yield an official Responses/Chat Completions model and close owned clients.

    Explicit clients remain caller-owned. Unsupported providers fail before any
    request; their existing LangChain configuration remains available on the
    production path. ModelSettings belong on Agent/RunConfig, not a second loop.
    """
    ref = parse_model_ref(model_ref)
    if ref.provider not in {"openai", "volcengine", "deepseek", "groq"}:
        raise ModelConfigurationError(
            f"No verified SDK adapter for {ref.provider!r}; "
            "supply an official SDK Model directly. LangChain support is separate."
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
    endpoint = resolve_base_url(ref, config, base_url)
    client_options: dict[str, Any] = {"api_key": credential, "base_url": endpoint}
    if max_retries is not None:
        client_options["max_retries"] = max_retries
    async with AsyncOpenAI(**client_options) as owned_client:
        yield model_class(model=ref.model, openai_client=owned_client)
