# 处理用户需求入口：图片上下文、需求澄清、研究简报和章节规划。
"""Intake stage of the public-opinion workflow."""

import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
    get_buffer_string,
)
from langchain_core.runnables import RunnableConfig

from open_deep_research.budget import (
    ainvoke_model_with_budget,
    budget_usage_with_reason,
    can_spend_model_call,
    capture_budget_usage,
    merge_budget_usage,
    model_budget_admission,
    start_budget_capture,
    stop_budget_capture,
    structured_output_chain,
)
from open_deep_research.configuration import Configuration
from open_deep_research.llm.errors import is_token_limit_exceeded
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


@dataclass
class IntakeDecision:
    """Carry a business transition and State patch without a workflow engine."""

    goto: str
    update: dict[str, Any] | None = None
    needs_plan_feedback: bool = False


# 检查消息历史是否已包含本轮图片解析上下文。
def _has_query_image_context(messages) -> bool:
    return any(
        bool(getattr(message, "additional_kwargs", {}).get("rag_query_image_context"))
        for message in messages
    )


# 过滤临时图片解析消息，避免重复注入或保存派生内容。
def _messages_without_query_image_context(messages):
    return [
        message
        for message in messages
        if not bool(
            getattr(message, "additional_kwargs", {}).get("rag_query_image_context")
        )
    ]


# 在研究规划前将用户图片转换为临时文本上下文。
async def enrich_query_images(state: AgentState, config: RunnableConfig) -> dict:
    """Convert user-question images into temporary text context before research planning."""
    configurable = Configuration.from_runnable_config(config)
    if not configurable.rag_query_image_enabled:
        return {}

    messages = state.get("messages", [])
    if _has_query_image_context(messages):
        return {}

    # The request helper captures usage; successful Steps consume State patches.
    # Mirror the captured delta to that patch and the existing failure boundary.
    with model_budget_admission(
        configurable, lambda: runtime_state(state).get("budget", {})
    ):
        token = start_budget_capture()
        try:
            context = await asyncio.to_thread(
                query_images.build_query_image_context,
                messages,
                vision_model=configurable.rag_vision_model,
                vision_prompt=configurable.rag_query_image_prompt,
                vision_max_tokens=configurable.rag_vision_max_tokens,
                max_images=configurable.rag_query_image_max_images,
                max_bytes=configurable.rag_query_image_max_bytes,
            )
        finally:
            usage = stop_budget_capture(token)
            capture_budget_usage(usage)
    patch = {"runtime": {"budget": usage}} if usage["model_calls"] else {}
    if not context.strip():
        return patch

    patch["messages"] = [
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
    return patch


# 为预算跳过的调用生成工具消息，明确说明跳过原因。
def budget_skip_tool_message(tool_call: dict, reason: str) -> ToolMessage:
    """Create a synthetic tool result when Budget Guard skips a tool call."""
    return ToolMessage(
        content=f"Budget guard skipped `{tool_call['name']}`: {reason}",
        name=tool_call["name"],
        tool_call_id=tool_call["id"],
    )


# 判断研究范围是否清晰，必要时向用户澄清，否则转入简报生成。
async def clarification_step(
    state: AgentState, config: RunnableConfig
) -> IntakeDecision:
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
        return IntakeDecision(goto="write_research_brief")
    budget_usage = runtime_state(state).get("budget", {})
    if not can_spend_model_call(
        configurable,
        budget_usage,
        reserve_final_report_call=True,
    ):
        return IntakeDecision(
            goto="write_research_brief",
            update={
                "runtime": {
                    "budget": budget_usage_with_reason(
                        "Skipped clarification to preserve the final report model call."
                    )
                }
            },
        )

    # Step 2: Prepare the model for structured clarification analysis
    messages = state["messages"]
    model_config = {
        "model": configurable.research_model,
        "max_tokens": configurable.research_model_max_tokens,
        "provider_config": config,
        "tags": ["langsmith:nostream"],
    }

    # Configure model with structured output and retry logic
    clarification_model = structured_output_chain(
        shared.configurable_model.with_config(model_config),
        ClarifyWithUser,
        max_attempts=configurable.max_structured_output_retries,
    )

    # Step 3: Analyze whether clarification is needed
    prompt_content = clarify_with_user_instructions.format(
        messages=get_buffer_string(messages), date=get_today_str()
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
        return IntakeDecision(
            goto="__end__",
            update={
                "messages": [AIMessage(content=response.question)],
                "runtime": {"budget": budget_update},
            },
        )
    else:
        # Proceed to research with verification message
        return IntakeDecision(
            goto="write_research_brief",
            update={
                "messages": [AIMessage(content=response.verification)],
                "runtime": {"budget": budget_update},
            },
        )


# 把用户消息整理为结构化研究简报，并进入规划或研究阶段。
async def research_brief_step(
    state: AgentState,
    config: RunnableConfig,
) -> IntakeDecision:
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
        return IntakeDecision(
            goto="research_phase",
            update={
                "workflow": {"brief": research_brief},
                "runtime": {
                    "budget": budget_usage_with_reason(
                        "Skipped research brief generation to preserve the final report model call."
                    )
                },
            },
        )

    research_model_config = {
        "model": configurable.research_model,
        "max_tokens": configurable.research_model_max_tokens,
        "provider_config": config,
        "tags": ["langsmith:nostream"],
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

    return IntakeDecision(
        goto="plan_report_sections",
        update={
            "workflow": {"brief": response.research_brief},
            "runtime": {"budget": budget_update},
        },
    )


# 根据简报生成章节计划；启用人工反馈时通过图中断接收调整意见。
async def section_plan_step(
    state: AgentState, config: RunnableConfig
) -> IntakeDecision:
    """Plan report sections using Plan-and-Execute pattern.

    Generates structured Sections from the research brief,
    with optional human feedback via LangGraph interrupt().
    """
    configurable = Configuration.from_runnable_config(config)
    budget_usage = runtime_state(state).get("budget", {})

    # Budget guard: degrade to single section if we can't reserve final report call
    if not can_spend_model_call(
        configurable, budget_usage, reserve_final_report_call=True
    ):
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
        return IntakeDecision(
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
        "provider_config": config,
        "tags": ["langsmith:nostream"],
    }
    if planner_max_tokens is not None:
        planner_model_config["max_tokens"] = planner_max_tokens

    feedback_text = (
        "\n///\n".join(report_state(state).get("plan_feedback", []))
        or "No feedback yet."
    )

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
            fallback_reason = "Section planning hit the model context limit; falling back to a single section."
    finally:
        budget_update = stop_budget_capture(attempt_token)
        if (failure := sys.exc_info()[1]) is not None:
            setattr(failure, "budget_usage", budget_update)
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
        return IntakeDecision(
            goto="research_phase",
            update={
                "report": {"sections": [single_section]},
                "runtime": {"budget": budget_update},
            },
        )

    # 章节共享角色研究结果，不对应独立研究单元。保留完整计划，避免截断议题；
    # 写作节点在研究结束后，按最新剩余调用次数分配实际写作额度。
    sections = list(response.sections)

    total_budget_update = merge_budget_usage(
        budget_update,
        budget_usage_with_reason(
            f"Planned {len(sections)} report sections (public-opinion mode)."
        ),
    )

    return IntakeDecision(
        goto="research_phase",
        update={
            "report": {"sections": sections},
            "runtime": {"budget": total_budget_update},
        },
        needs_plan_feedback=True,
    )


# Public-opinion role names for validation
