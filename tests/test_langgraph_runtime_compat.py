"""Tests for the native LangGraph export used by CLI and Studio."""

import json
from pathlib import Path

import pytest
from langgraph.pregel import Pregel

from open_deep_research.deep_researcher import deep_researcher, deep_researcher_graph


def test_langgraph_export_is_runtime_compatible() -> None:
    """The exported object is a factory whose result is a native Pregel graph."""

    assert callable(deep_researcher)
    assert not hasattr(deep_researcher, "ainvoke")
    assert isinstance(deep_researcher({}), Pregel)
    assert isinstance(deep_researcher_graph, Pregel)


def test_langgraph_json_graph_loads(monkeypatch) -> None:
    """The installed LangGraph API loader accepts the configured graph export."""

    monkeypatch.setenv(
        "DATABASE_URI",
        "postgresql://observer:observer@127.0.0.1:5432/observer",
    )
    monkeypatch.setenv("REDIS_URI", "redis://127.0.0.1:6379/0")
    langgraph_api_graph = pytest.importorskip("langgraph_api.graph")
    spec = langgraph_api_graph.GraphSpec(
        id="Deep Researcher",
        path="./src/open_deep_research/deep_researcher.py",
        variable="deep_researcher",
    )
    factory = langgraph_api_graph._graph_from_spec(spec)
    graph = factory({})
    assert callable(factory)
    assert isinstance(graph, Pregel)


def test_langgraph_json_points_to_factory() -> None:
    """The checked-in graph spec keeps the stable factory export name."""

    graph_config = json.loads(Path("langgraph.json").read_text(encoding="utf-8"))
    assert graph_config["graphs"]["Deep Researcher"].endswith(":deep_researcher")
