"""Assemble and run one configured public-opinion business agent."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage, filter_messages
from langchain_core.runnables import RunnableConfig

from open_deep_research.budget import (
    ainvoke_model_with_budget,
    budget_usage_with_reason,
    can_spend_model_call,
    start_budget_capture,
    stop_budget_capture,
)
from open_deep_research.configuration import Configuration
from open_deep_research.mcp.domain_filter import get_tool_domain, tag_tools_with_domain
from open_deep_research.observability import observe_tool_ainvoke
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
from open_deep_research.state import DeepResearchState, ResearchReview, ResearchTask
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

LOGGER = logging.getLogger(__name__)
_CONFIGURABLE_MODEL = init_chat_model(
    configurable_fields=("model", "max_tokens", "api_key"),
)

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


def _workflow(state: Mapping[str, Any]) -> dict[str, Any]:
    return dict(state.get("workflow", {}) or {})


def _agents(state: Mapping[str, Any]) -> dict[str, Any]:
    return dict(state.get("agents", {}) or {})


def _research(state: Mapping[str, Any]) -> dict[str, Any]:
    return dict(state.get("research", {}) or {})


def _runtime(state: Mapping[str, Any]) -> dict[str, Any]:
    return dict(state.get("runtime", {}) or {})


def _role_reports(state: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(role): str(value.get("report") or "")
        for role, value in _agents(state).items()
        if isinstance(value, Mapping) and value.get("report")
    }


def _coerce_research_task(value: Any) -> ResearchTask | None:
    if isinstance(value, ResearchTask):
        return value
    if isinstance(value, dict):
        try:
            return ResearchTask.model_validate(value)
        except Exception:
            return None
    return None


def _coerce_research_tasks(value: Any) -> list[ResearchTask]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    return [
        task
        for item in values
        if (task := _coerce_research_task(item)) is not None
    ]


def _coerce_research_review(value: Any) -> ResearchReview | None:
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


def _is_followup(state: Mapping[str, Any]) -> bool:
    return int(_workflow(state).get("round", 1) or 1) > 1


def _business_context(configurable: Configuration) -> str:
    context = (configurable.organization_context or "").strip()
    if context:
        return context
    return (
        "No additional organization context was configured. Use the user's request, "
        "local RAG evidence, and cited public sources without inventing company facts."
    )


def _role_context(role_reports: dict[str, str], roles: tuple[str, ...]) -> str:
    formatted_reports = [
        f"## {role}\n{report}"
        for role in roles
        for report in [role_reports.get(role, "")]
        if report
    ]
    return "\n\n".join(formatted_reports) or "No upstream role reports are available yet."


def _resolve_research_run_id(
    state: Mapping[str, Any],
    config: RunnableConfig | None = None,
) -> str:
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
    return f"run_{uuid.uuid4().hex}"


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
        for task in _coerce_research_tasks(_workflow(state).get("pending_tasks", []))
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
        task_id=stable_id("TASK", run_id, role, _workflow(state).get("round", 1)),
        objective=str(_workflow(state).get("brief", "") or ""),
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
        upstream_context = _role_context(
            _role_reports(state),
            _UPSTREAM_ROLES.get(role, ()),
        )
    review = _coerce_research_review(_workflow(state).get("review"))
    review_context = review.model_dump_json(indent=2) if review else "No research review yet."
    assignment = (
        f"Overall research brief:\n{_workflow(state).get('brief', '')}\n\n"
        f"Upstream research context:\n{upstream_context}\n\n"
        f"Latest research review:\n{review_context}\n\n"
        f"Input contract:\n{chr(10).join(f'- {item}' for item in spec.input_contract)}\n\n"
        f"Your role-specific objective:\n{spec.expected_output}"
    )
    if not _is_followup(state):
        return assignment

    followup_tasks = [
        task
        for task in _coerce_research_tasks(_workflow(state).get("pending_tasks", []))
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
        f"Current mode: follow-up research round {_workflow(state).get('round', 2)}.\n"
        "Do not repeat the first-round comprehensive survey. Focus only on these unresolved, "
        "decision-relevant research gaps and use the existing scoped research context:\n"
        f"{chr(10).join(task_lines)}"
    )


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name") or "web_search")
    return str(getattr(tool, "name", ""))


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
        raw_names = {_tool_name(tool) for tool in raw_search_tools}
        all_tools = [tool for tool in all_tools if _tool_name(tool) not in raw_names]
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
            rejected_tools.append((_tool_name(tool), domain or "unclassified"))
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
    *,
    tool_call_id: str | None = None,
) -> tuple[str, dict[str, Any], bool]:
    capture_token = start_budget_capture()
    try:
        observation = await observe_tool_ainvoke(
            tool,
            args,
            config,
            tool_call_id=tool_call_id,
        )
        return observation, stop_budget_capture(capture_token), True
    except asyncio.CancelledError:
        stop_budget_capture(capture_token)
        raise
    except Exception as exc:
        captured_budget = stop_budget_capture(capture_token)
        LOGGER.exception("Unexpected tool execution failure for '%s'.", _tool_name(tool))
        return f"Error executing tool: {exc}", captured_budget, False


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
    for _attempt in range(3):
        try:
            response, response_budget = await ainvoke_model_with_budget(
                model,
                [
                    SystemMessage(
                        content=compress_research_system_prompt.format(date=get_today_str())
                    ),
                    *researcher_messages,
                ],
                observer_model=configurable.compression_model,
            )
            raw_notes = "\n".join(
                str(message.content)
                for message in filter_messages(
                    researcher_messages, include_types=["tool", "ai"]
                )
            )
            return (
                str(response.content),
                [raw_notes],
                response_budget,
            )
        except Exception as exc:
            if is_token_limit_exceeded(exc, configurable.compression_model):
                researcher_messages = remove_up_to_last_ai_message(researcher_messages)
                continue
            LOGGER.exception("Unexpected research compression failure.")
            raise

    raw_notes = "\n".join(
        str(message.content)
        for message in filter_messages(researcher_messages, include_types=["tool", "ai"])
    )
    return (
        "Error synthesizing research report: Maximum retries exceeded",
        [raw_notes],
        budget_usage_with_reason("Research compression failed after maximum retries."),
    )


def _build_state_patch(
    *,
    role: str,
    state: DeepResearchState,
    research_round: int,
    result: AgentRuntimeResult,
    workspace: ResearchWorkspace,
) -> dict[str, Any]:
    followup = _is_followup(state)
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
                _coerce_research_tasks(_workflow(state).get("pending_tasks", []))
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
    research_round = max(1, int(_workflow(state).get("round", 1) or 1))
    run_id = _resolve_research_run_id(state, config)
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
        research_state=_research(state),
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
        organization_context=_business_context(configurable),
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
        agent_state=_agents(state).get(role, {}),
        workspace=workspace,
        config=configurable,
        runtime_config=config,
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        initial_budget=_runtime(state).get("budget", {}),
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
