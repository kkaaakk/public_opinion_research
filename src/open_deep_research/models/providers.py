"""Provider references and trusted configuration, independent of model backends."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

DEFAULT_ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"

_CREDENTIAL_ENV_BY_PROVIDER = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "groq": "GROQ_API_KEY",
    "volcengine": "ARK_API_KEY",
    "mistralai": "MISTRAL_API_KEY",
    "cohere": "COHERE_API_KEY",
    "ollama": "OLLAMA_API_KEY",
}

_PROVIDER_ALIASES = {"google": "google_genai", "mistral": "mistralai"}

# Keep known native-only references recognizable so preflight can explain
# their credential and protocol boundary.
_KNOWN_PROVIDERS = {
    "anthropic",
    "bedrock",
    "cohere",
    "deepseek",
    "google_vertexai",
    "google_genai",
    "groq",
    "mistralai",
    "ollama",
    "openai",
    "volcengine",
}


# 表示模型引用或供应商配置不合法的错误。
class ModelConfigurationError(ValueError):
    """Raised when a model reference or provider configuration is invalid."""


# 保存归一化后的供应商、模型 ID 及原始模型引用。
@dataclass(frozen=True)
class ModelRef:
    """A normalized ``provider:model`` reference."""

    provider: str
    model: str
    original: str


# 解析 provider:model 格式，同时保留供应商自定义模型和端点 ID。
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
    provider = _PROVIDER_ALIASES.get(provider, provider)
    model = model.strip()
    if not provider or not model:
        raise ModelConfigurationError(
            f"Invalid model reference {original!r}; provider and model are required."
        )
    if provider not in _KNOWN_PROVIDERS:
        raise ModelConfigurationError(
            f"Unknown model provider {provider!r} for model {model!r}."
        )
    return ModelRef(provider=provider, model=model, original=f"{provider}:{model}")


# 从运行配置安全取得供应商可配置项。
def _configurable(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    nested = config.get("configurable")
    return nested if isinstance(nested, Mapping) else config


# 按供应商从运行时配置或环境变量解析 API 凭据。
def credential_env_name(provider: str) -> str | None:
    """Return the environment variable that supplies this provider's key."""
    normalized = _PROVIDER_ALIASES.get(str(provider).strip().lower(), str(provider).strip().lower())
    return _CREDENTIAL_ENV_BY_PROVIDER.get(normalized)


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
    return os.getenv(env_name) or (
        os.getenv("GEMINI_API_KEY") if ref.provider == "google_genai" else None
    )


# 取得并校验火山方舟的 OpenAI 兼容接口地址。
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


# OpenAI-compatible endpoints share precedence, validation and defaults.
_COMPATIBLE_ENDPOINTS = {
    "openai": ("OPENAI_BASE_URL", None),
    "deepseek": ("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
    "groq": ("GROQ_API_BASE", "https://api.groq.com/openai/v1"),
    "anthropic": ("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
    "google_genai": (
        "GOOGLE_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
    ),
    "ollama": ("OLLAMA_HOST", "http://localhost:11434/v1"),
    "mistralai": ("MISTRAL_BASE_URL", "https://api.mistral.ai/v1"),
    "cohere": ("COHERE_BASE_URL", "https://api.cohere.ai/compatibility/v1"),
}


def resolve_base_url(
    model_ref: str | ModelRef,
    config: Mapping[str, Any] | None = None,
    explicit: str | None = None,
) -> str | None:
    """Resolve shared OpenAI-protocol endpoints; leave other protocols native."""
    ref = model_ref if isinstance(model_ref, ModelRef) else parse_model_ref(model_ref)
    if ref.provider == "volcengine":
        return resolve_ark_base_url(config, explicit)
    if ref.provider not in _COMPATIBLE_ENDPOINTS:
        return explicit
    key, default = _COMPATIBLE_ENDPOINTS[ref.provider]
    settings = _configurable(config)
    value = explicit or settings.get(key)
    if ref.provider == "openai":
        value = value or settings.get("OPENAI_API_BASE")
    value = value or os.getenv(key)
    if ref.provider == "openai":
        value = value or os.getenv("OPENAI_API_BASE")
    if not (value := value or default):
        return None
    endpoint = str(value).strip().rstrip("/")
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ModelConfigurationError("Model base URL must be an absolute http(s) URL.")
    if ref.provider == "ollama" and not parsed.path.rstrip("/").endswith("/v1"):
        endpoint += "/v1"
    return endpoint


def require_sdk_provider(ref: ModelRef) -> None:
    """Reject providers with no configured OpenAI-compatible SDK route."""
    if ref.provider == "google_vertexai":
        raise ModelConfigurationError(
            "google_vertexai (native ADC) has no SDK route. "
            "Use google_genai with a Gemini developer API key, or a compatible gateway."
        )
    if ref.provider == "bedrock":
        raise ModelConfigurationError(
            "bedrock (native AWS credentials) has no SDK route. "
            "Use a configured OpenAI-compatible gateway."
        )


def sdk_api_for(ref: ModelRef, config: Mapping[str, Any] | None) -> str:
    """Resolve the protocol once for the shared SDK factory and preflight."""
    if ref.provider != "openai":
        return "chat_completions"
    value = (
        _configurable(config).get("OPENAI_MODEL_API")
        or os.getenv("OPENAI_MODEL_API")
        or "responses"
    )
    if value not in ("responses", "chat_completions"):
        raise ModelConfigurationError(
            "OPENAI_MODEL_API must be responses or chat_completions."
        )
    return value
