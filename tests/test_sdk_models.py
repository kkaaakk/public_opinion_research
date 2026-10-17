"""Validate official provider adapters via real SDK serialization and mock HTTP."""

import asyncio
import importlib.util
import json
import sys
from argparse import Namespace
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import pytest
from agents import Agent, ModelSettings, RunConfig, Runner, function_tool
from agents.testing import ScriptedModel, assistant_message
from openai import AsyncOpenAI, AuthenticationError, InternalServerError, RateLimitError

import open_deep_research.models.sdk as sdk_models
from open_deep_research.models.factory import ModelConfigurationError


def _reply(api, turn, model):
    if api == "responses":
        output = (
            [
                {
                    "type": "function_call",
                    "id": "fc-1",
                    "name": "lookup",
                    "call_id": "call-lookup",
                    "arguments": '{"query":"brake"}',
                    "status": "completed",
                }
            ]
            if turn == 1
            else [
                {
                    "type": "message",
                    "id": "message-2",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "SDK_MODEL_OK",
                            "annotations": [],
                        }
                    ],
                }
            ]
        )
        return {
            "id": f"resp-{turn}",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": model,
            "output": output,
            "parallel_tool_calls": True,
            "usage": {
                "input_tokens": 12,
                "output_tokens": 3,
                "total_tokens": 15,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
    message = {"role": "assistant", "content": "SDK_MODEL_OK"}
    if turn == 1:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-lookup",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"query":"brake"}'},
                }
            ],
        }
        if "deepseek" in model:
            message["reasoning_content"] = "Check the brake evidence."
    return {
        "id": f"chat-{turn}",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if turn == 1 else "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }


@pytest.mark.parametrize(
    "model_ref,api,key_name,endpoint",
    [
        (
            "openai:fixture-model",
            "responses",
            "OPENAI_API_KEY",
            "https://openai.example.test/v1",
        ),
        (
            "openai:fixture-chat",
            "chat_completions",
            "OPENAI_API_KEY",
            "https://openai.example.test/v1",
        ),
        (
            "deepseek:deepseek-flash",
            "chat_completions",
            "DEEPSEEK_API_KEY",
            "https://deepseek.example.test/v1",
        ),
        (
            "volcengine:ep-fixture",
            "chat_completions",
            "ARK_API_KEY",
            "https://ark.example.test/api/v3",
        ),
        (
            "groq:fixture-model",
            "chat_completions",
            "GROQ_API_KEY",
            "https://groq.example.test/openai/v1",
        ),
    ],
)
def test_official_sdk_provider_model_tool_model_protocol_and_configuration(
    monkeypatch, model_ref, api, key_name, endpoint
):
    async def scenario():
        requests = []
        clients = []
        settings = (
            {"extra_body": {"thinking": {"type": "disabled"}}}
            if model_ref.startswith("deepseek:")
            else {}
        )

        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            assert request.headers["authorization"] == "Bearer fixture-credential"
            assert str(request.url).startswith(endpoint)
            assert request.url.path.endswith(
                "/responses" if api == "responses" else "/chat/completions"
            )
            return httpx2.Response(
                200, json=_reply(api, len(requests), model_ref.split(":", 1)[1])
            )

        def client_factory(**kwargs):
            assert kwargs == {"api_key": "fixture-credential", "base_url": endpoint}
            client = AsyncOpenAI(
                **kwargs,
                max_retries=0,
                http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
            )
            clients.append(client)
            return client

        monkeypatch.setattr(sdk_models, "AsyncOpenAI", client_factory)
        monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "true")
        config = {
            "configurable": {
                "apiKeys": {key_name: "fixture-credential"},
                {
                    "openai": "OPENAI_BASE_URL",
                    "deepseek": "DEEPSEEK_API_BASE",
                    "volcengine": "ARK_BASE_URL",
                    "groq": "GROQ_API_BASE",
                }[model_ref.split(":", 1)[0]]: endpoint,
            }
        }
        calls = []

        @function_tool
        async def lookup(query: str) -> str:
            """Return fixed evidence for the requested query."""
            calls.append(query)
            return "brake fixture evidence"

        async with sdk_models.sdk_model(
            model_ref,
            config=config,
            api=api if model_ref == "openai:fixture-chat" else None,
        ) as model:
            result = await Runner.run(
                Agent(
                    name="protocol-fixture",
                    model=model,
                    tools=[lookup],
                    model_settings=ModelSettings(**settings),
                ),
                "Research brakes",
                run_config=RunConfig(tracing_disabled=True),
            )
        assert calls == ["brake"]
        assert result.final_output == "SDK_MODEL_OK"
        assert len(requests) == 2
        assert result.context_wrapper.usage.requests == 2
        assert result.context_wrapper.usage.input_tokens == 24
        assert result.context_wrapper.usage.output_tokens == 6
        assert clients[0].is_closed()
        if api == "responses":
            outputs = [
                item
                for item in requests[1]["input"]
                if item.get("type") == "function_call_output"
            ]
            assert outputs == [
                {
                    "call_id": "call-lookup",
                    "output": "brake fixture evidence",
                    "type": "function_call_output",
                }
            ]
        else:
            messages = requests[1]["messages"]
            assert any(
                message.get("role") == "tool"
                and message["tool_call_id"] == "call-lookup"
                for message in messages
            )
            if model_ref.startswith("deepseek:"):
                assert requests[1]["thinking"] == {"type": "disabled"}
                assistant = next(
                    message for message in messages if message.get("tool_calls")
                )
                assert assistant["reasoning_content"] == "Check the brake evidence."

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,error_class",
    [(401, AuthenticationError), (429, RateLimitError), (503, InternalServerError)],
)
def test_sdk_propagates_official_provider_errors_without_a_custom_loop(
    status, error_class
):
    async def scenario():
        def handler(request):
            return httpx2.Response(
                status,
                json={
                    "error": {
                        "message": "fixture provider failure",
                        "type": "fixture_error",
                        "code": "fixture",
                    }
                },
            )

        async with AsyncOpenAI(
            api_key="fixture",
            base_url="https://provider.example.test/v1",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        ) as client:
            async with sdk_models.sdk_model("openai:fixture", client=client) as model:
                with pytest.raises(error_class):
                    await Runner.run(
                        Agent(name="errors", model=model),
                        "test",
                        run_config=RunConfig(tracing_disabled=True),
                    )
            assert not client.is_closed()
        assert client.is_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "provider",
    ["anthropic", "bedrock", "cohere", "google", "google_genai", "mistral", "ollama"],
)
def test_unverified_sdk_providers_fail_explicitly(provider):
    async def scenario():
        with pytest.raises(ModelConfigurationError, match="no verified SDK adapter"):
            async with sdk_models.sdk_model(f"{provider}:fixture"):
                raise AssertionError("An unsupported provider was admitted")

    asyncio.run(scenario())


def test_sdk_credentials_and_explicit_endpoint_preserve_existing_resolution(
    monkeypatch,
):
    async def scenario():
        created = []

        def factory(**kwargs):
            created.append(kwargs)
            return AsyncOpenAI(**kwargs)

        monkeypatch.setattr(sdk_models, "AsyncOpenAI", factory)
        monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "false")
        monkeypatch.setenv("ARK_API_KEY", "fixture-env-key")
        monkeypatch.setenv("ARK_BASE_URL", "https://env.example.test/api/v3")
        async with sdk_models.sdk_model("volcengine:ep-fixture"):
            pass
        async with sdk_models.sdk_model(
            "volcengine:ep-fixture",
            api_key="explicit-fixture-key",
            base_url="https://explicit.example.test/v3",
        ):
            pass
        assert created == [
            {
                "api_key": "fixture-env-key",
                "base_url": "https://env.example.test/api/v3",
            },
            {
                "api_key": "explicit-fixture-key",
                "base_url": "https://explicit.example.test/v3",
            },
        ]
        monkeypatch.delenv("ARK_API_KEY")
        with pytest.raises(ModelConfigurationError, match="credential"):
            async with sdk_models.sdk_model("volcengine:ep-fixture"):
                pass
        with pytest.raises(ModelConfigurationError, match="absolute http"):
            async with sdk_models.sdk_model(
                "volcengine:ep-fixture", api_key="fixture", base_url="relative/path"
            ):
                pass

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "filename, entrypoint",
    [("sdk_model_smoke.py", "_run"), ("sdk_context_smoke.py", "run_smoke")],
)
def test_real_api_smoke_requires_explicit_opt_in_before_any_invocation(
    monkeypatch, capsys, filename, entrypoint
):
    path = Path(__file__).resolve().parents[1] / "scripts" / filename
    spec = importlib.util.spec_from_file_location("sdk_smoke_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", [filename, "--model", "openai:fixture"])

    async def forbidden(_args):
        raise AssertionError("Smoke made a real invocation without explicit opt-in")

    monkeypatch.setattr(module, entrypoint, forbidden)
    with pytest.raises(SystemExit) as failure:
        module.main()
    assert failure.value.code == 2
    assert "Real API smoke is disabled" in capsys.readouterr().err


@pytest.mark.parametrize("output", ["SDK_SMOKE_OK", "SDK_SMOKE_OK truncated"])
def test_provider_smoke_checks_final_output_and_bounds_deepseek_thinking(
    monkeypatch, output
):
    path = Path(__file__).resolve().parents[1] / "scripts" / "sdk_model_smoke.py"
    spec = importlib.util.spec_from_file_location("sdk_smoke_response_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    model = ScriptedModel([[assistant_message(output)]])

    @asynccontextmanager
    async def fixture_model(*_args, **_kwargs):
        yield model

    monkeypatch.setattr(module, "sdk_model", fixture_model)
    args = Namespace(model="deepseek:fixture", api="chat_completions")
    if output == "SDK_SMOKE_OK":
        asyncio.run(module._run(args))
    else:
        with pytest.raises(RuntimeError, match="unexpected final output"):
            asyncio.run(module._run(args))
    assert model.calls[0].model_settings.extra_body == {
        "thinking": {"type": "disabled"}
    }
    assert model.calls[0].model_settings.max_tokens == 256
