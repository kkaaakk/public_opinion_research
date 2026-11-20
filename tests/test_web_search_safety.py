# 验证网页摘要顺序执行、失败降级和 web_search 工具输出边界。
"""Offline regressions for the Tavily summary and tool-output safety contract."""

import asyncio

import pytest
from dbos import error as dbos_error
from langchain_core.language_models.chat_models import BaseChatModel
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.budget import ModelBudgetExceeded
from open_deep_research.search import tavily
from open_deep_research.state import Summary
from open_deep_research.workflow.execution import ExecutionControlError


def _config(**overrides):
    return {"configurable": dict(overrides)}


def _search_response(*urls):
    return [
        {
            "query": "brand risk",
            "results": [
                {
                    "url": url,
                    "title": f"Title for {index}",
                    "content": f"snippet {index}",
                    "raw_content": f"raw page {index}",
                }
                for index, url in enumerate(urls)
            ],
        }
    ]


def _install_summary_stubs(monkeypatch):
    monkeypatch.setattr(
        tavily, "create_chat_model", lambda **_kwargs: object()
    )
    monkeypatch.setattr(
        tavily,
        "structured_output_chain",
        lambda model, _schema, **_kwargs: model,
    )


def test_web_search_summarizes_pages_strictly_in_sequence(monkeypatch):
    async def fake_search(*_args, **_kwargs):
        return _search_response(
            "https://example.test/one",
            "https://example.test/two",
            "https://example.test/three",
        )

    monkeypatch.setattr(tavily, "tavily_search_async", fake_search)
    _install_summary_stubs(monkeypatch)
    active = 0
    max_active = 0
    timeline = []

    async def fake_summarize(_model, content, **_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        timeline.append(("start", content))
        await asyncio.sleep(0)
        timeline.append(("end", content))
        active -= 1
        return f"summary of {content}"

    monkeypatch.setattr(tavily, "summarize_webpage", fake_summarize)

    output = asyncio.run(tavily._tavily_search(["brand risk"], config=_config()))

    assert max_active == 1
    assert [event for event, _ in timeline] == ["start", "end"] * 3
    assert [content for event, content in timeline if event == "start"] == [
        "raw page 0",
        "raw page 1",
        "raw page 2",
    ]
    assert output.count("summary of raw page") == 3
    for index, url in enumerate(
        [
            "https://example.test/one",
            "https://example.test/two",
            "https://example.test/three",
        ]
    ):
        assert url in output
        assert f"Title for {index}" in output


def test_next_summary_starts_only_after_the_previous_settles(monkeypatch):
    async def fake_search(*_args, **_kwargs):
        return _search_response("https://example.test/one", "https://example.test/two")

    monkeypatch.setattr(tavily, "tavily_search_async", fake_search)
    _install_summary_stubs(monkeypatch)
    events = []

    async def fake_budget_invoke(_model, payload, **_kwargs):
        events.append(("begin", payload[0].content))
        await asyncio.sleep(0)
        events.append(("settle", payload[0].content))
        return {
            "parsed": Summary(summary="model summary", key_excerpts="excerpt")
        }, {}

    monkeypatch.setattr(tavily, "ainvoke_model_with_budget", fake_budget_invoke)

    output = asyncio.run(tavily._tavily_search(["brand risk"], config=_config()))

    kinds = [kind for kind, _ in events]
    assert kinds == ["begin", "settle", "begin", "settle"]
    assert output.count("<summary>\nmodel summary\n</summary>") == 2


def test_failed_summary_returns_a_bounded_marked_excerpt(monkeypatch):
    class FailingChat(BaseChatModel):
        @property
        def _llm_type(self):
            return "failing"

        def bind_tools(self, _tools, **_kwargs):
            return self

        def _generate(self, *_args, **_kwargs):
            raise ValueError("provider secret sk-should-not-leak")

    result = asyncio.run(
        tavily.summarize_webpage(
            FailingChat(),
            "RAW PAGE CONTENT " * 1000,
            fallback_content="short Tavily snippet",
            max_excerpt_chars=80,
        )
    )

    assert result.startswith("[SUMMARY UNAVAILABLE:")
    assert "short Tavily snippet" in result
    assert "sk-should-not-leak" not in result
    assert "Traceback" not in result
    assert "RAW PAGE CONTENT" not in result
    assert len(result) < 300


def test_failed_summary_bounds_raw_fallback_without_hiding_the_failure(monkeypatch):
    async def failing_budget_invoke(*_args, **_kwargs):
        raise ValueError("provider failure")

    monkeypatch.setattr(tavily, "ainvoke_model_with_budget", failing_budget_invoke)

    result = asyncio.run(
        tavily.summarize_webpage(
            object(),
            "W" * 50_000,
            fallback_content="q" * 50_000,
            max_excerpt_chars=120,
        )
    )

    assert result.startswith("[SUMMARY UNAVAILABLE:")
    assert "failed" in result
    assert "[EXCERPT TRUNCATED]" in result
    assert result.count("q") == 120
    assert "W" not in result


@pytest.mark.parametrize(
    "error",
    [
        ModelBudgetExceeded("token bill is unsettled"),
        ExecutionControlError("execution control failed"),
        SQLAlchemyError("connection pool unavailable"),
        dbos_error.DBOSWorkflowCancelledError("workflow cancelled"),
    ],
)
def test_control_failures_are_not_silently_degraded(monkeypatch, error):
    async def failing_budget_invoke(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(tavily, "ainvoke_model_with_budget", failing_budget_invoke)

    with pytest.raises(type(error)):
        asyncio.run(tavily.summarize_webpage(object(), "page text"))


def test_web_search_output_is_bounded_and_keeps_every_source(monkeypatch):
    urls = [f"https://example.test/source-{index}" for index in range(4)]

    async def fake_search(*_args, **_kwargs):
        return _search_response(*urls)

    async def huge_summary(*_args, **_kwargs):
        return "long summary text " * 5000

    monkeypatch.setattr(tavily, "tavily_search_async", fake_search)
    _install_summary_stubs(monkeypatch)
    monkeypatch.setattr(tavily, "summarize_webpage", huge_summary)

    output = asyncio.run(tavily._tavily_search(["brand risk"], config=_config()))

    assert len(output) <= tavily.MAX_WEB_SEARCH_OUTPUT_CHARS
    assert output.startswith("Search results:")
    assert "[TRUNCATED: source content exceeded" in output
    for index, url in enumerate(urls):
        assert url in output
        assert f"Title for {index}" in output
    # Later sources are not crowded out by the first oversized source.
    positions = [output.index(url) for url in urls]
    assert positions == sorted(positions)


def test_format_search_results_caps_each_source_and_the_total():
    results = {
        f"https://example.test/{index}": {
            "title": f"Source {index}",
            "content": "z" * 50_000,
        }
        for index in range(3)
    }

    output = tavily._format_search_results(results)

    assert len(output) <= tavily.MAX_WEB_SEARCH_OUTPUT_CHARS
    assert output.count("[TRUNCATED: source content exceeded") == 3
    for index in range(3):
        assert f"https://example.test/{index}" in output
        assert f"Source {index}" in output
    # Every source still carries some evidence text after its header.
    assert output.count("SUMMARY:\nz") == 3
