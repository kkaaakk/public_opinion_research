"""Reporting stage of the public-opinion workflow."""

import asyncio
import logging
from typing import Any, Callable

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    get_buffer_string,
)
from langchain_core.runnables import RunnableConfig

from open_deep_research.budget import (
    ainvoke_model_with_budget,
    append_budget_summary,
    budget_capture,
    budget_usage_with_reason,
    can_spend_model_call,
    estimate_text_tokens,
    merge_budget_usage,
    remaining_input_tokens,
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
from open_deep_research.models import get_api_key_for_model
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

def _agent_private_memory_context(agent_memories: dict[str, list[dict[str, Any]]], role: str) -> str:
    """Format one agent's private short-term memory for prompt injection."""
    return format_private_memory(
        {"memory": list((agent_memories or {}).get(role, []) or [])}
    )


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
        if failure_reason:
            response_budget = merge_budget_usage(
                response_budget, budget_usage_with_reason(failure_reason)
            )
        return section, response_budget

    results = await asyncio.gather(*[_write_one(section) for section in sections])
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

    # Budget guard: if no budget, copy role reports directly as content
    budget_usage = runtime_state(state).get("budget", {})
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

    completed, budget_update = await _write_sections_parallel(
        research_sections,
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
        "report": {"completed_sections": completed},
        "runtime": {"budget": budget_update},
    }


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
        f"## {name}\n{content}"
        for name, content in completed_map.items()
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

    completed, budget_update = await _write_sections_parallel(
        final_sections,
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
        "report": {"completed_sections": completed},
        "runtime": {"budget": budget_update},
    }


async def compile_final_report(state: AgentState, config: RunnableConfig):
    """Compile the final report by assembling sections in planned order.

    Assembles sections in their original planned order.
    Falls back to notes-based synthesis when all sections are missing.
    """
    configurable = Configuration.from_runnable_config(config)

    sections = report_state(state).get("sections", [])
    completed_sections = report_state(state).get("completed_sections", [])
    budget_usage = runtime_state(state).get("budget", {})

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
                    else role_context(role_reports(state))
                )

                final_report_prompt = public_opinion_final_report_generation_prompt.format(
                    research_brief=workflow_state(state).get("brief", ""),
                    organization_context=business_context(configurable),
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
                            shared.configurable_model.with_config(writer_config),
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


async def _fallback_report_generation(state: AgentState, config: RunnableConfig):
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
                    research_brief=workflow_state(state).get("brief", ""),
                    organization_context=business_context(configurable),
                    messages=messages_text,
                    findings=findings,
                    date=get_today_str(),
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
