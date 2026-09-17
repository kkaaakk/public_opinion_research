"""Token accounting paths: retries, nested calls, proactive compaction, error routing."""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.callbacks import BaseCallbackHandler, UsageMetadataCallbackHandler
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from open_deep_research.budget import (
    budget_from_model_response,
    start_budget_capture,
    stop_budget_capture,
)
from open_deep_research.configuration import Configuration
from open_deep_research.observability import observe_model_ainvoke
from open_deep_research.research_graph.metrics import ResearchGraphMetrics
from open_deep_research.research_graph.models import RollingCompactOutput
from open_deep_research.runtime.agent_runtime import AgentRuntime
from open_deep_research.utils import summarize_webpage

_ATTEMPTS = {"count": 0}


class _AttemptCounter(BaseCallbackHandler):
    def __init__(self):
        self.starts = 0

    def on_llm_start(self, *_args, **_kwargs):
        self.starts += 1


class _FlakyModel(GenericFakeChatModel):
    """Fails once, then succeeds; used to observe retry accounting."""

    def _generate(self, *args, **kwargs):
        _ATTEMPTS["count"] += 1
        if _ATTEMPTS["count"] < 2:
            raise ValueError("transient provider error")
        return super()._generate(*args, **kwargs)


def _flaky_model() -> _FlakyModel:
    return _FlakyModel(
        messages=iter(
            [
                AIMessage(
                    content="ok",
                    response_metadata={"model_name": "flaky-model"},
                    usage_metadata={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
                )
            ]
        )
    )


def test_retry_counts_successful_usage_without_fabricating_failed_attempts():
    """Two provider attempts are visible; only the successful response has usage."""
    _ATTEMPTS["count"] = 0
    counter = _AttemptCounter()
    response = asyncio.run(
        _flaky_model()
        .with_retry(stop_after_attempt=5)
        .ainvoke([HumanMessage(content="hi")], config={"callbacks": [counter]})
    )

    usage = budget_from_model_response(response)

    assert counter.starts == 2  # one failed attempt + one successful attempt
    assert usage["model_calls"] == 1  # one successful model response is accounted
    assert usage["input_tokens"] == 4
    assert usage["output_tokens"] == 2
    assert usage["total_tokens"] == 6


def test_failed_model_call_never_fabricates_tokens():
    """All attempts failing yields no usage at all, never a guessed value."""
    _ATTEMPTS["count"] = 0

    class _AlwaysFailing(GenericFakeChatModel):
        def _generate(self, *args, **kwargs):
            raise ValueError("provider unavailable")

    model = _AlwaysFailing(messages=iter([AIMessage(content="never")]))
    with pytest.raises(ValueError):
        asyncio.run(model.with_retry(stop_after_attempt=2).ainvoke([HumanMessage(content="hi")]))


def test_nested_model_call_usage_is_captured_at_the_model_boundary():
    """summarize_webpage usage reaches the budget capture without manual recording."""

    class _FakeSummaryModel:
        async def ainvoke(self, _messages):
            return SimpleNamespace(
                summary="short summary",
                key_excerpts="evidence",
                usage_metadata={"input_tokens": 9, "output_tokens": 3, "total_tokens": 12},
            )

    async def exercise() -> dict:
        token = start_budget_capture()
        try:
            await summarize_webpage(_FakeSummaryModel(), "page text")
        finally:
            return stop_budget_capture(token)

    captured = asyncio.run(exercise())

    assert captured["model_calls"] == 1
    assert captured["input_tokens"] == 9
    assert captured["output_tokens"] == 3
    assert captured["total_tokens"] == 12


def test_official_usage_callback_and_budget_capture_each_count_once():
    """Run-level observability and state enforcement stay separate, without double counting."""
    handler = UsageMetadataCallbackHandler()
    model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="observed",
                    response_metadata={"model_name": "fixture-model"},
                    usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
                )
            ]
        )
    )

    async def exercise() -> dict:
        token = start_budget_capture()
        try:
            await observe_model_ainvoke(
                model,
                [HumanMessage(content="hi")],
                config={"callbacks": [handler]},
            )
        finally:
            return stop_budget_capture(token)

    captured = asyncio.run(exercise())

    assert captured["model_calls"] == 1
    assert captured["input_tokens"] == 7
    assert handler.usage_metadata["fixture-model"]["input_tokens"] == 7
    assert handler.usage_metadata["fixture-model"]["total_tokens"] == 10


class _CompactionModel:
    """Structured rolling-compaction model fixture."""

    def __init__(self, calls: list[str], summary: str = "compacted history") -> None:
        self.calls = calls
        self.summary = summary

    def with_structured_output(self, _schema):
        return self

    def with_retry(self, **_kwargs):
        return self

    def with_config(self, _config):
        return self

    async def ainvoke(self, _messages):
        self.calls.append("compact")
        return RollingCompactOutput(rolling_summary=self.summary)


class _MainModel:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def with_config(self, _config):
        return self

    async def ainvoke(self, _messages):
        self.calls.append("model")
        return AIMessage(content="done")


class _FakeWorkspace:
    """Minimal ResearchWorkspace surface used by AgentRuntime."""

    def __init__(self, compaction_model, protected: str = "", events: list | None = None) -> None:
        self.rolling_summary = ""
        self.metrics = ResearchGraphMetrics()
        self.transcript = None
        self.scope = SimpleNamespace(research_round=1)
        self.compaction_model = compaction_model
        self.protected = protected
        self.events = events

    def protected_context(self) -> str:
        if self.events is not None:
            self.events.append("context_check")
        return self.protected

    def model_factory(self, _model_name, _max_tokens):
        return self.compaction_model

    async def before_model(self, messages):
        return list(messages)

    async def ingest(self, messages, _batch):
        return SimpleNamespace(messages=list(messages), budget_usage={})

    async def finalize(self, _messages, _expected_output):
        return None


def _runtime(workspace, calls: list[str], configurable: Configuration) -> AgentRuntime:
    async def compress_report(_messages, _task, _contract, _budget):
        return "compressed report", [], {}

    return AgentRuntime(
        role="public_signal",
        spec=SimpleNamespace(expected_output="evidence", display_name="Public Signal"),
        agent_state={},
        workspace=workspace,
        config=configurable,
        runtime_config={},
        model=_MainModel(calls),
        system_prompt="system",
        tools=[],
        initial_budget={},
        execute_tool=None,
        compress_report=compress_report,
    )


def _history(steps: int) -> list:
    messages: list = [HumanMessage(content="assignment")]
    for index in range(steps):
        messages.append(
            AIMessage(
                content=f"step {index}",
                tool_calls=[{"name": "web_search", "args": {}, "id": f"call-{index}"}],
            )
        )
        messages.append(
            ToolMessage(
                content="raw result " * 100,
                name="web_search",
                tool_call_id=f"call-{index}",
            )
        )
    return messages


def test_high_context_pressure_triggers_compaction_before_model_call():
    calls: list[str] = []
    workspace = _FakeWorkspace(_CompactionModel(calls))
    configurable = Configuration(
        research_graph_context_capacity_tokens=300,
        context_compaction_threshold_ratio=0.75,
        context_warning_ratio=0.6,
        recent_raw_steps=1,
    )
    runtime = _runtime(workspace, calls, configurable)

    compacted, budget = asyncio.run(runtime._compact_history(_history(steps=4)))

    assert calls == ["compact"]
    assert budget["model_calls"] == 1
    # Older raw steps collapse into the rolling summary; the newest AI/tool step stays raw.
    assert len(compacted) == 4
    assert compacted[0].content == "assignment"
    assert compacted[-1].tool_call_id == "call-3"


def test_low_context_pressure_skips_compaction():
    calls: list[str] = []
    workspace = _FakeWorkspace(_CompactionModel(calls))
    configurable = Configuration(
        research_graph_context_capacity_tokens=1_000_000,
        context_compaction_threshold_ratio=0.75,
    )
    runtime = _runtime(workspace, calls, configurable)

    messages = _history(steps=1)
    compacted, budget = asyncio.run(runtime._compact_history(messages))

    assert calls == []
    assert compacted == messages
    assert budget == {}


def test_agent_runtime_checks_context_before_invoking_the_model():
    calls: list[str] = []
    # A large protected context forces the pressure check above the compaction ratio.
    workspace = _FakeWorkspace(
        _CompactionModel(calls), protected="schema " * 500, events=calls
    )
    configurable = Configuration(
        research_graph_context_capacity_tokens=200,
        context_compaction_threshold_ratio=0.75,
        context_warning_ratio=0.6,
    )
    runtime = _runtime(workspace, calls, configurable)

    asyncio.run(runtime.run("collect evidence"))

    # The context pressure hook must run before the first model invocation.
    assert "context_check" in calls
    assert "model" in calls
    assert calls.index("context_check") < calls.index("model")


# ---------------------------------------------------------------------------
# Error routing: token-limit recovery vs ordinary exception propagation
# ---------------------------------------------------------------------------


def _token_limit_error() -> Exception:
    error_type = type("BadRequestError", (Exception,), {})
    error_type.__module__ = "openai.error"
    return error_type("This model's maximum context length is 8192 tokens.")


def _writer_state() -> dict:
    from open_deep_research.state import Section

    return {
        "report": {
            "sections": [
                Section(
                    name="Risk",
                    description="Risk evidence",
                    research=True,
                    agent_role="risk_assessment",
                )
            ],
            "completed_sections": [],
        },
        "agents": {"risk_assessment": {"report": "risk evidence"}},
        "runtime": {"budget": {}},
    }


def _writer_config() -> dict:
    return {
        "configurable": {
            "section_writer_model": "fixture:model",
            "research_graph_enabled": False,
        }
    }


def test_ordinary_exception_is_not_routed_to_token_limit_recovery(monkeypatch):
    """Regression guard for historical ``is_token_limit_exceeded(...) or True`` logic."""
    import open_deep_research.deep_researcher as module

    monkeypatch.setenv("RESEARCH_GRAPH_ENABLED", "false")

    class _FailingModel:
        def with_config(self, _config):
            return self

        async def ainvoke(self, _messages):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(module, "configurable_model", _FailingModel())

    with pytest.raises(RuntimeError, match="provider unavailable"):
        asyncio.run(module.section_writer(_writer_state(), _writer_config()))


def test_token_limit_exception_still_enters_context_recovery(monkeypatch):
    import open_deep_research.deep_researcher as module

    monkeypatch.setenv("RESEARCH_GRAPH_ENABLED", "false")

    class _TokenLimitModel:
        def with_config(self, _config):
            return self

        async def ainvoke(self, _messages):
            raise _token_limit_error()

    monkeypatch.setattr(module, "configurable_model", _TokenLimitModel())

    result = asyncio.run(module.section_writer(_writer_state(), _writer_config()))

    budget = result["runtime"]["budget"]
    assert budget["model_calls"] == 0
    assert (
        "Section 'Risk' was not written because the model context limit was reached."
        in budget["degradation_reasons"]
    )


def test_token_limit_detection_is_specific_to_provider_errors():
    from open_deep_research.utils import is_token_limit_exceeded

    assert is_token_limit_exceeded(RuntimeError("provider unavailable"), "deepseek:deepseek-chat") is False
    assert is_token_limit_exceeded(_token_limit_error(), "openai:gpt-4.1") is True


# ---------------------------------------------------------------------------
# Final report input shaping
# ---------------------------------------------------------------------------


def test_fallback_final_report_truncates_findings_by_token_budget(monkeypatch):
    import open_deep_research.deep_researcher as module

    captured: dict = {}

    class _FinalReportModel:
        def with_config(self, _config):
            return self

        async def ainvoke(self, messages):
            captured["prompt"] = messages[0].content
            return AIMessage(content="final report body")

    monkeypatch.setattr(module, "configurable_model", _FinalReportModel())
    monkeypatch.setenv("RESEARCH_GRAPH_ENABLED", "false")
    huge_findings = "evidence line with facts, sources and citations. " * 400
    state = {
        "messages": [HumanMessage(content="brand risk question")],
        "agents": {"public_signal": {"report": huge_findings}},
        "workflow": {"brief": "brand risk brief"},
        "runtime": {"budget": {}},
    }
    config = {
        "configurable": {
            "budget_enabled": True,
            "max_input_tokens": 400,
            "final_report_model_max_tokens": 100,
            "research_graph_enabled": False,
        }
    }

    result = asyncio.run(module._fallback_report_generation(state, config))

    assert "final report body" in result["report"]["final"]
    assert "Truncated research findings to fit the remaining input token budget." in (
        result["report"]["final"]
    )
    assert len(captured["prompt"]) < len(huge_findings)
