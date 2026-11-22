# 按数据归属定义研究状态，由显式 reducer 合并并行角色增量。
"""Serializable research state grouped by data ownership."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from typing_extensions import TypedDict

from open_deep_research.budget import empty_budget_usage, merge_budget_usage

BusinessAgentRole = Literal[
    "public_signal", "internal_knowledge", "risk_assessment", "response_strategy"
]


# 保存研究摘要与可用于引用的代表性摘录。
class Summary(BaseModel):
    """Research summary with representative excerpts."""

    summary: str
    key_excerpts: str


# 定义是否澄清、澄清问题及研究确认信息的结构化输出。
class ClarifyWithUser(BaseModel):
    """Structured clarification decision."""

    need_clarification: bool = Field(description="Whether clarification is required.")
    question: str = Field(description="Question used to clarify the report scope.")
    verification: str = Field(description="Confirmation shown before research starts.")


# 承载生成后的正式研究简报。
class ResearchQuestion(BaseModel):
    """Structured research brief output."""

    research_brief: str = Field(description="Research question used to guide research.")


# 描述指派给公众信号或内部知识角色的证据缺口任务。
class ResearchTask(BaseModel):
    """One evidence-gap task assigned to a producer role."""

    task_id: str = Field(description="Stable task identifier within the run.")
    objective: str
    target_role: Literal["public_signal", "internal_knowledge"]
    evidence_needed: str
    reason: str
    priority: Literal["high", "medium", "low"] = "medium"


# 记录证据充分性、已确认发现、冲突、缺口与后续任务。
class ResearchReview(BaseModel):
    """Evidence sufficiency review and optional follow-up tasks."""

    research_complete: bool
    confirmed_findings: list[str] = Field(default_factory=list)
    unresolved_claims: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    research_gaps: list[str] = Field(default_factory=list)
    next_tasks: list[ResearchTask] = Field(default_factory=list)


class ResearchRoleResult(BaseModel):
    """One producer invocation, assembled by Python; never an LLM output schema."""

    model_config = ConfigDict(
        extra="forbid", strict=True, revalidate_instances="always", allow_inf_nan=False
    )

    role: Literal["public_signal", "internal_knowledge"]
    round: int = Field(ge=1)
    report: str
    replace_report: bool = False
    # 有效证据由本次调用的成功工具结果与未降级的报告构成，Review 仍判断缺口是否补齐。
    evidence_collected: bool = False
    budget_delta: dict[str, JsonValue] = Field(default_factory=dict)


# 保存当前研究简报、轮次、复核结果及任务进度。
class WorkflowState(TypedDict, total=False):
    """Where the public-opinion workflow is in the current run."""

    brief: str
    brief_version: str
    brief_hash: str
    brief_revision: int
    round: int
    review: ResearchReview | None
    pending_tasks: list[ResearchTask]
    completed_tasks: list[ResearchTask]


# 保存单个角色独占的正式报告。
class AgentRoleState(TypedDict, total=False):
    """State privately owned by one business agent role."""

    report: str


class ReportState(TypedDict, total=False):
    """Final Markdown report."""

    final: str


# 保存预算及观测指标，独立于业务路由状态。
class RuntimeState(TypedDict, total=False):
    """Non-business execution accounting and observability data."""

    budget: dict[str, Any]


# 判断输入是否为显式替换状态的 override 指令。
def _is_override(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("type") == "override"


# 解包显式替换值；普通增量原样返回。
def _override_value(value: Any, default: Any) -> Any:
    return value.get("value", default) if _is_override(value) else value


# 将模型或字典转为 ResearchTask，跳过无法校验的输入。
def _coerce_research_task(value: Any) -> ResearchTask | None:
    if isinstance(value, ResearchTask):
        return value
    if isinstance(value, Mapping):
        try:
            return ResearchTask.model_validate(value)
        except Exception:
            return None
    return None


# 优先使用 task_id，缺失时用角色、目标和证据要求生成去重键。
def research_task_identity(task: ResearchTask) -> str:
    """Return the stable identity used to de-duplicate tasks across rounds."""
    return task.task_id.strip() or (
        f"{task.target_role}:{task.objective.strip()}:{task.evidence_needed.strip()}"
    )


# 把单个、序列或替换指令中的任务统一为列表。
def _task_values(value: Any) -> list[Any]:
    value = _override_value(value, [])
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


# 合并并去重任务；收到 override 指令时允许清空或整体替换。
def research_tasks_reducer(current_value: Any, new_value: Any) -> list[ResearchTask]:
    """Merge and de-duplicate task values; an override token clears/replaces."""
    values = (
        _task_values(new_value)
        if _is_override(new_value)
        else (_task_values(current_value) + _task_values(new_value))
    )
    merged: list[ResearchTask] = []
    seen: set[str] = set()
    for value in values:
        task = _coerce_research_task(value)
        if task is None:
            continue
        identity = research_task_identity(task)
        if identity not in seen:
            seen.add(identity)
            merged.append(task)
    return merged


# 容错解析轮次并取不小于 1 的最大值，避免并发更新回退进度。
def _merge_round(current: Any, incoming: Any) -> int:
    incoming = _override_value(incoming, 1)
    try:
        left = int(current or 1)
    except (TypeError, ValueError):
        left = 1
    try:
        right = int(incoming or 1)
    except (TypeError, ValueError):
        right = 1
    return max(1, left, right)


# 按字段合并流程状态，任务列表去重、轮次单调递增。
def workflow_reducer(current_value: Any, new_value: Any) -> dict[str, Any]:
    """Merge workflow progress safely across parallel producer updates."""
    if _is_override(new_value):
        replacement = _override_value(new_value, {})
        return dict(replacement) if isinstance(replacement, Mapping) else {}
    current = dict(current_value) if isinstance(current_value, Mapping) else {}
    incoming = dict(new_value) if isinstance(new_value, Mapping) else {}
    merged: dict[str, Any] = dict(current)
    for key in (
        "brief",
        "review",
        "brief_version",
        "brief_hash",
        "brief_revision",
    ):
        if key in incoming:
            merged[key] = incoming[key]
    if "round" in incoming:
        merged["round"] = _merge_round(current.get("round"), incoming["round"])
    if "pending_tasks" in incoming:
        merged["pending_tasks"] = research_tasks_reducer(
            current.get("pending_tasks", []), incoming["pending_tasks"]
        )
    if "completed_tasks" in incoming:
        merged["completed_tasks"] = research_tasks_reducer(
            current.get("completed_tasks", []), incoming["completed_tasks"]
        )
    return merged


# 合并一个角色的报告，遵循替换与追加两种更新规则。
def _merge_role_state(current: Any, incoming: Any) -> dict[str, Any]:
    left = dict(current) if isinstance(current, Mapping) else {}
    right = dict(incoming) if isinstance(incoming, Mapping) else {}
    merged: dict[str, Any] = dict(left)
    if "report" in right:
        report_update = right["report"]
        if _is_override(report_update):
            merged["report"] = str(_override_value(report_update, "") or "")
        else:
            previous = str(left.get("report") or "")
            report = str(report_update or "")
            merged["report"] = (
                f"{previous}\n\n--- Additional role research report ---\n{report}"
                if previous and report and report != previous
                else report or previous
            )
    return merged


# 按角色分别合并输出，避免并发角色相互覆盖。
def agents_reducer(current_value: Any, new_value: Any) -> dict[str, Any]:
    """Merge per-role outputs without parallel-role overwrite."""
    if _is_override(new_value):
        replacement = _override_value(new_value, {})
        return dict(replacement) if isinstance(replacement, Mapping) else {}
    merged = dict(current_value) if isinstance(current_value, Mapping) else {}
    if isinstance(new_value, Mapping):
        for role, update in new_value.items():
            if role != "type":
                merged[str(role)] = _merge_role_state(merged.get(str(role)), update)
    return merged


def report_reducer(current_value: Any, new_value: Any) -> dict[str, Any]:
    """Store the final report and preserve it across empty patches."""
    if _is_override(new_value):
        new_value = _override_value(new_value, {})
        current_value = {}
    if isinstance(new_value, Mapping) and "final" in new_value:
        return {"final": _override_value(new_value["final"], "")}
    if isinstance(current_value, Mapping) and "final" in current_value:
        return {"final": current_value["final"]}
    return {}


# 独立累计预算，避免与流程路由混用。
def runtime_reducer(current_value: Any, new_value: Any) -> dict[str, Any]:
    """Accumulate budget independently of routing."""
    if _is_override(new_value):
        replacement = _override_value(new_value, {})
        return dict(replacement) if isinstance(replacement, Mapping) else {}
    current = dict(current_value) if isinstance(current_value, Mapping) else {}
    incoming = dict(new_value) if isinstance(new_value, Mapping) else {}
    merged: dict[str, Any] = dict(current)
    if "budget" in incoming:
        budget = incoming["budget"]
        merged["budget"] = (
            dict(_override_value(budget, empty_budget_usage()))
            if _is_override(budget)
            else merge_budget_usage(current.get("budget", {}), budget)
        )
    return merged


class AgentInputState(TypedDict):
    """Serializable message input; normalization is owned by python_state."""

    messages: list[BaseMessage]


# 组合原生消息及流程、角色、报告、运行统计四个状态域。
class DeepResearchState(AgentInputState):
    """Business state: four owned domains plus messages."""

    workflow: Annotated[WorkflowState, workflow_reducer]
    agents: Annotated[dict[str, AgentRoleState], agents_reducer]
    report: Annotated[ReportState, report_reducer]
    runtime: Annotated[RuntimeState, runtime_reducer]


__all__ = [
    "AgentInputState",
    "AgentRoleState",
    "BusinessAgentRole",
    "ClarifyWithUser",
    "DeepResearchState",
    "ReportState",
    "ResearchQuestion",
    "ResearchReview",
    "ResearchRoleResult",
    "ResearchTask",
    "RuntimeState",
    "Summary",
    "WorkflowState",
    "agents_reducer",
    "report_reducer",
    "research_task_identity",
    "research_tasks_reducer",
    "runtime_reducer",
    "workflow_reducer",
]
