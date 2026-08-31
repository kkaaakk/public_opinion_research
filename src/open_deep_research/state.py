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

def role_reports_reducer(current_value: Any, new_value: Any) -> dict[str, str]:
    """Reducer that merges public-opinion role reports by role name."""
    if isinstance(new_value, dict) and new_value.get("type") == "override":
        replacement = new_value.get("value", {})
        return dict(replacement) if isinstance(replacement, Mapping) else {}
    current_reports = current_value if isinstance(current_value, Mapping) else {}
    new_reports = new_value if isinstance(new_value, Mapping) else {}
    merged = dict(current_reports)
    merged.update(new_reports)
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

    supervisor_messages: Annotated[list[MessageLikeRepresentation], override_reducer]
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
