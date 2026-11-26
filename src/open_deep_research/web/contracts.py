"""Document the public Run and execution read models consumed by the Web UI."""

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field, JsonValue

from open_deep_research.web.images import ImageInfo
from open_deep_research.workflow.brief import PlanQuestion

TerminalStatus = Literal["completed", "failed", "cancelled", "interrupted"]
RunStatus = Literal[
    "queued",
    "running",
    "awaiting_approval",
    "awaiting_clarification",
    "cancelling",
    TerminalStatus,
]
Item = TypeVar("Item")


class OffsetPage(BaseModel, Generic[Item]):
    items: list[Item]
    next_offset: int
    has_more: bool


class HistoryItem(BaseModel):
    run_id: str
    research_session_id: str
    topic: str
    rag_enabled: bool | None
    status: TerminalStatus
    started_at: str | None
    completed_at: str | None


class SessionItem(BaseModel):
    research_session_id: str
    latest_run_id: str
    topic: str
    run_count: int
    updated_at: str


class SessionRun(BaseModel):
    run_id: str
    topic: str
    rag_enabled: bool | None
    native_status: str | None
    created_at: str


class SessionDetail(OffsetPage[SessionRun]):
    research_session_id: str
    run_count: int
    can_continue: bool


class RequestSummary(BaseModel):
    topic: str
    org_context: str | None = None
    model: str | None = None
    search_api: str | None = None
    mode: str | None = None
    rag_enabled: bool | None = None
    require_approval: bool | None = None
    allow_clarification: bool | None = None


class BudgetLimits(BaseModel):
    budget_enabled: bool | None = None
    max_model_calls: int | None = None
    max_tool_calls: int | None = None
    max_search_calls: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None


class Usage(BaseModel):
    model_calls: int | None = None
    tool_calls: int | None = None
    search_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    usage_complete: bool | None = None
    input_token_details: dict[str, int] | None = None
    output_token_details: dict[str, int] | None = None
    budget_warnings: list[str] | None = None
    degradation_reasons: list[str] | None = None


class RoleReport(BaseModel):
    role: str
    content: str
    stage_id: str | None = None
    completed_at: str | None = None


class Clarification(BaseModel):
    version: str
    expires_at: str
    round: int
    max_rounds: int
    questions: list[PlanQuestion]


class BriefRevisionStatus(BaseModel):
    used: int
    limit: int
    pending: bool
    error: str | None


class Brief(BaseModel):
    version: str
    revision: int
    content_hash: str
    brief: str
    locked: bool
    expires_at: str
    sections: list[dict[str, JsonValue]]
    ai_revision: BriefRevisionStatus | None


class RunSnapshot(BaseModel):
    run_id: str
    attempt_id: str
    research_session_id: str
    status: RunStatus
    native_status: str
    current_step: str
    completed_steps: list[str]
    created_at: str
    updated_at: str
    terminated_at: str | None
    event_cursor: int
    state_version: int
    generation: int
    settlement: Literal["settled", "pending", "unresolved"]
    events_complete: bool
    events_paused: bool
    retry_safe: bool
    retry_reason: str | None
    retry_requested: bool
    cancel_requested: bool
    can_cancel: bool
    can_clarify: bool
    can_approve: bool
    approval_required: bool
    enabled_roles: list[str]
    budget_limits: BudgetLimits
    usage: Usage
    error: dict[str, JsonValue]
    report: str
    role_reports: list[RoleReport]
    plan: Brief | None
    clarification: Clarification | None
    request: RequestSummary
    images: list[ImageInfo]
    image_context: str | None
    phase: str | None
    execution_engine: str
    workflow_backend: str
    workflow_version: str
    business_agent_runtime: str
    sdk_version: str
    runtime_version: str


class EventEnvelope(BaseModel):
    protocol_version: Literal[1]
    run_id: str
    seq: int
    timestamp: str
    type: str
    generation: int | None = None


class EventPage(BaseModel):
    # Events are additive. The typed envelope is shared by REST and SSE.
    items: list[EventEnvelope]
    next_cursor: int
    has_more: bool


class NativeWorkflow(BaseModel):
    workflow_id: str
    name: str
    status: str
    queue_name: str | None
    app_version: str | None
    parent_workflow_id: str | None
    created_at: int | None
    updated_at: int | None
    completed_at: int | None
    dequeued_at: int | None


class NativeStep(BaseModel):
    function_id: int
    function_name: str
    child_workflow_id: str | None
    started_at_epoch_ms: int | None
    completed_at_epoch_ms: int | None


class ExecutionCall(BaseModel):
    kind: Literal["model", "tool"]
    id: str
    stage_id: str | None
    name: str | None
    search: bool | None
    status: str | None
    usage: Usage | None


class ExecutionPage(BaseModel):
    run_id: str
    workflow_id: str
    workflows: list[NativeWorkflow]
    steps: list[NativeStep]
    workflows_has_more: bool
    next_workflow_offset: int
    steps_has_more: bool
    next_offset: int
    calls: list[ExecutionCall] = Field(default_factory=list)
    calls_available: bool
    calls_has_more: bool
    next_call_offset: int
    note: str
