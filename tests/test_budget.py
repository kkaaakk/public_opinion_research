"""Budget Guard and token accounting tests.

Token usage authority is LangChain ``UsageMetadata`` reported by the official
``UsageMetadataCallbackHandler``; model-call attempt authority is the official
``on_chat_model_start`` lifecycle.  These tests use real LangChain message types.
"""

from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.ai import UsageMetadata, add_usage
from langchain_core.tools import tool

from open_deep_research.budget import (
    ModelAttemptCounter,
    append_budget_summary,
    available_research_unit_slots,
    budget_from_model_accounting,
    budget_usage_with_reason,
    capture_budget_usage,
    context_pressure_ratio,
    diff_budget_usage,
    empty_budget_usage,
    estimate_context_tokens,
    estimate_text_tokens,
    filter_tool_calls_for_budget,
    format_budget_summary,
    merge_budget_usage,
    model_accounting_config,
    require_valid_structured_output,
    start_budget_capture,
    stop_budget_capture,
    truncate_text_to_token_budget,
)
from open_deep_research.configuration import Configuration
from open_deep_research.state import runtime_reducer


def _message(
    input_tokens: int = 13,
    output_tokens: int = 7,
    *,
    input_details: dict | None = None,
    output_details: dict | None = None,
) -> AIMessage:
    metadata: UsageMetadata = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if input_details:
        metadata["input_token_details"] = input_details
    if output_details:
        metadata["output_token_details"] = output_details
    return AIMessage(content="done", usage_metadata=metadata)


def _usage_handler(usage: dict | None = None) -> SimpleNamespace:
    """Stand-in for the public ``.usage_metadata`` of the official usage handler."""
    return SimpleNamespace(usage_metadata=({"fixture": usage} if usage else {}))


@tool
def sample_search(query: str) -> str:
    """Search the web for one query."""
    return ""


# ---------------------------------------------------------------------------
# 1. UsageMetadata enters BudgetUsage
# ---------------------------------------------------------------------------


def test_budget_from_model_accounting_usage_metadata():
    usage = budget_from_model_accounting(1, _usage_handler(_message(13, 7).usage_metadata))

    assert usage["model_calls"] == 1
    assert usage["input_tokens"] == 13
    assert usage["output_tokens"] == 7
    assert usage["total_tokens"] == 20


def test_provider_response_metadata_is_not_a_token_source():
    """Provider-specific token fields never reach the official usage handler."""
    message = AIMessage(
        content="done",
        response_metadata={
            "token_usage": {
                "prompt_tokens": 11,
                "completion_tokens": 5,
                "total_tokens": 16,
            }
        },
    )

    usage = budget_from_model_accounting(1, _usage_handler(message.usage_metadata))

    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["total_tokens"] == 0


def test_budget_from_model_accounting_aggregates_all_handler_entries():
    """Multiple model keys are aggregated with the official ``add_usage``."""
    handler = SimpleNamespace(
        usage_metadata={
            "model-a": _message(10, 4).usage_metadata,
            "model-b": _message(3, 6).usage_metadata,
        }
    )

    usage = budget_from_model_accounting(2, handler)

    assert usage["model_calls"] == 2
    assert usage["input_tokens"] == 13
    assert usage["output_tokens"] == 10
    assert usage["total_tokens"] == 23


def test_missing_usage_never_fabricates_tokens():
    usage = budget_from_model_accounting(1, _usage_handler())

    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["total_tokens"] == 0


def test_model_attempt_counter_counts_real_chat_model_starts():
    """One provider attempt equals one ``on_chat_model_start`` event."""
    model = GenericFakeChatModel(
        messages=iter([AIMessage(content="ok", response_metadata={"model_name": "fixture"})])
    )
    config, counter, _usage = model_accounting_config()

    model.invoke([HumanMessage(content="hi")], config=config)

    assert isinstance(counter, ModelAttemptCounter)
    assert counter.starts == 1


def test_model_attempt_counter_does_not_double_count_a_chat_model():
    """A ChatModel emits ``on_chat_model_start`` only; ``on_llm_start`` must not add."""
    model = GenericFakeChatModel(
        messages=iter([AIMessage(content="ok", response_metadata={"model_name": "fixture"})])
    )
    handler = ModelAttemptCounter()
    model.invoke([HumanMessage(content="hi")], config={"callbacks": [handler]})

    assert handler.starts == 1


def test_model_attempt_counter_counts_retry_attempts():
    """``with_retry`` re-invokes the provider, so each attempt is counted."""
    state = {"n": 0}

    class Flaky(GenericFakeChatModel):
        def _generate(self, *args, **kwargs):
            state["n"] += 1
            if state["n"] < 3:
                raise ValueError("transient")
            return super()._generate(*args, **kwargs)

    model = Flaky(
        messages=iter([AIMessage(content="ok", response_metadata={"model_name": "fixture"})])
    )
    config, counter, _usage = model_accounting_config()

    model.with_retry(stop_after_attempt=5).invoke([HumanMessage(content="hi")], config=config)

    assert state["n"] == 3
    assert counter.starts == 3


def test_require_valid_structured_output_raises_parsing_error():
    error = ValueError("bad parse")
    with pytest.raises(ValueError, match="bad parse"):
        require_valid_structured_output(
            {"raw": _message(), "parsed": None, "parsing_error": error}
        )
    with pytest.raises(ValueError, match="did not contain a parsed result"):
        require_valid_structured_output({"raw": _message(), "parsed": None, "parsing_error": None})
    envelope = {"raw": _message(), "parsed": object(), "parsing_error": None}
    assert require_valid_structured_output(envelope) is envelope


# ---------------------------------------------------------------------------
# 2. Cache / reasoning token details
# ---------------------------------------------------------------------------


def test_token_details_are_preserved_and_merged_without_guessing():
    left = budget_from_model_accounting(
        1,
        _usage_handler(
            _message(
                10,
                4,
                input_details={"cache_read": 3, "cache_creation": 2},
                output_details={"reasoning": 1},
            ).usage_metadata
        ),
    )
    right = budget_from_model_accounting(
        1,
        _usage_handler(_message(6, 2, input_details={"cache_read": 1}).usage_metadata),
    )

    merged = merge_budget_usage(left, right)

    assert merged["input_token_details"] == {"cache_read": 4, "cache_creation": 2}
    assert merged["output_token_details"] == {"reasoning": 1}


def test_missing_token_details_do_not_error_or_appear():
    usage = merge_budget_usage(
        budget_from_model_accounting(1, _usage_handler(_message().usage_metadata)),
        empty_budget_usage(),
    )

    assert "input_token_details" not in usage
    assert "output_token_details" not in usage


def test_token_details_formatting_only_shows_provider_reported_values():
    usage = budget_from_model_accounting(
        1,
        _usage_handler(
            _message(10, 4, input_details={"cache_read": 3}, output_details={"reasoning": 2}).usage_metadata
        ),
    )
    configurable = Configuration(budget_enabled=True)

    summary = format_budget_summary(configurable, usage)

    assert "Cache tokens (read/creation): 3 / 0" in summary
    assert "Reasoning tokens: 2" in summary


# ---------------------------------------------------------------------------
# 3. add_usage aggregation semantics
# ---------------------------------------------------------------------------


def test_merge_matches_langchain_add_usage_semantics():
    left = budget_from_model_accounting(
        1, _usage_handler(_message(10, 4, input_details={"cache_read": 2}).usage_metadata)
    )
    right = budget_from_model_accounting(
        1, _usage_handler(_message(3, 6, output_details={"reasoning": 5}).usage_metadata)
    )

    merged = merge_budget_usage(left, right)
    expected = add_usage(
        {
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
            "input_token_details": {"cache_read": 2},
        },
        {
            "input_tokens": 3,
            "output_tokens": 6,
            "total_tokens": 9,
            "output_token_details": {"reasoning": 5},
        },
    )

    assert merged["input_tokens"] == expected["input_tokens"]
    assert merged["output_tokens"] == expected["output_tokens"]
    assert merged["total_tokens"] == expected["total_tokens"]
    assert merged["input_token_details"] == expected["input_token_details"]
    assert merged["output_token_details"] == expected["output_token_details"]


# ---------------------------------------------------------------------------
# 4./5. Multi-agent reducer: deltas only, no double count
# ---------------------------------------------------------------------------


def _node_delta(message: AIMessage, *, attempts: int = 1) -> dict:
    """Build the delta a node returns from the official usage handler shape."""
    return budget_from_model_accounting(attempts, _usage_handler(message.usage_metadata))


def test_parallel_agent_deltas_reduce_once():
    researcher_a = _node_delta(_message(10, 2))
    researcher_b = _node_delta(_message(20, 4))
    supervisor = _node_delta(_message(5, 1))

    state: dict = {}
    for delta in (researcher_a, researcher_b, supervisor):
        state = runtime_reducer(state, {"budget": delta})

    budget = state["budget"]
    assert budget["model_calls"] == 3
    assert budget["input_tokens"] == 35
    assert budget["output_tokens"] == 7
    assert budget["total_tokens"] == 42


def test_subgraph_delta_returns_only_the_increment():
    baseline = _node_delta(_message(10, 2))
    subgraph_total = merge_budget_usage(baseline, _node_delta(_message(4, 1)))

    delta = diff_budget_usage(subgraph_total, baseline)

    assert delta["model_calls"] == 1
    assert delta["input_tokens"] == 4
    assert delta["output_tokens"] == 1
    # Re-applying the delta to the parent reproduces the subgraph total exactly once.
    assert merge_budget_usage(baseline, delta) == subgraph_total


def test_nested_model_call_records_once_without_double_count():
    model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="nested",
                    response_metadata={"model_name": "fixture"},
                    usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                )
            ]
        )
    )
    token = start_budget_capture()
    config, counter, usage_handler = model_accounting_config()
    model.invoke([HumanMessage(content="hi")], config=config)
    capture_budget_usage(
        budget_from_model_accounting(counter.starts, usage_handler)
    )

    captured_usage = stop_budget_capture(token)

    assert captured_usage["model_calls"] == 1
    assert captured_usage["input_tokens"] == 3
    assert captured_usage["output_tokens"] == 2


# ---------------------------------------------------------------------------
# Policy: tool/search/model budgets (unchanged behavior)
# ---------------------------------------------------------------------------


def test_filter_tool_calls_skips_after_tool_budget():
    configurable = Configuration(budget_enabled=True, max_tool_calls=1)
    tool_calls = [
        {"name": "web_search", "args": {"query": "alpha"}, "id": "call_1"},
        {"name": "rag_search", "args": {"query": "beta"}, "id": "call_2"},
    ]
    tools_by_name = {
        "web_search": SimpleNamespace(name="web_search", metadata={"type": "search"}),
        "rag_search": SimpleNamespace(name="rag_search", metadata={"type": "search"}),
    }

    allowed, skipped = filter_tool_calls_for_budget(
        configurable,
        {},
        tool_calls,
        tools_by_name,
    )

    assert [tool_call["id"] for tool_call in allowed] == ["call_1"]
    assert [tool_call["id"] for tool_call in skipped] == ["call_2"]


def test_filter_tool_calls_skips_after_search_budget():
    configurable = Configuration(budget_enabled=True, max_search_calls=1)
    usage = {"search_calls": 1}
    tool_calls = [
        {"name": "rag_search", "args": {"query": "alpha"}, "id": "call_1"},
    ]

    allowed, skipped = filter_tool_calls_for_budget(
        configurable,
        usage,
        tool_calls,
        {"rag_search": SimpleNamespace(name="rag_search", metadata={"type": "search"})},
    )

    assert allowed == []
    assert skipped == tool_calls


def test_available_research_units_reserves_final_report_call():
    configurable = Configuration(
        budget_enabled=True,
        max_model_calls=5,
        reserve_final_report_call=True,
    )

    assert available_research_unit_slots(configurable, {"model_calls": 2}) == 1
    assert available_research_unit_slots(configurable, {"model_calls": 3}) == 0


def test_budget_disabled_has_unlimited_research_unit_slots():
    configurable = Configuration(budget_enabled=False, max_model_calls=1)

    assert available_research_unit_slots(configurable, {"model_calls": 100}) is None


def test_budget_summary_includes_degradation_reasons():
    configurable = Configuration(budget_enabled=True, max_model_calls=3)
    usage = merge_budget_usage(
        {},
        budget_usage_with_reason("Section planning fell back to a single section."),
    )

    summary = format_budget_summary(configurable, usage)

    assert "Section planning fell back to a single section." in summary


# ---------------------------------------------------------------------------
# 6./7. Context estimation uses LangChain utilities
# ---------------------------------------------------------------------------


def test_context_estimate_uses_langchain_message_utility():
    messages = [SystemMessage(content="sys"), HumanMessage(content="hello " * 50)]

    assert estimate_context_tokens(messages) == pytest.approx(
        estimate_text_tokens("sys") + estimate_text_tokens("hello " * 50),
        abs=8,
    )
    assert estimate_context_tokens(messages) > estimate_text_tokens("hello " * 50)


def test_tool_schemas_are_part_of_the_context_estimate():
    messages = [HumanMessage(content="find brand risk")]

    without_tools = estimate_context_tokens(messages)
    with_tools = estimate_context_tokens(messages, tools=[sample_search])

    assert with_tools > without_tools


def test_unconvertible_tool_schema_does_not_break_the_estimate():
    class NotATool:
        name = "mystery"

    messages = [HumanMessage(content="hello")]

    assert estimate_context_tokens(messages, tools=[NotATool()]) == estimate_context_tokens(
        messages
    )


def test_truncate_text_to_token_budget_uses_official_counter():
    text = "brand risk analysis " * 500
    budget = 100

    truncated, changed = truncate_text_to_token_budget(text, budget)

    assert changed is True
    assert estimate_text_tokens(truncated) <= budget
    unchanged, changed_again = truncate_text_to_token_budget("short text", 100)
    assert unchanged == "short text"
    assert changed_again is False


# ---------------------------------------------------------------------------
# 8. Context pressure
# ---------------------------------------------------------------------------


def test_context_pressure_ratio_boundaries():
    assert context_pressure_ratio(0, 1000) == 0.0
    assert context_pressure_ratio(100, None) == 0.0
    assert context_pressure_ratio(600, 1000) == pytest.approx(0.6)
    assert context_pressure_ratio(1000, 1000) == pytest.approx(1.0)


def test_configuration_validates_pressure_ordering():
    configurable = Configuration(
        context_warning_ratio=0.6,
        context_compaction_threshold_ratio=0.75,
    )
    assert configurable.context_warning_ratio == 0.6

    with pytest.raises(ValueError):
        Configuration(context_warning_ratio=0.9, context_compaction_threshold_ratio=0.75)


def test_budget_summary_appends_only_when_budget_enabled():
    report = "final report"
    assert append_budget_summary(report, Configuration(budget_enabled=False), {}) == report
    appended = append_budget_summary(
        report,
        Configuration(budget_enabled=True, max_model_calls=2),
        _node_delta(_message()),
    )
    assert appended.startswith(report)
    assert "### Budget usage" in appended


def test_estimate_text_tokens_handles_empty_text():
    assert estimate_text_tokens("") == 0
    assert estimate_context_tokens([]) == 0


def test_runtime_reducer_keeps_list_and_token_state():
    first = merge_budget_usage(
        {},
        budget_usage_with_reason("first reason"),
    )
    second = merge_budget_usage(
        {},
        budget_usage_with_reason("second reason"),
    )

    state = runtime_reducer({}, {"budget": first})
    state = runtime_reducer(state, {"budget": second})

    assert state["budget"]["degradation_reasons"] == ["first reason", "second reason"]
    assert state["budget"]["model_calls"] == 0


def test_tool_messages_do_not_contribute_token_usage():
    """Only AIMessage usage reaches the handler; tool outputs never bill tokens."""
    assert not hasattr(ToolMessage(content="x", tool_call_id="call-1", name="web_search"), "usage_metadata")

    usage = budget_from_model_accounting(1, _usage_handler(ToolMessage(
        content="raw result", tool_call_id="call-1", name="web_search"
    ).model_dump().get("usage_metadata")))

    assert usage["total_tokens"] == 0
