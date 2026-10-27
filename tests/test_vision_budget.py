"""Vision requests use the real LangChain lifecycle and the existing ledger."""

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

from open_deep_research.budget import (
    ModelBudgetExceeded,
    budget_capture,
    model_budget_admission,
)
from open_deep_research.configuration import Configuration
from open_deep_research.rag.loaders import knowledge

USAGE = {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}


class VisionModel(BaseChatModel):
    requests: list = []
    usage: dict | None = USAGE
    error: bool = False

    @property
    def _llm_type(self):
        return "vision-budget-fixture"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.requests.append(messages)
        if self.error:
            raise RuntimeError("provider unavailable")
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=" visible text ",
                        usage_metadata=self.usage,
                        response_metadata={"model_name": "vision-fixture"},
                    )
                )
            ]
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
    monkeypatch.setattr(knowledge, "init_chat_model", lambda **kw: model)
    with budget_capture() as captured:
        assert invoke() == "visible text"
    assert captured["model_calls"] == 1
    assert captured["total_tokens"] == (15 if usage else 0)
    assert captured["input_tokens"] == (12 if usage else 0)
    assert model.requests[0][0].content == [
        {"type": "text", "text": "Describe"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,Zml4dHVyZS1pbWFnZQ=="},
        },
    ]


def test_vision_provider_failure_counts_attempt_and_preserves_exception(monkeypatch):
    model = VisionModel(error=True)
    monkeypatch.setattr(knowledge, "init_chat_model", lambda **kw: model)
    with budget_capture() as captured:
        with pytest.raises(RuntimeError, match="provider unavailable"):
            invoke()
    assert captured["model_calls"] == 1
    assert captured["total_tokens"] == 0


@pytest.mark.parametrize("inside_runnable", [False, True])
def test_vision_parse_failure_preserves_observed_usage_once(
    monkeypatch, inside_runnable
):
    def fail(_):
        raise ValueError("parse failed")

    model = VisionModel()
    runnable = model | RunnableLambda(fail) if inside_runnable else model
    monkeypatch.setattr(knowledge, "init_chat_model", lambda **kw: runnable)
    if not inside_runnable:
        monkeypatch.setattr(knowledge, "_message_content_to_text", fail)
    with budget_capture() as outer:
        with budget_capture() as captured:
            with pytest.raises(ValueError, match="parse failed"):
                invoke()
        assert outer["model_calls"] == 0
    assert captured["model_calls"] == len(model.requests) == 1
    assert captured["total_tokens"] == 15


def test_vision_budget_rejects_request_before_provider_start(monkeypatch):
    model = VisionModel()
    monkeypatch.setattr(knowledge, "init_chat_model", lambda **kw: model)
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
    monkeypatch.setattr(knowledge, "init_chat_model", lambda **kw: model)
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
    def fail(**kwargs):
        raise ValueError("invalid model configuration")

    monkeypatch.setattr(knowledge, "init_chat_model", fail)
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
    from open_deep_research.workflow.business_steps import _step
    from open_deep_research.workflow.python_state import apply_patch

    model = VisionModel(
        error=outcome == "provider_failure",
        usage=None if outcome == "no_usage" else USAGE,
    )
    monkeypatch.setattr(knowledge, "init_chat_model", lambda **kw: model)
    if outcome == "parse_failure":

        def fail(_):
            raise ValueError("unreadable content")

        monkeypatch.setattr(knowledge, "_message_content_to_text", fail)
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
    patch = asyncio.run(
        _step("enrich_query_images", intake.enrich_query_images, state, config, None)
    )
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
    monkeypatch.setattr(knowledge, "init_chat_model", lambda **kw: model)
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
    from open_deep_research.workflow.business_steps import _step

    model = VisionModel()
    monkeypatch.setattr(knowledge, "init_chat_model", lambda **kw: model)

    def build(*args, **kwargs):
        invoke()
        raise ValueError("local assembly failed")

    monkeypatch.setattr(intake.query_images, "build_query_image_context", build)
    with pytest.raises(ValueError, match="local assembly failed") as failure:
        asyncio.run(
            _step("enrich_query_images", intake.enrich_query_images, {}, {}, None)
        )
    assert failure.value.budget_usage["model_calls"] == 1
    assert failure.value.budget_usage["total_tokens"] == 15
