"""Verify the single rolling history compaction used by SDK Agent invocations."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from agents.run import CallModelData, ModelInputData
from sdk_model_support import Reply, install_sequence

from open_deep_research.budget import budget_capture
from open_deep_research.configuration import Configuration
from open_deep_research.llm.context import (
    UNKNOWN_MODEL_CONTEXT_CAPACITY,
    get_model_token_limit,
    resolve_context_capacity,
)
from open_deep_research.runtime import sdk_context
from open_deep_research.runtime.compaction import (
    RollingCompactOutput,
    summarize_history,
)
from open_deep_research.runtime.sdk_context import (
    SDKContextHarness,
    SDKContextLimitExceeded,
)
from open_deep_research.web.run_data import RunJSON, digest


def _harness(*, capacity=None, recent=1):
    config = Configuration(
        research_model="openai:fixture",
        context_capacity_tokens=capacity,
        recent_raw_steps=recent,
    )
    return SDKContextHarness(
        role="public_signal",
        task_id="task-1",
        research_round=1,
        config=config,
        runtime_config={},
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


def _input(unit_count=3, payload_size=400, *, capacity=None):
    payload = "evidence " * payload_size
    items = [{"role": "user", "content": "initial task"}]
    harness = _harness(capacity=capacity)
    starts = []
    for turn in range(unit_count):
        unit = _unit(turn, payload)
        items.extend(unit)
        starts.append(digest(unit[0]))
    harness._response_starts = starts
    return harness, ModelInputData(input=items, instructions="system prompt")


def test_summarize_history_merges_previous_summary_and_keeps_task(monkeypatch):
    requests = install_sequence(monkeypatch, Reply({"rolling_summary": "MERGED SUMMARY"}, 5, 3))
    summary, _budget = asyncio.run(
        summarize_history(
            "older tool steps",
            previous_summary="PREVIOUS",
            task_context="CURRENT TASK",
            model_name="openai:fixture-summary",
        )
    )
    assert summary == "MERGED SUMMARY"
    assert _budget["total_tokens"] == 8
    prompt = requests[0].prompt
    assert "PREVIOUS" in prompt
    assert "CURRENT TASK" in prompt
    assert "older tool steps" in prompt
    assert "Research Graph" not in prompt


def test_summarize_history_rejects_empty_summary(monkeypatch):
    install_sequence(monkeypatch, {"rolling_summary": "  "})
    with pytest.raises(ValueError, match="empty summary"):
        asyncio.run(
            summarize_history(
                "steps",
                previous_summary="",
                task_context="task",
                model_name="openai:fixture-summary",
            )
        )


def test_compaction_is_skipped_below_the_threshold():
    harness, data = _input(unit_count=1, payload_size=1)
    agent = SimpleNamespace(tools=[])
    result = asyncio.run(harness.filter_model_input(
        CallModelData(model_data=data, agent=agent, context=harness)
    ))
    assert result is data
    assert harness.rolling_summary == ""
    assert harness._compacted_turns == set()


def test_compaction_summarizes_older_turns_and_keeps_recent_raw(monkeypatch):
    harness, data = _input()
    harness.config = Configuration(
        research_model="openai:fixture",
        context_capacity_tokens=4000,
        recent_raw_steps=1,
    )

    async def summarize(*_args, **_kwargs):
        return "older research summarized", {}

    monkeypatch.setattr(sdk_context, "summarize_history", summarize)
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

    async def summarize(*_args, **_kwargs):
        return "summary", {}

    monkeypatch.setattr(sdk_context, "summarize_history", summarize)
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


def test_compaction_failure_keeps_original_history_and_raises(monkeypatch, caplog):
    harness, data = _input()
    harness.config = Configuration(
        research_model="openai:fixture",
        context_capacity_tokens=4000,
        recent_raw_steps=1,
    )

    async def summarize(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(sdk_context, "summarize_history", summarize)
    agent = SimpleNamespace(tools=[])
    original = json.dumps(data.input)
    with pytest.raises(SDKContextLimitExceeded, match="original history retained"):
        asyncio.run(harness._compact_history(data, agent))
    assert harness.rolling_summary == ""
    assert harness._compacted_turns == set()
    assert json.dumps(data.input) == original
    assert "SDK rolling compaction failed safely" in caplog.text


def test_compaction_rejects_summary_that_is_not_smaller(monkeypatch):
    harness, data = _input()
    harness.config = Configuration(
        research_model="openai:fixture",
        context_capacity_tokens=4000,
        recent_raw_steps=1,
    )
    huge = "x" * 4000

    async def summarize(*_args, **_kwargs):
        return huge, {}

    monkeypatch.setattr(sdk_context, "summarize_history", summarize)
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

    message = str(caught.value)
    diagnostics = json.loads(message.split("diagnostics=", 1)[1])
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

    assert "private analysis" not in message


@pytest.mark.parametrize("summary", ["saved summary", ""], ids=["success", "failure"])
def test_context_step_replays_the_window_and_summary_usage(monkeypatch, summary):
    requests = install_sequence(
        monkeypatch, Reply({"rolling_summary": summary}, 5, 3)
    )
    serializer = RunJSON()
    saved = None

    async def step(options, prepare):
        nonlocal saved
        assert options == {"name": "por.agent_context", "preemptible": True}
        if saved is None:
            try:
                payload = await prepare()
            except SDKContextLimitExceeded as exc:
                payload = exc
            saved = serializer.serialize(payload)
        payload = serializer.deserialize(saved)
        if isinstance(payload, SDKContextLimitExceeded):
            raise payload
        assert set(payload) == {
            "model_input", "compacted_turns", "rolling_summary", "usage"
        }
        return payload

    monkeypatch.setattr(sdk_context.DBOS, "run_step_async", step)
    agent = SimpleNamespace(tools=[])
    for _ in range(2):
        harness, original = _input(capacity=4000)
        data = CallModelData(model_data=original, agent=agent, context=harness)
        with budget_capture() as usage:
            if summary:
                result = asyncio.run(harness.durable_filter_model_input(data))
                assert result.instructions == original.instructions
                assert result.input[0] == original.input[0]
                assert result.input[-3:] == original.input[-3:]
                assert len(result.input) == 5
                assert summary in result.input[1]["content"]
                assert harness._compacted_turns == {0, 1}
            else:
                with pytest.raises(SDKContextLimitExceeded, match="original history retained"):
                    asyncio.run(harness.durable_filter_model_input(data))
                assert not harness._compacted_turns
        assert harness.rolling_summary == summary
        assert usage["model_calls"] == 1
        assert usage["total_tokens"] == 8
        assert len(requests) == 1
