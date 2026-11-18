# 检查共享状态读取、角色数据清理和任务身份规则的一致性。
"""Regression guards for shared workflow state helpers."""

from open_deep_research.state import ResearchTask, research_tasks_reducer
from open_deep_research.workflow.state_utils import (
    coerce_research_tasks,
    is_followup,
    role_context,
    role_reports,
)


# 创建用于状态辅助函数测试的标准任务。
def _task(task_id: str = "task-1") -> ResearchTask:
    return ResearchTask(
        task_id=task_id,
        objective="Verify the claim",
        target_role="public_signal",
        evidence_needed="Two cited sources",
        reason="Evidence gap",
    )


# 验证角色辅助函数正确读取正式报告。
def test_shared_role_helpers_preserve_reports() -> None:
    state = {
        "agents": {
            "public_signal": {"report": "Public report"},
            "internal_knowledge": {"report": ""},
        }
    }

    assert role_reports(state) == {"public_signal": "Public report"}
    assert role_context(role_reports(state)) == "## public_signal\nPublic report"


# 验证补充轮次判断稳定。
def test_followup_detection_is_stable() -> None:
    assert is_followup({"workflow": {"round": 1}}) is False
    assert is_followup({"workflow": {"round": 2}}) is True


# 验证任务转换与 reducer 使用一致的去重身份。
def test_task_coercion_and_reducer_share_the_same_identity_rule() -> None:
    first = _task("")
    duplicate = first.model_copy(update={"reason": "Another explanation"})
    distinct = first.model_copy(update={"evidence_needed": "One official source"})

    coerced = coerce_research_tasks([first.model_dump(), "invalid", duplicate])
    assert coerced == [first, duplicate]
    assert research_tasks_reducer([first], [duplicate, distinct]) == [first, distinct]
