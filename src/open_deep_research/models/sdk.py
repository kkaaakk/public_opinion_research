"""Construct official SDK models and retain their provider's terminal status."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agents import Model, OpenAIChatCompletionsModel, OpenAIResponsesModel, Usage
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from open_deep_research.models.providers import (
    ModelConfigurationError,
    parse_model_ref,
    require_sdk_provider,
    resolve_api_key,
    resolve_base_url,
    sdk_api_for,
)

if TYPE_CHECKING:
    from httpx2 import Response

SDKModelAPI = Literal["responses", "chat_completions"]


@dataclass
class ProviderCompletion:
    """Completion facts omitted by SDK 0.24.0's normalized ModelResponse."""

    status: str | None = None
    reason: str | None = None
    usage: Usage | None = None


_completion: ContextVar[ProviderCompletion | None] = ContextVar(
    "sdk_provider_completion", default=None
)


@contextmanager
def observe_provider_completion() -> Iterator[ProviderCompletion]:
    completion = ProviderCompletion()
    token = _completion.set(completion)
    try:
        yield completion
    finally:
        _completion.reset(token)


async def _remember_completion(response: Response) -> None:
    """Read only terminal metadata from the nonstreaming provider response."""
    completion = _completion.get()
    if completion is None or not response.is_success:
        return
    path = response.request.url.path.rstrip("/")
    is_chat = path.endswith("/chat/completions")
    if not (is_chat or path.endswith("/responses")):
        return
    if "json" not in response.headers.get("content-type", "").lower():
        return
    await response.aread()
    try:
        body = response.json()
    except ValueError:
        return
    if not isinstance(body, dict):
        return

    if is_chat:
        choices = body.get("choices") or []
        choice = choices[0] if isinstance(choices, list) and choices else {}
        status = choice.get("finish_reason") if isinstance(choice, dict) else None
    else:
        status = body.get("status")
        details = body.get("incomplete_details")
        if isinstance(details, dict):
            completion.reason = details.get("reason")
    completion.status = status if isinstance(status, str) else None

    # Normally the SDK supplies Usage. Keep the provider totals for a rejected
    # response (e.g. a length stop before the first visible token).
    raw = body.get("usage")
    if isinstance(raw, dict):
        input_key, output_key = (
            ("prompt_tokens", "completion_tokens")
            if is_chat
            else ("input_tokens", "output_tokens")
        )
        counts = [raw.get(key) for key in (input_key, output_key, "total_tokens")]
        input_details = raw.get(f"{input_key}_details")
        output_details = raw.get(f"{output_key}_details")
        input_details = input_details if isinstance(input_details, dict) else {}
        output_details = output_details if isinstance(output_details, dict) else {}
        if any(type(count) is int and count >= 0 for count in counts):
            input_tokens, output_tokens, total_tokens = map(_reported_count, counts)
            completion.usage = Usage(
                requests=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                input_tokens_details={
                    "cached_tokens": _reported_count(
                        input_details.get(
                            "cached_tokens",
                            raw.get("prompt_cache_hit_tokens"),
                        )
                    ),
                    "cache_write_tokens": _reported_count(
                        input_details.get("cache_write_tokens")
                    ),
                },
                output_tokens_details={
                    "reasoning_tokens": _reported_count(
                        output_details.get("reasoning_tokens")
                    ),
                },
            )


def _reported_count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


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

    Explicit clients remain caller-owned. Every client disables HTTP retries so
    one SDK model invocation corresponds to one admitted request.
    """
    ref = parse_model_ref(model_ref)
    require_sdk_provider(ref)
    model_api = api or sdk_api_for(ref, config)
    if model_api not in {"responses", "chat_completions"}:
        raise ModelConfigurationError(
            "SDK model API must be responses or chat_completions."
        )
    model_class = (
        OpenAIResponsesModel if model_api == "responses" else OpenAIChatCompletionsModel
    )
    if client is not None:
        yield model_class(
            model=ref.model, openai_client=client.with_options(max_retries=0)
        )
        return
    credential = api_key or resolve_api_key(ref, config)
    if not credential and ref.provider == "ollama":
        credential = "ollama"
    if not credential:
        raise ModelConfigurationError(
            f"A configured credential is required for {ref.provider!r}."
        )
    endpoint = resolve_base_url(ref, config, base_url)
    async with DefaultAsyncHttpxClient(
        event_hooks={"response": [_remember_completion]}
    ) as http_client:
        async with AsyncOpenAI(
            api_key=credential,
            base_url=endpoint,
            http_client=http_client,
            max_retries=0,
        ) as owned_client:
            yield model_class(model=ref.model, openai_client=owned_client)
