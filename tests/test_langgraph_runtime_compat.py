"""Retired Graph Server entrypoints cannot regain execution authority."""

from pathlib import Path

import pytest

from open_deep_research import deep_researcher
from open_deep_research.workflow import get_workflow


def test_langgraph_export_is_runtime_compatible():
    for name in (
        "deep_researcher",
        "deep_researcher_builder",
        "deep_researcher_graph",
        "public_opinion_builder",
        "public_opinion_subgraph",
    ):
        assert not hasattr(deep_researcher, name)
    with pytest.raises(ValueError, match="durable Run API"):
        get_workflow({})


def test_langgraph_json_graph_loads():
    assert not Path("langgraph.json").exists()
    assert not Path("src/security/auth.py").exists()


def test_langgraph_json_points_to_factory():
    from open_deep_research.runtime import SDKAgentRuntime

    assert SDKAgentRuntime.__bases__ == (object,)
    assert not Path("src/open_deep_research/runtime/agent_runtime.py").exists()
    assert not Path("src/open_deep_research/workflow/graph.py").exists()
