"""Exercise SDK adapter conversion with local HTTP transport fixtures."""

import asyncio
import json

import httpx2
import pytest
from agents import (
    Agent,
    ModelSettings,
    OpenAIChatCompletionsModel,
    OpenAIResponsesCompactionSession,
    RunConfig,
    RunContextWrapper,
    Runner,
    SQLiteSession,
)
from openai import AsyncOpenAI


def local_client(handler):
    return AsyncOpenAI(
        api_key="offline-fixture",
        base_url="https://offline.invalid/v1",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


@pytest.mark.parametrize(
    "usage_kind", ["provider_fields", "standard_details", "omitted"]
)
def test_deepseek_chat_completions_usage(usage_kind):
    async def scenario():
        requests = []

        def respond(request):
            assert request.url.path == "/v1/chat/completions"
            requests.append(json.loads(request.content))
            data = {
                "id": "deepseek-offline",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-chat",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "fixture"},
                        "finish_reason": "stop",
                    }
                ],
            }
            if usage_kind != "omitted":
                data["usage"] = {
                    "prompt_tokens": 11,
                    "completion_tokens": 3,
                    "total_tokens": 14,
                    "prompt_cache_hit_tokens": 7,
                    "prompt_cache_miss_tokens": 4,
                }
            if usage_kind == "standard_details":
                data["usage"]["prompt_tokens_details"] = {"cached_tokens": 6}
                data["usage"]["completion_tokens_details"] = {"reasoning_tokens": 2}
            return httpx2.Response(200, json=data)

        async with local_client(respond) as client:
            result = await Runner.run(
                Agent(
                    name="deepseek",
                    model=OpenAIChatCompletionsModel(
                        model="deepseek-chat",
                        openai_client=client,
                    ),
                    model_settings=ModelSettings(
                        preserve_raw_usage=True, max_tokens=30
                    ),
                ),
                "brief",
                run_config=RunConfig(tracing_disabled=True),
            )
        usage = result.context_wrapper.usage
        raw = result.raw_responses[0].raw_usage
        assert len(requests) == 1
        assert requests[0]["model"] == "deepseek-chat"
        assert requests[0]["max_tokens"] == 30
        assert "context_management" not in requests[0]
        assert usage.requests == 1
        assert len(usage.request_usage_entries) == (0 if usage_kind == "omitted" else 1)
        if usage_kind == "omitted":
            assert usage.total_tokens == 0
            assert raw is None
        else:
            assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (
                11,
                3,
                14,
            )
            assert raw["prompt_cache_hit_tokens"] == 7
            assert usage.input_tokens_details.cached_tokens == (
                6 if usage_kind == "standard_details" else 0
            )
            assert usage.output_tokens_details.reasoning_tokens == (
                2 if usage_kind == "standard_details" else 0
            )

    asyncio.run(scenario())


def test_responses_compaction_uses_separate_endpoint_and_rejects_deepseek_model():
    async def scenario():
        requests = []
        storage = SQLiteSession("compaction-fixture")

        def respond(request):
            assert request.url.path == "/v1/responses/compact"
            requests.append(json.loads(request.content))
            return httpx2.Response(
                200,
                json={
                    "id": "compact-offline",
                    "created_at": 1,
                    "object": "response.compaction",
                    "output": [
                        {
                            "type": "compaction",
                            "id": "compacted",
                            "encrypted_content": "opaque-fixture",
                        }
                    ],
                    "usage": {
                        "input_tokens": 11,
                        "output_tokens": 3,
                        "total_tokens": 14,
                        "input_tokens_details": {
                            "cached_tokens": 0,
                            "cache_write_tokens": 0,
                        },
                        "output_tokens_details": {"reasoning_tokens": 0},
                    },
                },
            )

        try:
            async with local_client(respond) as client:
                with pytest.raises(ValueError, match="Unsupported model"):
                    OpenAIResponsesCompactionSession(
                        "compaction-fixture",
                        storage,
                        model="deepseek-chat",
                        client=client,
                    )
                assert requests == []
                await storage.add_items(
                    [{"role": "user", "content": "a large evidence set"}]
                )
                session = OpenAIResponsesCompactionSession(
                    "compaction-fixture",
                    storage,
                    model="gpt-4.1",
                    client=client,
                    compaction_mode="input",
                )
                wrapper = RunContextWrapper(context=None)
                await session.run_compaction({"force": True}, wrapper=wrapper)
                assert len(requests) == 1
                assert "a large evidence set" in json.dumps(requests[0])
                assert wrapper.usage.total_tokens == 14
                assert [item["type"] for item in await storage.get_items()] == [
                    "compaction"
                ]
        finally:
            storage.close()

    asyncio.run(scenario())
