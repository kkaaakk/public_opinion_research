"""Intake stage of the public-opinion workflow."""

import asyncio
import logging
from typing import Any, Literal

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
    get_buffer_string,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END
from langgraph.types import Command, interrupt

from open_deep_research.budget import (
    ainvoke_model_with_budget,
    available_research_unit_slots,
    budget_usage_with_reason,
    can_spend_model_call,
    merge_budget_usage,
    start_budget_capture,
    stop_budget_capture,
    structured_output_chain,
)
from open_deep_research.configuration import Configuration
from open_deep_research.llm.errors import is_token_limit_exceeded
from open_deep_research.models import get_api_key_for_model
from open_deep_research.prompts import (
    clarify_with_user_instructions,
    report_planner_instructions,
    transform_messages_into_research_topic_prompt,
)
from open_deep_research.rag import query_images
from open_deep_research.state import (
    AgentState,
    ClarifyWithUser,
    ResearchQuestion,
    Section,
    Sections,
)
from open_deep_research.time_utils import get_today_str

# Initialize a configurable model that we will use throughout the agent
from open_deep_research.workflow import shared
from open_deep_research.workflow.state_utils import (
    report_state,
    runtime_state,
    workflow_state,
)

LOGGER = logging.getLogger("open_deep_research.deep_researcher")

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
    budget_usage = runtime_state(state).get("budget", {})
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
        shared.configurable_model.with_config(model_config),
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


async def write_research_brief(
    state: AgentState,
    config: RunnableConfig,
) -> Command[Literal["plan_report_sections", "research_phase"]]:
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
    budget_usage = runtime_state(state).get("budget", {})
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
        shared.configurable_model.with_config(research_model_config),
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
    budget_usage = runtime_state(state).get("budget", {})

    # Budget guard: degrade to single section if we can't reserve final report call
    if not can_spend_model_call(configurable, budget_usage, reserve_final_report_call=True):
        budget_update = budget_usage_with_reason(
            "Skipped section planning due to budget constraints; falling back to single section."
        )
        single_section = Section(
            name="Research Report",
            description=workflow_state(state).get("brief", ""),
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

    feedback_text = "\n///\n".join(report_state(state).get("plan_feedback", [])) or "No feedback yet."

    prompt = report_planner_instructions.format(
        topic=workflow_state(state).get("brief", ""),
        report_organization=configurable.report_structure,
        feedback=feedback_text,
        date=get_today_str(),
    )

    planner = structured_output_chain(
        shared.configurable_model.with_config(planner_model_config),
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
            description=workflow_state(state).get("brief", ""),
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
            description=workflow_state(state).get("brief", ""),
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
