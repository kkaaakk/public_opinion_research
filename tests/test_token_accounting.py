"""SDK token accounting, bounded retries, context pressure and delta isolation."""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from sdk_model_support import Reply, install_inference, install_sequence

from open_deep_research.budget import (
    budget_capture,
    empty_budget_usage,
    start_budget_capture,
    stop_budget_capture,
)
from open_deep_research.configuration import Configuration
from open_deep_research.llm.errors import ModelInvocationError
from open_deep_research.llm.generation import (
    ModelOutputError,
    generate_structured,
    generate_text,
)
from open_deep_research.search.tavily import summarize_webpage
from open_deep_research.state import ClarifyWithUser

MODEL = "openai:fixture"
VALID = {"need_clarification": False, "question": "", "verification": "Confirmed"}


@pytest.mark.parametrize("bad_replies", [0, 1, 2, 3])
def test_structured_validation_retains_reported_usage_from_every_attempt(
    monkeypatch, bad_replies
):
    bad = [Reply({"missing_fields": True}, 10, 5)] * bad_replies
    replies = [*bad, Reply(VALID, 4, 2)]
    requests = install_sequence(monkeypatch, *replies)
    with budget_capture() as captured:
        decision, usage = asyncio.run(
            generate_structured(
                MODEL, "Clarify", ClarifyWithUser, max_attempts=len(replies)
            )
        )
    assert decision == ClarifyWithUser.model_validate(VALID)
    assert usage == captured
    assert captured["model_calls"] == len(requests) == 1 + bad_replies
    assert captured["input_tokens"] == 4 + 10 * bad_replies
    assert captured["output_tokens"] == 2 + 5 * bad_replies
    assert captured["total_tokens"] == 6 + 15 * bad_replies


@pytest.mark.parametrize(
    "reply,match,usage",
    [
        (Reply("invalid JSON", 4, 2), "Invalid structured output", 6),
        (Reply("", 4, 2), "empty output", 6),
        (Reply("partial", 4, 2, status="length"), "truncated", 6),
        (Reply("partial", 4, 2, status="content_filter"), "refused", 6),
    ],
)
def test_invalid_completed_responses_record_usage_before_returning_error(
    monkeypatch, reply, match, usage
):
    requests = install_sequence(monkeypatch, reply, reply)
    with (
        budget_capture() as captured,
        pytest.raises(ModelOutputError, match=match) as failure,
    ):
        asyncio.run(
            generate_structured(MODEL, "Clarify", ClarifyWithUser, max_attempts=2)
        )
    expected = 1 if reply.status == "content_filter" else 2
    assert (
        len(requests)
        == failure.value.budget_usage["model_calls"]
        == captured["model_calls"]
        == expected
    )
    assert captured["total_tokens"] == usage * expected


@pytest.mark.parametrize("prior_invalid", [False, True])
def test_provider_failure_counts_its_attempt_without_fabricating_tokens(
    monkeypatch, prior_invalid
):
    first = [Reply("bad JSON", 4, 2)] if prior_invalid else []
    requests = install_sequence(monkeypatch, *first, RuntimeError("unavailable"))
    with (
        budget_capture() as usage,
        pytest.raises(ModelInvocationError, match="RuntimeError") as failure,
    ):
        asyncio.run(
            generate_structured(MODEL, "Clarify", ClarifyWithUser, max_attempts=4)
        )
    assert len(requests) == usage["model_calls"] == 1 + prior_invalid
    assert usage["total_tokens"] == 6 * prior_invalid
    assert failure.value.budget_usage == usage


def test_boundary_preserves_base_exception_and_leaves_capture_balanced(monkeypatch):
    class Interrupted(BaseException):
        pass

    failure = Interrupted()
    install_sequence(monkeypatch, failure, Reply("done", 4, 2))
    with budget_capture() as first, pytest.raises(Interrupted) as raised:
        asyncio.run(generate_text(MODEL, "Interrupt"))
    with budget_capture() as second:
        asyncio.run(generate_text(MODEL, "Complete"))
    assert raised.value is failure
    assert first["model_calls"] == second["model_calls"] == 1
    assert first["total_tokens"] == 0 and second["total_tokens"] == 6


@pytest.mark.parametrize("valid", [False, True])
def test_nested_summary_retry_keeps_all_attempts_in_tool_capture(monkeypatch, valid):
    final = {"summary": "Verified", "key_excerpts": "Source"} if valid else {}
    requests = install_sequence(monkeypatch, Reply({}, 4, 2), Reply(final, 10, 5))
    with budget_capture() as captured:
        result = asyncio.run(
            summarize_webpage(
                MODEL, "raw evidence", max_attempts=2, fallback_content="Short excerpt"
            )
        )
    assert (
        len(requests) == captured["model_calls"] == 2 and captured["total_tokens"] == 21
    )
    assert ("<summary>" in result) == valid
    assert ("Short excerpt" in result) != valid


# 验证研究复核节点保留结构化输出的原始用量。
def test_research_review_node_reports_structured_usage(monkeypatch):
    from open_deep_research.workflow import research as module

    install_sequence(monkeypatch, Reply({"research_complete": True}, 55, 15))
    state = {
        "messages": [HumanMessage(content="brand risk")],
        "workflow": {"brief": "brand risk", "round": 1},
        "agents": {
            "public_signal": {"report": "signal"},
            "internal_knowledge": {"report": "internal"},
        },
        "report": {},
        "runtime": {"budget": {}},
    }
    config = {
        "configurable": {
            "research_model": "openai:fixture-review",
            "max_structured_output_retries": 2,
        }
    }

    result = asyncio.run(module.research_review(state, config))

    assert result["workflow"]["review"].research_complete is True
    budget = result["runtime"]["budget"]
    assert budget["model_calls"] == 1
    assert budget["input_tokens"] == 55
    assert budget["output_tokens"] == 15


# ---------------------------------------------------------------------------
# Context pressure uses the actual request structure with approximate counting
# ---------------------------------------------------------------------------


# 提供可控制上下文与压缩结果的工作区替身。
def _sdk_compaction(config, *, instructions="system", tools=None):
    from agents.run import ModelInputData

    from open_deep_research.runtime.sdk_context import SDKContextHarness
    from open_deep_research.web.run_data import digest

    harness = SDKContextHarness(
        role="public_signal",
        task_id="t1",
        research_round=1,
        config=config,
        runtime_config={},
    )
    items = [{"role": "user", "content": "assignment"}]
    for index in range(4):
        call = {
            "type": "function_call",
            "call_id": f"c{index}",
            "name": "lookup",
            "arguments": "{}",
        }
        harness._response_starts.append(digest(call))
        items.extend(
            [
                call,
                {
                    "type": "function_call_output",
                    "call_id": f"c{index}",
                    "output": "raw result " * 300,
                },
            ]
        )
    data = ModelInputData(input=items, instructions=instructions)
    agent = SimpleNamespace(tools=tools or [])
    return harness, data, agent


def _compact_sdk(harness, data, agent):
    token = start_budget_capture()
    try:
        result = asyncio.run(harness._compact_history(data, agent))
    finally:
        usage = stop_budget_capture(token)
    return result, usage


def test_high_context_pressure_triggers_compaction(monkeypatch):
    install_sequence(monkeypatch, {"rolling_summary": "compacted history"})
    harness, data, agent = _sdk_compaction(
        Configuration(context_capacity_tokens=1800, recent_raw_steps=1)
    )
    compacted, budget = _compact_sdk(harness, data, agent)
    assert budget["model_calls"] == 1
    assert len(compacted.input) == 4
    assert compacted.input[0] == data.input[0]
    assert harness._compacted_turns == {0, 1, 2}


def test_low_context_pressure_skips_compaction(monkeypatch):
    install_sequence(monkeypatch)
    harness, data, agent = _sdk_compaction(
        Configuration(context_capacity_tokens=1_000_000)
    )
    compacted, budget = _compact_sdk(harness, data, agent)
    assert compacted.input == data.input
    assert budget == empty_budget_usage()
    assert harness._compacted_turns == set()


def test_system_prompt_is_part_of_context_pressure(monkeypatch):
    from open_deep_research.runtime.sdk_runtime import SDKContextLimitExceeded

    install_sequence(monkeypatch, {"rolling_summary": "short"})
    harness, data, agent = _sdk_compaction(
        Configuration(context_capacity_tokens=1800),
        instructions="system policy " * 3000,
    )
    # Protected instructions cannot be discarded just to send an oversized call.
    with pytest.raises(SDKContextLimitExceeded, match="cannot fit"):
        _compact_sdk(harness, data, agent)
    assert harness._compacted_turns == set()


def test_tools_are_part_of_context_pressure(monkeypatch):
    from agents import FunctionTool

    from open_deep_research.runtime.sdk_runtime import SDKContextLimitExceeded

    async def invoke(*args):
        raise AssertionError("capacity rejection must precede tool execution")

    huge = FunctionTool(
        name="huge",
        description="large schema",
        params_json_schema={"type": "object", "description": "schema " * 8000},
        on_invoke_tool=invoke,
    )
    install_sequence(monkeypatch, {"rolling_summary": "short"})
    harness, data, agent = _sdk_compaction(
        Configuration(context_capacity_tokens=1800), tools=[huge]
    )
    assert harness._estimate(data, agent) > harness._estimate(
        data, SimpleNamespace(tools=[])
    )
    with pytest.raises(SDKContextLimitExceeded, match="cannot fit"):
        _compact_sdk(harness, data, agent)
    assert harness._compacted_turns == set()


def test_pressure_estimation_uses_the_same_message_structure_as_the_call():
    import json

    from open_deep_research.budget import estimate_text_tokens

    harness, data, agent = _sdk_compaction(
        Configuration(context_capacity_tokens=1_000_000)
    )
    actual = json.dumps(
        {"instructions": data.instructions, "input": data.input, "tools": []},
        ensure_ascii=False,
        default=str,
    )
    assert harness._estimate(data, agent) == estimate_text_tokens(actual)
    compacted, budget = _compact_sdk(harness, data, agent)
    assert compacted.input == data.input
    assert budget == empty_budget_usage()


def test_agent_runtime_checks_context_before_invoking_the_model(monkeypatch):
    from agents.testing import ModelStep, ScriptedModel, assistant_message
    from test_sdk_runtime import _runtime as sdk_runtime

    from open_deep_research.runtime.sdk_context import SDKContextHarness

    events = []
    original = SDKContextHarness._compact_history

    async def compact(self, data, agent):
        events.append("pressure_check")
        return await original(self, data, agent)

    def reply(call):
        events.append("model")
        assert "collect evidence" in str(call.input)
        return [assistant_message("done")]

    monkeypatch.setattr(SDKContextHarness, "_compact_history", compact)
    runtime = sdk_runtime(model=ScriptedModel([ModelStep.respond(reply)]))
    asyncio.run(runtime.run("collect evidence"))
    assert events == ["pressure_check", "model"]


# ---------------------------------------------------------------------------
# Error routing: token-limit recovery vs ordinary exception propagation
# ---------------------------------------------------------------------------


# 创建供应商特定上下文超限异常。
def _token_limit_error() -> Exception:
    error_type = type("BadRequestError", (Exception,), {})
    error_type.__module__ = "openai.error"
    return error_type("This model's maximum context length is 8192 tokens.")


def test_report_provider_error_propagates_without_retry(monkeypatch):
    from open_deep_research.workflow import reporting

    requests = install_sequence(monkeypatch, RuntimeError("provider unavailable"))
    with pytest.raises(ModelInvocationError, match="RuntimeError") as failure:
        asyncio.run(reporting.generate_final_report({"workflow": {"brief": "brand risk"}}, {}))
    assert str(failure.value.__cause__) == "provider unavailable"
    assert failure.value.budget_usage["model_calls"] == len(requests) == 1


# 验证超限识别依赖供应商错误特征，避免误判。
def test_token_limit_detection_is_specific_to_provider_errors():
    from open_deep_research.llm.errors import is_token_limit_exceeded

    assert (
        is_token_limit_exceeded(
            RuntimeError("provider unavailable"), "deepseek:deepseek-chat"
        )
        is False
    )
    assert is_token_limit_exceeded(_token_limit_error(), "openai:gpt-4.1") is True


# ---------------------------------------------------------------------------
# Final report input shaping
# ---------------------------------------------------------------------------


def test_final_report_bounds_evidence_but_preserves_the_brief(monkeypatch):
    from open_deep_research.budget import estimate_text_tokens
    from open_deep_research.workflow import reporting

    requests = install_sequence(monkeypatch, "final report body")
    huge_findings = "evidence line with facts, sources and citations. " * 400
    brief = "brand risk scope and every research constraint"
    state = {
        "agents": {"public_signal": {"report": huge_findings}},
        "workflow": {"brief": brief},
    }
    config = {
        "configurable": {
            "budget_enabled": True,
            "max_input_tokens": 1600,
            "final_report_model_max_tokens": 100,
        }
    }

    result = asyncio.run(reporting.generate_final_report(state, config))

    assert "final report body" in result["report"]["final"]
    [request] = requests
    assert brief in request.prompt
    assert "[Evidence context bounded;" in request.prompt
    assert estimate_text_tokens(request.prompt) <= 1600
    assert state["agents"]["public_signal"]["report"] == huge_findings


def test_final_report_surfaces_context_limit_with_billed_usage(monkeypatch):
    from open_deep_research.workflow import reporting

    requests = install_sequence(monkeypatch, _token_limit_error())
    with pytest.raises(ModelInvocationError) as failure:
        asyncio.run(reporting.generate_final_report({"workflow": {"brief": "brand risk"}}, {}))
    assert failure.value.context_limit
    assert failure.value.budget_usage["model_calls"] == len(requests) == 1
    assert failure.value.budget_usage["total_tokens"] == 0


# ---------------------------------------------------------------------------
# Reducer stays delta-only
# ---------------------------------------------------------------------------


# 验证并行 reducer 只累加新消耗增量。
def test_parallel_reducer_remains_delta_only():
    from open_deep_research.state import runtime_reducer

    # 按给定次数和令牌构造单份预算增量。
    def delta(calls: int, tokens: int) -> dict:
        return {"model_calls": calls, "input_tokens": tokens, "total_tokens": tokens}

    state: dict = {}
    for value in (delta(2, 10), delta(1, 5), delta(1, 7)):
        state = runtime_reducer(state, {"budget": value})

    assert state["budget"]["model_calls"] == 4
    assert state["budget"]["input_tokens"] == 22


# 验证并发调用的捕获独立，汇总时每份增量只处理一次。
def test_parallel_invocations_keep_usage_isolated_and_reduce_once(monkeypatch):
    from open_deep_research.state import runtime_reducer

    async def respond(request):
        await asyncio.sleep(0)
        tokens = int(request.prompt)
        return Reply("done", tokens, tokens // 2)

    install_inference(monkeypatch, respond)

    async def invoke(tokens):
        with budget_capture() as delta:
            await generate_text(MODEL, str(tokens))
        return delta

    async def exercise():
        return await asyncio.gather(invoke(10), invoke(20))

    first, second = asyncio.run(exercise())
    assert first["input_tokens"] == 10 and first["total_tokens"] == 15
    assert second["input_tokens"] == 20 and second["total_tokens"] == 30

    state: dict = {}
    for delta in (first, second):
        state = runtime_reducer(state, {"budget": delta})

    assert state["budget"]["model_calls"] == 2
    assert state["budget"]["input_tokens"] == 30
    assert state["budget"]["total_tokens"] == 45
