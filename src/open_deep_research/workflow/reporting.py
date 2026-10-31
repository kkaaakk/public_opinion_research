# 依据角色证据或研究图生成章节，并按计划顺序汇编最终报告。
"""Reporting stage of the public-opinion workflow."""

import logging
import sys
from typing import Any, Callable

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    get_buffer_string,
)
from langchain_core.runnables import RunnableConfig

from open_deep_research.budget import (
    ModelBudgetExceeded,
    ainvoke_model_with_budget,
    append_budget_summary,
    budget_capture,
    budget_usage_with_reason,
    can_spend_model_call,
    estimate_text_tokens,
    merge_budget_usage,
    remaining_input_tokens,
    remaining_model_calls,
    remaining_output_tokens,
    start_budget_capture,
    stop_budget_capture,
    truncate_text_to_token_budget,
)
from open_deep_research.configuration import Configuration
from open_deep_research.llm.context import get_model_token_limit
from open_deep_research.llm.errors import is_token_limit_exceeded
from open_deep_research.memory.writer import (
    maybe_persist_chat_memory,
)
from open_deep_research.prompts import (
    final_section_writer_instructions,
    public_opinion_final_report_generation_prompt,
    section_writer_from_role_reports_prompt,
)
from open_deep_research.research_graph import (
    WorkingContext,
    format_relevant_subgraph,
    render_working_context,
    retrieve_research_context,
)
from open_deep_research.runtime import format_private_memory
from open_deep_research.state import (
    AgentState,
    Section,
)
from open_deep_research.time_utils import get_today_str

# Initialize a configurable model that we will use throughout the agent
from open_deep_research.workflow import shared
from open_deep_research.workflow.execution import execution_effect, gather_owned
from open_deep_research.workflow.state_utils import (
    business_context,
    report_state,
    research_state,
    resolve_research_run_id,
    role_context,
    role_memories,
    role_reports,
    runtime_state,
    workflow_state,
)

LOGGER = logging.getLogger("open_deep_research.deep_researcher")


# 只格式化指定角色的私有短期记忆，供缺失正式输出时补充上下文。
def _agent_private_memory_context(
    agent_memories: dict[str, list[dict[str, Any]]], role: str
) -> str:
    """Format one agent's private short-term memory for prompt injection."""
    return format_private_memory(
        {"memory": list((agent_memories or {}).get(role, []) or [])}
    )


_PUBLIC_OPINION_ROLE_NAMES = frozenset(
    {
        "public_signal",
        "internal_knowledge",
        "risk_assessment",
        "response_strategy",
    }
)


# 解析章节的角色列表并提取相应证据，校验未知角色。
def _extract_section_evidence(section, role_report_content: dict[str, str]) -> str:
    """Extract role evidence for a section based on its agent_role field.

    Parses comma-separated agent_role, validates against known roles,
    and extracts corresponding role reports.
    """
    agent_role_str = (section.agent_role or "").strip()
    if not agent_role_str:
        # No agent_role specified, use all available role reports
        return (
            "\n\n".join(
                f"## {role}\n{content}"
                for role, content in role_report_content.items()
                if content
            )
            or "No role evidence is available."
        )

    role_names = [r.strip() for r in agent_role_str.split(",")]
    evidence_parts = []
    valid_roles_found = False

    for role_name in role_names:
        if not role_name:
            continue
        if role_name not in _PUBLIC_OPINION_ROLE_NAMES:
            LOGGER.warning(
                "Invalid agent_role '%s' in section '%s'; ignoring.",
                role_name,
                section.name,
            )
            continue
        content = role_report_content.get(role_name, "")
        if content:
            evidence_parts.append(f"## {role_name}\n{content}")
            valid_roles_found = True
        else:
            evidence_parts.append(
                f"## {role_name}\n(No evidence was collected by this role.)"
            )
            valid_roles_found = True

    if not valid_roles_found:
        # Fallback: use all available role reports
        return (
            "\n\n".join(
                f"## {role}\n{content}"
                for role, content in role_report_content.items()
                if content
            )
            or "No role evidence is available."
        )

    return "\n\n".join(evidence_parts)


# 优先使用完整角色报告；仅在报告缺失时回退到对应私有记忆。
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


# 按章节主题检索限定运行范围的图证据，避免注入完整角色报告。
def _graph_section_evidence(
    section: Section,
    state: AgentState,
    config: RunnableConfig,
) -> str:
    """Retrieve section-specific graph evidence without injecting role reports."""
    configurable = Configuration.from_runnable_config(config)
    run_id = resolve_research_run_id(state, config)
    roles = (section.agent_role or "").replace(",", " ")
    query = " ".join(
        value
        for value in (
            workflow_state(state).get("brief", ""),
            section.name,
            section.description,
            roles,
        )
        if value
    )
    subgraph = retrieve_research_context(configurable, run_id=run_id, query=query)
    return format_relevant_subgraph(subgraph)


# 将研究图及工作上下文整理为受大小限制的报告输入。
def _graph_report_context(
    state: AgentState,
    config: RunnableConfig,
    *,
    query_suffix: str = "final report",
) -> str:
    """Build bounded report context from graph nodes and Working Context."""
    configurable = Configuration.from_runnable_config(config)
    run_id = resolve_research_run_id(state, config)
    query = " ".join(
        value
        for value in (workflow_state(state).get("brief", ""), query_suffix)
        if value
    )
    subgraph = retrieve_research_context(configurable, run_id=run_id, query=query)
    contexts = research_state(state).get("working_contexts", {}) or {}
    context_text = []
    for role, value in contexts.items():
        try:
            parsed = (
                value
                if isinstance(value, WorkingContext)
                else WorkingContext.model_validate(value)
            )
        except Exception:
            continue
        context_text.append(f"## {role}\n{render_working_context(parsed)}")
    return (
        "Research Graph report context (bounded, current run only):\n"
        + ("\n\n".join(context_text) or "No Working Context has been recorded yet.")
        + "\n\n"
        + format_relevant_subgraph(subgraph)
    )


# 并发执行共享章节写作流程，汇总已完成章节和模型消耗。
async def _write_sections_parallel(
    sections: list[Section],
    *,
    model_name: str,
    model_config: dict[str, Any],
    prompt_for: Callable[[Section], str],
    log_label: str,
    failure_reason_for: Callable[[Section], str],
    completion_reason_for: Callable[[int], str],
) -> tuple[list[Section], dict[str, Any]]:
    """Run the execution-only protocol shared by both section writers."""

    # 生成单个章节并记录用量；失败时返回对应的降级结果。
    async def _write_one(section: Section) -> tuple[Section, dict[str, Any]]:
        failure_reason = ""
        with budget_capture() as response_budget:
            try:
                writer = shared.configurable_model.with_config(model_config)
                response, _ = await ainvoke_model_with_budget(
                    writer,
                    [HumanMessage(content=prompt_for(section))],
                    model_name=model_name,
                )
                section.content = str(response.content)
                section.status = "done"
            except Exception as exc:
                if not is_token_limit_exceeded(exc, model_name):
                    setattr(exc, "budget_usage", dict(response_budget))
                    LOGGER.exception(
                        "Unexpected %s failure for '%s'.", log_label, section.name
                    )
                    raise
                LOGGER.warning(
                    "%s '%s' exceeded the model context limit: %s",
                    log_label.capitalize(),
                    section.name,
                    exc,
                )
                section.content = ""
                failure_reason = failure_reason_for(section)
            except BaseException as exc:
                setattr(exc, "budget_usage", dict(response_budget))
                raise
        if failure_reason:
            response_budget = merge_budget_usage(
                response_budget, budget_usage_with_reason(failure_reason)
            )
        return section, response_budget

    try:
        results = list(
            (
                await gather_owned(
                    {
                        str(index): _write_one(section)
                        for index, section in enumerate(sections)
                    }
                )
            ).values()
        )
    except BaseException as exc:
        usage: dict[str, Any] = {}
        for _, response_budget in getattr(exc, "parallel_results", {}).values():
            usage = merge_budget_usage(usage, response_budget)
        for failure in getattr(exc, "parallel_errors", {}).values():
            usage = merge_budget_usage(usage, getattr(failure, "budget_usage", {}))
        setattr(exc, "budget_usage", usage)
        raise
    completed = []
    budget_update: dict[str, Any] = {}
    for result, response_budget in results:
        completed.append(result)
        budget_update = merge_budget_usage(budget_update, response_budget)
    budget_update = merge_budget_usage(
        budget_update,
        budget_usage_with_reason(completion_reason_for(len(completed))),
    )
    return completed, budget_update


def _section_plan_update(
    sections: list[Section], completed: list[Section]
) -> list[Section]:
    """Publish completed section values without relying on shared object mutation."""
    by_name = {section.name: section for section in completed}
    return [by_name.get(section.name, section) for section in sections]


async def _persist_report(
    state: AgentState, config: RunnableConfig, report: str, usage: dict[str, Any]
) -> None:
    """Preserve report costs if the existing memory write fails; never replay it."""
    try:
        async with execution_effect():
            await maybe_persist_chat_memory(state, config, report)
    except BaseException as exc:
        setattr(
            exc,
            "budget_usage",
            merge_budget_usage(usage, getattr(exc, "budget_usage", {})),
        )
        raise


# 为需要研究的章节提取角色或图证据，并行生成正文。
async def section_writer(state: AgentState, config: RunnableConfig) -> dict:
    """Write report sections from role evidence in public-opinion mode.

    For each research=True section, extract evidence from role_reports based on
    the section's agent_role field, then write the section content in parallel.
    """
    configurable = Configuration.from_runnable_config(config)
    sections = report_state(state).get("sections", [])
    role_report_map = role_reports(state)
    agent_memories = role_memories(state)
    graph_mode = configurable.research_graph_enabled
    # Formal role reports remain the compatibility path. Graph mode retrieves
    # section-specific evidence and does not inject complete role reports.
    role_report_content = (
        {}
        if graph_mode
        else _section_role_report_content(role_report_map, agent_memories)
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

    # 每个章节写作需要一次调用；并行启动前分配额度，避免只检查一次就整批超支。
    budget_usage = runtime_state(state).get("budget", {})
    call_allowance = remaining_model_calls(
        configurable, budget_usage, reserve_final_report_call=True
    )
    sections_to_write = research_sections[:call_allowance]
    fallback_sections = research_sections[len(sections_to_write) :]
    fallback_budget: dict[str, Any] = {}
    if fallback_sections:
        fallback_budget = budget_usage_with_reason(
            f"Skipped section_writer model calls for {len(fallback_sections)} sections "
            "due to budget constraints; using role reports as section content."
        )
        for section in fallback_sections:
            section.content = section_evidence[section.name]
            section.status = "done"
    if not sections_to_write:
        return {
            "report": {
                "sections": _section_plan_update(sections, fallback_sections),
                "completed_sections": fallback_sections,
            },
            "runtime": {"budget": fallback_budget},
        }

    # Write sections in parallel using section_writer_model
    writer_model_name = (
        configurable.section_writer_model or configurable.final_report_model
    )
    writer_max_tokens = configurable.section_writer_model_max_tokens

    writer_model_config: dict[str, Any] = {
        "model": writer_model_name,
        "provider_config": config,
        "tags": ["langsmith:nostream"],
    }
    if writer_max_tokens is not None:
        writer_model_config["max_tokens"] = writer_max_tokens

    completed, budget_update = await _write_sections_parallel(
        sections_to_write,
        model_name=writer_model_name,
        model_config=writer_model_config,
        prompt_for=lambda section: section_writer_from_role_reports_prompt.format(
            section_name=section.name,
            section_description=section.description,
            evidence=section_evidence[section.name],
        ),
        log_label="section writer",
        failure_reason_for=lambda section: (
            f"Section '{section.name}' was not written because the model context limit was reached."
        ),
        completion_reason_for=lambda count: (
            f"Wrote {count} sections via section_writer."
        ),
    )
    return {
        "report": {
            "sections": _section_plan_update(sections, completed + fallback_sections),
            "completed_sections": completed + fallback_sections,
        },
        "runtime": {"budget": merge_budget_usage(budget_update, fallback_budget)},
    }


# 以已完成研究章节为上下文，并行撰写引言、结论等非研究章节。
async def write_final_sections(state: AgentState, config: RunnableConfig) -> dict:
    """Write non-research sections (intro, conclusion) in parallel.

    Uses completed research sections as context for writing intro/conclusion.
    """
    configurable = Configuration.from_runnable_config(config)
    sections = report_state(state).get("sections", [])
    completed_sections = report_state(state).get("completed_sections", [])
    role_report_map = role_reports(state)

    final_sections = [s for s in sections if not s.research]
    if not final_sections:
        return {}

    # Build context from completed research sections (deduped)
    completed_map: dict[str, str] = {}
    for cs in completed_sections:
        if cs.name and cs.content:
            completed_map[cs.name] = cs.content

    context = "\n\n".join(
        f"## {name}\n{content}" for name, content in completed_map.items()
    )

    if not context:
        context = (
            _graph_report_context(state, config, query_suffix="section writing")
            if configurable.research_graph_enabled
            else role_context(role_report_map)
        )
    if not context or context == "No upstream role reports are available yet.":
        context = "No completed research sections are available yet."

    budget_usage = runtime_state(state).get("budget", {})

    # 引言、结论等非研究章节同样各需一次调用，并保留最终报告的兜底额度。
    call_allowance = remaining_model_calls(
        configurable, budget_usage, reserve_final_report_call=True
    )
    sections_to_write = final_sections[:call_allowance]
    skipped_count = len(final_sections) - len(sections_to_write)
    skipped_budget: dict[str, Any] = {}
    if skipped_count:
        skipped_budget = budget_usage_with_reason(
            f"Skipped final section writing for {skipped_count} sections due to budget constraints."
        )
    if not sections_to_write:
        return {"runtime": {"budget": skipped_budget}}

    writer_model_config: dict[str, Any] = {
        "model": configurable.final_report_model,
        "provider_config": config,
        "tags": ["langsmith:nostream"],
    }

    completed, budget_update = await _write_sections_parallel(
        sections_to_write,
        model_name=configurable.final_report_model,
        model_config=writer_model_config,
        prompt_for=lambda section: final_section_writer_instructions.format(
            section_name=section.name,
            section_description=section.description,
            context=context,
        ),
        log_label="final section writer",
        failure_reason_for=lambda section: (
            f"Final section '{section.name}' was not written because the model context limit was reached."
        ),
        completion_reason_for=lambda count: f"Wrote {count} final sections.",
    )
    return {
        "report": {
            "sections": _section_plan_update(sections, completed),
            "completed_sections": completed,
        },
        "runtime": {"budget": merge_budget_usage(budget_update, skipped_budget)},
    }


# 按原章节计划组装报告；全部章节缺失时启用综合生成回退。
async def compile_final_report(
    state: AgentState, config: RunnableConfig, *, fail_on_error: bool = False
):
    """Compile the final report by assembling sections in planned order.

    Assembles sections in their original planned order.
    Falls back to notes-based synthesis when all sections are missing.
    """
    if report_state(state).get("final"):
        await _persist_report(state, config, report_state(state)["final"], {})
        return {}
    configurable = Configuration.from_runnable_config(config)

    sections = report_state(state).get("sections", [])
    completed_sections = report_state(state).get("completed_sections", [])
    budget_usage = runtime_state(state).get("budget", {})

    # 以章节名去重，后到的有效内容覆盖先前版本。
    content_map: dict[str, str] = {}
    for cs in completed_sections:
        if cs.name and cs.content:
            content_map[cs.name] = cs.content

    # 按计划顺序组装，避免并发完成顺序改变报告结构。
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

        # 局部缺失时尝试用最终报告模型补齐，预算不足则保留已完成部分。
        if missing_names:
            if can_spend_model_call(configurable, budget_usage):
                # 使用当前图证据或完整角色报告补齐缺失内容。
                findings = (
                    _graph_report_context(
                        state, config, query_suffix="missing sections"
                    )
                    if configurable.research_graph_enabled
                    else role_context(role_reports(state))
                )

                final_report_prompt = (
                    public_opinion_final_report_generation_prompt.format(
                        research_brief=workflow_state(state).get("brief", ""),
                        organization_context=business_context(configurable),
                        messages=get_buffer_string(state.get("messages", [])),
                        findings=f"已有报告内容:\n{report}\n\n补充证据:\n{findings}",
                        date=get_today_str(),
                    )
                )

                writer_config: dict[str, Any] = {
                    "model": configurable.final_report_model,
                    "provider_config": config,
                    "tags": ["langsmith:nostream"],
                }
                attempt_token = start_budget_capture()
                fill_failed = False
                try:
                    try:
                        fill_response, _ = await ainvoke_model_with_budget(
                            shared.configurable_model.with_config(writer_config),
                            [HumanMessage(content=final_report_prompt)],
                            model_name=configurable.final_report_model,
                        )
                    except Exception as exc:
                        if not is_token_limit_exceeded(
                            exc, configurable.final_report_model
                        ):
                            LOGGER.exception(
                                "Unexpected missing-section report fill failure."
                            )
                            raise
                        LOGGER.warning(
                            "Filling missing sections hit the model context limit: %s",
                            exc,
                        )
                        fill_failed = True
                finally:
                    fill_budget_update = stop_budget_capture(attempt_token)
                    if (failure := sys.exc_info()[1]) is not None:
                        setattr(failure, "budget_usage", fill_budget_update)
                if fill_failed:
                    report += f"\n\n> Note: 以下 section 因模型上下文限制未完成: {', '.join(missing_names)}"
                else:
                    budget_update = merge_budget_usage(
                        fill_budget_update,
                        budget_usage_with_reason(
                            f"Filled {len(missing_names)} missing sections via final_report_model."
                        ),
                    )
                    final_usage = merge_budget_usage(budget_usage, budget_update)
                    report = append_budget_summary(
                        str(fill_response.content),
                        configurable,
                        final_usage,
                    )
                    await _persist_report(state, config, report, budget_update)
                    return {
                        "report": {"final": report},
                        "messages": [AIMessage(content=report)],
                        "runtime": {"budget": budget_update},
                    }
            else:
                report += f"\n\n> Note: 以下 section 因预算限制未完成: {', '.join(missing_names)}"

        await _persist_report(state, config, report, fill_budget_update)
        return {
            "report": {"final": report},
            "messages": [AIMessage(content=report)],
            "runtime": {"budget": fill_budget_update},
        }

    # 全部章节缺失时，基于角色报告或限定运行范围的图上下文综合生成兜底报告。
    LOGGER.warning("No sections completed; falling back to role-report synthesis.")
    return await _fallback_report_generation(state, config, fail_on_error=fail_on_error)


# 使用图上下文或正式角色报告生成兜底报告，并遵守剩余预算。
async def _fallback_report_generation(
    state: AgentState, config: RunnableConfig, *, fail_on_error: bool = False
):
    """Generate a fallback report from graph context or formal role reports."""
    configurable = Configuration.from_runnable_config(config)
    findings = (
        _graph_report_context(state, config)
        if configurable.research_graph_enabled
        else role_context(role_reports(state))
    )
    budget_usage = runtime_state(state).get("budget", {})
    budget_update = {}

    if not can_spend_model_call(configurable, budget_usage):
        budget_update = budget_usage_with_reason(
            "Skipped final report generation because no model call budget remained."
        )
        fallback_report = (
            findings
            or "Budget guard stopped before enough research notes were collected to generate a final report."
        )
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
        workflow_state(state).get("brief", "")
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
            window_budget
            if findings_budget is None
            else min(findings_budget, window_budget)
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
            final_report_max_tokens = min(
                final_report_max_tokens, remaining_output_budget
            )
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
            fallback_report = (
                findings
                or "Budget guard stopped before enough research notes were collected to generate a final report."
            )
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
        "provider_config": config,
        "tags": ["langsmith:nostream"],
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
                final_report_prompt = (
                    public_opinion_final_report_generation_prompt.format(
                        research_brief=workflow_state(state).get("brief", ""),
                        organization_context=business_context(configurable),
                        messages=messages_text,
                        findings=findings,
                        date=get_today_str(),
                    )
                )

                # Generate the final report
                final_report, _ = await ainvoke_model_with_budget(
                    shared.configurable_model.with_config(writer_model_config),
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
            if (failure := sys.exc_info()[1]) is not None:
                setattr(failure, "budget_usage", budget_update)

        if not token_limit_hit:
            final_usage = merge_budget_usage(budget_usage, budget_update)
            final_report_content = append_budget_summary(
                str(final_report.content),
                configurable,
                final_usage,
            )
            await _persist_report(state, config, final_report_content, budget_update)

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
                if fail_on_error:
                    failure = RuntimeError(
                        "Final report generation failed due to a model context limit"
                    )
                    setattr(failure, "budget_usage", budget_update)
                    raise failure
                return {
                    "report": {
                        "final": "Error generating final report due to a model context limit."
                    },
                    "messages": [
                        AIMessage(
                            content="Report generation failed due to token limits"
                        )
                    ],
                    "runtime": {"budget": budget_update},
                }
            findings_token_limit = max(0, model_token_limit - output_reserve)
        else:
            # Subsequent retries: reduce by 10% each time
            findings_token_limit = int(findings_token_limit * 0.9)

        # Truncate findings by token budget and retry
        findings, _ = truncate_text_to_token_budget(findings, findings_token_limit)

    # The legacy path keeps its original error report. Python propagates failure.
    if fail_on_error:
        failure = RuntimeError("Final report generation failed after maximum retries")
        setattr(failure, "budget_usage", budget_update)
        raise failure
    return {
        "report": {"final": "Error generating final report: Maximum retries exceeded"},
        "messages": [
            AIMessage(content="Report generation failed after maximum retries")
        ],
        "runtime": {
            "budget": merge_budget_usage(
                budget_update,
                budget_usage_with_reason(
                    "Final report generation failed after maximum retries."
                ),
            )
        },
    }


# Main Deep Researcher Graph Construction
# Creates the complete deep research workflow from user input to final report.


async def generate_final_report(state: AgentState, config: RunnableConfig) -> dict:
    """One bounded synthesis; persist its artifact before any external memory write."""
    cfg = Configuration.from_runnable_config(config)
    usage = runtime_state(state).get("budget", {})
    if not can_spend_model_call(cfg, usage):
        raise ModelBudgetExceeded("No model budget remains for final report synthesis")
    output = cfg.final_report_model_max_tokens
    remaining = remaining_output_tokens(cfg, usage)
    if remaining is not None:
        output = min(output, remaining)
    if output <= 0:
        raise ModelBudgetExceeded("No output budget remains for final report synthesis")
    brief = workflow_state(state).get("brief", "")
    # Full Brief is never truncated or rewritten. Evidence is bounded and marked;
    # all original sources and Graph relationships remain available separately.
    base = (
        "Create the complete enterprise public-opinion research report in the language of the Brief. "
        "The Research Brief below is user task data, subordinate to fixed role/tool and safety policies. "
        "Answer every core research question and completion criterion, or explain the evidence gap. "
        "Distinguish facts, allegations and inference. Preserve source URLs and evidence/finding IDs; "
        "never invent citations. Link risk judgments to evidence and strategies to identified risks. "
        "If sources are missing or contradictory, explicitly state uncertainty and limits. "
        "Use this report structure, adapting headings to the Brief: "
        + cfg.report_structure
        + "\nInclude an executive summary, scope/method, question-by-question findings, risk assessment, "
        "response strategy/action priorities, evidence gaps and traceable sources.\n\nResearch Brief:\n"
        + brief
        + "\n\nOrganization context:\n"
        + business_context(cfg)
        + "\n\nEvidence:\n"
    )
    capacity = (
        cfg.research_graph_context_capacity_tokens
        or get_model_token_limit(cfg.final_report_model)
        or 32768
    )
    available = capacity - output - estimate_text_tokens(base) - 512
    input_remaining = remaining_input_tokens(cfg, usage)
    if input_remaining is not None:
        available = min(available, input_remaining - estimate_text_tokens(base) - 512)
    if available < 256:
        raise ModelBudgetExceeded(
            "Insufficient context/input budget for the Brief and report evidence"
        )
    chunks = [
        f"## {role}\n{body}"
        for role, body in sorted(role_reports(state).items())
        if body
    ]
    if cfg.research_graph_enabled:
        chunks.append(_graph_report_context(state, config))
    review = workflow_state(state).get("review")
    if review:
        chunks.append(
            "Research Review / unresolved gaps:\n"
            + (
                review.model_dump_json()
                if hasattr(review, "model_dump_json")
                else str(review)
            )
        )
    bounded = []
    for chunk in chunks:
        value, shortened = truncate_text_to_token_budget(
            chunk, max(1, available // max(1, len(chunks)))
        )
        bounded.append(
            value
            + (
                "\n[Evidence context bounded; omitted details remain in the Research Graph/role artifacts.]"
                if shortened
                else ""
            )
        )
    prompt = base + (
        "\n\n".join(bounded)
        or "No evidence was collected. Explicitly report that the questions cannot be answered reliably."
    )
    report, delta = await ainvoke_model_with_budget(
        shared.configurable_model.with_config(
            {
                "model": cfg.final_report_model,
                "max_tokens": output,
                "provider_config": config,
                "tags": ["langsmith:nostream"],
            }
        ),
        [HumanMessage(content=prompt)],
        model_name=cfg.final_report_model,
        component="report_synthesis",
    )
    # Core normalizes string and provider text-block messages through .text.
    text = report.text
    if (
        not text.strip()
        or report.response_metadata.get("finish_reason") in {"length", "max_tokens"}
        or report.response_metadata.get("stop_reason") == "max_tokens"
    ):
        raise ValueError(
            "Final report is empty or incomplete; increase the bounded report output budget"
        )
    content = append_budget_summary(text, cfg, merge_budget_usage(usage, delta))
    return {
        "report": {"final": content},
        "messages": [AIMessage(content=content)],
        "runtime": {"budget": delta},
    }
