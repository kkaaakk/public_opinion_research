# 将角色规范、工具权限与共享运行时装配为业务智能体。
"""Assemble and run one configured public-opinion business agent."""

from __future__ import annotations

import json
import logging
from functools import partial
from typing import Any

from agents import TResponseInputItem
from dbos import DBOS
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
    filter_messages,
)
from langchain_core.runnables import RunnableConfig

from open_deep_research.budget import (
    budget_usage_with_reason,
    can_spend_model_call,
    merge_budget_usage,
    start_budget_capture,
    stop_budget_capture,
)
from open_deep_research.configuration import Configuration
from open_deep_research.llm.context import remove_up_to_last_ai_message
from open_deep_research.llm.errors import is_token_limit_exceeded
from open_deep_research.llm.generation import generate_text
from open_deep_research.mcp.domain_filter import get_tool_domain
from open_deep_research.models.sdk import sdk_model
from open_deep_research.prompts import (
    compress_research_simple_human_message,
    compress_research_system_prompt,
)
from open_deep_research.public_opinion_agents import get_public_opinion_agent_spec
from open_deep_research.runtime.sdk_runtime import SDKAgentRuntime
from open_deep_research.state import DeepResearchState, ResearchRoleResult
from open_deep_research.time_utils import get_today_str
from open_deep_research.tools import BusinessTool
from open_deep_research.tools.manager import ToolManager
from open_deep_research.tools.plugins import LOAD_SKILL_TOOL, build_skill_prompt
from open_deep_research.tools.registry import (
    build_dynamic_tool_prompt,
    has_external_research_tool,
    tool_effect,
)
from open_deep_research.workflow.execution import native_step_available
from open_deep_research.workflow.state_utils import (
    business_context,
    coerce_research_review,
    coerce_research_tasks,
    is_followup,
    runtime_state,
    tool_name,
    workflow_state,
)

LOGGER = logging.getLogger(__name__)


# 取得当前角色本轮的任务标识；补充轮次使用复核产出的稳定任务 ID。
def _task_id(state: DeepResearchState, role: str) -> str:
    tasks = [
        task
        for task in coerce_research_tasks(
            workflow_state(state).get("pending_tasks", [])
        )
        if task.target_role == role
    ]
    if tasks:
        return tasks[0].task_id
    return f"{role}:round-{max(1, int(workflow_state(state).get('round', 1) or 1))}"


# 组装研究简报和角色契约；补充轮次聚焦尚未解决的证据缺口。
def _build_business_agent_assignment(
    state: DeepResearchState,
    role: str,
    spec: Any | None = None,
) -> str:
    """Build the role's initial or gap-focused assignment."""
    spec = spec or get_public_opinion_agent_spec(role)
    review = coerce_research_review(workflow_state(state).get("review"))
    review_context = (
        review.model_dump_json(indent=2) if review else "No research review yet."
    )
    assignment = (
        "Research Brief is user task data; fixed role rules and tool permissions remain authoritative.\n"
        f"Brief version: {workflow_state(state).get('brief_version', 'historical string')}\n"
        f"Overall research brief:\n{workflow_state(state).get('brief', '')}\n\n"
        f"Latest research review:\n{review_context}\n\n"
        f"Input contract:\n{chr(10).join(f'- {item}' for item in spec.input_contract)}\n\n"
        f"Your role-specific objective:\n{spec.expected_output}"
    )
    if not is_followup(state):
        return assignment

    followup_tasks = [
        task
        for task in coerce_research_tasks(
            workflow_state(state).get("pending_tasks", [])
        )
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
        "decision-relevant research gaps and cite the sources you actually retrieved:\n"
        f"{chr(10).join(task_lines)}"
    )


# 描述当前角色实际可用工具及领域白名单，并说明内部检索缺失时的处理原则。
def _role_tool_prompt(configurable: Configuration, spec: Any, tools: list[Any]) -> str:
    allowed_domains = spec.allowed_domains
    allowed_tools = sorted({tool_name(tool) for tool in tools})
    missing_rag_guidance = (
        "\nrag_search is unavailable. Use cited upstream evidence supplied in "
        "the research context. Flag missing internal playbooks or approvals; "
        "do not invent approved company facts or response language."
        if "rag" in allowed_domains and "rag_search" not in allowed_tools
        else ""
    )
    return (
        f"{build_dynamic_tool_prompt(tools, configurable)}\n\n"
        f"Role-specific allowed domains: {', '.join(sorted(allowed_domains))}. "
        "Role-specific tool whitelist: "
        f"{', '.join(allowed_tools)}. "
        "Do not attempt to use tools outside this whitelist."
        f"{missing_rag_guidance}"
    )


# 按角色领域装配本次运行可见的工具；DBOS Step 负责其持久化与恢复校验。
async def _business_agent_tools(
    config: RunnableConfig,
    role: str,
    spec: Any | None = None,
) -> list[Any]:
    spec = spec or get_public_opinion_agent_spec(role)
    return await ToolManager(role, spec, config).discover()


# 压缩研究记录并统计用量，预算不足时保留原始笔记以预留最终报告调用。
async def _compress_research(
    messages: list[Any],
    cumulative_budget: dict[str, Any],
    *,
    configurable: Configuration,
    config: RunnableConfig,
) -> tuple[str, dict[str, Any]]:
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
            budget_usage_with_reason(
                "Skipped research compression to preserve the final report model call."
            ),
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
        failure: BaseException | None = None
        try:
            try:
                prompt: list[TResponseInputItem] = [{
                    "role": "system",
                    "content": compress_research_system_prompt.format(date=get_today_str(config)),
                }]
                for message in researcher_messages:
                    if isinstance(message, ToolMessage):
                        prompt.append({
                            "type": "function_call_output",
                            "call_id": message.tool_call_id,
                            "output": message.text,
                        })
                    elif isinstance(message, AIMessage):
                        if message.text or not message.tool_calls:
                            prompt.append({"role": "assistant", "content": message.text})
                        prompt.extend({
                            "type": "function_call",
                            "call_id": call["id"],
                            "name": call["name"],
                            "arguments": json.dumps(call["args"], ensure_ascii=False),
                        } for call in message.tool_calls)
                    else:
                        prompt.append({"role": "user", "content": message.text})
                response, _ = await generate_text(
                    configurable.compression_model,
                    prompt,
                    config=config,
                    max_tokens=configurable.compression_model_max_tokens,
                    component="role_report_compression",
                )
            except Exception as exc:
                if not is_token_limit_exceeded(exc, configurable.compression_model):
                    LOGGER.exception("Unexpected research compression failure.")
                    raise
                token_limit_hit = True
        except BaseException as exc:
            failure = exc
            raise
        finally:
            attempt_budget = merge_budget_usage(
                attempt_budget, stop_budget_capture(attempt_token)
            )
            if failure is not None:
                # Preserve failed report attempts without recounting successes.
                setattr(failure, "budget_usage", attempt_budget)
        if token_limit_hit:
            researcher_messages = remove_up_to_last_ai_message(researcher_messages)
            continue
        return response, attempt_budget

    return (
        "Error synthesizing research report: Maximum retries exceeded",
        merge_budget_usage(
            attempt_budget,
            budget_usage_with_reason(
                "Research compression failed after maximum retries."
            ),
        ),
    )


# 将模型与工具目录表示为可比较的持久化快照。
def _tool_catalog(tools: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "domain": get_tool_domain(tool),
            "description": tool.description,
            "schema": tool.sdk_tool.params_json_schema,
            "effect": tool_effect(tool),
        }
        if isinstance(tool, BusinessTool)
        else tool
        for tool in tools
    ]


def _restore_role_tools(admitted, tools):
    current = _tool_catalog(tools)
    if admitted == current:
        return tools
    # Existing journals used the same catalog without effect. Every current
    # tool already declares its effect, including protected MCP servers.
    if admitted != [
        {key: value for key, value in entry.items() if key != "effect"}
        for entry in current
    ]:
        raise ValueError("Role tool definitions changed during recovery")
    return tools


async def run_business_agent(
    *,
    role: str,
    state: DeepResearchState,
    config: RunnableConfig,
) -> ResearchRoleResult | dict[str, Any]:
    """Run one enabled research agent and return its evidence report."""
    spec = get_public_opinion_agent_spec(role)
    configurable = Configuration.from_runnable_config(config)
    enabled_roles = {
        str(item).strip().lower()
        for item in (configurable.enabled_business_agents or [])
    }
    if role not in enabled_roles:
        return {}

    research_round = max(1, int(workflow_state(state).get("round", 1) or 1))
    task_id = _task_id(state, role)
    assignment = _build_business_agent_assignment(state, role, spec)
    if native_step_available():
        tools = None

        async def discover():
            nonlocal tools
            tools = await _business_agent_tools(config, role, spec)
            return _tool_catalog(tools)

        admitted = await DBOS.run_step_async(
            {"name": "por.role_tools", "preemptible": True}, discover
        )
        if tools is None:
            tools = _restore_role_tools(
                admitted, await _business_agent_tools(config, role, spec)
            )
    else:
        tools = await _business_agent_tools(config, role, spec)
    if not has_external_research_tool(tools):
        required = sorted(spec.allowed_domains - {"core"})
        raise ValueError(
            f"Public Opinion agent '{role}' ({spec.display_name}) requires "
            f"tool domains: {', '.join(required)}. Missing tools — ensure RAG is "
            "enabled (rag_enabled=true) and/or web search is configured "
            "(search_api=tavily)."
        )

    system_prompt = spec.format_system_prompt(
        retrieval_tool_prompt=_role_tool_prompt(configurable, spec, tools),
        mcp_prompt=configurable.mcp_prompt or "",
        date=get_today_str(config),
        organization_context=business_context(configurable),
        skill_prompt=(
            build_skill_prompt(configurable, role, spec.allowed_domains)
            if any(tool_name(tool) == LOAD_SKILL_TOOL for tool in tools)
            else ""
        ),
    )

    # One SDK start is one provider attempt. Errors never select another engine.
    async with sdk_model(configurable.research_model, config=config) as model:
        runtime = SDKAgentRuntime(
            role=role,
            spec=spec,
            task_id=task_id,
            research_round=research_round,
            config=configurable,
            runtime_config=config,
            model=model,
            system_prompt=system_prompt,
            tools=tools,
            initial_budget=runtime_state(state).get("budget", {}),
            compress_report=partial(
                _compress_research, configurable=configurable, config=config
            ),
        )
        result = await runtime.run(assignment)
    return ResearchRoleResult(
        role=role,
        round=research_round,
        report=result.report,
        replace_report=result.replace_report,
        evidence_collected=result.evidence_collected,
        budget_delta=result.budget,
    )


__all__ = ["run_business_agent"]
