"""Regression tests for the domain-owned Deep Research state architecture."""

import asyncio

from langchain_core.messages import HumanMessage

import open_deep_research.deep_researcher as deep_researcher_module
from open_deep_research.runtime import AgentRuntime
from open_deep_research.state import (
    DeepResearchState,
    ResearchReview,
    ResearchTask,
    agents_reducer,
    research_reducer,
    runtime_reducer,
    workflow_reducer,
)


def _task(task_id: str, role: str = "public_signal") -> ResearchTask:
    return ResearchTask(
        task_id=task_id,
        objective="Resolve evidence gap",
        target_role=role,
        evidence_needed="Primary evidence",
        reason="May change risk judgment",
    )


def test_top_level_state_contains_only_owned_domains_and_messages() -> None:
    """The checkpoint schema exposes six stable top-level concepts."""
    assert set(DeepResearchState.__annotations__) == {
        "messages", "workflow", "agents", "research", "report", "runtime"
    }


def test_parallel_agents_merge_report_memory_and_summary_by_role() -> None:
    """Parallel producer updates cannot overwrite another role's state."""
    merged = agents_reducer(
        {},
        {
            "public_signal": {
                "report": "public report",
                "memory": [{"content": "public memory"}],
                "rolling_summary": "public summary",
            }
        },
    )
    merged = agents_reducer(
        merged,
        {
            "internal_knowledge": {
                "report": "internal report",
                "memory": [{"content": "internal memory"}],
                "rolling_summary": "internal summary",
            }
        },
    )
    assert merged["public_signal"]["memory"][0]["content"] == "public memory"
    assert merged["internal_knowledge"]["report"] == "internal report"
    assert merged["public_signal"]["rolling_summary"] == "public summary"


def test_working_context_and_runtime_updates_merge_without_cross_role_loss() -> None:
    """Workspace context, budget, and metrics use their domain reducers."""
    research = research_reducer(
        {"run_id": "run-1", "working_contexts": {"public_signal": {"v": 1}}},
        {"working_contexts": {"internal_knowledge": {"v": 2}}},
    )
    runtime = runtime_reducer(
        {"budget": {"model_calls": 1}, "metrics": {"retrieved_nodes": 2}},
        {"budget": {"model_calls": 2}, "metrics": {"retrieved_nodes": 3}},
    )
    assert set(research["working_contexts"]) == {
        "public_signal", "internal_knowledge"
    }
    assert runtime["budget"]["model_calls"] == 3
    assert runtime["metrics"]["retrieved_nodes"] == 5


def test_followup_send_transports_domain_packets() -> None:
    """Dynamic Send payloads carry aggregates and no former flat channels."""
    task = _task("gap-1")
    state = {
        "messages": [],
        "workflow": {"brief": "brief", "round": 1, "review": ResearchReview(
            research_complete=False, next_tasks=[task]
        )},
        "agents": {"public_signal": {"report": "round one"}},
        "research": {"run_id": "run-1", "working_contexts": {}},
        "report": {},
        "runtime": {"budget": {}, "metrics": {}},
    }
    payload = deep_researcher_module._build_followup_send_payload(state, [task], 2)
    assert set(payload) == {
        "messages", "workflow", "agents", "research", "report", "runtime"
    }
    assert payload["workflow"]["pending_tasks"][0]["task_id"] == "gap-1"
    assert "research_mode" not in payload


def test_workflow_reducer_can_clear_pending_tasks_without_losing_history() -> None:
    """Review can clear transient work while completed tasks remain deduplicated."""
    task = _task("gap-1")
    merged = workflow_reducer(
        {"round": 2, "pending_tasks": [task], "completed_tasks": [task]},
        {"pending_tasks": {"type": "override", "value": []}},
    )
    assert merged["round"] == 2
    assert merged["pending_tasks"] == []
    assert [item.task_id for item in merged["completed_tasks"]] == ["gap-1"]


def test_private_memory_has_one_dynamic_context_entry() -> None:
    """Private memory is in the assignment and absent from the stable system prompt."""
    state = {
        "workflow": {"brief": "brief", "round": 1},
        "agents": {"public_signal": {"memory": [{"content": "PRIVATE_SENTINEL"}]}},
    }
    assignment = deep_researcher_module._build_public_opinion_agent_assignment(
        state, "public_signal"
    )
    spec = deep_researcher_module.get_public_opinion_agent_spec("public_signal")
    system_prompt = spec.format_system_prompt(
        retrieval_tool_prompt="tools", mcp_prompt="", date="2026-09-12",
        organization_context="org",
    )
    dynamic_context = AgentRuntime.format_private_memory(
        state["agents"]["public_signal"]
    )
    assert "PRIVATE_SENTINEL" not in assignment
    assert dynamic_context.count("PRIVATE_SENTINEL") == 1
    assert "PRIVATE_SENTINEL" not in system_prompt


def test_final_report_still_persists_conversation_memory(monkeypatch) -> None:
    """Conversation persistence remains outside research-run state."""
    captured = {}

    def fake_persist(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(deep_researcher_module, "persist_conversation_memory", fake_persist)
    asyncio.run(
        deep_researcher_module.maybe_persist_chat_memory(
            {
                "messages": [HumanMessage(content="question")],
                "workflow": {"brief": "brief"},
            },
            {"configurable": {"rag_memory_write_enabled": True}},
            "final report",
        )
    )
    assert captured["summary"] == "final report"
    assert captured["memories"] == ["brief"]
