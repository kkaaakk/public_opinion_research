"""Vision requests use SDK Usage and the existing ledger."""

import asyncio
import threading

import httpx2
import pytest
from sdk_model_support import Reply, install_inference, install_sequence

from open_deep_research.budget import (
    ModelBudgetExceeded,
    budget_capture,
    model_budget_admission,
)
from open_deep_research.configuration import Configuration
from open_deep_research.rag.loaders import knowledge

USAGE = {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}


class VisionModel:
    def __init__(self, *, usage=USAGE, error=False, status="stop"):
        self.requests = []
        self.usage = usage or {}
        self.error = error
        self.status = status

    def __call__(self, request):
        if request.settings is None or request.model_ref != "openai:vision-fixture":
            return None
        self.requests.append(request.messages)
        if self.error:
            raise RuntimeError("provider unavailable")
        return Reply(
            " visible text ",
            self.usage.get("input_tokens", 0),
            self.usage.get("output_tokens", 0),
            status=self.status,
        )


def invoke():
    return knowledge.extract_image_bytes_vision_text(
        b"fixture-image", "openai:vision-fixture", "Describe", 123, "image/jpeg"
    )


@pytest.mark.parametrize("usage", [USAGE, None])
def test_vision_success_captures_one_attempt_and_only_reported_tokens(
    monkeypatch, usage
):
    model = VisionModel(usage=usage)
    install_inference(monkeypatch, model)
    with budget_capture() as captured:
        assert invoke() == "visible text"
    assert captured["model_calls"] == 1
    assert captured["total_tokens"] == (15 if usage else 0)
    assert captured["input_tokens"] == (12 if usage else 0)
    assert model.requests[0][0]["content"] == [
        {"type": "input_text", "text": "Describe"},
        {
            "type": "input_image",
            "image_url": "data:image/jpeg;base64,Zml4dHVyZS1pbWFnZQ==",
            "detail": "auto",
        },
    ]


def test_vision_provider_failure_counts_attempt_and_preserves_exception(monkeypatch):
    model = VisionModel(error=True)
    install_inference(monkeypatch, model)
    with budget_capture() as captured:
        with pytest.raises(RuntimeError, match="RuntimeError") as failure:
            invoke()
        assert str(failure.value.__cause__) == "provider unavailable"
    assert captured["model_calls"] == 1
    assert captured["total_tokens"] == 0


@pytest.mark.parametrize("status", ["length", "content_filter"])
def test_vision_rejected_output_preserves_observed_usage_once(monkeypatch, status):
    model = VisionModel(status=status)
    install_inference(monkeypatch, model)
    with budget_capture() as outer:
        with budget_capture() as captured:
            with pytest.raises(ValueError, match="truncated|refused"):
                invoke()
        assert outer["model_calls"] == 0
    assert captured["model_calls"] == len(model.requests) == 1
    assert captured["total_tokens"] == 15


def test_vision_budget_rejects_request_before_provider_start(monkeypatch):
    model = VisionModel()
    install_inference(monkeypatch, model)
    with budget_capture() as captured:
        with model_budget_admission(
            Configuration(budget_enabled=True, max_model_calls=1), lambda: {}
        ):
            with pytest.raises(ModelBudgetExceeded):
                invoke()
    assert model.requests == []
    assert captured["model_calls"] == captured["total_tokens"] == 0


def test_vision_repeated_calls_consume_remaining_budget(monkeypatch):
    model = VisionModel()
    install_inference(monkeypatch, model)
    with budget_capture() as role:
        with model_budget_admission(
            Configuration(budget_enabled=True, max_model_calls=2), lambda: role
        ):
            invoke()
            with pytest.raises(ModelBudgetExceeded):
                invoke()
    assert len(model.requests) == role["model_calls"] == 1
    assert role["total_tokens"] == 15


def test_local_file_encoding_and_construction_errors_are_not_model_attempts(
    monkeypatch, tmp_path
):
    def fail(*args):
        raise ValueError("invalid model configuration")

    install_inference(monkeypatch, lambda request: pytest.fail("unexpected request"), open_model=fail)
    with budget_capture() as captured:
        with pytest.raises(FileNotFoundError):
            knowledge.extract_image_vision_text(
                tmp_path / "missing.png", "fixture", "", 1
            )
        with pytest.raises(TypeError):
            knowledge._invoke_vision_model(
                image_bytes=object(),
                mime_type="image/png",
                model="fixture",
                prompt="",
                max_tokens=1,
            )
        with pytest.raises(ValueError, match="invalid model configuration"):
            invoke()
    assert captured["model_calls"] == captured["total_tokens"] == 0


@pytest.mark.parametrize(
    "outcome", ["success", "provider_failure", "parse_failure", "no_usage"]
)
def test_intake_vision_delta_reaches_success_patch_once(monkeypatch, outcome):
    import asyncio

    from langchain_core.messages import HumanMessage
    from test_query_images import PNG_BYTES, _data_url

    from open_deep_research.workflow import intake
    from open_deep_research.workflow.python_state import apply_patch

    model = VisionModel(
        error=outcome == "provider_failure",
        usage=None if outcome == "no_usage" else USAGE,
        status="length" if outcome == "parse_failure" else "stop",
    )
    install_inference(monkeypatch, model)
    state = {
        "messages": [
            HumanMessage(
                content=[
                    {"type": "image_url", "image_url": {"url": _data_url(PNG_BYTES)}}
                ]
            )
        ],
        "runtime": {"budget": {"model_calls": 2}},
    }
    config = {
        "configurable": {
            "rag_vision_model": "openai:vision-fixture",
            "budget_enabled": True,
            "max_model_calls": 4,
        }
    }
    patch = asyncio.run(intake.enrich_query_images(state, config))
    assert patch["runtime"]["budget"]["model_calls"] == len(model.requests) == 1
    assert patch["runtime"]["budget"]["total_tokens"] == (
        0 if outcome in {"provider_failure", "no_usage"} else 15
    )
    assert bool(patch.get("messages")) == (outcome in {"success", "no_usage"})
    apply_patch(state, patch)
    assert state["runtime"]["budget"]["model_calls"] == 3


def test_intake_vision_admission_preserves_final_report_reserve(monkeypatch):
    import asyncio

    from langchain_core.messages import HumanMessage
    from test_query_images import _data_url

    from open_deep_research.workflow import intake

    model = VisionModel()
    install_inference(monkeypatch, model)
    state = {
        "messages": [
            HumanMessage(
                content=[{"type": "image_url", "image_url": {"url": _data_url()}}]
            )
        ],
        "runtime": {"budget": {"model_calls": 2}},
    }
    config = {"configurable": {"budget_enabled": True, "max_model_calls": 3}}
    assert asyncio.run(intake.enrich_query_images(state, config)) == {}
    assert model.requests == []


def test_intake_failure_after_vision_preserves_existing_step_error_accounting(
    monkeypatch,
):
    import asyncio

    from open_deep_research.workflow import intake

    model = VisionModel()
    install_inference(monkeypatch, model)

    def build(*args, **kwargs):
        invoke()
        raise ValueError("local assembly failed")

    monkeypatch.setattr(intake.query_images, "build_query_image_context", build)
    with pytest.raises(ValueError, match="local assembly failed") as failure:
        asyncio.run(intake.enrich_query_images({}, {}))
    assert failure.value.budget_usage["model_calls"] == 1
    assert failure.value.budget_usage["total_tokens"] == 15


class BlockingVision(VisionModel):
    def __init__(self, *, started, release, finished, wait_seconds=5, **kwargs):
        super().__init__(**kwargs)
        self.started, self.release, self.finished = started, release, finished
        self.wait_seconds = wait_seconds

    def __call__(self, request):
        if request.model_ref != "openai:vision-fixture":
            return None
        self.started.set()
        try:
            assert self.release.wait(self.wait_seconds), "Test did not release provider request"
            return super().__call__(request)
        finally:
            self.finished.set()


@pytest.mark.parametrize(
    "exit_kind", ["cancel", "timeout", "repeat_cancel", "repeat_cancel_deadline"]
)
@pytest.mark.parametrize("provider_error", [False, True])
def test_intake_waits_for_started_vision_before_settling_cancel_or_timeout(
    monkeypatch, exit_kind, provider_error
):
    from langchain_core.messages import HumanMessage
    from test_query_images import _data_url

    from open_deep_research.workflow import intake

    model = BlockingVision(
        started=threading.Event(),
        release=threading.Event(),
        finished=threading.Event(),
        error=provider_error,
    )
    install_inference(monkeypatch, model)
    state = {
        "messages": [
            HumanMessage(
                content=[
                    {"type": "image_url", "image_url": {"url": _data_url()}},
                    {"type": "image_url", "image_url": {"url": _data_url()}},
                ]
            )
        ]
    }
    cfg = {"configurable": {"rag_vision_model": "openai:vision-fixture"}}

    async def run():
        from open_deep_research.workflow.execution import error_chain, stage_timeout

        async def enrich():
            timeout = {"timeout": 0.1, "repeat_cancel_deadline": 10}.get(exit_kind)
            async with stage_timeout(timeout):
                return await intake.enrich_query_images(state, cfg)

        task = asyncio.create_task(enrich())
        try:
            assert await asyncio.to_thread(model.started.wait, 2)
            if exit_kind != "timeout":
                task.cancel()
            if exit_kind.startswith("repeat_cancel"):
                await asyncio.sleep(0.05)
                task.cancel()
            await asyncio.sleep(0.2)
            settled_before_provider = task.done()
        finally:
            model.release.set()
        with pytest.raises(
            asyncio.CancelledError if exit_kind != "timeout" else TimeoutError
        ) as failure:
            await task
        assert await asyncio.to_thread(model.finished.wait, 2)
        assert not settled_before_provider
        assert len(model.requests) == 1, "Cancellation admitted a later image request"
        usage = next(
            error.budget_usage
            for error in error_chain(failure.value)
            if hasattr(error, "budget_usage")
        )
        assert usage["model_calls"] == 1
        assert usage["total_tokens"] == (0 if provider_error else 15)

    asyncio.run(run())


def test_real_google_vision_reports_usage_without_optional_model_version(monkeypatch):
    import json

    from test_query_images import PNG_BYTES
    from test_sdk_models import _chat_text, _mock_http

    requests = []

    def respond(request):
        assert request.url.host == "generativelanguage.googleapis.com"
        requests.append(json.loads(request.content))
        body = _chat_text(request, content="visible")
        body.pop("model")
        return httpx2.Response(200, json=body)

    _mock_http(monkeypatch, respond)
    monkeypatch.setenv("GOOGLE_API_KEY", "fixture-only")
    with budget_capture() as captured:
        assert knowledge.extract_image_bytes_vision_text(
            PNG_BYTES, "google_genai:gemini-2.5-pro", "Describe", 123,
        ) == "visible"
    assert len(requests) == captured["model_calls"] == 1
    assert requests[0]["messages"][0]["content"][1]["type"] == "image_url"
    assert captured["total_tokens"] == 15


@pytest.mark.parametrize("named", [False, True])
def test_observable_partial_usage_survives_sdk_rejecting_billed_output(monkeypatch, named):
    from test_sdk_models import _mock_http, _reply

    def respond(request):
        body = _reply("responses", 2, "vision-fixture")
        body["status"] = "incomplete"
        body["incomplete_details"] = {"reason": "max_output_tokens"}
        if not named:
            body.pop("model")
        return httpx2.Response(200, json=body)

    _mock_http(monkeypatch, respond)
    with budget_capture() as captured, pytest.raises(ValueError, match="truncated"):
        invoke()
    assert captured["model_calls"] == 1 and captured["total_tokens"] == 15


def test_nameless_structured_retry_counts_each_observed_response_once(monkeypatch):
    from open_deep_research.llm.generation import generate_structured
    from open_deep_research.state import ClarifyWithUser

    install_sequence(
        monkeypatch,
        Reply({"invalid": True}, 12, 3),
        Reply({"need_clarification": False, "question": "", "verification": "ok"}, 12, 3),
    )
    with budget_capture() as captured:
        result, delta = asyncio.run(generate_structured(
            "openai:fixture", "question", ClarifyWithUser, max_attempts=2
        ))
    assert result.verification == "ok"
    assert captured["model_calls"] == delta["model_calls"] == 2
    assert captured["total_tokens"] == delta["total_tokens"] == 30
