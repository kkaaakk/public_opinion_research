"""The single authoritative factory for LangChain chat models."""

from __future__ import annotations

from typing import Any, Mapping

from langchain_classic.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

# Preserve historical imports while the resolver no longer imports any backend.
from open_deep_research.models.providers import (
    DEFAULT_ARK_BASE_URL as DEFAULT_ARK_BASE_URL,
)
from open_deep_research.models.providers import (
    ModelConfigurationError as ModelConfigurationError,
)
from open_deep_research.models.providers import (
    ModelRef as ModelRef,
)
from open_deep_research.models.providers import (
    parse_model_ref as parse_model_ref,
)
from open_deep_research.models.providers import (
    resolve_api_key as resolve_api_key,
)
from open_deep_research.models.providers import (
    resolve_ark_base_url as resolve_ark_base_url,
)
from open_deep_research.models.providers import (
    resolve_base_url,
)


# 为方舟模型提供基于工具调用的结构化输出适配。
class VolcengineChatOpenAI(ChatOpenAI):
    """Ark adapter that uses tool calling for portable structured output."""

    logical_model_ref: str

    # 使用方舟兼容的函数调用方式创建结构化响应链。
    def with_structured_output(self, schema: Any = None, **kwargs: Any) -> Any:
        """Build structured output using Ark-compatible function calling."""
        kwargs.setdefault("method", "function_calling")
        return super().with_structured_output(schema, **kwargs)


# 适配 DeepSeek Flash，在结构化输出时处理思考模式与工具选择的兼容性。
class DeepSeekFlashChat(ChatDeepSeek):
    """DeepSeek Flash adapter that disables thinking only for structured output."""

    logical_model_ref: str

    # 构建结构化输出时关闭不兼容的思考配置。
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


# 根据逻辑模型引用、凭据和供应商参数构造对应 LangChain 聊天模型。
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
    if ref.provider in {"openai", "volcengine", "deepseek", "groq"}:
        prefix = "groq" if ref.provider == "groq" else "openai"
        native_key = kwargs.pop(f"{prefix}_api_key", None)
        native_endpoint = kwargs.pop(f"{prefix}_api_base", None)
        api_key = api_key or native_key
        base_url = base_url or native_endpoint
    credential = api_key or resolve_api_key(ref, config)
    endpoint = resolve_base_url(ref, config, base_url)
    if endpoint is not None:
        kwargs["base_url"] = endpoint

    if ref.provider == "volcengine" or (
        ref.provider == "deepseek" and ref.model == "deepseek-flash"
    ):
        if not credential:
            key_name = (
                "ARK_API_KEY" if ref.provider == "volcengine" else "DEEPSEEK_API_KEY"
            )
            raise ModelConfigurationError(
                f"{key_name} is required for provider {ref.provider!r} and model {ref.model!r}."
            )
        metadata = dict(kwargs.pop("metadata", {}) or {})
        metadata.setdefault("logical_model", ref.original)
        adapter = (
            VolcengineChatOpenAI if ref.provider == "volcengine" else DeepSeekFlashChat
        )
        return adapter(
            model=ref.model,
            api_key=credential,
            metadata=metadata,
            logical_model_ref=ref.original,
            **kwargs,
        )

    # Native constructors may otherwise fall back to environment keys even when
    # GET_API_KEYS_FROM_CONFIG explicitly selected an empty operator key map.
    if ref.provider in {"openai", "deepseek", "groq"} and not credential:
        raise ModelConfigurationError(
            f"A configured credential is required for {ref.provider!r}."
        )
    if credential:
        kwargs["api_key"] = credential
    try:
        return init_chat_model(model=ref.original, **kwargs)
    except ImportError as exc:
        raise ModelConfigurationError(
            f"Provider {ref.provider!r} requires its optional LangChain integration; "
            "install the provider package named in the underlying ImportError."
        ) from exc


# 延迟构建模型的兼容代理，在最终配置阶段统一创建真实实例。
class _ConfigurableChatModel:
    """Lazy compatibility proxy for LangChain's configurable-model pattern."""

    # 保存待绑定工具和重试参数，此时不创建模型或发送请求。
    def __init__(
        self,
        *,
        bound_tools: tuple[tuple[Any, ...], dict[str, Any]] | None = None,
        retry_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._bound_tools = bound_tools
        self._retry_kwargs = retry_kwargs

    # 返回带新工具绑定信息的代理，保留现有重试配置。
    def bind_tools(self, tools: Any, **kwargs: Any) -> _ConfigurableChatModel:
        return _ConfigurableChatModel(
            bound_tools=((tools,), kwargs), retry_kwargs=self._retry_kwargs
        )

    # 返回带重试设置的代理，保留已有工具绑定。
    def with_retry(self, **kwargs: Any) -> _ConfigurableChatModel:
        return _ConfigurableChatModel(
            bound_tools=self._bound_tools, retry_kwargs=dict(kwargs)
        )

    # 合并配置后创建真实模型，再依次应用工具绑定和重试。
    def with_config(
        self, config: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        settings = dict(config or {})
        settings.update(kwargs)
        instance = create_chat_model(
            config=settings.pop("provider_config", None), **settings
        )
        if self._bound_tools is not None:
            args, tool_kwargs = self._bound_tools
            instance = instance.bind_tools(*args, **tool_kwargs)
        if self._retry_kwargs is not None:
            instance = instance.with_retry(**self._retry_kwargs)
        return instance


# 返回可延迟选择模型和供应商的配置代理。
def configurable_chat_model() -> _ConfigurableChatModel:
    """Return a lazy model whose final construction uses :func:`create_chat_model`."""
    return _ConfigurableChatModel()
