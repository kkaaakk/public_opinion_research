"""Real Core callbacks, one OI generation per model attempt, no LC tool recorder."""

import asyncio
import json

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from opentelemetry.trace import StatusCode
from test_token_accounting import (
    SMALL_USAGE,
    ScriptedChat,
    _error_step,
    _script,
    _text_step,
    _tool_step,
)

from open_deep_research.budget import ainvoke_model_with_budget, structured_output_chain
from open_deep_research.observability import telemetry
from open_deep_research.state import ClarifyWithUser


def generations(capture):
    return [
        s
        for s in capture.spans()
        if s.attributes.get("openinference.span.kind") == "LLM"
    ]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_one_generation_with_usage_and_native_parent(trace_capture, asynchronous):
    usage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "input_token_details": {"cache_read": 2, "cache_creation": 1},
        "output_token_details": {"reasoning": 2},
    }
    _script(_text_step("PRIVATE_RESPONSE_SENTINEL", usage))
    with telemetry.correlation(
        run_id="lc-run", role="public_signal", workflow_id="lc-run:role", generation=2
    ):
        with telemetry.business_span("evidence_extraction"):
            config = {
                "metadata": {
                    "ls_model_name": "offline-model",
                    "ls_provider": "openai",
                    "private": "PRIVATE_MEMORY_SENTINEL",
                }
            }
            model = ScriptedChat()
            if asynchronous:
                result = asyncio.run(model.ainvoke("PRIVATE_PROMPT_SENTINEL", config))
            else:
                result = model.invoke("PRIVATE_PROMPT_SENTINEL", config)
    assert result.content == "PRIVATE_RESPONSE_SENTINEL"
    llms = generations(trace_capture)
    assert len(llms) == 1
    span = llms[0]
    assert span.attributes["llm.token_count.total"] == 15
    assert span.attributes["llm.token_count.prompt_details.cache_read"] == 2
    assert span.attributes["llm.token_count.prompt_details.cache_write"] == 1
    assert span.attributes["llm.token_count.completion_details.reasoning"] == 2
    assert span.attributes["llm.model_name"] == "offline-model"
    assert span.attributes["llm.provider"] == "scripted"
    assert span.attributes["por.run_id"] == "lc-run"
    assert span.attributes["por.role"] == "public_signal"
    assert span.attributes["por.status"] == "succeeded"
    root = next(s for s in trace_capture.spans() if s.name == "evidence_extraction")
    assert span.parent.span_id == root.context.span_id
    # OI's Core lifecycle timestamps have datetime's microsecond precision.
    assert root.start_time // 1000 <= span.start_time // 1000
    assert span.start_time <= span.end_time
    assert span.end_time // 1000 <= root.end_time // 1000
    assert not any("PRIVATE_" in s.to_json() for s in trace_capture.spans())


def test_structured_retry_counts_only_actual_requests_and_keeps_budget(trace_capture):
    _script(
        _tool_step("ClarifyWithUser", {"wrong_field": True}, SMALL_USAGE),
        _tool_step(
            "ClarifyWithUser",
            {"need_clarification": False, "question": "", "verification": "ok"},
        ),
    )
    chain = structured_output_chain(ScriptedChat(), ClarifyWithUser, max_attempts=2)
    with telemetry.business_span("research_planning", run_id="retry-run"):
        result, delta = asyncio.run(
            ainvoke_model_with_budget(
                chain, [HumanMessage(content="PRIVATE_PROMPT_SENTINEL")]
            )
        )
    assert result["parsed"].verification == "ok"
    assert delta["model_calls"] == 2 and delta["total_tokens"] == 21
    spans = trace_capture.spans()
    llms = generations(trace_capture)
    assert len(llms) == 2
    assert sum(s.attributes["llm.token_count.total"] for s in llms) == 21
    assert not any(
        s.attributes.get("openinference.span.kind") in {"CHAIN", "TOOL", "RETRIEVER"}
        for s in spans
    )
    assert all(s.parent.span_id == spans[-1].context.span_id for s in llms)


def test_langchain_tool_chain_does_not_duplicate_sdk_owned_tool(trace_capture):
    @tool
    def query(query: str) -> str:
        """Controlled tool with an independent bounded model call."""
        return ScriptedChat().invoke(query).content

    _script(_text_step())
    with telemetry.business_span("tool_execution"):
        assert (RunnableLambda(lambda x: x) | query).invoke("private") == "done"
    assert len(generations(trace_capture)) == 1
    assert len(trace_capture.spans()) == 2


@pytest.mark.parametrize("cancel", [False, True])
def test_model_failure_and_cancel_end_one_generation_without_content(
    trace_capture, cancel
):
    error = (
        asyncio.CancelledError("PRIVATE_ERROR_SENTINEL")
        if cancel
        else RuntimeError("PRIVATE_ERROR_SENTINEL")
    )
    _script(_error_step(error))
    with telemetry.business_span("research_review"):
        with pytest.raises(type(error)):
            ScriptedChat().invoke("private")
    llms = generations(trace_capture)
    assert len(llms) == 1
    assert llms[0].status.status_code == StatusCode.ERROR
    assert llms[0].attributes["por.status"] == ("cancelled" if cancel else "failed")
    assert "llm.token_count.total" not in llms[0].attributes
    assert not any("PRIVATE_" in s.to_json() for s in trace_capture.spans())


def test_parallel_producer_and_sync_thread_identity(trace_capture):
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import AIMessage

    async def producer(role):
        model = FakeMessagesListChatModel(
            responses=[AIMessage(content="PRIVATE_" + role)]
        )
        with telemetry.correlation(
            run_id="parallel-run", role=role, invocation_id=role + ":1"
        ):
            with telemetry.business_span("producer"):
                await asyncio.to_thread(model.invoke, "private")

    async def run():
        await asyncio.gather(producer("public_signal"), producer("internal_knowledge"))

    asyncio.run(run())
    llms = generations(trace_capture)
    assert len(llms) == 2
    by_id = {s.context.span_id: s for s in trace_capture.spans()}
    for span in llms:
        parent = by_id[span.parent.span_id]
        assert span.attributes["por.role"] == parent.attributes["por.role"]
        assert (
            span.attributes["por.invocation_id"]
            == parent.attributes["por.invocation_id"]
        )
    assert {s.attributes["por.role"] for s in llms} == {
        "public_signal",
        "internal_knowledge",
    }
    assert "PRIVATE_" not in json.dumps([s.to_json() for s in trace_capture.spans()])


def test_sdk_and_langchain_share_one_pipeline_without_duplicate_tools(trace_capture):
    from agents import Agent, RunConfig, Runner, function_tool
    from agents.testing import ScriptedModel, assistant_message, function_call

    @function_tool
    async def lookup(query: str) -> str:
        result, delta = await ainvoke_model_with_budget(ScriptedChat(), query)
        assert delta["model_calls"] == 1 and delta["total_tokens"] == 15
        return result.content

    _script(_text_step())
    model = ScriptedModel(
        [
            [
                function_call(
                    "lookup", {"query": "PRIVATE_ARGS_SENTINEL"}, call_id="lookup-1"
                )
            ],
            [assistant_message("done")],
        ],
        emit_traces=True,
    )
    with telemetry.correlation(
        run_id="mixed-run", role="public_signal", invocation_id="mixed:1"
    ):
        asyncio.run(
            Runner.run(
                Agent(name="public_signal", model=model, tools=[lookup]),
                "private",
                run_config=RunConfig(trace_include_sensitive_data=False),
            )
        )
    spans = trace_capture.spans()
    llms = generations(trace_capture)
    assert len(llms) == 3
    assert (
        len([s for s in spans if s.attributes.get("openinference.span.kind") == "TOOL"])
        == 1
    )
    assert (
        len(
            [
                s
                for s in llms
                if s.instrumentation_scope.name
                == "openinference.instrumentation.langchain"
            ]
        )
        == 1
    )
    assert all(s.attributes["por.run_id"] == "mixed-run" for s in llms)
    assert not any("PRIVATE_" in s.to_json() for s in spans)


def test_actual_openai_langchain_model_over_mock_http(trace_capture):
    import httpx
    from langchain_openai import ChatOpenAI

    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "offline-chat",
                "object": "chat.completion",
                "created": 1,
                "model": "offline-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "PRIVATE_RESPONSE_SENTINEL",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "prompt_tokens_details": {"cached_tokens": 2},
                    "completion_tokens_details": {"reasoning_tokens": 1},
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        model = ChatOpenAI(
            model="offline-model",
            api_key="offline-only",
            base_url="http://127.0.0.1:1",
            http_client=client,
            max_retries=0,
            use_responses_api=False,
        )
        with telemetry.business_span("vision_generation", run_id="vision-run"):
            result = model.invoke(
                [
                    HumanMessage(
                        content=[
                            {"type": "text", "text": "PRIVATE_PROMPT_SENTINEL"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64,PRIVATE_IMAGE_SENTINEL"
                                },
                            },
                        ]
                    )
                ]
            )
    assert result.content == "PRIVATE_RESPONSE_SENTINEL" and len(requests) == 1
    llms = generations(trace_capture)
    assert len(llms) == 1
    assert llms[0].attributes["llm.provider"] == "openai"
    assert llms[0].attributes["llm.model_name"] == "offline-model"
    assert llms[0].attributes["llm.token_count.total"] == 15
    assert llms[0].attributes["llm.token_count.prompt_details.cache_read"] == 2
    assert not any("PRIVATE_" in s.to_json() for s in trace_capture.spans())


def test_partial_failure_usage_enriches_only_the_existing_generation(trace_capture):
    from uuid import uuid4

    from langchain_core.callbacks import CallbackManager
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk, LLMResult

    manager = CallbackManager.configure()
    run = manager.on_chat_model_start(
        {}, [[HumanMessage(content="PRIVATE_PROMPT_SENTINEL")]], run_id=uuid4()
    )[0]
    run.on_llm_error(
        RuntimeError("PRIVATE_ERROR_SENTINEL"),
        response=LLMResult(
            generations=[
                [
                    ChatGenerationChunk(
                        message=AIMessageChunk(
                            content="PRIVATE_PARTIAL_SENTINEL",
                            usage_metadata={
                                "input_tokens": 5,
                                "output_tokens": 2,
                                "total_tokens": 7,
                            },
                        )
                    )
                ]
            ]
        ),
    )
    llms = generations(trace_capture)
    assert len(llms) == 1 and llms[0].status.status_code == StatusCode.ERROR
    assert llms[0].attributes["llm.token_count.total"] == 7
    assert not any("PRIVATE_" in s.to_json() for s in trace_capture.spans())


def test_reported_model_label_is_not_lost_when_invocation_omits_it(trace_capture):
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import AIMessage

    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="private", response_metadata={"model_name": "reported-model"}
            )
        ]
    )
    model.invoke("private")
    llms = generations(trace_capture)
    assert len(llms) == 1
    assert llms[0].attributes["llm.model_name"] == "reported-model"
    assert "llm.token_count.total" not in llms[0].attributes


def test_cancelled_awaiter_closes_its_started_generation_without_replaying(
    trace_capture,
):
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import AIMessage

    async def run():
        entered = asyncio.Event()
        calls = []

        class BlockingModel(FakeMessagesListChatModel):
            async def _agenerate(self, *args, **kwargs):
                calls.append(1)
                entered.set()
                await asyncio.Event().wait()

        model = BlockingModel(responses=[AIMessage(content="private")])
        task = asyncio.create_task(model.ainvoke("PRIVATE_PROMPT_SENTINEL"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert calls == [1]

    with telemetry.correlation(run_id="cancel-run"):
        asyncio.run(run())
    llms = generations(trace_capture)
    assert len(llms) == 1
    assert llms[0].attributes["por.status"] == "cancelled"
    assert llms[0].attributes["por.effect_status"] == "unknown"
    assert llms[0].attributes["por.failure_kind"] == "model_callback_missing"
    assert "llm.token_count.total" not in llms[0].attributes
    assert "PRIVATE_" not in llms[0].to_json()


def test_budget_rejection_never_creates_a_generation_for_an_unissued_request(
    trace_capture,
):
    from test_token_accounting import _SCRIPT

    from open_deep_research.budget import (
        ModelBudgetExceeded,
        budget_capture,
        model_budget_admission,
    )
    from open_deep_research.configuration import Configuration

    async def run():
        with model_budget_admission(
            Configuration(budget_enabled=True, max_model_calls=1), lambda: {}
        ):
            await ainvoke_model_with_budget(ScriptedChat(), "private")

    _script(_text_step())
    with budget_capture() as usage:
        with pytest.raises(ModelBudgetExceeded):
            asyncio.run(run())
    assert len(_SCRIPT) == 1
    assert usage["model_calls"] == 0 and usage["total_tokens"] == 0
    assert generations(trace_capture) == []
