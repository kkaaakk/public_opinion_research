# 检查搜索网页摘要实际通过统一工厂路由到指定模型。
"""Focused integration tests for runtime paths using the unified factory."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sdk_model_support import install_inference

from open_deep_research.search import tavily as utils


# 验证 Tavily 摘要采用豆包模型配置与工厂入口。
def test_tavily_summarization_routes_to_doubao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUMMARIZATION_MODEL", raising=False)
    captured = []

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

    def summarize(request):
        captured.append(request)
        return {"summary": "summary", "key_excerpts": "page body"}

    monkeypatch.setattr(utils, "tavily_search_async", fake_search)
    install_inference(monkeypatch, summarize)
    output = asyncio.run(utils.tavily_search.coroutine(
        ["q"],
        config={
            "configurable": {
                "summarization_model": "volcengine:doubao-seed-2-0-mini-260428"
            }
        },
    ))
    assert "summary" in output and len(captured) == 1
    assert captured[0].model_ref == "volcengine:doubao-seed-2-0-mini-260428"
