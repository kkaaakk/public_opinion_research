"""Reporting stage of the public-opinion workflow."""

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from open_deep_research.budget import (
    ModelBudgetExceeded,
    append_budget_summary,
    can_spend_model_call,
    estimate_text_tokens,
    merge_budget_usage,
    remaining_input_tokens,
    remaining_output_tokens,
    truncate_text_to_token_budget,
)
from open_deep_research.configuration import Configuration
from open_deep_research.llm.context import resolve_context_capacity
from open_deep_research.llm.generation import generate_text
from open_deep_research.prompts import public_opinion_final_report_generation_prompt
from open_deep_research.state import DeepResearchState
from open_deep_research.workflow.state_utils import (
    business_context,
    role_reports,
    runtime_state,
    workflow_state,
)


async def generate_final_report(state: DeepResearchState, config: RunnableConfig) -> dict:
    """One bounded synthesis from the Brief, role reports and research review."""
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
    workflow = workflow_state(state)
    # Include the full Brief. Mark omissions in evidence and retain the role reports.
    base = public_opinion_final_report_generation_prompt.format(
        brief=workflow.get("brief", ""),
        organization_context=business_context(cfg),
        report_structure=cfg.report_structure,
    )
    capacity = resolve_context_capacity(
        cfg.final_report_model, cfg.context_capacity_tokens
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
    review = workflow.get("review")
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
                "\n[Evidence context bounded; omitted details remain in the role reports.]"
                if shortened
                else ""
            )
        )
    prompt = base + (
        "\n\n".join(bounded)
        or "No evidence was collected. Explicitly report that the questions cannot be answered reliably."
    )
    text, delta = await generate_text(
        cfg.final_report_model,
        prompt,
        config=config,
        max_tokens=output,
        component="report_synthesis",
    )
    content = append_budget_summary(text, cfg, merge_budget_usage(usage, delta))
    return {
        "report": {"final": content},
        "messages": [AIMessage(content=content)],
        "runtime": {"budget": delta},
    }
