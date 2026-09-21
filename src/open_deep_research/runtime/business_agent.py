"""Assemble and run one configured public-opinion business agent."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, filter_messages
from langchain_core.runnables import RunnableConfig

from open_deep_research.budget import (
    ainvoke_model_with_budget,
    budget_usage_with_reason,
    can_spend_model_call,
    merge_budget_usage,
    start_budget_capture,
    stop_budget_capture,
)
from open_deep_research.configuration import Configuration
from open_deep_research.mcp.domain_filter import get_tool_domain, tag_tools_with_domain
from open_deep_research.models import configurable_chat_model
from open_deep_research.observability import agent_metadata
from open_deep_research.prompts import (
    compress_research_simple_human_message,
    compress_research_system_prompt,
)
from open_deep_research.public_opinion_agents import get_public_opinion_agent_spec
from open_deep_research.research_graph import (
    ResearchWorkspace,
    TaskDescriptor,
    create_context_strategy,
)
from open_deep_research.research_graph.schema import stable_id
from open_deep_research.runtime.agent_runtime import AgentRuntime, AgentRuntimeResult
from open_deep_research.social_media.tools import (
    SOCIAL_MEDIA_TOOL_NAMES,
    get_social_media_tools,
)
from open_deep_research.state import DeepResearchState
from open_deep_research.utils import (
    get_all_tools,
    get_api_key_for_model,
    get_raw_search_tool,
    get_research_tool_prompt,
    get_today_str,
    has_external_research_tool,
    is_token_limit_exceeded,
    remove_up_to_last_ai_message,
)
from open_deep_research.workflow.state_utils import (
    agents_state,
    business_context,
    coerce_research_review,
    coerce_research_tasks,
    is_followup,
    research_state,
    resolve_research_run_id,
    role_context,
    role_reports,
    runtime_state,
    tool_name,
    workflow_state,
)

LOGGER = logging.getLogger(__name__)
_CONFIGURABLE_MODEL = configurable_chat_model()

_UPSTREAM_ROLES: dict[str, tuple[str, ...]] = {
    "public_signal": (),
    "internal_knowledge": (),
    "risk_assessment": ("public_signal", "internal_knowledge"),
    "response_strategy": (
        "public_signal",
        "internal_knowledge",
        "risk_assessment",
    ),
}


def _effective_context_strategy(configurable: Configuration, spec: Any) -> str:
    configured = configurable.context_strategy.strip().lower()
    if configured != "auto":
        return configured
    return str(spec.context_strategy)


def _task_descriptor(
    state: DeepResearchState,
    role: str,
    run_id: str,
) -> TaskDescriptor:
    tasks = [
        task
        for task in coerce_research_tasks(workflow_state(state).get("pending_tasks", []))
        if task.target_role == role
    ]
    if tasks:
        task = tasks[0]
        return TaskDescriptor(
            task_id=task.task_id,
            objective=task.objective,
            evidence_needed=task.evidence_needed,
            reason=task.reason,
        )
    return TaskDescriptor(
        task_id=stable_id("TASK", run_id, role, workflow_state(state).get("round", 1)),
        objective=str(workflow_state(state).get("brief", "") or ""),
        evidence_needed="Evidence required by the role contract and current research brief.",
        reason="Initial role research task.",
    )


def _build_business_agent_assignment(
    state: DeepResearchState,
    role: str,
    configurable: Configuration | None = None,
    spec: Any | None = None,
) -> str:
    """Build the role's initial or gap-focused assignment."""
    configurable = configurable or Configuration.from_runnable_config({})
    spec = spec or get_public_opinion_agent_spec(role)
    if configurable.research_graph_enabled:
        upstream_context = (
            "Current-run upstream evidence is stored in the scoped Research Graph. "
            "The ResearchWorkspace will retrieve only the relevant subgraph; do not "
            "reconstruct or request complete upstream role reports."
        )
    else:
        upstream_context = role_context(
            role_reports(state),
            _UPSTREAM_ROLES.get(role, ()),
        )
    review = coerce_research_review(workflow_state(state).get("review"))
    review_context = review.model_dump_json(indent=2) if review else "No research review yet."
    assignment = (
        f"Overall research brief:\n{workflow_state(state).get('brief', '')}\n\n"
        f"Upstream research context:\n{upstream_context}\n\n"
        f"Latest research review:\n{review_context}\n\n"
        f"Input contract:\n{chr(10).join(f'- {item}' for item in spec.input_contract)}\n\n"
        f"Your role-specific objective:\n{spec.expected_output}"
    )
    if not is_followup(state):
        return assignment

    followup_tasks = [
        task
        for task in coerce_research_tasks(workflow_state(state).get("pending_tasks", []))
        if task.target_role == role
    ]
    if not followup_tasks:
        return assignment

    task_lines = [
        (
            f"{index}. {task.objective}\n"
            f"   Evidence needed: {task.evidence_needed}\n"
            f"   Why it matters: {task.reason}\n"
            f"   Priority: {task.priority}\n"
            f"   Task ID: {task.task_id}"
        )
        for index, task in enumerate(followup_tasks, start=1)
    ]
    return (
        f"{assignment}\n\n"
        f"Current mode: follow-up research round {workflow_state(state).get('round', 2)}.\n"
        "Do not repeat the first-round comprehensive survey. Focus only on these unresolved, "
        "decision-relevant research gaps and use the existing scoped research context:\n"
        f"{chr(10).join(task_lines)}"
    )


def _role_tool_prompt(configurable: Configuration, spec: Any) -> str:
    allowed_domains = spec.allowed_domains
    allowed_tools = []
    if "core" in allowed_domains:
        allowed_tools.extend(["think_tool", "ResearchComplete"])
    if "web_search" in allowed_domains:
        allowed_tools.append("web_search")
    if "rag" in allowed_domains:
        allowed_tools.append("rag_search")
    if "social_media" in allowed_domains:
        allowed_tools.extend(sorted(SOCIAL_MEDIA_TOOL_NAMES))
    return (
        f"{get_research_tool_prompt(configurable)}\n\n"
        f"Role-specific allowed domains: {', '.join(sorted(allowed_domains))}. "
        "Role-specific tool whitelist: "
        f"{', '.join(allowed_tools)}. "
        "Do not attempt to use tools outside this whitelist."
    )


async def _business_agent_tools(
    config: RunnableConfig,
    role: str,
    spec: Any | None = None,
) -> list[Any]:
    spec = spec or get_public_opinion_agent_spec(role)
    allowed_domains = spec.allowed_domains
    all_tools = await get_all_tools(config)
    configurable = Configuration.from_runnable_config(config)
    if (
        configurable.research_graph_enabled
        and _effective_context_strategy(configurable, spec) == "research_graph_producer"
    ):
        raw_search_tools = await get_raw_search_tool(configurable.search_api)
        raw_names = {tool_name(tool) for tool in raw_search_tools}
        all_tools = [tool for tool in all_tools if tool_name(tool) not in raw_names]
        all_tools.extend(raw_search_tools)
    if "social_media" in allowed_domains:
        all_tools.extend(tag_tools_with_domain(get_social_media_tools(), "social_media"))

    filtered_tools = []
    rejected_tools = []
    for tool in all_tools:
        domain = get_tool_domain(tool)
        if domain in allowed_domains:
            filtered_tools.append(tool)
        else:
            rejected_tools.append((tool_name(tool), domain or "unclassified"))
    LOGGER.debug(
        "Public-opinion agent %s: allowed_domains=%s tools_before=%d "
        "tools_after=%d rejected=%s",
        role,
        sorted(allowed_domains),
        len(all_tools),
        len(filtered_tools),
        rejected_tools,
    )
    return filtered_tools


async def _execute_tool_safely(
    tool: Any,
    args: Any,
    config: RunnableConfig,
) -> tuple[str, dict[str, Any], bool]:
    # try/finally guarantees the Budget Capture ContextVar is reset on every exit
    # path, including CancelledError/BaseException.
    capture_token = start_budget_capture()
    failed = False
    try:
        try:
            observation = await tool.ainvoke(args, config)
        except Exception as exc:
            LOGGER.exception("Unexpected tool execution failure for '%s'.", tool_name(tool))
            observation = f"Error executing tool: {exc}"
            failed = True
    finally:
        captured_budget = stop_budget_capture(capture_token)
    return observation, captured_budget, not failed


async def _compress_research(
    messages: list[Any],
    task: str,
    output_contract: str,
    cumulative_budget: dict[str, Any],
    *,
    configurable: Configuration,
    config: RunnableConfig,
) -> tuple[str, list[str], dict[str, Any]]:
    del task, output_contract
    if not can_spend_model_call(
        configurable,
        cumulative_budget,
        reserve_final_report_call=True,
    ):
        raw_notes = "\n".join(
            str(message.content)
            for message in filter_messages(messages, include_types=["tool", "ai"])
        )
        return (
            raw_notes
            or "Budget guard skipped research compression before any research notes were collected.",
            [raw_notes],
            budget_usage_with_reason(
                "Skipped research compression to preserve the final report model call."
            ),
        )

    model = _CONFIGURABLE_MODEL.with_config(
        {
            "model": configurable.compression_model,
            "max_tokens": configurable.compression_model_max_tokens,
            "api_key": get_api_key_for_model(configurable.compression_model, config),
            "tags": ["langsmith:nostream"],
        }
    )
    researcher_messages = [
        *messages,
        HumanMessage(content=compress_research_simple_human_message),
    ]
    attempt_budget: dict[str, Any] = {}
    for _attempt in range(3):
        # Budget Capture so a failed compression attempt keeps its model_calls.
        # try/finally guarantees the ContextVar is reset on every exit path.
        attempt_token = start_budget_capture()
        token_limit_hit = False
        try:
            try:
                response, _response_budget = await ainvoke_model_with_budget(
                    model,
                    [
                        SystemMessage(
                            content=compress_research_system_prompt.format(date=get_today_str())
                        ),
                        *researcher_messages,
                    ],
                    model_name=configurable.compression_model,
                )
            except Exception as exc:
                if not is_token_limit_exceeded(exc, configurable.compression_model):
                    LOGGER.exception("Unexpected research compression failure.")
                    raise
                token_limit_hit = True
        finally:
            attempt_budget = merge_budget_usage(
                attempt_budget, stop_budget_capture(attempt_token)
            )
        if token_limit_hit:
            researcher_messages = remove_up_to_last_ai_message(researcher_messages)
            continue
        raw_notes = "\n".join(
            str(message.content)
            for message in filter_messages(
                researcher_messages, include_types=["tool", "ai"]
            )
        )
        return (
            str(response.content),
            [raw_notes],
            attempt_budget,
        )

    raw_notes = "\n".join(
        str(message.content)
        for message in filter_messages(researcher_messages, include_types=["tool", "ai"])
    )
    return (
        "Error synthesizing research report: Maximum retries exceeded",
        [raw_notes],
        merge_budget_usage(
            attempt_budget,
            budget_usage_with_reason("Research compression failed after maximum retries."),
        ),
    )


def _build_state_patch(
    *,
    role: str,
    state: DeepResearchState,
    research_round: int,
    result: AgentRuntimeResult,
    workspace: ResearchWorkspace,
) -> dict[str, Any]:
    followup = is_followup(state)
    patch: dict[str, Any] = {
        "agents": {
            role: {
                "report": (
                    {"type": "override", "value": result.report}
                    if result.replace_report
                    else result.report
                ),
                "memory": [result.memory],
                "rolling_summary": result.rolling_summary,
            }
        },
        "workflow": {
            "completed_tasks": (
                coerce_research_tasks(workflow_state(state).get("pending_tasks", []))
                if followup
                else []
            ),
            **({"round": research_round} if followup else {}),
        },
        "runtime": {"budget": result.budget},
    }
    if workspace.strategy.graph_enabled:
        workspace_update = workspace.state_update()
        patch["research"] = workspace_update["research"]
        patch["runtime"]["metrics"] = workspace_update["runtime"]["metrics"]
    return patch


async def run_business_agent(
    *,
    role: str,
    state: DeepResearchState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Assemble, run, and translate one business agent into a state patch."""
    configurable = Configuration.from_runnable_config(config)
    enabled_roles = {
        str(item).strip().lower()
        for item in (configurable.enabled_business_agents or [])
    }
    if role not in enabled_roles:
        return {}

    spec = get_public_opinion_agent_spec(role)
    research_round = max(1, int(workflow_state(state).get("round", 1) or 1))
    run_id = resolve_research_run_id(state, config)
    strategy = create_context_strategy(
        _effective_context_strategy(configurable, spec),
        graph_enabled=configurable.research_graph_enabled,
    )
    assignment = _build_business_agent_assignment(state, role, configurable, spec)
    tools = await _business_agent_tools(config, role, spec)
    if not has_external_research_tool(tools):
        required = sorted(spec.allowed_domains - {"core"})
        raise ValueError(
            f"Public Opinion agent '{role}' ({spec.display_name}) requires "
            f"tool domains: {', '.join(required)}. Missing tools — ensure RAG is "
            "enabled (rag_enabled=true) and/or web search is configured "
            "(search_api=tavily)."
        )

    model_config = {
        "model": configurable.research_model,
        "max_tokens": configurable.research_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.research_model, config),
        "tags": ["langsmith:nostream"],
        "metadata": {
            **agent_metadata(getattr(spec, "node_name", f"{role}_agent"), role),
            "research_round": research_round,
        },
    }
    model = (
        _CONFIGURABLE_MODEL.bind_tools(tools)
        .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
        .with_config(model_config)
    )

    def model_factory(model_name: str, max_tokens: int | None) -> Any:
        nested_model_config: dict[str, Any] = {
            "model": model_name,
            "api_key": get_api_key_for_model(model_name, config),
            "tags": ["langsmith:nostream"],
        }
        if max_tokens is not None:
            nested_model_config["max_tokens"] = max_tokens
        return _CONFIGURABLE_MODEL.with_config(nested_model_config)

    workspace = ResearchWorkspace(
        strategy=strategy,
        research_state=research_state(state),
        role=role,
        configurable=configurable,
        model_factory=model_factory,
        task=_task_descriptor(state, role, run_id),
        run_id=run_id,
        research_round=research_round,
    )
    system_prompt = spec.format_system_prompt(
        retrieval_tool_prompt=_role_tool_prompt(configurable, spec),
        mcp_prompt=configurable.mcp_prompt or "",
        date=get_today_str(),
        organization_context=business_context(configurable),
    )

    async def compress_report(
        messages: list[Any],
        task: str,
        output_contract: str,
        cumulative_budget: dict[str, Any],
    ) -> tuple[str, list[str], dict[str, Any]]:
        return await _compress_research(
            messages,
            task,
            output_contract,
            cumulative_budget,
            configurable=configurable,
            config=config,
        )

    runtime = AgentRuntime(
        role=role,
        spec=spec,
        agent_state=agents_state(state).get(role, {}),
        workspace=workspace,
        config=configurable,
        runtime_config=config,
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        initial_budget=runtime_state(state).get("budget", {}),
        execute_tool=_execute_tool_safely,
        compress_report=compress_report,
    )
    result = await runtime.run(assignment)
    return _build_state_patch(
        role=role,
        state=state,
        research_round=research_round,
        result=result,
        workspace=workspace,
    )


__all__ = ["run_business_agent"]
