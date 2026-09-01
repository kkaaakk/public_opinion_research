"""Graph state definitions and data structures for the Deep Research agent."""

import operator
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Optional

from langchain_core.messages import MessageLikeRepresentation
from langgraph.graph import MessagesState
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from open_deep_research.budget import empty_budget_usage, merge_budget_usage

###################
# Structured Outputs
###################
BusinessAgentRole = Literal[
    "public_signal",
    "internal_knowledge",
    "risk_assessment",
    "response_strategy",
]



class ResearchComplete(BaseModel):
    """Call this tool to indicate that the research is complete."""

class Summary(BaseModel):
    """Research summary with key findings."""
    
    summary: str
    key_excerpts: str

class ClarifyWithUser(BaseModel):
    """Model for user clarification requests."""
    
    need_clarification: bool = Field(
        description="Whether the user needs to be asked a clarifying question.",
    )
    question: str = Field(
        description="A question to ask the user to clarify the report scope",
    )
    verification: str = Field(
        description="Verify message that we will start research after the user has provided the necessary information.",
    )

class ResearchQuestion(BaseModel):
    """Research question and brief for guiding research."""

    research_brief: str = Field(
        description="A research question that will be used to guide the research.",
    )


class ResearchTask(BaseModel):
    """One evidence-gap task assigned to a public-opinion research role."""

    task_id: str = Field(
        description="Stable identifier for this research task within the current run.",
    )
    objective: str = Field(
        description="The specific unresolved question or claim to investigate.",
    )
    target_role: Literal["public_signal", "internal_knowledge"] = Field(
        description="The public-opinion research role that should execute this task.",
    )
    evidence_needed: str = Field(
        description="The evidence required to resolve the task.",
    )
    reason: str = Field(
        description="Why resolving this task can affect the risk assessment.",
    )
    priority: Literal["high", "medium", "low"] = Field(
        default="medium",
        description="Relative priority of this research task.",
    )


class ResearchReview(BaseModel):
    """Structured review of public-opinion evidence and remaining research gaps."""

    research_complete: bool = Field(
        description=(
            "Whether the available evidence is sufficient to proceed to risk assessment. "
            "Do not keep researching gaps that would not materially change the risk judgment."
        ),
    )
    confirmed_findings: list[str] = Field(
        default_factory=list,
        description="Findings supported well enough for downstream risk assessment.",
    )
    unresolved_claims: list[str] = Field(
        default_factory=list,
        description="Important claims that remain unverified or weakly supported.",
    )
    conflicts: list[str] = Field(
        default_factory=list,
        description="Material conflicts between public signals and internal knowledge.",
    )
    research_gaps: list[str] = Field(
        default_factory=list,
        description="Evidence gaps that could materially change the risk judgment.",
    )
    next_tasks: list[ResearchTask] = Field(
        default_factory=list,
        description="Executable follow-up tasks, grouped later by target_role.",
    )


class SearchQuery(BaseModel):
    """A single web search query."""

    search_query: str = Field(
        description="Query for web search.",
    )


class Section(BaseModel):
    """A section of a structured research report.

    Used by the Plan-and-Execute workflow to represent one planned section.
    In public-opinion mode, ``agent_role`` maps the section to the public-opinion
    sub-agent roles whose evidence should be used when writing the section.
    """

    name: str = Field(
        description="Name for this section of the report.",
    )
    description: str = Field(
        description="Brief overview of the main topics and concepts to be covered in this section.",
    )
    research: bool = Field(
        default=True,
        description="Whether to perform research (e.g. role-based evidence) for this section.",
    )
    content: str = Field(
        default="",
        description="The content of the section. Empty during planning, filled after writing.",
    )
    agent_role: str = Field(
        default="",
        description=(
            "Comma-separated public-opinion agent roles this section depends on. "
            "Values are chosen from: public_signal, internal_knowledge, "
            "risk_assessment, response_strategy."
        ),
    )
    status: Literal["pending", "done"] = Field(
        default="pending",
        description="Completion status of the section.",
    )


class Sections(BaseModel):
    """Container for a list of report sections, used for structured output."""

    sections: list[Section] = Field(
        description="Sections of the report.",
    )


class Feedback(BaseModel):
    """Reflection feedback on a research section.

    Kept for future use; public-opinion mode does not strictly require it but
    the prompts may reference it.
    """

    grade: Literal["pass", "fail"] = Field(
        description="Evaluation result indicating whether the response meets requirements ('pass') or needs revision ('fail').",
    )
    follow_up_queries: list[SearchQuery] = Field(
        description="List of follow-up search queries.",
    )


###################
# State Definitions
###################

def override_reducer(current_value, new_value):
    """Reducer function that allows overriding values in state."""
    if isinstance(new_value, dict) and new_value.get("type") == "override":
        return new_value.get("value", new_value)
    else:
        return operator.add(current_value, new_value)

def budget_usage_reducer(current_value: Any, new_value: Any):
    """Reducer that accumulates budget counters across graph nodes."""
    if isinstance(new_value, dict) and new_value.get("type") == "override":
        return new_value.get("value", empty_budget_usage())
    return merge_budget_usage(current_value, new_value)


def research_round_reducer(current_value: Any, new_value: Any) -> int:
    """Keep the greatest completed/current round across parallel follow-ups."""
    if isinstance(new_value, dict) and new_value.get("type") == "override":
        new_value = new_value.get("value", 1)
    try:
        current_round = int(current_value or 1)
    except (TypeError, ValueError):
        current_round = 1
    try:
        incoming_round = int(new_value or 1)
    except (TypeError, ValueError):
        incoming_round = 1
    return max(1, current_round, incoming_round)


def _coerce_research_task(value: Any) -> ResearchTask | None:
    """Convert a task-like value to the canonical structured task model."""
    if isinstance(value, ResearchTask):
        return value
    if isinstance(value, Mapping):
        try:
            return ResearchTask.model_validate(value)
        except Exception:
            return None
    return None


def _research_task_values(value: Any) -> list[Any]:
    """Return task-like values from a state update without assuming its encoding."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def research_tasks_reducer(current_value: Any, new_value: Any) -> list[ResearchTask]:
    """Accumulate research tasks while de-duplicating stable task identifiers."""
    if isinstance(new_value, dict) and new_value.get("type") == "override":
        values = _research_task_values(new_value.get("value", []))
    else:
        values = _research_task_values(current_value) + _research_task_values(new_value)

    merged: list[ResearchTask] = []
    seen: set[str] = set()
    for value in values:
        task = _coerce_research_task(value)
        if task is None:
            continue
        identity = task.task_id.strip() or (
            f"{task.target_role}:{task.objective.strip()}:{task.evidence_needed.strip()}"
        )
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(task)
    return merged


def role_reports_reducer(current_value: Any, new_value: Any) -> dict[str, str]:
    """Reducer that preserves every public-opinion report produced for each role."""
    if isinstance(new_value, dict) and new_value.get("type") == "override":
        replacement = new_value.get("value", {})
        return dict(replacement) if isinstance(replacement, Mapping) else {}
    current_reports = current_value if isinstance(current_value, Mapping) else {}
    new_reports = new_value if isinstance(new_value, Mapping) else {}
    merged = dict(current_reports)
    for role, report in new_reports.items():
        normalized_role = str(role)
        normalized_report = str(report or "")
        previous_report = str(merged.get(normalized_role) or "")
        if not previous_report:
            merged[normalized_role] = normalized_report
        elif normalized_report and normalized_report != previous_report:
            merged[normalized_role] = (
                f"{previous_report}\n\n"
                f"--- Additional {normalized_role} research report ---\n"
                f"{normalized_report}"
            )
    return merged

def agent_memories_reducer(current_value: Any, new_value: Any):
    """Reducer that keeps short-term private memories separated by agent role."""
    if isinstance(new_value, dict) and new_value.get("type") == "override":
        return new_value.get("value", {})
    merged = {
        str(role): list(entries or [])
        for role, entries in dict(current_value or {}).items()
    }
    for role, entries in dict(new_value or {}).items():
        normalized_role = str(role)
        if isinstance(entries, list):
            new_entries = entries
        else:
            new_entries = [entries]
        merged.setdefault(normalized_role, [])
        merged[normalized_role].extend(new_entries)
    return merged
    
class AgentInputState(MessagesState):
    """InputState is only 'messages'."""

class AgentState(MessagesState):
    """Main agent state containing messages and research data."""

    research_brief: Optional[str]
    # Complete role outputs for the current public-opinion run.
    role_reports: Annotated[dict[str, str], role_reports_reducer]
    # Compact private context; this channel may be truncated by design.
    agent_memories: Annotated[dict[str, list[dict[str, Any]]], agent_memories_reducer]
    raw_notes: Annotated[list[str], override_reducer] = []
    notes: Annotated[list[str], override_reducer] = []
    budget_usage: Annotated[dict[str, Any], budget_usage_reducer]
    final_report: str
    # Plan-and-Execute fields (used by public-opinion mode)
    sections: list[Section] = []
    completed_sections: Annotated[list[Section], operator.add] = []
    feedback_on_report_plan: Annotated[list[str], operator.add] = []



class PublicOpinionState(TypedDict):
    """State for the explicit public-opinion multi-agent workflow."""

    messages: list[MessageLikeRepresentation]
    research_brief: str
    # Complete role outputs are a formal subgraph input/output channel.
    role_reports: Annotated[dict[str, str], role_reports_reducer]
    # Private per-agent memories remain compact and reducer-managed.
    agent_memories: Annotated[dict[str, list[dict[str, Any]]], agent_memories_reducer]
    notes: Annotated[list[str], override_reducer] = []
    raw_notes: Annotated[list[str], override_reducer] = []
    budget_usage: Annotated[dict[str, Any], budget_usage_reducer]
    research_round: Annotated[int, research_round_reducer]
    research_mode: Literal["initial", "followup"]
    research_review: ResearchReview | None
    current_research_tasks: Annotated[list[ResearchTask], research_tasks_reducer]
    completed_research_tasks: Annotated[list[ResearchTask], research_tasks_reducer]
