"""Verify the single rolling history compaction used by SDK Agent invocations."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from agents.run import ModelInputData
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from open_deep_research.configuration import Configuration
from open_deep_research.llm.context import (
    UNKNOWN_MODEL_CONTEXT_CAPACITY,
    get_model_token_limit,
    resolve_context_capacity,
)
from open_deep_research.runtime import sdk_runtime
from open_deep_research.runtime.compaction import (
    RollingCompactOutput,
    summarize_history,
)
from open_deep_research.runtime.sdk_runtime import (
    SDKContextLimitExceeded,
    _BusinessAgentHarness,
    _RunBudget,
)


class _StructuredModel:
    """Return one structured summary and record the compaction prompt."""

    def __init__(self, summary="MERGED SUMMARY"):
        self.summary = summary
        self.prompts = []

    def with_structured_output(self, schema, include_raw=False):
        async def respond(messages):
            self.prompts.append(str(messages[0].content))
            return {
                "raw": AIMessage(
                    content="",
                    usage_metadata={
                        "input_tokens": 5,
                        "output_tokens": 3,
                        "total_tokens": 8,
                    },
                ),
                "parsed": schema(rolling_summary=self.summary),
            }

        return RunnableLambda(respond)


def _harness(*, model_factory=None, capacity=None, recent=1):
    config = Configuration(
        research_model="openai:fixture",
        context_capacity_tokens=capacity,
        recent_raw_steps=recent,
    )
    return _BusinessAgentHarness(
        role="public_signal",
        task_id="task-1",
        research_round=1,
        config=config,
        budget=_RunBudget(initial={}),
        model_factory=model_factory or (lambda *_: pytest.fail("unexpected model")),
    )


def _unit(turn, payload):
    return [
        {"role": "assistant", "content": f"analysis {turn} {payload}"},
        {
            "type": "function_call",
            "call_id": f"call-{turn}",
            "name": "search",
            "arguments": json.dumps({"query": payload}),
        },
        {
            "type": "function_call_output",
            "call_id": f"call-{turn}",
            "output": payload,
        },
    ]


def _input(unit_count=3, payload_size=400):
    payload = "evidence " * payload_size
    items = [{"role": "user", "content": "initial task"}]
    harness = _harness()
    starts = []
    for turn in range(unit_count):
        unit = _unit(turn, payload)
        items.extend(unit)
        starts.append(harness._item_key(unit[0]))
    harness._response_starts = starts
    return harness, ModelInputData(input=items, instructions="system prompt")


def test_summarize_history_merges_previous_summary_and_keeps_task():
    model = _StructuredModel()
    summary, _budget = asyncio.run(
        summarize_history(
            "older tool steps",
            previous_summary="PREVIOUS",
            task_context="CURRENT TASK",
            model=model,
            model_name="fixture:summary",
        )
    )
    assert summary == "MERGED SUMMARY"
    prompt = model.prompts[0]
    assert "PREVIOUS" in prompt
    assert "CURRENT TASK" in prompt
    assert "older tool steps" in prompt
    assert "Research Graph" not in prompt


def test_summarize_history_rejects_empty_summary():
    with pytest.raises(ValueError, match="empty summary"):
        asyncio.run(
            summarize_history(
                "steps",
                previous_summary="",
                task_context="task",
                model=_StructuredModel(summary="  "),
                model_name="fixture:summary",
            )
        )


def test_compaction_is_skipped_below_the_threshold():
    harness, data = _input(unit_count=1, payload_size=1)
    agent = SimpleNamespace(tools=[])
    result = asyncio.run(harness._compact_history(data, agent))
    assert result == data
    assert harness.rolling_summary == ""
    assert harness._compacted_turns == set()


def test_compaction_summarizes_older_turns_and_keeps_recent_raw(monkeypatch):
    harness, data = _input()
    model = _StructuredModel("older research summarized")
    harness.config = Configuration(
        research_model="openai:fixture",
        context_capacity_tokens=4000,
        recent_raw_steps=1,
    )
    harness.model_factory = lambda *_: model

    async def summarize(*_args, **_kwargs):
        return "older research summarized", {}

    monkeypatch.setattr(sdk_runtime, "summarize_history", summarize)
    agent = SimpleNamespace(tools=[])
    result = asyncio.run(harness._compact_history(data, agent))

    texts = [json.dumps(item, ensure_ascii=False) for item in result.input]
    assert any("older research summarized" in text for text in texts)
    # Newest complete unit stays raw; older calls are compacted away.
    assert any("call-2" in text for text in texts)
    assert not any("call-0" in text or "call-1" in text for text in texts)
    assert harness._compacted_turns == {0, 1}
    assert harness.rolling_summary == "older research summarized"


def test_compaction_preserves_call_and_output_pairing(monkeypatch):
    harness, data = _input()
    harness.config = Configuration(
        research_model="openai:fixture",
        context_capacity_tokens=4000,
        recent_raw_steps=1,
    )
    harness.model_factory = lambda *_: _StructuredModel()

    async def summarize(*_args, **_kwargs):
        return "summary", {}

    monkeypatch.setattr(sdk_runtime, "summarize_history", summarize)
    agent = SimpleNamespace(tools=[])
    result = asyncio.run(harness._compact_history(data, agent))
    calls = sorted(
        item["call_id"] for item in result.input if item.get("type") == "function_call"
    )
    outputs = sorted(
        item["call_id"]
        for item in result.input
        if item.get("type") == "function_call_output"
    )
    assert calls == outputs == ["call-2"]


def test_compaction_failure_keeps_original_history_and_raises(monkeypatch):
    harness, data = _input()
    harness.config = Configuration(
        research_model="openai:fixture",
        context_capacity_tokens=4000,
        recent_raw_steps=1,
    )
    harness.model_factory = lambda *_: _StructuredModel()

    async def summarize(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(sdk_runtime, "summarize_history", summarize)
    agent = SimpleNamespace(tools=[])
    with pytest.raises(SDKContextLimitExceeded, match="original history retained"):
        asyncio.run(harness._compact_history(data, agent))
    assert harness.rolling_summary == ""
    assert harness._compacted_turns == set()
    assert any(event[0] == "rolling_compact_failed" for event in harness.events)


def test_compaction_rejects_summary_that_is_not_smaller(monkeypatch):
    harness, data = _input()
    harness.config = Configuration(
        research_model="openai:fixture",
        context_capacity_tokens=4000,
        recent_raw_steps=1,
    )
    huge = "x" * 4000
    harness.model_factory = lambda *_: _StructuredModel(huge)

    async def summarize(*_args, **_kwargs):
        return huge, {}

    monkeypatch.setattr(sdk_runtime, "summarize_history", summarize)
    agent = SimpleNamespace(tools=[])
    with pytest.raises(SDKContextLimitExceeded, match="cannot fit"):
        asyncio.run(harness._compact_history(data, agent))


def test_history_without_complete_units_raises_instead_of_dropping():
    harness = _harness()
    harness.config = Configuration(
        research_model="openai:fixture",
        context_capacity_tokens=200,
        recent_raw_steps=1,
    )
    data = ModelInputData(
        input=[
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "large " * 1000},
        ],
        instructions="",
    )
    agent = SimpleNamespace(tools=[])
    with pytest.raises(SDKContextLimitExceeded, match="cannot be safely discarded"):
        asyncio.run(harness._compact_history(data, agent))


def test_rolling_output_model_is_the_only_compaction_schema():
    assert RollingCompactOutput.model_fields.keys() == {"rolling_summary"}


def test_deepseek_flash_uses_the_verified_provider_context_window():
    # DeepSeek-V4.1-Flash (served as deepseek-flash) documents a 1M window.
    assert get_model_token_limit("deepseek:deepseek-flash") == 1_048_576
    assert resolve_context_capacity("deepseek:deepseek-flash") == 1_048_576


def test_explicit_application_window_overrides_the_provider_table():
    assert (
        resolve_context_capacity("deepseek:deepseek-flash", 131_072)
        == 131_072
    )


def test_unknown_model_keeps_the_conservative_application_window():
    assert UNKNOWN_MODEL_CONTEXT_CAPACITY == 32_768
    assert resolve_context_capacity("unknown:future-model") == 32_768


def test_context_limit_diagnostics_contain_only_bounded_metrics():
    harness = _harness()
    harness.config = Configuration(
        research_model="openai:fixture",
        context_capacity_tokens=200,
        recent_raw_steps=1,
    )
    data = ModelInputData(
        input=[
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "private analysis " * 1000},
        ],
        instructions="",
    )
    agent = SimpleNamespace(tools=[])

    with pytest.raises(SDKContextLimitExceeded) as caught:
        asyncio.run(harness._compact_history(data, agent))

    diagnostics = harness.context_diagnostics
    assert set(diagnostics) == {
        "estimated_input_tokens",
        "context_capacity_tokens",
        "compaction_threshold_ratio",
        "recent_raw_steps",
        "eligible_compaction_units",
    }
    assert diagnostics["estimated_input_tokens"] > 0
    assert diagnostics["context_capacity_tokens"] == 200
    assert diagnostics["compaction_threshold_ratio"] == 0.75
    assert diagnostics["recent_raw_steps"] == 1
    assert diagnostics["eligible_compaction_units"] == 0

    message = str(caught.value)
    for key in diagnostics:
        assert key in message
    assert "private analysis" not in message
