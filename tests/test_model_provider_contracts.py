"""Provider configuration and endpoint contracts at the single SDK factory."""

import asyncio
import json
from typing import get_type_hints

import httpx2
import pytest
from agents import OpenAIChatCompletionsModel, OpenAIResponsesModel
from test_sdk_models import _chat_text, _mock_http

from open_deep_research.llm.generation import generate_structured, generate_text
from open_deep_research.models import ModelConfigurationError, get_api_key_for_model
from open_deep_research.models.sdk import sdk_model
from open_deep_research.state import Summary


@pytest.mark.parametrize(
    "reference,endpoint",
    [
        ("openai:gpt-4.1-mini", "https://api.openai.com/v1"),
        ("volcengine:ep-fixture", "https://ark.cn-beijing.volces.com/api/v3"),
        ("deepseek:deepseek-flash", "https://api.deepseek.com"),
        ("groq:fixture", "https://api.groq.com/openai/v1"),
        ("anthropic:claude-sonnet-4", "https://api.anthropic.com/v1"),
        (
            "google:gemini-2.5-pro",
            "https://generativelanguage.googleapis.com/v1beta/openai",
        ),
        (
            "google_genai:gemini-2.5-pro",
            "https://generativelanguage.googleapis.com/v1beta/openai",
        ),
        ("ollama:llama2:70b", "http://localhost:11434/v1"),
        ("mistral:fixture", "https://api.mistral.ai/v1"),
        ("cohere:fixture", "https://api.cohere.ai/compatibility/v1"),
    ],
)
def test_keyed_sdk_routes_keep_logical_model_id_and_default_endpoint(
    monkeypatch, reference, endpoint
):
    for var in (
        "ARK_BASE_URL",
        "DEEPSEEK_API_BASE",
        "GROQ_API_BASE",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_MODEL_API",
        "GOOGLE_BASE_URL",
        "ANTHROPIC_BASE_URL",
        "OLLAMA_HOST",
        "MISTRAL_BASE_URL",
        "COHERE_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    provider, model_id = reference.split(":", 1)

    async def inspect():
        async with sdk_model(reference, api_key="fixture-only") as model:
            implementation = (
                OpenAIResponsesModel
                if provider == "openai"
                else OpenAIChatCompletionsModel
            )
            assert isinstance(model, implementation)
            assert model.model == model_id
            assert str(model._client.base_url).rstrip("/") == endpoint
            assert model._client.max_retries == 0
            assert model._client.api_key == "fixture-only"

    asyncio.run(inspect())


@pytest.mark.parametrize(
    "reference,key_var,url_var",
    [
        ("openai:fixture", "OPENAI_API_KEY", "OPENAI_BASE_URL"),
        ("volcengine:ep-fixture", "ARK_API_KEY", "ARK_BASE_URL"),
        ("deepseek:deepseek-flash", "DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE"),
        ("groq:fixture", "GROQ_API_KEY", "GROQ_API_BASE"),
    ],
)
@pytest.mark.parametrize("source", ["environment", "config", "explicit"])
def test_key_and_endpoint_precedence(monkeypatch, reference, key_var, url_var, source):
    monkeypatch.setenv(key_var, "key-env")
    monkeypatch.setenv(url_var, "https://env.example.test/v1")
    monkeypatch.setenv(
        "GET_API_KEYS_FROM_CONFIG", "true" if source == "config" else "false"
    )
    config = {
        "configurable": {
            "apiKeys": {key_var: "key-config"},
            **(
                {url_var: "https://config.example.test/v1"}
                if source != "environment"
                else {}
            ),
        }
    }
    explicit = (
        dict(api_key="key-explicit", base_url="https://explicit.example.test/v1")
        if source == "explicit"
        else {}
    )
    suffix = {"environment": "env", "config": "config", "explicit": "explicit"}[source]

    async def inspect():
        async with sdk_model(reference, config=config, **explicit) as model:
            assert model._client.api_key == "key-" + suffix
            assert (
                str(model._client.base_url).rstrip("/")
                == f"https://{suffix}.example.test/v1"
            )

    asyncio.run(inspect())


@pytest.mark.parametrize(
    "provider,key",
    [
        ("openai", "OPENAI_API_KEY"),
        ("volcengine", "ARK_API_KEY"),
        ("deepseek", "DEEPSEEK_API_KEY"),
        ("groq", "GROQ_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("google", "GOOGLE_API_KEY"),
    ],
)
def test_config_key_mode_does_not_fall_back_to_environment(monkeypatch, provider, key):
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "true")
    monkeypatch.setenv(key, "fixture-env")

    async def inspect():
        with pytest.raises(ModelConfigurationError, match="credential"):
            async with sdk_model(
                f"{provider}:fixture", config={"configurable": {"apiKeys": {}}}
            ):
                pytest.fail("Unconfigured key admitted")

    asyncio.run(inspect())


@pytest.mark.parametrize("provider", ["openai", "volcengine", "deepseek", "groq"])
def test_invalid_compatible_endpoint_does_not_disclose_credential(provider):
    async def inspect():
        with pytest.raises(ModelConfigurationError, match="absolute http") as failure:
            async with sdk_model(
                f"{provider}:fixture",
                api_key="secret-placeholder",
                base_url="relative/path",
            ):
                pytest.fail("Invalid endpoint admitted")
        assert "secret-placeholder" not in str(failure.value)

    asyncio.run(inspect())


def test_openai_chat_only_gateway_uses_shared_operator_configuration(monkeypatch):
    def respond(request):
        assert request.url.host == "gateway.example.test"
        assert request.url.path == "/v1/chat/completions"
        return httpx2.Response(200, json=_chat_text(request, content="gateway reply"))

    _mock_http(monkeypatch, respond)
    config = {
        "configurable": {
            "OPENAI_MODEL_API": "chat_completions",
            "OPENAI_API_BASE": "https://gateway.example.test/v1",
        }
    }
    result, used = asyncio.run(
        generate_text("openai:gateway-model", "question", config=config)
    )
    assert result == "gateway reply" and used["total_tokens"] == 15


@pytest.mark.parametrize("thinking", [None, {"type": "enabled", "budget_tokens": 1024}])
def test_claude_structured_mode_does_not_force_tools_during_manual_thinking(
    monkeypatch, thinking
):
    def respond(request):
        assert request.url.host == "api.anthropic.com"
        body = json.loads(request.content)
        assert body["max_completion_tokens"] == 2000
        answer = '{"summary":"evidence","key_excerpts":"source"}'
        if thinking is not None:
            assert body["thinking"] == thinking and "tool_choice" not in body
            assert "JSON object" in body["messages"][0]["content"]
            return httpx2.Response(200, json=_chat_text(request, content=answer))
        assert body["tool_choice"]["function"]["name"] == "Summary"
        response = _chat_text(request, content=None, finish="tool_calls")
        response["choices"][0]["message"]["tool_calls"] = [
            {
                "type": "function",
                "id": "summary-result",
                "function": {"name": "Summary", "arguments": answer},
            }
        ]
        return httpx2.Response(200, json=response)

    _mock_http(monkeypatch, respond)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-only")
    result, used = asyncio.run(
        generate_structured(
            "anthropic:claude-sonnet-4",
            "summarize",
            Summary,
            max_tokens=2000,
            extra_body={"thinking": thinking} if thinking else None,
        )
    )
    assert result.summary == "evidence" and used["model_calls"] == 1


def test_legacy_public_key_helper_supports_runtime_type_introspection():
    assert get_type_hints(get_api_key_for_model)["config"]
