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
from open_deep_research.budget import (
    ModelBudgetExceeded,
    budget_capture,
    model_budget_admission,
)
from open_deep_research.configuration import Configuration
from open_deep_research.llm import generation
from open_deep_research.llm.generation import (
    ModelOutputError,
    generate_structured,
    generate_text,
)
from open_deep_research.models import ModelConfigurationError
from open_deep_research.state import ResearchReview, Summary
from open_deep_research.workflow.brief import PlanDecision


def _mock_http(monkeypatch, handler):
    client_class = sdk_models.DefaultAsyncHttpxClient
    monkeypatch.setattr(
        sdk_models,
        "DefaultAsyncHttpxClient",
        lambda **kwargs: client_class(
            transport=httpx2.MockTransport(handler), **kwargs
        ),
    )
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "false")
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ARK_API_KEY", "GROQ_API_KEY"):
        monkeypatch.setenv(key, "fixture-key")
    monkeypatch.delenv("OPENAI_MODEL_API", raising=False)


def _chat_text(request, *, content, finish="stop"):
    body = _reply("chat_completions", 2, json.loads(request.content)["model"])
    choice = body["choices"][0]
    choice["message"]["content"] = content
    choice["finish_reason"] = finish
    return body


def test_openai_plan_and_review_validate_and_bill_each_structured_attempt(monkeypatch):
    brief = "# Scope\n\n## Objective\nVerify recall claims and document independent evidence."
    payloads = iter(
        [
            {"action": "ready", "questions": [], "brief_md": "not Markdown"},
            {"action": "ready", "questions": [], "brief_md": brief},
            {
                "research_complete": False,
                "next_tasks": [
                    {
                        "task_id": "notice",
                        "objective": "Find notice",
                        "target_role": "public_signal",
                        "evidence_needed": "Official recall notice",
                        "reason": "Verify date",
                    }
                ],
            },
        ]
    )
    bodies = []
    receipts = []

    async def begin(_model_ref, _max_output_tokens):
        return len(receipts) + 1

    async def settle(identity, usage):
        receipts.append((identity, usage))

    def respond(request):
        assert request.url.path.endswith("/responses")
        request_body = json.loads(request.content)
        bodies.append(request_body)
        body = _reply("responses", 2, request_body["model"])
        body["output"][0]["content"][0]["text"] = json.dumps(next(payloads))
        return httpx2.Response(200, json=body)

    _mock_http(monkeypatch, respond)
    monkeypatch.setattr(generation, "begin_model_call", begin)
    monkeypatch.setattr(generation, "end_model_call", settle)
    with budget_capture() as captured:
        decision, used = asyncio.run(
            generate_structured(
                "openai:fixture",
                "Investigate recall",
                PlanDecision,
                max_attempts=2,
                max_tokens=450,
            )
        )
        review, review_usage = asyncio.run(
            generate_structured(
                "openai:fixture",
                "Review recall evidence",
                ResearchReview,
            )
        )
    assert decision.brief_md == brief and used["total_tokens"] == 30
    assert (
        review.next_tasks[0].priority == "medium" and review_usage["model_calls"] == 1
    )
    assert [identity for identity, _ in receipts] == [1, 2, 3]
    assert [usage["total_tokens"] for _, usage in receipts] == [15, 15, 15]
    assert captured["model_calls"] == 3 and captured["total_tokens"] == 45
    assert bodies[0]["text"]["format"]["schema"]["title"] == "PlanDecision"
    assert bodies[0]["max_output_tokens"] == 450
    assert bodies[2]["text"]["format"]["schema"]["title"] == "ResearchReview"
    assert all(request.get("tools", []) == [] for request in bodies)


@pytest.mark.parametrize(
    "reference",
    [
        "deepseek:deepseek-flash",
        "deepseek:deepseek-v4-flash",
        "groq:fixture",
    ],
)
def test_json_mode_uses_sdk_schema_and_preserves_plain_text_settings(
    monkeypatch, reference
):
    bodies = []

    def respond(request):
        assert request.url.path.endswith("/chat/completions")
        bodies.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json=_chat_text(
                request,
                content='{"summary":"Evidence","key_excerpts":"Quote"}'
                if len(bodies) == 1
                else "plain",
            ),
        )

    _mock_http(monkeypatch, respond)
    summary, usage = asyncio.run(
        generate_structured(reference, "Summarize", Summary, max_tokens=140)
    )
    plain, plain_usage = asyncio.run(generate_text(reference, "Answer", max_tokens=65))
    assert (
        isinstance(summary, Summary)
        and summary.key_excerpts == "Quote"
        and plain == "plain"
    )
    assert usage["model_calls"] == plain_usage["model_calls"] == 1
    structured, text = bodies
    assert structured["response_format"] == {"type": "json_object"}
    assert structured["max_tokens"] == 140 and text["max_tokens"] == 65
    assert "key_excerpts" in structured["messages"][0]["content"]
    assert "tool_choice" not in structured and "response_format" not in text
    if reference.startswith("deepseek:"):
        assert structured["thinking"] == {"type": "disabled"}
        assert "thinking" not in text


@pytest.mark.parametrize("model", ["ep-fixture", "doubao-seed-2-0-mini-260428"])
def test_ark_uses_one_output_cap_and_function_schema(monkeypatch, model):
    def respond(request):
        assert request.url.path.endswith("/api/v3/chat/completions")
        sent = json.loads(request.content)
        assert sent["model"] == model
        assert sent["max_completion_tokens"] == 321 and "max_tokens" not in sent
        assert "response_format" not in sent
        assert sent["tool_choice"]["function"]["name"] == "Summary"
        function = sent["tools"][0]["function"]
        assert function["name"] == "Summary"
        assert function["parameters"]["required"] == ["summary", "key_excerpts"]
        body = _reply("chat_completions", 1, model)
        body["choices"][0]["message"]["tool_calls"][0]["function"] = {
            "name": "Summary",
            "arguments": '{"summary":"Evidence","key_excerpts":"Quote"}',
        }
        return httpx2.Response(200, json=body)

    _mock_http(monkeypatch, respond)
    monkeypatch.setenv("ARK_BASE_URL", "https://ark.example.test/api/v3")
    result, usage = asyncio.run(
        generate_structured(
            f"volcengine:{model}",
            "Summarize",
            Summary,
            max_tokens=321,
        )
    )
    assert result.summary == "Evidence" and usage["total_tokens"] == 15


@pytest.mark.parametrize(
    "status,content,message",
    [
        ("length", "Visible but partial", "truncated"),
        ("length", "", "truncated"),
        ("content_filter", "partial before policy filter", "refused"),
        ("stop", "", "empty"),
        (None, "No status", "confirm model completion"),
    ],
)
def test_chat_rejects_incomplete_output_after_usage_settlement(
    monkeypatch, status, content, message
):
    _mock_http(
        monkeypatch,
        lambda request: httpx2.Response(
            200,
            json=_chat_text(request, content=content, finish=status),
        ),
    )
    with budget_capture() as usage, pytest.raises(ModelOutputError, match=message):
        asyncio.run(generate_text("deepseek:deepseek-flash", "Write a full report"))
    assert usage["model_calls"] == 1 and usage["total_tokens"] == 15


@pytest.mark.parametrize(
    "status,reason,match",
    [
        ("incomplete", "max_output_tokens", "truncated"),
        ("incomplete", "content_filter", "refused"),
        ("failed", None, "confirm model completion"),
    ],
)
def test_responses_checks_top_level_completion_even_with_text(
    monkeypatch, status, reason, match
):
    def respond(request):
        body = _reply("responses", 2, "fixture")
        body["status"] = status
        body["incomplete_details"] = {"reason": reason} if reason else None
        return httpx2.Response(200, json=body)

    _mock_http(monkeypatch, respond)
    with budget_capture() as used, pytest.raises(ModelOutputError, match=match):
        asyncio.run(generate_text("openai:fixture", "Write the report"))
    assert used["total_tokens"] == 15


def test_retry_admission_sees_consumed_budget_and_never_sends_rejected_attempt(
    monkeypatch,
):
    sent = []

    def respond(request):
        sent.append(request)
        return httpx2.Response(
            200, json=_chat_text(request, content='{"unknown":false}')
        )

    _mock_http(monkeypatch, respond)
    cfg = Configuration(budget_enabled=True, max_model_calls=2)
    with budget_capture() as used, model_budget_admission(cfg, dict):
        with pytest.raises(ModelBudgetExceeded):
            asyncio.run(
                generate_structured(
                    "deepseek:deepseek-flash",
                    "Try",
                    Summary,
                    max_attempts=3,
                )
            )
    assert len(sent) == used["model_calls"] == 1 and used["total_tokens"] == 15


def test_sdk_never_retries_transport_or_http_errors_internally(monkeypatch):
    sent = []

    def respond(request):
        sent.append(request)
        return httpx2.Response(503, json={"error": {"message": "fixture unavailable"}})

    _mock_http(monkeypatch, respond)
    with (
        budget_capture() as used,
        pytest.raises(RuntimeError, match="InternalServerError"),
    ):
        asyncio.run(
            generate_structured(
                "deepseek:deepseek-flash",
                "Try",
                Summary,
                max_attempts=3,
            )
        )
    assert len(sent) == used["model_calls"] == 1 and used["total_tokens"] == 0


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
            assert kwargs["api_key"] == "fixture-credential"
            assert kwargs["base_url"] == endpoint
            assert kwargs["max_retries"] == 0
            client = AsyncOpenAI(**kwargs)
            clients.append(client)
            return client

        _mock_http(monkeypatch, handler)
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
    ["bedrock", "google_vertexai"],
)
def test_unverified_sdk_providers_fail_explicitly(provider):
    async def scenario():
        with pytest.raises(ModelConfigurationError, match="has no SDK route"):
            async with sdk_models.sdk_model(f"{provider}:fixture"):
                raise AssertionError("An unsupported provider was admitted")

    asyncio.run(scenario())


def test_sdk_credentials_and_explicit_endpoint_preserve_existing_resolution(
    monkeypatch,
):
    async def scenario():
        created = []

        def factory(**kwargs):
            created.append(
                {key: value for key, value in kwargs.items() if key != "http_client"}
            )
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
                "max_retries": 0,
            },
            {
                "api_key": "explicit-fixture-key",
                "base_url": "https://explicit.example.test/v3",
                "max_retries": 0,
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
    [("sdk_model_smoke.py", "_run")],
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
