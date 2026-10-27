"""Construct locked provider implementations without network or paid inference."""

import asyncio
import os
import subprocess
import sys

import pytest
from langchain_core.language_models import BaseChatModel

from open_deep_research.models import ModelConfigurationError, create_chat_model
from open_deep_research.models.sdk import sdk_model


@pytest.mark.parametrize(
    "reference,backend",
    [
        ("openai:gpt-4.1-mini", "langchain_openai"),
        ("volcengine:ep-fixture", "open_deep_research.models.factory"),
        ("deepseek:deepseek-flash", "open_deep_research.models.factory"),
        ("deepseek:deepseek-chat", "langchain_deepseek"),
        ("anthropic:claude-sonnet-4", "langchain_anthropic"),
        ("groq:llama-3.3-70b-versatile", "langchain_groq"),
        ("google:gemini-2.5-pro", "langchain_google_genai"),
        ("google_genai:gemini-2.5-pro", "langchain_google_genai"),
    ],
)
def test_real_keyed_provider_construction(reference, backend, monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "false")
    model = create_chat_model(reference, api_key="fixture-only", max_tokens=12)
    assert isinstance(model, BaseChatModel)
    assert type(model).__module__.startswith(backend)


def test_real_vertex_construction_uses_adc_contract_without_discovery():
    from google.auth.credentials import AnonymousCredentials

    model = create_chat_model(
        "google_vertexai:gemini-2.5-pro",
        project="fixture-project",
        location="us-central1",
        credentials=AnonymousCredentials(),
    )
    assert isinstance(model, BaseChatModel)
    assert type(model).__name__ == "ChatVertexAI"
    assert model.project == "fixture-project"
    assert model.full_model_name.endswith("/publishers/google/models/gemini-2.5-pro")


def test_real_bedrock_construction_uses_explicit_local_auth():
    model = create_chat_model(
        "bedrock:anthropic.claude-3-haiku-20240307-v1:0",
        region_name="us-east-1",
        aws_access_key_id="fixture-access",
        aws_secret_access_key="fixture-secret",
        endpoint_url="http://127.0.0.1:1",
    )
    assert isinstance(model, BaseChatModel)
    assert type(model).__name__ == "ChatBedrock"
    assert model.client.meta.endpoint_url == "http://127.0.0.1:1"


def test_real_ollama_community_fallback_preserves_model_and_endpoint(monkeypatch):
    import langchain_classic.chat_models.base as classic

    original = classic._check_pkg

    def check(package, *args, **kwargs):
        if package == "langchain_ollama":
            raise ImportError("exercise community fallback")
        return original(package, *args, **kwargs)

    monkeypatch.setattr(classic, "_check_pkg", check)
    model = create_chat_model("ollama:llama2:70b", base_url="http://127.0.0.1:11434")
    assert type(model).__module__.startswith("langchain_community")
    assert model.model == "llama2:70b"
    assert model.base_url == "http://127.0.0.1:11434"


@pytest.mark.parametrize(
    "reference,package",
    [
        ("mistral:mistral-small", "langchain_mistralai"),
        ("mistralai:mistral-small", "langchain_mistralai"),
        ("cohere:command-r", "langchain_cohere"),
    ],
)
def test_uninstalled_optional_provider_fails_with_installation_guidance(
    reference, package, monkeypatch
):
    import langchain_classic.chat_models.base as classic

    original = classic._check_pkg

    def check(name, *args, **kwargs):
        if name == package:
            raise ImportError("missing optional integration")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(classic, "_check_pkg", check)
    with pytest.raises(ModelConfigurationError, match="optional.*integration"):
        create_chat_model(reference, api_key="fixture-only")


@pytest.mark.parametrize(
    "reference,key,endpoint_key",
    [
        ("openai:fixture", "OPENAI_API_KEY", "OPENAI_BASE_URL"),
        ("deepseek:deepseek-flash", "DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE"),
        ("deepseek:deepseek-chat", "DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE"),
        ("groq:fixture", "GROQ_API_KEY", "GROQ_API_BASE"),
        ("volcengine:ep-fixture", "ARK_API_KEY", "ARK_BASE_URL"),
    ],
)
@pytest.mark.parametrize("source", ["config", "environment", "explicit"])
def test_sdk_and_langchain_resolve_the_same_key_and_endpoint(
    monkeypatch, reference, key, endpoint_key, source
):
    monkeypatch.setenv(
        "GET_API_KEYS_FROM_CONFIG", "true" if source == "config" else "false"
    )
    monkeypatch.setenv(key, "environment-key")
    monkeypatch.setenv(endpoint_key, "https://environment.example.test/v1/")
    config = {
        "configurable": {
            "apiKeys": {key: "config-key"},
            endpoint_key: "https://config.example.test/v1/",
        }
    }
    expected_source = "config" if source != "environment" else "environment"
    kwargs = {}
    if source == "explicit":
        expected_source = "explicit"
        kwargs = {
            "api_key": "explicit-key",
            "base_url": "https://explicit.example.test/v1/",
        }
    expected_key = expected_source + "-key"
    # Config endpoints override environment endpoints independently of key mode.
    expected_endpoint = (
        "https://explicit.example.test/v1"
        if source == "explicit"
        else "https://config.example.test/v1"
    )
    model = create_chat_model(reference, config=config, **kwargs)
    actual_key = getattr(model, "openai_api_key", None) or getattr(
        model, "groq_api_key", None
    )
    assert actual_key.get_secret_value() == expected_key
    actual_endpoint = getattr(model, "openai_api_base", None) or getattr(
        model, "groq_api_base", None
    )
    assert str(actual_endpoint).rstrip("/") == expected_endpoint

    async def check():
        async with sdk_model(reference, config=config, **kwargs) as native:
            client = native._client
            assert client.api_key == expected_key
            assert str(client.base_url).rstrip("/") == expected_endpoint

    asyncio.run(check())


@pytest.mark.parametrize(
    "module", ["open_deep_research.models", "open_deep_research.models.sdk"]
)
def test_native_resolver_import_does_not_load_langchain_backends(module):
    code = """
import importlib, sys
class Block:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'langchain_classic','langchain_openai','langchain_deepseek'}:
            raise AssertionError('eager LangChain backend import: ' + fullname)
sys.meta_path.insert(0, Block())
importlib.import_module(sys.argv[1])
from open_deep_research.models import parse_model_ref, resolve_api_key
assert parse_model_ref('openai:fixture').model == 'fixture'
"""
    result = subprocess.run(
        [sys.executable, "-c", code, module],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "PYTHON_DOTENV_DISABLED": "1",
            "OPENAI_AGENTS_DISABLE_TRACING": "1",
        },
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "reference,key",
    [
        ("openai:fixture", "OPENAI_API_KEY"),
        ("deepseek:deepseek-chat", "DEEPSEEK_API_KEY"),
        ("groq:fixture", "GROQ_API_KEY"),
    ],
)
def test_config_key_mode_never_falls_back_to_an_environment_credential(
    monkeypatch, reference, key
):
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "true")
    monkeypatch.setenv(key, "must-not-be-used")
    with pytest.raises(ModelConfigurationError, match="credential"):
        create_chat_model(reference, config={"configurable": {"apiKeys": {}}})

    async def check():
        with pytest.raises(ModelConfigurationError, match="credential"):
            async with sdk_model(reference, config={"configurable": {"apiKeys": {}}}):
                pytest.fail("Missing configuration key was admitted")

    asyncio.run(check())


@pytest.mark.parametrize(
    "reference", ["openai:fixture", "deepseek:deepseek-flash", "groq:fixture"]
)
def test_invalid_compatible_endpoint_is_rejected_by_both_backends(reference):
    with pytest.raises(ModelConfigurationError, match="absolute http"):
        create_chat_model(
            reference, api_key="must-not-appear", base_url="relative/path"
        )

    async def check():
        with pytest.raises(ModelConfigurationError, match="absolute http"):
            async with sdk_model(
                reference, api_key="must-not-appear", base_url="relative/path"
            ):
                pytest.fail("Invalid endpoint was admitted")

    asyncio.run(check())


def test_resolver_compatibility_exports_and_lazy_with_config_preserve_provider_settings(
    monkeypatch,
):
    from open_deep_research import models
    from open_deep_research.models import factory, providers

    for name in (
        "ModelRef",
        "ModelConfigurationError",
        "parse_model_ref",
        "resolve_api_key",
        "resolve_ark_base_url",
    ):
        assert (
            getattr(models, name) is getattr(factory, name) is getattr(providers, name)
        )
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "true")
    model = models.configurable_chat_model().with_config(
        {
            "model": "volcengine:ep-fixture",
            "max_tokens": 17,
            "provider_config": {
                "configurable": {
                    "apiKeys": {"ARK_API_KEY": "configured-key"},
                    "ARK_BASE_URL": "https://config.example.test/v3/",
                }
            },
        }
    )
    assert model.openai_api_key.get_secret_value() == "configured-key"
    assert str(model.openai_api_base).rstrip("/") == "https://config.example.test/v3"
    assert model.max_tokens == 17
