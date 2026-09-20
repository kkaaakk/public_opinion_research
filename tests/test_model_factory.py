"""Tests for unified provider/model construction and default routing."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel

from open_deep_research.configuration import Configuration
from open_deep_research.models import (
    DEFAULT_ARK_BASE_URL,
    ModelConfigurationError,
    configurable_chat_model,
    create_chat_model,
    parse_model_ref,
    resolve_api_key,
    resolve_ark_base_url,
)
from open_deep_research.models import factory as factory_module
from open_deep_research.state import Summary


def test_parse_deepseek_model_ref() -> None:
    ref = parse_model_ref("deepseek:deepseek-flash")
    assert (ref.provider, ref.model, ref.original) == (
        "deepseek",
        "deepseek-flash",
        "deepseek:deepseek-flash",
    )


@pytest.mark.parametrize(
    "model_id",
    ["doubao-seed-2-0-mini-260428", "ep-20240920-example"],
)
def test_parse_volcengine_model_ref_accepts_preset_and_endpoint(model_id: str) -> None:
    ref = parse_model_ref(f"volcengine:{model_id}")
    assert ref.provider == "volcengine"
    assert ref.model == model_id


@pytest.mark.parametrize(
    ("model_ref", "env_name"),
    [
        ("deepseek:deepseek-flash", "DEEPSEEK_API_KEY"),
        ("volcengine:doubao-seed-2-0-mini-260428", "ARK_API_KEY"),
    ],
)
def test_credentials_from_environment(
    monkeypatch: pytest.MonkeyPatch, model_ref: str, env_name: str
) -> None:
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "false")
    monkeypatch.setenv(env_name, "environment-secret")
    assert resolve_api_key(model_ref, {}) == "environment-secret"


@pytest.mark.parametrize(
    ("model_ref", "env_name"),
    [
        ("openai:gpt-4.1-mini", "OPENAI_API_KEY"),
        ("anthropic:claude-sonnet-4", "ANTHROPIC_API_KEY"),
        ("google:gemini-2.5-pro", "GOOGLE_API_KEY"),
        ("deepseek:deepseek-flash", "DEEPSEEK_API_KEY"),
        ("groq:llama-3.3-70b-versatile", "GROQ_API_KEY"),
        ("volcengine:doubao-seed-2-0-mini-260428", "ARK_API_KEY"),
    ],
)
def test_credentials_from_oap_config(
    monkeypatch: pytest.MonkeyPatch, model_ref: str, env_name: str
) -> None:
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "true")
    config = {"configurable": {"apiKeys": {env_name: "injected-secret"}}}
    assert resolve_api_key(model_ref, config) == "injected-secret"


def test_ark_base_url_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARK_BASE_URL", raising=False)
    assert resolve_ark_base_url() == DEFAULT_ARK_BASE_URL
    monkeypatch.setenv("ARK_BASE_URL", "https://example.invalid/v3/")
    assert resolve_ark_base_url() == "https://example.invalid/v3"


@pytest.mark.parametrize(
    "model_id",
    ["doubao-seed-2-0-mini-260428", "ep-20240920-example"],
)
def test_volcengine_factory_accepts_preset_and_endpoint_model_ids(
    model_id: str,
) -> None:
    model = create_chat_model(f"volcengine:{model_id}", api_key="fake-key")
    assert isinstance(model, BaseChatModel)
    assert model.model_name == model_id
    assert "volcengine:" not in model.model_name
    assert model.logical_model_ref == f"volcengine:{model_id}"
    assert str(model.openai_api_base).rstrip("/") == DEFAULT_ARK_BASE_URL


def test_volcengine_structured_output_uses_function_calling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    sentinel = object()

    def fake_structured(self: Any, schema: Any = None, **kwargs: Any) -> object:
        captured.update({"schema": schema, **kwargs})
        return sentinel

    monkeypatch.setattr(
        factory_module.ChatOpenAI, "with_structured_output", fake_structured
    )
    model = create_chat_model(
        "volcengine:doubao-seed-2-0-mini-260428", api_key="fake-key"
    )
    assert model.with_structured_output(Summary, include_raw=True) is sentinel
    assert captured == {
        "schema": Summary,
        "include_raw": True,
        "method": "function_calling",
    }


def test_deepseek_flash_keeps_default_thinking_for_normal_calls() -> None:
    model = create_chat_model("deepseek:deepseek-flash", api_key="fake-key")
    assert isinstance(model, factory_module.DeepSeekFlashChat)
    assert not model.extra_body or "thinking" not in model.extra_body

    bound = model.bind_tools(
        [
            {
                "name": "lookup",
                "description": "Look up a value.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }
        ]
    )
    assert bound.kwargs.get("tool_choice") in (None, "auto")


def test_deepseek_flash_disables_thinking_only_for_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    sentinel = object()

    def fake_structured(self: Any, schema: Any = None, **kwargs: Any) -> object:
        captured.update(
            {
                "schema": schema,
                "extra_body": self.extra_body,
                **kwargs,
            }
        )
        return sentinel

    monkeypatch.setattr(
        factory_module.ChatDeepSeek, "with_structured_output", fake_structured
    )
    model = create_chat_model("deepseek:deepseek-flash", api_key="fake-key")
    assert model.with_structured_output(Summary, include_raw=True) is sentinel
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    assert captured["method"] == "function_calling"
    assert captured["include_raw"] is True
    assert not model.extra_body or "thinking" not in model.extra_body


def test_deepseek_structured_output_never_combines_enabled_thinking_with_forced_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_structured(self: Any, schema: Any = None, **kwargs: Any) -> object:
        captured["thinking"] = (self.extra_body or {}).get("thinking")
        captured["method"] = kwargs.get("method")
        captured["schema"] = schema
        return object()

    monkeypatch.setattr(
        factory_module.ChatDeepSeek, "with_structured_output", fake_structured
    )
    model = create_chat_model(
        "deepseek:deepseek-flash",
        api_key="fake-key",
        extra_body={"thinking": {"type": "enabled"}},
    )
    model.with_structured_output(Summary)
    assert captured == {
        "thinking": {"type": "disabled"},
        "method": "function_calling",
        "schema": Summary,
    }


@pytest.mark.parametrize(
    "model_ref",
    [
        "openai:gpt-4.1-mini",
        "anthropic:claude-sonnet-4",
        "google:gemini-2.5-pro",
        "deepseek:deepseek-chat",
        "groq:llama-3.3-70b-versatile",
    ],
)
def test_native_providers_delegate_to_langchain(
    monkeypatch: pytest.MonkeyPatch, model_ref: str
) -> None:
    captured: dict[str, Any] = {}
    sentinel = object()

    def fake_init_chat_model(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(factory_module, "init_chat_model", fake_init_chat_model)
    assert create_chat_model(model_ref, api_key="fake-key") is sentinel
    assert captured["model"] == model_ref
    assert captured["api_key"] == "fake-key"


def test_missing_ark_key_and_unknown_provider_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "false")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    with pytest.raises(ModelConfigurationError, match="ARK_API_KEY"):
        create_chat_model("volcengine:doubao-seed-2-0-mini-260428")
    with pytest.raises(ModelConfigurationError, match="Unknown model provider"):
        create_chat_model("typo:model")


def test_invalid_ark_base_url_does_not_disclose_key() -> None:
    with pytest.raises(ModelConfigurationError, match="ARK_BASE_URL") as exc_info:
        create_chat_model(
            "volcengine:doubao-seed-2-0-mini-260428",
            api_key="must-not-appear",
            base_url="not-a-url",
        )
    assert "must-not-appear" not in str(exc_info.value)


def test_configurable_proxy_preserves_bind_tools_and_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, Any]] = []

    class FakeModel:
        def bind_tools(self, *args: Any, **kwargs: Any) -> "FakeModel":
            events.append(("bind_tools", (args, kwargs)))
            return self

        def with_retry(self, **kwargs: Any) -> "FakeModel":
            events.append(("with_retry", kwargs))
            return self

    def fake_create(model_ref: str, **kwargs: Any) -> FakeModel:
        events.append(("create", (model_ref, kwargs)))
        return FakeModel()

    monkeypatch.setattr(factory_module, "create_chat_model", fake_create)
    result = (
        configurable_chat_model()
        .bind_tools(["tool"])
        .with_retry(stop_after_attempt=2)
        .with_config(
            {
                "model": "deepseek:deepseek-flash",
                "api_key": "fake-key",
                "max_tokens": 32,
            }
        )
    )
    assert isinstance(result, FakeModel)
    assert [event[0] for event in events] == ["create", "bind_tools", "with_retry"]
    assert events[0][1][0] == "deepseek:deepseek-flash"


def test_default_model_routing() -> None:
    config = Configuration()
    doubao = "volcengine:doubao-seed-2-0-mini-260428"
    deepseek = "deepseek:deepseek-flash"
    assert config.summarization_model == doubao
    assert config.rag_query_rewrite_model == doubao
    assert config.context_manager_model == doubao
    assert config.rolling_compaction_model == doubao
    assert config.research_graph_extraction_model == doubao
    assert config.research_model == deepseek
    assert config.compression_model == deepseek
    assert config.final_report_model == deepseek
