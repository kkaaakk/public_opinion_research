"""Stable graph structure and module ownership contracts."""

import ast
from pathlib import Path

from open_deep_research.deep_researcher import deep_researcher_builder
from open_deep_research.workflow.research import public_opinion_builder

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "open_deep_research"


def test_main_graph_contract() -> None:
    """The top-level graph keeps its checkpoint-visible node and edge names."""
    assert set(deep_researcher_builder.nodes) == {
        "enrich_query_images",
        "clarify_with_user",
        "write_research_brief",
        "plan_report_sections",
        "research_phase",
        "section_writer",
        "write_final_sections",
        "compile_final_report",
    }
    assert set(deep_researcher_builder.edges) == {
        ("__start__", "enrich_query_images"),
        ("enrich_query_images", "clarify_with_user"),
        ("research_phase", "section_writer"),
        ("section_writer", "write_final_sections"),
        ("write_final_sections", "compile_final_report"),
        ("compile_final_report", "__end__"),
    }


def test_research_subgraph_contract() -> None:
    """Parallel producers, review routing, and final agents stay connected."""
    assert set(public_opinion_builder.nodes) == {
        "public_signal_agent",
        "internal_knowledge_agent",
        "research_review",
        "risk_assessment_agent",
        "response_strategy_agent",
    }
    assert set(public_opinion_builder.edges) == {
        ("__start__", "public_signal_agent"),
        ("__start__", "internal_knowledge_agent"),
        ("risk_assessment_agent", "response_strategy_agent"),
        ("response_strategy_agent", "__end__"),
    }
    assert {source: set(branches) for source, branches in public_opinion_builder.branches.items()} == {
        "public_signal_agent": {"route_after_research_agent"},
        "internal_knowledge_agent": {"route_after_research_agent"},
        "research_review": {"route_after_research_review"},
    }
    assert public_opinion_builder.branches["research_review"]["route_after_research_review"].ends == {
        "public_signal_agent": "public_signal_agent",
        "internal_knowledge_agent": "internal_knowledge_agent",
        "risk_assessment_agent": "risk_assessment_agent",
    }


def test_graph_and_utils_keep_their_module_boundaries() -> None:
    """Graph assembly and compatibility exports cannot regain implementation."""
    graph = ast.parse((SOURCE_ROOT / "workflow" / "graph.py").read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in graph.body
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(module and set(module.split(".")) & {"search", "rag", "mcp", "memory"} for module in imports)

    shim = ast.parse((SOURCE_ROOT / "utils.py").read_text(encoding="utf-8"))
    assert all(isinstance(node, (ast.Expr, ast.ImportFrom, ast.Assign)) for node in shim.body)
    assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for node in shim.body)
