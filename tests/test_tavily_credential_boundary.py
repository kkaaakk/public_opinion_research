"""Keep the SDK credential accessor outside the model-visible tool lifecycle."""

import asyncio

from open_deep_research.search import tavily


def test_tavily_sdk_receives_the_plain_helper_key(monkeypatch):
    received = []
    class Client:
        def __init__(self, api_key):
            received.append(api_key)

        async def search(self, query, **kwargs):
            return {"results": [], "query": query, **kwargs}
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "false")
    monkeypatch.setenv("TAVILY_API_KEY", "unit-secret")
    monkeypatch.setattr(tavily, "AsyncTavilyClient", Client)
    result = asyncio.run(tavily.tavily_search_async(["battery"], config={}))
    assert received == ["unit-secret"] and result[0]["query"] == "battery"
    assert callable(tavily.get_tavily_api_key)
    assert not hasattr(tavily.get_tavily_api_key, "tool_call_schema")


def test_tavily_config_key_is_resolved_without_a_tool_invocation(monkeypatch):
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "true")
    assert tavily.get_tavily_api_key({"configurable": {"apiKeys": {"TAVILY_API_KEY": "config-secret"}}}) == "config-secret"
