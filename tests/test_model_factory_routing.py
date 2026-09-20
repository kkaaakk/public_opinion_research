"""Focused integration tests for runtime paths using the unified factory."""

from __future__ import annotations

from typing import Any

import pytest

from open_deep_research import utils


@pytest.mark.asyncio
async def test_tavily_summarization_routes_to_doubao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUMMARIZATION_MODEL", raising=False)
    captured: dict[str, Any] = {}

    async def fake_search(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "query": "q",
                "results": [
                    {
                        "url": "https://example.test",
                        "title": "Example",
                        "content": "snippet",
                        "raw_content": "page body",
                    }
                ],
            }
        ]

    def fake_factory(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    async def fake_summarize(*_args: Any, **_kwargs: Any) -> str:
        return "summary"

    monkeypatch.setattr(utils, "tavily_search_async", fake_search)
    monkeypatch.setattr(utils, "create_chat_model", fake_factory)
    monkeypatch.setattr(
        utils, "structured_output_chain", lambda model, *_a, **_k: model
    )
    monkeypatch.setattr(utils, "summarize_webpage", fake_summarize)
    monkeypatch.setattr(utils, "get_api_key_for_model", lambda *_a, **_k: "fake-key")

    await utils.tavily_search.coroutine(
        ["q"],
        config={
            "configurable": {
                "summarization_model": "volcengine:doubao-seed-2-0-mini-260428"
            }
        },
    )

    assert captured["model"] == "volcengine:doubao-seed-2-0-mini-260428"
