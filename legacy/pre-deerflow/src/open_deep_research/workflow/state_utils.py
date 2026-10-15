# 为五个状态域提供只读取值、任务转换及跨角色上下文组装工具。
"""Shared state access and coercion helpers for workflow nodes.

One authoritative implementation for the state-shape helpers that were
previously duplicated between the main researcher module and the business-agent
runtime.  These are intentionally plain functions: no manager/service layer.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig

from open_deep_research.state import (
    ResearchReview,
    ResearchTask,
    research_task_identity,
)

__all__ = [
    "agents_for_new_run",
    "agents_state",
    "business_context",
    "coerce_research_review",
    "coerce_research_task",
    "coerce_research_tasks",
    "is_followup",
    "report_state",
    "research_state",
    "research_task_identity",
    "research_task_payload",
    "resolve_research_run_id",
    "role_context",
    "role_memories",
    "role_reports",
    "runtime_state",
    "tool_name",
    "workflow_state",
]


# 复制并返回流程状态域，供节点安全构造更新。
def workflow_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the workflow domain of the graph state."""
    return dict(state.get("workflow", {}) or {})


# 复制并返回按角色划分的智能体状态域。
def agents_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the per-role agent domain of the graph state."""
    return dict(state.get("agents", {}) or {})


# 复制并返回研究运行引用和工作上下文状态。
def research_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the research domain of the graph state."""
    return dict(state.get("research", {}) or {})


# 复制并返回章节与最终报告状态域。
def report_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the report domain of the graph state."""
    return dict(state.get("report", {}) or {})


# 复制并返回预算及指标状态域。
def runtime_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the runtime accounting domain of the graph state."""
    return dict(state.get("runtime", {}) or {})


# 提取各角色正式报告，跳过没有报告的角色。
def role_reports(state: Mapping[str, Any]) -> dict[str, str]:
    """Map each role to its formal report, skipping roles without one."""
    return {
        str(role): str(value.get("report") or "")
        for role, value in agents_state(state).items()
        if isinstance(value, Mapping) and value.get("report")
    }


# 提取按角色隔离的私有记忆列表。
def role_memories(state: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Map each role to its private memory list."""
    return {
        str(role): list(value.get("memory", []) or [])
        for role, value in agents_state(state).items()
        if isinstance(value, Mapping)
    }


# 保留私有记忆并清空上次运行的报告和滚动摘要。
def agents_for_new_run(state: Mapping[str, Any]) -> dict[str, Any]:
    """Carry private memory forward while clearing run-scoped reports/summaries."""
    return {
        role: {"memory": list(value.get("memory", []) or [])}
        for role, value in agents_state(state).items()
        if isinstance(value, Mapping) and value.get("memory")
    }


# 判断当前是否进入首轮之后的补充研究。
def is_followup(state: Mapping[str, Any]) -> bool:
    """Return whether the workflow is in a follow-up research round."""
    return int(workflow_state(state).get("round", 1) or 1) > 1


# 从配置组装品牌、业务等舆情场景上下文。
def business_context(configurable: Any) -> str:
    """Build business context for business-scenario prompts."""
    context = (configurable.organization_context or "").strip()
    if context:
        return context
    return (
        "No additional organization context was configured. Use the user's request, "
        "local RAG evidence, and cited public sources without inventing company facts."
    )


# 兼容 LangChain 工具与供应商工具字典，提取稳定名称。
def tool_name(available_tool: Any) -> str:
    """Return a stable name for LangChain tools and provider-native tool dicts."""
    if isinstance(available_tool, dict):
        return str(available_tool.get("name") or "web_search")
    return str(getattr(available_tool, "name", ""))


# 将模型或序列化值转成单个合法研究任务。
def coerce_research_task(value: Any) -> ResearchTask | None:
    """Normalize a task from a Pydantic or serialized LangGraph state value."""
    if isinstance(value, ResearchTask):
        return value
    if isinstance(value, dict):
        try:
            return ResearchTask.model_validate(value)
        except Exception:
            return None
    return None


# 转换列表式任务输入，并跳过格式错误的元素。
def coerce_research_tasks(value: Any) -> list[ResearchTask]:
    """Normalize a list-like task channel and ignore malformed task values."""
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    return [
        task
        for item in values
        if (task := coerce_research_task(item)) is not None
    ]


# 将模型或序列化字典转换为合法复核结果。
def coerce_research_review(value: Any) -> ResearchReview | None:
    """Normalize a review from a Pydantic or serialized LangGraph state value."""
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


# 把任务序列化为可发送给动态 Send 分支的字典。
def research_task_payload(task: ResearchTask) -> dict[str, Any]:
    """Serialize a task for a dynamic Send payload."""
    return task.model_dump()


# 解析稳定的研究运行范围，避免使用凭据或完整历史作为标识。
def resolve_research_run_id(
    state: Mapping[str, Any] | dict[str, Any],
    config: RunnableConfig | None = None,
) -> str:
    """Resolve one stable run scope without exposing credentials or raw history."""
    state_run_id = str(research_state(state).get("run_id") or "").strip()
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
    # A new root invocation without a LangGraph thread id still gets a unique
    # scope.  research_phase writes it into state before parallel Sends begin.
    return f"run_{uuid.uuid4().hex}"


# 按指定角色筛选并格式化上游正式报告。
def role_context(
    role_reports: dict[str, str],
    roles: tuple[str, ...] | None = None,
) -> str:
    """Format upstream role reports for downstream public-opinion agents."""
    if not role_reports:
        return "No upstream role reports are available yet."

    role_names = roles if roles is not None else tuple(role_reports)
    formatted_reports = [
        f"## {role}\n{report}"
        for role in role_names
        for report in [role_reports.get(role, "")]
        if report
    ]
    return "\n\n".join(formatted_reports) or "No upstream role reports are available yet."
