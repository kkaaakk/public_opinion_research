"""Main LangGraph implementation for the Deep Research agent."""

import asyncio
import logging
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
    get_buffer_string,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from open_deep_research.budget import (
    ainvoke_model_with_budget,
    append_budget_summary,
    available_research_unit_slots,
    budget_usage_with_reason,
    can_spend_model_call,
    diff_budget_usage,
    estimate_text_tokens,
    merge_budget_usage,
    remaining_input_tokens,
    remaining_output_tokens,
    start_budget_capture,
    stop_budget_capture,
    structured_output_chain,
    truncate_text_to_token_budget,
)
from open_deep_research.configuration import Configuration
from open_deep_research.memory.writer import persist_conversation_memory
from open_deep_research.models import configurable_chat_model
from open_deep_research.observability import (
    WORKFLOW_NAME,
    ensure_langsmith_configuration,
    invocation_metadata,
)
from open_deep_research.observability.langsmith import node_metadata
from open_deep_research.prompts import (
    clarify_with_user_instructions,
    final_section_writer_instructions,
    public_opinion_final_report_generation_prompt,
    report_planner_instructions,
    research_review_prompt,
    section_writer_from_role_reports_prompt,
    transform_messages_into_research_topic_prompt,
)
from open_deep_research.rag import query_images
from open_deep_research.research_graph import (
    WorkingContext,
    build_research_review_context,
    format_relevant_subgraph,
    render_working_context,
    retrieve_research_context,
)
from open_deep_research.runtime import format_private_memory, run_business_agent
from open_deep_research.state import (
    AgentInputState,
    AgentState,
    ClarifyWithUser,
    DeepResearchState,
    PublicOpinionState,
    ResearchQuestion,
    ResearchReview,
    ResearchTask,
    Section,
    Sections,
)
from open_deep_research.utils import (
    get_api_key_for_model,
    get_model_token_limit,
    get_today_str,
    is_token_limit_exceeded,
)

# Initialize a configurable model that we will use throughout the agent
configurable_model = configurable_chat_model()
LOGGER = logging.getLogger(__name__)


def _workflow(state: Mapping[str, Any]) -> dict[str, Any]:
    return dict(state.get("workflow", {}) or {})


def _agents(state: Mapping[str, Any]) -> dict[str, Any]:
    return dict(state.get("agents", {}) or {})


def _research(state: Mapping[str, Any]) -> dict[str, Any]:
    return dict(state.get("research", {}) or {})


def _report(state: Mapping[str, Any]) -> dict[str, Any]:
    return dict(state.get("report", {}) or {})


def _runtime(state: Mapping[str, Any]) -> dict[str, Any]:
    return dict(state.get("runtime", {}) or {})


def _role_reports(state: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(role): str(value.get("report") or "")
        for role, value in _agents(state).items()
        if isinstance(value, Mapping) and value.get("report")
    }


def _role_memories(state: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        str(role): list(value.get("memory", []) or [])
        for role, value in _agents(state).items()
        if isinstance(value, Mapping)
    }


def _agents_for_new_run(state: Mapping[str, Any]) -> dict[str, Any]:
    """Carry private memory forward while clearing run-scoped reports/summaries."""
    return {
        role: {"memory": list(value.get("memory", []) or [])}
        for role, value in _agents(state).items()
        if isinstance(value, Mapping) and value.get("memory")
    }


def _is_followup(state: Mapping[str, Any]) -> bool:
    return int(_workflow(state).get("round", 1) or 1) > 1


def _business_context(configurable: Configuration) -> str:
    """Build business context for business-scenario prompts."""
    context = (configurable.organization_context or "").strip()
    if context:
        return context
    return (
        "No additional organization context was configured. Use the user's request, "
        "local RAG evidence, and cited public sources without inventing company facts."
    )


def _tool_name(available_tool) -> str:
    """Return a stable name for LangChain tools and provider-native tool dicts."""
    if isinstance(available_tool, dict):
        return available_tool.get("name") or "web_search"
    return getattr(available_tool, "name", "")


_RESEARCH_TASK_ROLES = ("public_signal", "internal_knowledge")


def _coerce_research_task(value: Any) -> ResearchTask | None:
    """Normalize a task from a Pydantic or serialized LangGraph state value."""
    if isinstance(value, ResearchTask):
        return value
    if isinstance(value, dict):
        try:
            return ResearchTask.model_validate(value)
        except Exception:
            return None
    return None


def _coerce_research_review(value: Any) -> ResearchReview | None:
    """Normalize a review from a Pydantic or serialized LangGraph state value."""
    if isinstance(value, ResearchReview):
        return value
    if isinstance(value, dict):
        try:
            return ResearchReview.model_validate(value)
        except Exception:
            return None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return ResearchReview.model_validate(model_dump())
        except Exception:
            return None
    return None


def _coerce_research_tasks(value: Any) -> list[ResearchTask]:
    """Normalize a list-like task channel and ignore malformed task values."""
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    return [
        task
        for item in values
        if (task := _coerce_research_task(item)) is not None
    ]


def _research_task_identity(task: ResearchTask) -> str:
    """Return a stable identity for task de-duplication across review rounds."""
    return task.task_id.strip() or (
        f"{task.target_role}:{task.objective.strip()}:{task.evidence_needed.strip()}"
    )


def _research_task_payload(task: ResearchTask) -> dict[str, Any]:
    """Serialize a task for a dynamic Send payload."""
    return task.model_dump()


def _with_correlation_metadata(
    config: RunnableConfig | None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a copied config with bounded LangSmith correlation metadata.

    The base config is never mutated, and the metadata is derived only from
    identifiers, never from graph state or user content.
    """
    merged: dict[str, Any] = dict(config) if isinstance(config, Mapping) else {}
    metadata: dict[str, Any] = {}
    existing = merged.get("metadata")
    if isinstance(existing, Mapping):
        metadata.update(existing)
    metadata.update(invocation_metadata(merged))
    if extra:
        metadata.update({str(key): value for key, value in extra.items() if value})
    merged["metadata"] = metadata
    return merged


def _resolve_research_run_id(
    state: Mapping[str, Any] | dict[str, Any],
    config: RunnableConfig | None = None,
) -> str:
    """Resolve one stable run scope without exposing credentials or raw history."""
    state_run_id = str(_research(state).get("run_id") or "").strip()
    if state_run_id:
        return state_run_id
    configurable: Mapping[str, Any] = (
        config.get("configurable", {}) if isinstance(config, Mapping) else {}
    )
    for key in ("research_run_id", "thread_id", "run_id"):
        value = str(configurable.get(key) or "").strip()
        if value:
            return value
    metadata = config.get("metadata", {}) if isinstance(config, Mapping) else {}
    for key in ("research_run_id", "thread_id", "run_id"):
        value = str(metadata.get(key) or "").strip() if isinstance(metadata, Mapping) else ""
        if value:
            return value
    # A new root invocation without a LangGraph thread id still gets a unique
    # scope.  research_phase writes it into state before parallel Sends begin.
    return f"run_{uuid.uuid4().hex}"


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
    current_round = max(1, int(_workflow(state).get("round", 1) or 1))
    if current_round >= configurable.max_research_rounds:
        return []

    completed_ids = {
        _research_task_identity(task)
        for task in _coerce_research_tasks(_workflow(state).get("completed_tasks", []))
    }
    seen_ids = set(completed_ids)
    effective_tasks: list[ResearchTask] = []
    for raw_task in review.next_tasks:
        task = _coerce_research_task(raw_task)
        if task is None or task.target_role not in _RESEARCH_TASK_ROLES:
            continue
        if task.target_role not in _enabled_research_roles(configurable):
            continue
        if not task.objective.strip() or not task.evidence_needed.strip() or not task.reason.strip():
            continue
        identity = _research_task_identity(task)
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
    workflow = _workflow(state)
    workflow.update(
        {
            "round": next_round,
            "pending_tasks": [_research_task_payload(task) for task in tasks],
        }
    )
    return {
        "messages": list(state.get("messages", [])),
        "workflow": workflow,
        "agents": _agents(state),
        "research": _research(state),
        "report": _report(state),
        "runtime": _runtime(state),
    }


def _role_context(
    role_reports: dict[str, str],
    roles: tuple[str, ...] | None = None,
) -> str:
    """Format upstream role reports for downstream public-opinion agents."""
    if not role_reports:
        return "No upstream role reports are available yet."

    role_names = roles if roles is not None else tuple(role_reports)
    formatted_reports = [
        f"## {role}\n{report}"
        for role in role_names
        for report in [role_reports.get(role, "")]
        if report
    ]
    return "\n\n".join(formatted_reports) or "No upstream role reports are available yet."


def _agent_private_memory_context(agent_memories: dict[str, list[dict[str, Any]]], role: str) -> str:
    """Format one agent's private short-term memory for prompt injection."""
    return format_private_memory(
        {"memory": list((agent_memories or {}).get(role, []) or [])}
    )


def _has_query_image_context(messages) -> bool:
    return any(
        bool(getattr(message, "additional_kwargs", {}).get("rag_query_image_context"))
        for message in messages
    )


def _messages_without_query_image_context(messages):
    return [
        message
        for message in messages
        if not bool(getattr(message, "additional_kwargs", {}).get("rag_query_image_context"))
    ]


async def enrich_query_images(state: AgentState, config: RunnableConfig) -> dict:
    """Convert user-question images into temporary text context before research planning."""
    configurable = Configuration.from_runnable_config(config)
    if (
        not configurable.rag_query_image_enabled
        or not configurable.rag_multimodal_enabled
    ):
        return {}

    messages = state.get("messages", [])
    if _has_query_image_context(messages):
        return {}

    context = await asyncio.to_thread(
        query_images.build_query_image_context,
        messages,
        provider=configurable.rag_multimodal_provider,
        ocr_languages=configurable.rag_ocr_languages,
        vision_enabled=configurable.rag_vision_enabled,
        vision_model=configurable.rag_vision_model,
        vision_prompt=configurable.rag_vision_prompt,
        vision_max_tokens=configurable.rag_vision_max_tokens,
        max_images=configurable.rag_query_image_max_images,
        max_bytes=configurable.rag_query_image_max_bytes,
    )
    if not context.strip():
        return {}

    return {
        "messages": [
            HumanMessage(
                content=(
                    "Recognized image context from the user's question. "
                    "Use it as temporary query context; it is not persisted as local "
                    "knowledge or memory.\n\n"
                    f"{context.strip()}"
                ),
                additional_kwargs={"rag_query_image_context": True},
            )
        ]
    }


def budget_skip_tool_message(tool_call: dict, reason: str) -> ToolMessage:
    """Create a synthetic tool result when Budget Guard skips a tool call."""
    return ToolMessage(
        content=f"Budget guard skipped `{tool_call['name']}`: {reason}",
        name=tool_call["name"],
        tool_call_id=tool_call["id"],
    )


async def clarify_with_user(state: AgentState, config: RunnableConfig) -> Command[Literal["write_research_brief", "__end__"]]:
    """Analyze user messages and ask clarifying questions if the research scope is unclear.
    
    This function determines whether the user's request needs clarification before proceeding
    with research. If clarification is disabled or not needed, it proceeds directly to research.
    
    Args:
        state: Current agent state containing user messages
        config: Runtime configuration with model settings and preferences
        
    Returns:
        Command to either end with a clarifying question or proceed to research brief
    """
    # Step 1: Check if clarification is enabled in configuration
    configurable = Configuration.from_runnable_config(config)
    if not configurable.allow_clarification:
        # Skip clarification step and proceed directly to research
        return Command(goto="write_research_brief")
    budget_usage = _runtime(state).get("budget", {})
    if not can_spend_model_call(
        configurable,
        budget_usage,
        reserve_final_report_call=True,
    ):
        return Command(
            goto="write_research_brief",
            update={
                "runtime": {"budget": budget_usage_with_reason(
                    "Skipped clarification to preserve the final report model call."
                )}
            },
        )
    
    # Step 2: Prepare the model for structured clarification analysis
    messages = state["messages"]
    model_config = {
        "model": configurable.research_model,
        "max_tokens": configurable.research_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.research_model, config),
        "tags": ["langsmith:nostream"]
    }
    
    # Configure model with structured output and retry logic
    clarification_model = structured_output_chain(
        configurable_model.with_config(model_config),
        ClarifyWithUser,
        max_attempts=configurable.max_structured_output_retries,
    )
    
    # Step 3: Analyze whether clarification is needed
    prompt_content = clarify_with_user_instructions.format(
        messages=get_buffer_string(messages), 
        date=get_today_str()
    )
    response, budget_update = await ainvoke_model_with_budget(
        clarification_model,
        [HumanMessage(content=prompt_content)],
        model_name=configurable.research_model,
        structured_output=True,
        component="clarification",
    )
    response = response["parsed"]
    
    # Step 4: Route based on clarification analysis
    if response.need_clarification:
        # End with clarifying question for user
        return Command(
            goto=END, 
            update={
                "messages": [AIMessage(content=response.question)],
                "runtime": {"budget": budget_update},
            }
        )
    else:
        # Proceed to research with verification message
        return Command(
            goto="write_research_brief", 
            update={
                "messages": [AIMessage(content=response.verification)],
                "runtime": {"budget": budget_update},
            }
        )


async def write_research_brief(state: AgentState, config: RunnableConfig) -> Command[Literal["research_phase"]]:
    """Transform user messages into a structured brief for the research phase.
    
    This function analyzes the user's messages and generates a focused research brief
    that will guide the public-opinion multi-agent subgraph.
    
    Args:
        state: Current agent state containing user messages
        config: Runtime configuration with model settings
        
    Returns:
        Command to proceed to the research phase with the initialized brief
    """
    # Step 1: Set up the research model for structured output
    configurable = Configuration.from_runnable_config(config)
    budget_usage = _runtime(state).get("budget", {})
    if not can_spend_model_call(
        configurable,
        budget_usage,
        reserve_final_report_call=True,
    ):
        research_brief = get_buffer_string(state.get("messages", []))
        return Command(
            goto="research_phase",
            update={
                "workflow": {"brief": research_brief},
                "runtime": {"budget": budget_usage_with_reason(
                    "Skipped research brief generation to preserve the final report model call."
                )},
            },
        )

    research_model_config = {
        "model": configurable.research_model,
        "max_tokens": configurable.research_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.research_model, config),
        "tags": ["langsmith:nostream"]
    }
    
    # Configure model for structured research question generation
    research_model = structured_output_chain(
        configurable_model.with_config(research_model_config),
        ResearchQuestion,
        max_attempts=configurable.max_structured_output_retries,
    )
    
    # Step 2: Generate structured research brief from user messages.
    prompt_content = transform_messages_into_research_topic_prompt.format(
        messages=get_buffer_string(state.get("messages", [])),
        date=get_today_str(),
    )
    prompt_content += (
        "\n\nBusiness scenario: Treat this request as enterprise public-opinion "
        "and brand-risk monitoring. Preserve the target organization, product, "
        "issue, geography, monitoring window, suspected claims, stakeholders, "
        "and any internal-knowledge requirements. If a detail is unspecified, "
        "state that it is unspecified rather than inventing it."
    )
    response, budget_update = await ainvoke_model_with_budget(
        research_model,
        [HumanMessage(content=prompt_content)],
        model_name=configurable.research_model,
        structured_output=True,
        component="research_brief",
    )
    response = response["parsed"]

    return Command(
        goto="plan_report_sections",
        update={
            "workflow": {"brief": response.research_brief},
            "runtime": {"budget": budget_update},
        }
    )


async def plan_report_sections(state: AgentState, config: RunnableConfig) -> Command[Literal["plan_report_sections", "research_phase"]]:
    """Plan report sections using Plan-and-Execute pattern.
    
    Generates structured Sections from the research brief,
    with optional human feedback via LangGraph interrupt().
    """
    configurable = Configuration.from_runnable_config(config)
    budget_usage = _runtime(state).get("budget", {})
    
    # Budget guard: degrade to single section if we can't reserve final report call
    if not can_spend_model_call(configurable, budget_usage, reserve_final_report_call=True):
        budget_update = budget_usage_with_reason(
            "Skipped section planning due to budget constraints; falling back to single section."
        )
        single_section = Section(
            name="Research Report",
            description=_workflow(state).get("brief", ""),
            research=True,
            agent_role="public_signal,internal_knowledge,risk_assessment,response_strategy",
            status="pending",
        )
        return Command(
            goto="research_phase",
            update={
                "report": {"sections": [single_section]},
                "runtime": {"budget": budget_update},
            },
        )
    
    # Plan sections using planner model
    planner_model_name = configurable.planner_model or configurable.research_model
    planner_max_tokens = configurable.planner_model_max_tokens
    
    planner_model_config: dict[str, Any] = {
        "model": planner_model_name,
        "api_key": get_api_key_for_model(planner_model_name, config),
        "tags": ["langsmith:nostream"],
    }
    if planner_max_tokens is not None:
        planner_model_config["max_tokens"] = planner_max_tokens
    
    feedback_text = "\n///\n".join(_report(state).get("plan_feedback", [])) or "No feedback yet."
    
    prompt = report_planner_instructions.format(
        topic=_workflow(state).get("brief", ""),
        report_organization=configurable.report_structure,
        feedback=feedback_text,
        date=get_today_str(),
    )
    
    planner = structured_output_chain(
        configurable_model.with_config(planner_model_config),
        Sections,
        max_attempts=configurable.max_structured_output_retries,
    )
    
    # Budget Capture so a failed planner attempt keeps its model_calls/usage.
    # try/finally guarantees the ContextVar is reset on every exit path.
    fallback_reason = ""
    attempt_token = start_budget_capture()
    try:
        try:
            response, _ = await ainvoke_model_with_budget(
                planner,
                [HumanMessage(content=prompt)],
                model_name=planner_model_name,
                structured_output=True,
                component="report_planner",
            )
            response = response["parsed"]
        except Exception as exc:
            if not is_token_limit_exceeded(exc, planner_model_name):
                LOGGER.exception("Unexpected report section planning failure.")
                raise
            LOGGER.warning(
                "Section planning hit the model context limit: %s. Falling back to single section.",
                exc,
            )
            fallback_reason = (
                "Section planning hit the model context limit; falling back to a single section."
            )
    finally:
        budget_update = stop_budget_capture(attempt_token)
    if fallback_reason:
        budget_update = merge_budget_usage(
            budget_update, budget_usage_with_reason(fallback_reason)
        )
        single_section = Section(
            name="Research Report",
            description=_workflow(state).get("brief", ""),
            research=True,
            agent_role="public_signal,internal_knowledge,risk_assessment,response_strategy",
            status="pending",
        )
        return Command(
            goto="research_phase",
            update={
                "report": {"sections": [single_section]},
                "runtime": {"budget": budget_update},
            },
        )
    
    # Budget-aware section count limit with section_writer reservation
    available_slots = available_research_unit_slots(configurable, budget_usage)
    max_sections = available_slots if available_slots is not None else 5
    
    all_sections = list(response.sections)
    research_section_count = sum(1 for s in all_sections if s.research)
    slots_after_reservation = max_sections - research_section_count
    if slots_after_reservation <= 0:
        LOGGER.warning(
            "Not enough budget slots for section_writer after planning %d research sections. "
            "Falling back to single section.",
            research_section_count,
        )
        budget_update = merge_budget_usage(
            budget_update,
            budget_usage_with_reason(
                "Insufficient budget slots for section_writer; falling back to single section."
            ),
        )
        single_section = Section(
            name="Research Report",
            description=_workflow(state).get("brief", ""),
            research=True,
            agent_role="public_signal,internal_knowledge,risk_assessment,response_strategy",
            status="pending",
        )
        return Command(
            goto="research_phase",
            update={
                "report": {"sections": [single_section]},
                "runtime": {"budget": budget_update},
            },
        )
    
    sections = all_sections[:max_sections]
    
    total_budget_update = merge_budget_usage(
        budget_update,
        budget_usage_with_reason(f"Planned {len(sections)} report sections (public-opinion mode)."),
    )
    
    # Human feedback via interrupt when enabled
    if configurable.allow_plan_feedback:
        feedback = interrupt({"sections": [s.model_dump() for s in sections]})
        
        if feedback is True:
            return Command(
                goto="research_phase",
                update={
                    "report": {"sections": sections},
                    "runtime": {"budget": total_budget_update},
                },
            )
        elif isinstance(feedback, str):
            return Command(
                goto="plan_report_sections",
                update={
                    "report": {"plan_feedback": [feedback]},
                    "runtime": {"budget": total_budget_update},
                },
            )
        else:
            raise TypeError(
                f"Expected resume value to be True (bool) or str, got {type(feedback).__name__}. "
                f"Value: {feedback!r}"
            )
    
    return Command(
        goto="research_phase",
        update={
            "report": {"sections": sections},
            "runtime": {"budget": total_budget_update},
        },
    )


# Public-opinion role names for validation
_PUBLIC_OPINION_ROLE_NAMES = frozenset({
    "public_signal", "internal_knowledge", "risk_assessment", "response_strategy",
})


def _extract_section_evidence(section, role_report_content: dict[str, str]) -> str:
    """Extract role evidence for a section based on its agent_role field.
    
    Parses comma-separated agent_role, validates against known roles,
    and extracts corresponding role reports.
    """
    agent_role_str = (section.agent_role or "").strip()
    if not agent_role_str:
        # No agent_role specified, use all available role reports
        return "\n\n".join(
            f"## {role}\n{content}"
            for role, content in role_report_content.items()
            if content
        ) or "No role evidence is available."
    
    role_names = [r.strip() for r in agent_role_str.split(",")]
    evidence_parts = []
    valid_roles_found = False
    
    for role_name in role_names:
        if not role_name:
            continue
        if role_name not in _PUBLIC_OPINION_ROLE_NAMES:
            LOGGER.warning("Invalid agent_role '%s' in section '%s'; ignoring.", role_name, section.name)
            continue
        content = role_report_content.get(role_name, "")
        if content:
            evidence_parts.append(f"## {role_name}\n{content}")
            valid_roles_found = True
        else:
            evidence_parts.append(f"## {role_name}\n(No evidence was collected by this role.)")
            valid_roles_found = True
    
    if not valid_roles_found:
        # Fallback: use all available role reports
        return "\n\n".join(
            f"## {role}\n{content}"
            for role, content in role_report_content.items()
            if content
        ) or "No role evidence is available."
    
    return "\n\n".join(evidence_parts)


def _section_role_report_content(
    role_reports: dict[str, str],
    agent_memories: dict[str, list[dict[str, Any]]],
) -> dict[str, str]:
    """Build section evidence with full reports first and memory as a fallback.

    A compact memory can preserve backward-compatible context when a role did not
    produce a formal report. It must never replace or be appended to an existing
    full current-run report, because the memory may be truncated.
    """
    content = {
        role: str(role_reports.get(role) or "")
        for role in _PUBLIC_OPINION_ROLE_NAMES
        if role in role_reports and role_reports.get(role)
    }
    for role in _PUBLIC_OPINION_ROLE_NAMES:
        if content.get(role):
            continue
        memory_context = _agent_private_memory_context(agent_memories, role)
        if memory_context != "No private memory has been recorded for this agent yet.":
            content[role] = (
                "[Compact private-memory fallback; no complete role report was provided]\n"
                f"{memory_context}"
            )
    return content


def _graph_section_evidence(
    section: Section,
    state: AgentState,
    config: RunnableConfig,
) -> str:
    """Retrieve section-specific graph evidence without injecting role reports."""
    configurable = Configuration.from_runnable_config(config)
    run_id = _resolve_research_run_id(state, config)
    roles = (section.agent_role or "").replace(",", " ")
    query = " ".join(
        value
        for value in (
            _workflow(state).get("brief", ""),
            section.name,
            section.description,
            roles,
        )
        if value
    )
    subgraph = retrieve_research_context(configurable, run_id=run_id, query=query)
    return format_relevant_subgraph(subgraph)


def _graph_report_context(
    state: AgentState,
    config: RunnableConfig,
    *,
    query_suffix: str = "final report",
) -> str:
    """Build bounded report context from graph nodes and Working Context."""
    configurable = Configuration.from_runnable_config(config)
    run_id = _resolve_research_run_id(state, config)
    query = " ".join(
        value
        for value in (_workflow(state).get("brief", ""), query_suffix)
        if value
    )
    subgraph = retrieve_research_context(configurable, run_id=run_id, query=query)
    contexts = _research(state).get("working_contexts", {}) or {}
    context_text = []
    for role, value in contexts.items():
        try:
            parsed = value if isinstance(value, WorkingContext) else WorkingContext.model_validate(value)
        except Exception:
            continue
        context_text.append(f"## {role}\n{render_working_context(parsed)}")
    return (
        "Research Graph report context (bounded, current run only):\n"
        + ("\n\n".join(context_text) or "No Working Context has been recorded yet.")
        + "\n\n"
        + format_relevant_subgraph(subgraph)
    )


async def section_writer(state: AgentState, config: RunnableConfig) -> dict:
    """Write report sections from role evidence in public-opinion mode.
    
    For each research=True section, extract evidence from role_reports based on 
    the section's agent_role field, then write the section content in parallel.
    """
    configurable = Configuration.from_runnable_config(config)
    sections = _report(state).get("sections", [])
    role_reports = _role_reports(state)
    agent_memories = _role_memories(state)
    graph_mode = configurable.research_graph_enabled
    # Formal role reports remain the compatibility path. Graph mode retrieves
    # section-specific evidence and does not inject complete role reports.
    role_report_content = (
        {}
        if graph_mode
        else _section_role_report_content(role_reports, agent_memories)
    )
    
    research_sections = [s for s in sections if s.research and s.status != "done"]
    if not research_sections:
        return {}

    section_evidence = {
        section.name: (
            _graph_section_evidence(section, state, config)
            if graph_mode
            else _extract_section_evidence(section, role_report_content)
        )
        for section in research_sections
    }
    
    # Budget guard: if no budget, copy role reports directly as content
    budget_usage = _runtime(state).get("budget", {})
    if not can_spend_model_call(configurable, budget_usage, reserve_final_report_call=True):
        budget_update = budget_usage_with_reason(
            "Skipped section_writer model calls due to budget constraints; using role reports as section content."
        )
        completed = []
        for section in research_sections:
            evidence = section_evidence[section.name]
            section.content = evidence
            section.status = "done"
            completed.append(section)
        return {
            "report": {"completed_sections": completed},
            "runtime": {"budget": budget_update},
        }
    
    # Write sections in parallel using section_writer_model
    writer_model_name = configurable.section_writer_model or configurable.final_report_model
    writer_max_tokens = configurable.section_writer_model_max_tokens
    
    writer_model_config: dict[str, Any] = {
        "model": writer_model_name,
        "api_key": get_api_key_for_model(writer_model_name, config),
        "tags": ["langsmith:nostream"],
    }
    if writer_max_tokens is not None:
        writer_model_config["max_tokens"] = writer_max_tokens
    
    async def _write_one(section: Section) -> tuple[Section, dict[str, Any]]:
        # Budget Capture so a failed section attempt keeps its model_calls/usage.
        # try/finally guarantees the ContextVar is reset on every exit path.
        failure_reason = ""
        attempt_token = start_budget_capture()
        try:
            try:
                evidence = section_evidence[section.name]
                prompt = section_writer_from_role_reports_prompt.format(
                    section_name=section.name,
                    section_description=section.description,
                    evidence=evidence,
                )
                writer = configurable_model.with_config(writer_model_config)
                response, _ = await ainvoke_model_with_budget(
                    writer,
                    [HumanMessage(content=prompt)],
                    model_name=writer_model_name,
                )
                section.content = str(response.content)
                section.status = "done"
            except Exception as exc:
                if not is_token_limit_exceeded(exc, writer_model_name):
                    LOGGER.exception("Unexpected section writer failure for '%s'.", section.name)
                    raise
                LOGGER.warning("Section '%s' exceeded the model context limit: %s", section.name, exc)
                section.content = ""
                failure_reason = (
                    f"Section '{section.name}' was not written because the model context limit was reached."
                )
        finally:
            response_budget = stop_budget_capture(attempt_token)
        if failure_reason:
            response_budget = merge_budget_usage(
                response_budget, budget_usage_with_reason(failure_reason)
            )
        return section, response_budget

    results = await asyncio.gather(*[_write_one(s) for s in research_sections])

    completed = []
    budget_update = {}
    for result, response_budget in results:
        completed.append(result)
        budget_update = merge_budget_usage(budget_update, response_budget)

    budget_update = merge_budget_usage(
        budget_update,
        budget_usage_with_reason(f"Wrote {len(completed)} sections via section_writer."),
    )
    return {
        "report": {"completed_sections": completed},
        "runtime": {"budget": budget_update},
    }


async def write_final_sections(state: AgentState, config: RunnableConfig) -> dict:
    """Write non-research sections (intro, conclusion) in parallel.
    
    Uses completed research sections as context for writing intro/conclusion.
    """
    configurable = Configuration.from_runnable_config(config)
    sections = _report(state).get("sections", [])
    completed_sections = _report(state).get("completed_sections", [])
    role_reports = _role_reports(state)
    
    final_sections = [s for s in sections if not s.research]
    if not final_sections:
        return {}
    
    # Build context from completed research sections (deduped)
    completed_map: dict[str, str] = {}
    for cs in completed_sections:
        if cs.name and cs.content:
            completed_map[cs.name] = cs.content
    
    context = "\n\n".join(
        f"## {name}\n{content}"
        for name, content in completed_map.items()
    )
    
    if not context:
        context = (
            _graph_report_context(state, config, query_suffix="section writing")
            if configurable.research_graph_enabled
            else _role_context(role_reports)
        )
    if not context or context == "No upstream role reports are available yet.":
        context = "No completed research sections are available yet."
    
    budget_usage = _runtime(state).get("budget", {})
    
    # Budget guard
    if not can_spend_model_call(configurable, budget_usage, reserve_final_report_call=True):
        budget_update = budget_usage_with_reason(
            "Skipped final section writing due to budget constraints."
        )
        return {"runtime": {"budget": budget_update}}
    
    writer_model_config: dict[str, Any] = {
        "model": configurable.final_report_model,
        "api_key": get_api_key_for_model(configurable.final_report_model, config),
        "tags": ["langsmith:nostream"],
    }
    
    async def _write_one(section: Section) -> tuple[Section, dict[str, Any]]:
        # Budget Capture so a failed final-section attempt keeps its model_calls/usage.
        # try/finally guarantees the ContextVar is reset on every exit path.
        failure_reason = ""
        attempt_token = start_budget_capture()
        try:
            try:
                prompt = final_section_writer_instructions.format(
                    section_name=section.name,
                    section_description=section.description,
                    context=context,
                )
                writer = configurable_model.with_config(writer_model_config)
                response, _ = await ainvoke_model_with_budget(
                    writer,
                    [HumanMessage(content=prompt)],
                    model_name=configurable.final_report_model,
                )
                section.content = str(response.content)
                section.status = "done"
            except Exception as exc:
                if not is_token_limit_exceeded(exc, configurable.final_report_model):
                    LOGGER.exception("Unexpected final section writer failure for '%s'.", section.name)
                    raise
                LOGGER.warning("Final section '%s' exceeded the model context limit: %s", section.name, exc)
                section.content = ""
                failure_reason = (
                    f"Final section '{section.name}' was not written because the model context limit was reached."
                )
        finally:
            response_budget = stop_budget_capture(attempt_token)
        if failure_reason:
            response_budget = merge_budget_usage(
                response_budget, budget_usage_with_reason(failure_reason)
            )
        return section, response_budget

    results = await asyncio.gather(*[_write_one(s) for s in final_sections])

    completed = []
    budget_update = {}
    for result, response_budget in results:
        completed.append(result)
        budget_update = merge_budget_usage(budget_update, response_budget)

    budget_update = merge_budget_usage(
        budget_update,
        budget_usage_with_reason(f"Wrote {len(completed)} final sections."),
    )
    return {
        "report": {"completed_sections": completed},
        "runtime": {"budget": budget_update},
    }


async def compile_final_report(state: AgentState, config: RunnableConfig):
    """Compile the final report by assembling sections in planned order.

    Assembles sections in their original planned order.
    Falls back to notes-based synthesis when all sections are missing.
    """
    configurable = Configuration.from_runnable_config(config)
    
    sections = _report(state).get("sections", [])
    completed_sections = _report(state).get("completed_sections", [])
    budget_usage = _runtime(state).get("budget", {})
    
    # Build deduped content map: take the latest content for each section name
    content_map: dict[str, str] = {}
    for cs in completed_sections:
        if cs.name and cs.content:
            content_map[cs.name] = cs.content
    
    # Assemble in planned order
    parts = []
    missing_names = []
    for section in sections:
        content = content_map.get(section.name, "")
        if content:
            parts.append(content)
        else:
            missing_names.append(section.name)
    
    if parts:
        report = "\n\n".join(parts)
        fill_budget_update: dict[str, Any] = {}
        
        # Handle missing sections
        if missing_names:
            if can_spend_model_call(configurable, budget_usage):
                # Try to fill missing sections with final_report_model
                findings = (
                    _graph_report_context(state, config, query_suffix="missing sections")
                    if configurable.research_graph_enabled
                    else _role_context(_role_reports(state))
                )

                final_report_prompt = public_opinion_final_report_generation_prompt.format(
                    research_brief=_workflow(state).get("brief", ""),
                    organization_context=_business_context(configurable),
                    messages=get_buffer_string(state.get("messages", [])),
                    findings=f"已有报告内容:\n{report}\n\n补充证据:\n{findings}",
                    date=get_today_str(),
                )

                writer_config: dict[str, Any] = {
                    "model": configurable.final_report_model,
                    "api_key": get_api_key_for_model(configurable.final_report_model, config),
                    "tags": ["langsmith:nostream"],
                }
                attempt_token = start_budget_capture()
                fill_failed = False
                try:
                    try:
                        fill_response, _ = await ainvoke_model_with_budget(
                            configurable_model.with_config(writer_config),
                            [HumanMessage(content=final_report_prompt)],
                            model_name=configurable.final_report_model,
                        )
                    except Exception as exc:
                        if not is_token_limit_exceeded(exc, configurable.final_report_model):
                            LOGGER.exception("Unexpected missing-section report fill failure.")
                            raise
                        LOGGER.warning(
                            "Filling missing sections hit the model context limit: %s",
                            exc,
                        )
                        fill_failed = True
                finally:
                    fill_budget_update = stop_budget_capture(attempt_token)
                if fill_failed:
                    report += f"\n\n> Note: 以下 section 因模型上下文限制未完成: {', '.join(missing_names)}"
                else:
                    budget_update = merge_budget_usage(
                        fill_budget_update,
                        budget_usage_with_reason(f"Filled {len(missing_names)} missing sections via final_report_model."),
                    )
                    final_usage = merge_budget_usage(budget_usage, budget_update)
                    report = append_budget_summary(
                        str(fill_response.content),
                        configurable,
                        final_usage,
                    )
                    await maybe_persist_chat_memory(state, config, report)
                    return {
                        "report": {"final": report},
                        "messages": [AIMessage(content=report)],
                        "runtime": {"budget": budget_update},
                    }
            else:
                report += f"\n\n> Note: 以下 section 因预算限制未完成: {', '.join(missing_names)}"
        
        await maybe_persist_chat_memory(state, config, report)
        return {
            "report": {"final": report},
            "messages": [AIMessage(content=report)],
            "runtime": {"budget": fill_budget_update},
        }
    
    # All sections missing: degrade from role reports (or scoped graph context).
    LOGGER.warning("No sections completed; falling back to role-report synthesis.")
    return await _fallback_report_generation(state, config)


async def research_review(state: PublicOpinionState, config: RunnableConfig) -> dict:
    """Review collected evidence and optionally create targeted follow-up tasks."""
    configurable = Configuration.from_runnable_config(config)
    current_round = max(1, int(_workflow(state).get("round", 1) or 1))
    previous_review = _coerce_research_review(_workflow(state).get("review"))
    completed_tasks = _coerce_research_tasks(
        _workflow(state).get("completed_tasks", [])
    )
    completed_tasks_text = "\n".join(
        f"- {task.model_dump_json()}" for task in completed_tasks
    ) or "None"
    previous_review_text = (
        previous_review.model_dump_json(indent=2) if previous_review else "None"
    )
    graph_review_metrics: dict[str, Any] = {}
    if configurable.research_graph_enabled:
        run_id = _resolve_research_run_id(state, config)
        review_subgraph = retrieve_research_context(
            configurable, run_id=run_id, query=_workflow(state).get("brief", "")
        )
        graph_review_metrics = {
            "graph_retrieval_calls": 1,
            "retrieved_nodes": len(review_subgraph.nodes),
            "retrieved_edges": len(review_subgraph.edges),
            "graph_retrieval_latency": 0,
            "quality": {"graph_retrieval_latency": "unavailable"},
        }
        working_context_values = _research(state).get("working_contexts", {}) or {}
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
        public_signal_report = _role_reports(state).get(
            "public_signal", "No public-signal report is available."
        )
        internal_knowledge_report = _role_reports(state).get(
            "internal_knowledge", "No internal-knowledge report is available."
        )
    prompt = research_review_prompt.format(
        research_brief=_workflow(state).get("brief", ""),
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
        configurable_model.with_config(review_model_config),
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
    review = _coerce_research_review(response["parsed"])
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
    review = _coerce_research_review(_workflow(state).get("review"))
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

    current_round = max(1, int(_workflow(state).get("round", 1) or 1))
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
    if _is_followup(state):
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
    input_budget = _runtime(state).get("budget", {})
    research_run_id = _resolve_research_run_id(state, config)
    # Bounded LangSmith correlation metadata for the multi-agent subgraph.  It
    # never enters graph state or checkpoints; only the LangSmith run tree.
    subgraph_config = _with_correlation_metadata(
        config, {"research_run_id": research_run_id}
    )
    result = await public_opinion_subgraph.ainvoke(
        {
            "messages": state.get("messages", []),
            "workflow": {
                "brief": _workflow(state).get("brief", ""),
                "round": 1,
                "review": None,
                "pending_tasks": [],
                "completed_tasks": [],
            },
            "agents": _agents_for_new_run(state),
            "research": {"run_id": research_run_id, "working_contexts": {}},
            "report": _report(state),
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


async def maybe_persist_chat_memory(
    state: AgentState,
    config: RunnableConfig,
    final_report_content: str,
) -> None:
    """Persist chat transcript, generated summary, and durable memory into MySQL RAG memory."""
    configurable = Configuration.from_runnable_config(config)
    if not configurable.rag_memory_write_enabled:
        return

    messages_text = get_buffer_string(
        _messages_without_query_image_context(state.get("messages", []))
    )
    research_brief = str(_workflow(state).get("brief") or "").strip()
    memories = [research_brief] if research_brief else []
    try:
        await asyncio.to_thread(
            persist_conversation_memory,
            configurable=configurable,
            runtime_config=config,
            chat_content=messages_text,
            summary=final_report_content,
            memories=memories,
            metadata={
                "workflow": "deep_researcher",
                "date": get_today_str(),
            },
        )
    except Exception:  # pragma: no cover - depends on external MySQL/vector DB
        LOGGER.exception("Failed to persist chat memory; continuing without persistence.")


async def _fallback_report_generation(state: AgentState, config: RunnableConfig):
    """Generate a fallback report from graph context or formal role reports."""
    configurable = Configuration.from_runnable_config(config)
    findings = (
        _graph_report_context(state, config)
        if configurable.research_graph_enabled
        else _role_context(_role_reports(state))
    )
    budget_usage = _runtime(state).get("budget", {})
    budget_update = {}

    if not can_spend_model_call(configurable, budget_usage):
        budget_update = budget_usage_with_reason(
            "Skipped final report generation because no model call budget remained."
        )
        fallback_report = findings or "Budget guard stopped before enough research notes were collected to generate a final report."
        final_usage = merge_budget_usage(budget_usage, budget_update)
        final_report_content = append_budget_summary(
            fallback_report,
            configurable,
            final_usage,
        )
        return {
            "report": {"final": final_report_content},
            "messages": [AIMessage(content=final_report_content)],
            "runtime": {"budget": budget_update},
        }

    messages_text = get_buffer_string(state.get("messages", []))
    context_window = (
        configurable.research_graph_context_capacity_tokens
        or get_model_token_limit(configurable.final_report_model)
    )
    output_reserve = int(configurable.final_report_model_max_tokens or 0)
    reserved_prompt_budget = estimate_text_tokens(
        _workflow(state).get("brief", "")
    ) + estimate_text_tokens(messages_text)
    remaining_input_budget = remaining_input_tokens(configurable, budget_usage)
    findings_budget = (
        max(0, remaining_input_budget - reserved_prompt_budget)
        if remaining_input_budget is not None
        else None
    )
    if context_window:
        window_budget = max(0, context_window - output_reserve - reserved_prompt_budget)
        findings_budget = (
            window_budget if findings_budget is None else min(findings_budget, window_budget)
        )
    if findings_budget is not None:
        findings, findings_truncated = truncate_text_to_token_budget(
            findings,
            findings_budget,
        )
        if findings_truncated:
            budget_update = merge_budget_usage(
                budget_update,
                budget_usage_with_reason(
                    "Truncated research findings to fit the remaining input token budget."
                ),
            )
    
    # Step 2: Configure the final report generation model
    final_report_max_tokens = configurable.final_report_model_max_tokens
    remaining_output_budget = remaining_output_tokens(
        configurable,
        merge_budget_usage(budget_usage, budget_update),
    )
    if remaining_output_budget is not None:
        if remaining_output_budget > 0:
            final_report_max_tokens = min(final_report_max_tokens, remaining_output_budget)
        elif configurable.reserve_final_report_call:
            final_report_max_tokens = min(final_report_max_tokens, 512)
            budget_update = merge_budget_usage(
                budget_update,
                budget_usage_with_reason(
                    "Generated a compact final report after the output token budget was exhausted."
                ),
            )
        else:
            budget_update = merge_budget_usage(
                budget_update,
                budget_usage_with_reason(
                    "Skipped final report generation because no output token budget remained."
                ),
            )
            fallback_report = findings or "Budget guard stopped before enough research notes were collected to generate a final report."
            final_usage = merge_budget_usage(budget_usage, budget_update)
            final_report_content = append_budget_summary(
                fallback_report,
                configurable,
                final_usage,
            )
            return {
                "report": {"final": final_report_content},
                "messages": [AIMessage(content=final_report_content)],
                "runtime": {"budget": budget_update},
            }

    writer_model_config = {
        "model": configurable.final_report_model,
        "max_tokens": final_report_max_tokens,
        "api_key": get_api_key_for_model(configurable.final_report_model, config),
        "tags": ["langsmith:nostream"]
    }
    
    # Step 3: Attempt report generation with token limit retry logic
    max_retries = 3
    current_retry = 0
    findings_token_limit = None
    
    while current_retry <= max_retries:
        # Budget Capture so a failed context-limit attempt keeps its attempts/usage.
        # try/finally guarantees the ContextVar is reset on every exit path.
        attempt_token = start_budget_capture()
        token_limit_hit = False
        try:
            try:
                # Create comprehensive prompt with all research context
                final_report_prompt = public_opinion_final_report_generation_prompt.format(
                    research_brief=_workflow(state).get("brief", ""),
                    organization_context=_business_context(configurable),
                    messages=messages_text,
                    findings=findings,
                    date=get_today_str(),
                )

                # Generate the final report
                final_report, _ = await ainvoke_model_with_budget(
                    configurable_model.with_config(writer_model_config),
                    [HumanMessage(content=final_report_prompt)],
                    model_name=configurable.final_report_model,
                )
            except Exception as exc:
                if not is_token_limit_exceeded(exc, configurable.final_report_model):
                    LOGGER.exception("Unexpected final report generation failure.")
                    raise
                token_limit_hit = True
        finally:
            budget_update = merge_budget_usage(
                budget_update, stop_budget_capture(attempt_token)
            )

        if not token_limit_hit:
            final_usage = merge_budget_usage(budget_usage, budget_update)
            final_report_content = append_budget_summary(
                str(final_report.content),
                configurable,
                final_usage,
            )
            await maybe_persist_chat_memory(state, config, final_report_content)

            # Return successful report generation
            return {
                "report": {"final": final_report_content},
                "messages": [AIMessage(content=final_report_content)],
                "runtime": {"budget": budget_update},
            }

        # Handle token limit exceeded errors with progressive truncation
        current_retry += 1

        if current_retry == 1:
            # First retry: determine initial truncation limit
            model_token_limit = get_model_token_limit(configurable.final_report_model)
            if not model_token_limit:
                LOGGER.exception(
                    "Final report model exceeded its context limit and no model limit is configured."
                )
                return {
                    "report": {"final": "Error generating final report due to a model context limit."},
                    "messages": [AIMessage(content="Report generation failed due to token limits")],
                    "runtime": {"budget": budget_update},
                }
            findings_token_limit = max(0, model_token_limit - output_reserve)
        else:
            # Subsequent retries: reduce by 10% each time
            findings_token_limit = int(findings_token_limit * 0.9)

        # Truncate findings by token budget and retry
        findings, _ = truncate_text_to_token_budget(findings, findings_token_limit)
    
    # Step 4: Return failure result if all retries exhausted
    return {
        "report": {"final": "Error generating final report: Maximum retries exceeded"},
        "messages": [AIMessage(content="Report generation failed after maximum retries")],
        "runtime": {"budget": merge_budget_usage(
            budget_update,
            budget_usage_with_reason("Final report generation failed after maximum retries."),
        )},
    }

# Main Deep Researcher Graph Construction
# Creates the complete deep research workflow from user input to final report.
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
