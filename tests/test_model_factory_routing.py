# 检查搜索网页摘要实际通过统一工厂路由到指定模型。
"""Focused integration tests for runtime paths using the unified factory."""

from __future__ import annotations

from typing import Any

import pytest

from open_deep_research.search import tavily as utils


# 验证 Tavily 摘要采用豆包模型配置与工厂入口。
@pytest.mark.asyncio
async def test_tavily_summarization_routes_to_doubao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUMMARIZATION_MODEL", raising=False)
    captured: dict[str, Any] = {}

    # 返回固定网页来源与正文，隔离实际 Tavily 请求。
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

    # 保存摘要模型创建参数，避免实例化真实供应商模型。
    def fake_factory(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    # 返回固定网页摘要，隔离实际摘要模型调用。
    async def fake_summarize(*_args: Any, **_kwargs: Any) -> str:
        return "summary"

    monkeypatch.setattr(utils, "tavily_search_async", fake_search)
    monkeypatch.setattr(utils, "create_chat_model", fake_factory)
    monkeypatch.setattr(
        utils, "structured_output_chain", lambda model, *_a, **_k: model
    )
    monkeypatch.setattr(utils, "summarize_webpage", fake_summarize)

    await utils.tavily_search.coroutine(
        ["q"],
        config={
            "configurable": {
                "summarization_model": "volcengine:doubao-seed-2-0-mini-260428"
            }
        },
    )

    assert captured["model"] == "volcengine:doubao-seed-2-0-mini-260428"
