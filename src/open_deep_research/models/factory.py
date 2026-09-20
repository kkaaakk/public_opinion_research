"""The single authoritative factory for LangChain chat models."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

DEFAULT_ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"

_CREDENTIAL_ENV_BY_PROVIDER = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "groq": "GROQ_API_KEY",
    "volcengine": "ARK_API_KEY",
}

# Providers already delegated to LangChain by this project.  Keeping this list
# explicit prevents a typo from silently selecting another provider.
_LANGCHAIN_PROVIDERS = {
    "anthropic",
    "bedrock",
    "cohere",
    "deepseek",
    "google",
    "google_genai",
    "groq",
    "mistral",
    "ollama",
    "openai",
}


class ModelConfigurationError(ValueError):
    """Raised when a model reference or provider configuration is invalid."""


@dataclass(frozen=True)
class ModelRef:
    """A normalized ``provider:model`` reference."""

    provider: str
    model: str
    original: str


def parse_model_ref(value: str) -> ModelRef:
    """Parse a ``provider:model`` reference without constraining provider model IDs.

    In particular, Volcengine accepts both readable preset model IDs and custom
    ``ep-*`` endpoint IDs.  The factory owns only provider-prefix parsing; the
    upstream provider remains authoritative for model availability.
    """
    original = str(value or "").strip()
    if ":" not in original:
        raise ModelConfigurationError(
            f"Invalid model reference {original!r}; expected 'provider:model'."
        )
    provider, model = original.split(":", 1)
    provider = provider.strip().lower()
    model = model.strip()
    if not provider or not model:
        raise ModelConfigurationError(
            f"Invalid model reference {original!r}; provider and model are required."
        )
    if provider != "volcengine" and provider not in _LANGCHAIN_PROVIDERS:
        raise ModelConfigurationError(
            f"Unknown model provider {provider!r} for model {model!r}."
        )
    return ModelRef(provider=provider, model=model, original=f"{provider}:{model}")


def _configurable(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    nested = config.get("configurable")
    return nested if isinstance(nested, Mapping) else config


def resolve_api_key(
    model_ref: str | ModelRef,
    config: Mapping[str, Any] | None = None,
) -> str | None:
    """Resolve a provider credential from OAP config or the environment."""
    ref = model_ref if isinstance(model_ref, ModelRef) else parse_model_ref(model_ref)
    env_name = _CREDENTIAL_ENV_BY_PROVIDER.get(ref.provider)
    if env_name is None:
        return None
    if os.getenv("GET_API_KEYS_FROM_CONFIG", "false").strip().lower() == "true":
        configurable = _configurable(config)
        api_keys = configurable.get("apiKeys", {})
        if isinstance(api_keys, Mapping):
            value = api_keys.get(env_name)
            return str(value) if value else None
        return None
    return os.getenv(env_name) or None


def resolve_ark_base_url(
    config: Mapping[str, Any] | None = None,
    explicit: str | None = None,
) -> str:
    """Resolve and validate the Volcengine Ark OpenAI-compatible endpoint."""
    configurable = _configurable(config)
    value = (
        explicit
        or configurable.get("ARK_BASE_URL")
        or configurable.get("ark_base_url")
        or os.getenv("ARK_BASE_URL")
        or DEFAULT_ARK_BASE_URL
    )
    endpoint = str(value).strip().rstrip("/")
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ModelConfigurationError(
            "Invalid ARK_BASE_URL; expected an absolute http(s) URL."
        )
    return endpoint


class VolcengineChatOpenAI(ChatOpenAI):
    """Ark adapter that uses tool calling for portable structured output."""

    logical_model_ref: str

    def with_structured_output(self, schema: Any = None, **kwargs: Any) -> Any:
        """Build structured output using Ark-compatible function calling."""
        kwargs.setdefault("method", "function_calling")
        return super().with_structured_output(schema, **kwargs)


class DeepSeekFlashChat(ChatDeepSeek):
    """DeepSeek Flash adapter that disables thinking only for structured output."""

    logical_model_ref: str

    def with_structured_output(
        self,
        schema: Any = None,
        *,
        method: str = "function_calling",
        include_raw: bool = False,
        strict: bool | None = None,
        **kwargs: Any,
    ) -> Any:
        """Build structured output without an invalid thinking/tool-choice pair."""
        extra_body = dict(self.extra_body or {})
        thinking = dict(extra_body.get("thinking") or {})
        thinking["type"] = "disabled"
        extra_body["thinking"] = thinking
        structured_model = self.model_copy(update={"extra_body": extra_body})
        return ChatDeepSeek.with_structured_output(
            structured_model,
            schema,
            method=method,
            include_raw=include_raw,
            strict=strict,
            **kwargs,
        )


def create_chat_model(
    model_ref: str | None = None,
    *,
    model: str | None = None,
    config: Mapping[str, Any] | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """Create a LangChain chat model from one logical model reference."""
    logical_name = model_ref or model
    ref = parse_model_ref(str(logical_name or ""))
    credential = api_key or resolve_api_key(ref, config)

    if ref.provider == "volcengine":
        if not credential:
            raise ModelConfigurationError(
                f"ARK_API_KEY is required for provider 'volcengine' and model {ref.model!r}."
            )
        endpoint = resolve_ark_base_url(config, base_url)
        metadata = dict(kwargs.pop("metadata", {}) or {})
        metadata.setdefault("logical_model", ref.original)
        return VolcengineChatOpenAI(
            model=ref.model,
            api_key=credential,
            base_url=endpoint,
            metadata=metadata,
            logical_model_ref=ref.original,
            **kwargs,
        )

    if ref.provider == "deepseek" and ref.model == "deepseek-flash":
        if not credential:
            raise ModelConfigurationError(
                f"DEEPSEEK_API_KEY is required for provider 'deepseek' and model {ref.model!r}."
            )
        metadata = dict(kwargs.pop("metadata", {}) or {})
        metadata.setdefault("logical_model", ref.original)
        return DeepSeekFlashChat(
            model=ref.model,
            api_key=credential,
            metadata=metadata,
            logical_model_ref=ref.original,
            **kwargs,
        )

    init_kwargs = dict(kwargs)
    if credential:
        init_kwargs["api_key"] = credential
    return init_chat_model(model=ref.original, **init_kwargs)


class _ConfigurableChatModel:
    """Lazy compatibility proxy for LangChain's configurable-model pattern."""

    def __init__(
        self,
        *,
        bound_tools: tuple[tuple[Any, ...], dict[str, Any]] | None = None,
        retry_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._bound_tools = bound_tools
        self._retry_kwargs = retry_kwargs

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ConfigurableChatModel:
        return _ConfigurableChatModel(
            bound_tools=((tools,), kwargs), retry_kwargs=self._retry_kwargs
        )

    def with_retry(self, **kwargs: Any) -> _ConfigurableChatModel:
        return _ConfigurableChatModel(
            bound_tools=self._bound_tools, retry_kwargs=dict(kwargs)
        )

    def with_config(
        self, config: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        settings = dict(config or {})
        settings.update(kwargs)
        model_name = settings.pop("model", None)
        api_key = settings.pop("api_key", None)
        base_url = settings.pop("base_url", None)
        runtime_config = settings.pop("provider_config", None)
        instance = create_chat_model(
            model_name,
            config=runtime_config,
            api_key=api_key,
            base_url=base_url,
            **settings,
        )
        if self._bound_tools is not None:
            args, tool_kwargs = self._bound_tools
            instance = instance.bind_tools(*args, **tool_kwargs)
        if self._retry_kwargs is not None:
            instance = instance.with_retry(**self._retry_kwargs)
        return instance


def configurable_chat_model() -> _ConfigurableChatModel:
    """Return a lazy model whose final construction uses :func:`create_chat_model`."""
    return _ConfigurableChatModel()
