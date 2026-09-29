"""Declare and export the top-level LangGraph workflow."""

from typing import Any

from langgraph.graph import END, START, StateGraph

from open_deep_research.configuration import Configuration
from open_deep_research.observability import (
    WORKFLOW_NAME,
    ensure_langsmith_configuration,
)
from open_deep_research.observability.langsmith import node_metadata
from open_deep_research.state import AgentInputState, DeepResearchState
from open_deep_research.workflow.intake import (
    clarify_with_user,
    enrich_query_images,
    plan_report_sections,
    write_research_brief,
)
from open_deep_research.workflow.reporting import (
    compile_final_report,
    section_writer,
    write_final_sections,
)
from open_deep_research.workflow.research import research_phase


def _create_deep_researcher_builder() -> StateGraph:
    """Build a native LangGraph state graph."""
    builder = StateGraph(
        DeepResearchState,
        input=AgentInputState,
        config_schema=Configuration,
    )

    builder.add_node(
        "enrich_query_images",
        enrich_query_images,
        metadata=node_metadata("enrich_query_images"),
    )
    builder.add_node(
        "clarify_with_user",
        clarify_with_user,
        metadata=node_metadata("clarify_with_user"),
    )
    builder.add_node(
        "write_research_brief",
        write_research_brief,
        metadata=node_metadata("write_research_brief"),
    )
    builder.add_node(
        "plan_report_sections",
        plan_report_sections,
        metadata=node_metadata("plan_report_sections"),
    )
    builder.add_node(
        "research_phase",
        research_phase,
        metadata=node_metadata("research_phase", kind="subgraph"),
    )
    builder.add_node(
        "section_writer",
        section_writer,
        metadata=node_metadata("section_writer", kind="writer"),
    )
    builder.add_node(
        "write_final_sections",
        write_final_sections,
        metadata=node_metadata("write_final_sections", kind="writer"),
    )
    builder.add_node(
        "compile_final_report",
        compile_final_report,
        metadata=node_metadata("compile_final_report", kind="writer"),
    )

    builder.add_edge(START, "enrich_query_images")
    builder.add_edge("enrich_query_images", "clarify_with_user")
    builder.add_edge("research_phase", "section_writer")
    builder.add_edge("section_writer", "write_final_sections")
    builder.add_edge("write_final_sections", "compile_final_report")
    builder.add_edge("compile_final_report", END)
    return builder


# Normalize LangSmith env vars once optional modules have loaded their .env.
ensure_langsmith_configuration()

# Keep the builder available for existing Python-side tests and tooling. The
# exported ``deep_researcher`` below is a factory so LangGraph CLI/Studio sees
# the native Pregel returned by the factory.
deep_researcher_builder = _create_deep_researcher_builder()
deep_researcher_graph = deep_researcher_builder.compile(name=WORKFLOW_NAME)


def deep_researcher(config: Any = None):
    """Return a freshly compiled native graph for LangGraph API per-invocation use.

    LangSmith traces the native Pregel under the ``public_opinion_research``
    name; correlation metadata is attached at the graph boundary in
    ``research_phase``.
    """
    del config  # The node-level RunnableConfig carries the invocation settings.
    return _create_deep_researcher_builder().compile(name=WORKFLOW_NAME)
