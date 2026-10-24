# 验证五个状态域的归属、并发合并、动态任务载荷及最终记忆保存。
"""Regression tests for the domain-owned Deep Research state architecture."""

import asyncio

from langchain_core.messages import HumanMessage

import open_deep_research.deep_researcher as deep_researcher_module
import open_deep_research.memory.writer as memory_writer
import open_deep_research.runtime.business_agent as business_agent_module
from open_deep_research.public_opinion_agents import get_public_opinion_agent_spec
from open_deep_research.runtime import SDKAgentRuntime as AgentRuntime
from open_deep_research.state import (
    AgentState,
    DeepResearchState,
    PublicOpinionState,
    ResearchReview,
    ResearchTask,
    agents_reducer,
    research_reducer,
    runtime_reducer,
    workflow_reducer,
)


# 构造标准研究任务供状态合并测试使用。
def _task(task_id: str, role: str = "public_signal") -> ResearchTask:
    return ResearchTask(
        task_id=task_id,
        objective="Resolve evidence gap",
        target_role=role,
        evidence_needed="Primary evidence",
        reason="May change risk judgment",
    )


# 验证顶层状态只包含约定状态域和原生消息。
def test_top_level_state_contains_only_owned_domains_and_messages() -> None:
    """The checkpoint schema exposes six stable top-level concepts."""
    assert set(DeepResearchState.__annotations__) == {
        "messages",
        "workflow",
        "agents",
        "research",
        "report",
        "runtime",
    }


# 验证旧状态别名共用同一个规范结构。
def test_state_aliases_share_one_schema_source_of_truth() -> None:
    """Main, subgraph, and compatibility names resolve to one schema object."""
    assert AgentState is DeepResearchState
    assert PublicOpinionState is DeepResearchState
    assert not hasattr(deep_researcher_module, "deep_researcher_builder")
    assert not hasattr(deep_researcher_module, "public_opinion_builder")


# 验证并行角色的报告、记忆和摘要不会互相覆盖。
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


# 验证工作上下文和运行统计合并时不丢失其他角色数据。
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
    assert set(research["working_contexts"]) == {"public_signal", "internal_knowledge"}
    assert runtime["budget"]["model_calls"] == 3
    assert runtime["metrics"]["retrieved_nodes"] == 5


# 验证补充 Send 按完整状态域传输数据。
def test_followup_send_transports_domain_packets() -> None:
    """Dynamic Send payloads carry aggregates and no former flat channels."""
    task = _task("gap-1")
    state = {
        "messages": [],
        "workflow": {
            "brief": "brief",
            "round": 1,
            "review": ResearchReview(research_complete=False, next_tasks=[task]),
        },
        "agents": {"public_signal": {"report": "round one"}},
        "research": {"run_id": "run-1", "working_contexts": {}},
        "report": {},
        "runtime": {"budget": {}, "metrics": {}},
    }
    payload = deep_researcher_module._build_followup_send_payload(state, [task], 2)
    assert set(payload) == {
        "messages",
        "workflow",
        "agents",
        "research",
        "report",
        "runtime",
    }
    assert payload["workflow"]["pending_tasks"][0]["task_id"] == "gap-1"
    assert "research_mode" not in payload


# 验证显式清空待办时仍保留已完成任务历史。
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


# 验证私有记忆只注入一次动态上下文。
def test_private_memory_has_one_dynamic_context_entry() -> None:
    """Private memory is in the assignment and absent from the stable system prompt."""
    state = {
        "workflow": {"brief": "brief", "round": 1},
        "agents": {"public_signal": {"memory": [{"content": "PRIVATE_SENTINEL"}]}},
    }
    assignment = business_agent_module._build_business_agent_assignment(
        state, "public_signal"
    )
    spec = get_public_opinion_agent_spec("public_signal")
    system_prompt = spec.format_system_prompt(
        retrieval_tool_prompt="tools",
        mcp_prompt="",
        date="2026-09-12",
        organization_context="org",
    )
    dynamic_context = AgentRuntime.format_private_memory(
        state["agents"]["public_signal"]
    )
    assert "PRIVATE_SENTINEL" not in assignment
    assert dynamic_context.count("PRIVATE_SENTINEL") == 1
    assert "PRIVATE_SENTINEL" not in system_prompt


# 验证最终报告完成后仍调用对话记忆持久化。
def test_final_report_still_persists_conversation_memory(monkeypatch) -> None:
    """Conversation persistence remains outside research-run state."""
    captured = {}

    # 记录最终报告阶段传给记忆持久化入口的参数。
    def fake_persist(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(memory_writer, "persist_conversation_memory", fake_persist)
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
