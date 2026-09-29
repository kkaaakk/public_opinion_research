"""Research stage of the public-opinion workflow."""

import logging
from typing import Any

from langchain_core.messages import (
    HumanMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from open_deep_research.budget import (
    ainvoke_model_with_budget,
    diff_budget_usage,
    structured_output_chain,
)
from open_deep_research.configuration import Configuration
from open_deep_research.models import get_api_key_for_model
from open_deep_research.observability.langsmith import node_metadata
from open_deep_research.prompts import (
    research_review_prompt,
)
from open_deep_research.research_graph import (
    WorkingContext,
    build_research_review_context,
    format_relevant_subgraph,
    retrieve_research_context,
)
from open_deep_research.runtime import run_business_agent
from open_deep_research.state import (
    AgentState,
    DeepResearchState,
    PublicOpinionState,
    ResearchReview,
    ResearchTask,
)

# Initialize a configurable model that we will use throughout the agent
from open_deep_research.workflow import shared
from open_deep_research.workflow.shared import _with_correlation_metadata
from open_deep_research.workflow.state_utils import (
    agents_for_new_run,
    agents_state,
    coerce_research_review,
    coerce_research_task,
    coerce_research_tasks,
    is_followup,
    report_state,
    research_state,
    research_task_identity,
    research_task_payload,
    resolve_research_run_id,
    role_reports,
    runtime_state,
    workflow_state,
)

LOGGER = logging.getLogger("open_deep_research.deep_researcher")

_RESEARCH_TASK_ROLES = ("public_signal", "internal_knowledge")

def _enabled_research_roles(configurable: Configuration) -> set[str]:
    """Return enabled roles that can execute dynamic follow-up research."""
    enabled = configurable.enabled_business_agents or []
    normalized = {str(role).strip().lower() for role in enabled}
    return normalized.intersection(_RESEARCH_TASK_ROLES)


def _effective_followup_tasks(
    state: PublicOpinionState,
    review: ResearchReview,
    configurable: Configuration,
) -> list[ResearchTask]:
    """Filter review tasks to new, executable, decision-relevant follow-up work."""
    current_round = max(1, int(workflow_state(state).get("round", 1) or 1))
    if current_round >= configurable.max_research_rounds:
        return []

    completed_ids = {
        research_task_identity(task)
        for task in coerce_research_tasks(workflow_state(state).get("completed_tasks", []))
    }
    seen_ids = set(completed_ids)
    effective_tasks: list[ResearchTask] = []
    for raw_task in review.next_tasks:
        task = coerce_research_task(raw_task)
        if task is None or task.target_role not in _RESEARCH_TASK_ROLES:
            continue
        if task.target_role not in _enabled_research_roles(configurable):
            continue
        if not task.objective.strip() or not task.evidence_needed.strip() or not task.reason.strip():
            continue
        identity = research_task_identity(task)
        if identity in seen_ids:
            continue
        seen_ids.add(identity)
        effective_tasks.append(task)
    return effective_tasks


def _build_followup_send_payload(
    state: PublicOpinionState,
    tasks: list[ResearchTask],
    next_round: int,
) -> dict[str, Any]:
    """Build one follow-up packet from domain aggregates, not individual fields."""
    workflow = workflow_state(state)
    workflow.update(
        {
            "round": next_round,
            "pending_tasks": [research_task_payload(task) for task in tasks],
        }
    )
    return {
        "messages": list(state.get("messages", [])),
        "workflow": workflow,
        "agents": agents_state(state),
        "research": research_state(state),
        "report": report_state(state),
        "runtime": runtime_state(state),
    }


async def research_review(state: PublicOpinionState, config: RunnableConfig) -> dict:
    """Review collected evidence and optionally create targeted follow-up tasks."""
    configurable = Configuration.from_runnable_config(config)
    current_round = max(1, int(workflow_state(state).get("round", 1) or 1))
    previous_review = coerce_research_review(workflow_state(state).get("review"))
    completed_tasks = coerce_research_tasks(
        workflow_state(state).get("completed_tasks", [])
    )
    completed_tasks_text = "\n".join(
        f"- {task.model_dump_json()}" for task in completed_tasks
    ) or "None"
    previous_review_text = (
        previous_review.model_dump_json(indent=2) if previous_review else "None"
    )
    graph_review_metrics: dict[str, Any] = {}
    if configurable.research_graph_enabled:
        run_id = resolve_research_run_id(state, config)
        review_subgraph = retrieve_research_context(
            configurable, run_id=run_id, query=workflow_state(state).get("brief", "")
        )
        graph_review_metrics = {
            "graph_retrieval_calls": 1,
            "retrieved_nodes": len(review_subgraph.nodes),
            "retrieved_edges": len(review_subgraph.edges),
            "graph_retrieval_latency": 0,
            "quality": {"graph_retrieval_latency": "unavailable"},
        }
        working_context_values = research_state(state).get("working_contexts", {}) or {}
        working_contexts = {}
        for role, value in working_context_values.items():
            try:
                working_contexts[str(role)] = (
                    value
                    if isinstance(value, WorkingContext)
                    else WorkingContext.model_validate(value)
                )
            except Exception:
                continue
        review_context = build_research_review_context(
            run_id=run_id,
            working_contexts=working_contexts,
            relevant_subgraph=review_subgraph,
        )
        graph_context_text = (
            f"{review_context.model_dump_json(indent=2)}\n\n"
            f"{format_relevant_subgraph(review_subgraph)}"
        )
        public_signal_report = (
            "Graph-mode review context (public signal nodes are scoped and retrieved):\n"
            + graph_context_text
        )
        internal_knowledge_report = (
            "Graph-mode review context (internal knowledge nodes are scoped and retrieved):\n"
            + graph_context_text
        )
    else:
        public_signal_report = role_reports(state).get(
            "public_signal", "No public-signal report is available."
        )
        internal_knowledge_report = role_reports(state).get(
            "internal_knowledge", "No internal-knowledge report is available."
        )
    prompt = research_review_prompt.format(
        research_brief=workflow_state(state).get("brief", ""),
        public_signal_report=public_signal_report,
        internal_knowledge_report=internal_knowledge_report,
        research_round=current_round,
        max_research_rounds=configurable.max_research_rounds,
        completed_tasks=completed_tasks_text,
        previous_review=previous_review_text,
    )
    review_model_config: dict[str, Any] = {
        "model": configurable.research_model,
        "api_key": get_api_key_for_model(configurable.research_model, config),
        "tags": ["langsmith:nostream"],
    }
    if configurable.research_model_max_tokens is not None:
        review_model_config["max_tokens"] = configurable.research_model_max_tokens

    reviewer = structured_output_chain(
        shared.configurable_model.with_config(review_model_config),
        ResearchReview,
        max_attempts=configurable.max_structured_output_retries,
    )
    response, review_budget = await ainvoke_model_with_budget(
        reviewer,
        [HumanMessage(content=prompt)],
        model_name=configurable.research_model,
        structured_output=True,
        component=f"research_review_round_{current_round}",
    )
    review = coerce_research_review(response["parsed"])
    if review is None:
        raise TypeError(
            "Research review model returned an invalid ResearchReview structured output."
        )

    return {
        "workflow": {
            "review": review,
            "pending_tasks": {"type": "override", "value": []},
        },
        "runtime": {
            "budget": review_budget,
            **({"metrics": graph_review_metrics} if graph_review_metrics else {}),
        },
    }


def route_after_research_review(
    state: PublicOpinionState,
    config: RunnableConfig | None = None,
) -> list[Send | str]:
    """Route a review to risk assessment or grouped role-specific Sends."""
    review = coerce_research_review(workflow_state(state).get("review"))
    if review is None or review.research_complete:
        return ["risk_assessment_agent"]

    configurable = (
        config
        if isinstance(config, Configuration)
        else Configuration.from_runnable_config(config)
    )
    # Deliberately route only on review findings, task validity, and the
    # workflow safety limit. Budget usage is carried for instrumentation and
    # must not decide whether this research loop continues.
    effective_tasks = _effective_followup_tasks(state, review, configurable)
    if not effective_tasks:
        return ["risk_assessment_agent"]

    current_round = max(1, int(workflow_state(state).get("round", 1) or 1))
    next_round = current_round + 1
    grouped_tasks = {
        role: [task for task in effective_tasks if task.target_role == role]
        for role in _RESEARCH_TASK_ROLES
    }
    sends: list[Send | str] = []
    node_by_role = {
        "public_signal": "public_signal_agent",
        "internal_knowledge": "internal_knowledge_agent",
    }
    for role in _RESEARCH_TASK_ROLES:
        tasks = grouped_tasks[role]
        if tasks:
            sends.append(
                Send(
                    node_by_role[role],
                    _build_followup_send_payload(state, tasks, next_round),
                )
            )
    return sends or ["risk_assessment_agent"]


def route_after_research_agent(state: PublicOpinionState) -> list[str]:
    """Return to review only for agents launched by a follow-up Send."""
    if is_followup(state):
        return ["research_review"]
    return []


async def public_signal_agent(state: PublicOpinionState, config: RunnableConfig) -> dict:
    """Collect integrated news, social, complaint, competitor, and spread evidence."""
    return await run_business_agent(role="public_signal", state=state, config=config)


async def internal_knowledge_agent(state: PublicOpinionState, config: RunnableConfig) -> dict:
    """Collect internal RAG evidence from company knowledge, playbooks, and memory."""
    return await run_business_agent(role="internal_knowledge", state=state, config=config)


async def risk_assessment_agent(state: PublicOpinionState, config: RunnableConfig) -> dict:
    """Verify claims and assess compliance, legal, and product-risk signals."""
    return await run_business_agent(role="risk_assessment", state=state, config=config)


async def response_strategy_agent(state: PublicOpinionState, config: RunnableConfig) -> dict:
    """Create PR response posture, FAQ points, actions, and monitoring keywords."""
    return await run_business_agent(role="response_strategy", state=state, config=config)


public_opinion_builder = StateGraph(DeepResearchState, config_schema=Configuration)
public_opinion_builder.add_node(
    "public_signal_agent",
    public_signal_agent,
    metadata=node_metadata("public_signal_agent", kind="agent"),
)
public_opinion_builder.add_node(
    "internal_knowledge_agent",
    internal_knowledge_agent,
    metadata=node_metadata("internal_knowledge_agent", kind="agent"),
)
public_opinion_builder.add_node(
    "research_review",
    research_review,
    metadata=node_metadata("research_review"),
)
public_opinion_builder.add_node(
    "risk_assessment_agent",
    risk_assessment_agent,
    metadata=node_metadata("risk_assessment_agent", kind="agent"),
)
public_opinion_builder.add_node(
    "response_strategy_agent",
    response_strategy_agent,
    metadata=node_metadata("response_strategy_agent", kind="agent"),
)

public_opinion_builder.add_edge(START, "public_signal_agent")
public_opinion_builder.add_edge(START, "internal_knowledge_agent")
public_opinion_builder.add_edge(
    [
        "public_signal_agent",
        "internal_knowledge_agent",
    ],
    "research_review",
)
public_opinion_builder.add_conditional_edges(
    "public_signal_agent",
    route_after_research_agent,
    path_map={"research_review": "research_review"},
)
public_opinion_builder.add_conditional_edges(
    "internal_knowledge_agent",
    route_after_research_agent,
    path_map={"research_review": "research_review"},
)
public_opinion_builder.add_conditional_edges(
    "research_review",
    route_after_research_review,
    path_map={
        "public_signal_agent": "public_signal_agent",
        "internal_knowledge_agent": "internal_knowledge_agent",
        "risk_assessment_agent": "risk_assessment_agent",
    },
)
public_opinion_builder.add_edge("risk_assessment_agent", "response_strategy_agent")
public_opinion_builder.add_edge("response_strategy_agent", END)

public_opinion_subgraph = public_opinion_builder.compile(name="public_opinion_agents")


async def research_phase(state: AgentState, config: RunnableConfig) -> dict:
    """Run initial and gap-driven public-opinion research before report writing."""
    input_budget = runtime_state(state).get("budget", {})
    research_run_id = resolve_research_run_id(state, config)
    # Bounded LangSmith correlation metadata for the multi-agent subgraph.  It
    # never enters graph state or checkpoints; only the LangSmith run tree.
    subgraph_config = _with_correlation_metadata(
        config, {"research_run_id": research_run_id}
    )
    result = await public_opinion_subgraph.ainvoke(
        {
            "messages": state.get("messages", []),
            "workflow": {
                "brief": workflow_state(state).get("brief", ""),
                "round": 1,
                "review": None,
                "pending_tasks": [],
                "completed_tasks": [],
            },
            "agents": agents_for_new_run(state),
            "research": {"run_id": research_run_id, "working_contexts": {}},
            "report": report_state(state),
            "runtime": {"budget": input_budget, "metrics": {}},
        },
        subgraph_config,
    )
    result_runtime = result.get("runtime", {}) or {}
    budget_update = diff_budget_usage(result_runtime.get("budget", {}), input_budget)
    return {
        "workflow": {"type": "override", "value": result.get("workflow", {})},
        "agents": {"type": "override", "value": result.get("agents", {})},
        "research": {"type": "override", "value": result.get("research", {})},
        "runtime": {
            "budget": budget_update,
            "metrics": result_runtime.get("metrics", {}),
        },
    }
