# 统一统计模型、工具、检索和令牌消耗，并在预算不足时控制降级与上下文裁剪。
"""Budget policy and enforcement for bounded deep research runs.

Model requests use SDK Usage and share the same physical admission and receipt
policy. This module also owns model/tool/search limits, token budgets, warnings,
degradation reasons, and per-stage budget deltas for the State reducers.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Callable, Iterator, Mapping, cast

from dbos import DBOS
from langchain_core.messages import HumanMessage
from langchain_core.messages.ai import UsageMetadata, add_usage
from langchain_core.messages.utils import count_tokens_approximately
from sqlalchemy import text

from open_deep_research.configuration import Configuration
from open_deep_research.llm.errors import ModelInvocationError

LOGGER = logging.getLogger(__name__)

BUDGET_COUNTER_FIELDS = ("model_calls", "tool_calls", "search_calls")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
TOKEN_DETAIL_FIELDS = ("input_token_details", "output_token_details")
BUDGET_LIST_FIELDS = ("budget_warnings", "degradation_reasons")
SEARCH_TOOL_NAMES = {"web_search", "rag_search"}
MIN_MODEL_CALLS_PER_RESEARCH_UNIT = 2

# Per-tool-call attribution for model calls executed *inside* a tool (e.g. per-URL
# webpage summarization in ``tavily_search``).  Model attempts and provider tokens
# are recorded here while a capture is active, so the enclosing node can merge one
# delta for the tool and the Budget Guard can use it mid-run.
_BUDGET_CAPTURE: ContextVar[dict[str, Any] | None] = ContextVar(
    "open_deep_research_budget_capture",
    default=None,
)
_MODEL_ADMISSION: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "research_model_admission", default=None
)


class ModelBudgetExceeded(RuntimeError):
    """Reject a nested model attempt before its provider request starts."""


# Cross-Agent admission uses the existing request row lock for one atomic
# check-and-insert. A blocked call closes that transaction before sleeping, so
# it holds neither a pool connection nor the row lock while another Agent's
# provider request settles. This is a conservative sequential admission policy,
# not a distributed quota reservation.
MODEL_BILL_WAIT_SECONDS = 300.0
MODEL_BILL_POLL_SECONDS = 0.5


def require_physical_model_budget(config, baseline, actual, *, reserve_report):
    """Check paid attempts, including prior failures and outstanding receipts."""
    total = merge_budget_usage(baseline, actual)
    if is_over_model_budget(config, total) or not can_spend_model_call(
        config, total, reserve_final_report_call=reserve_report
    ):
        raise ModelBudgetExceeded(
            "Physical call receipts exhausted the research budget"
        )


def model_bill(db, run_id):
    """Known physical charges, a count of open invocations, and usage completeness."""
    from open_deep_research.web.run_data import loads

    total: dict[str, Any] = {}
    pending = 0
    complete = True
    for usage in db.execute(
        text("SELECT usage FROM por_domain.model_attempts WHERE run_id=:r"),
        {"r": run_id},
    ).scalars():
        if usage is None:
            pending += 1
            continue
        value = loads(usage)
        complete &= value.get("usage_complete", True)
        total = merge_budget_usage(total, value)
    return (
        merge_budget_usage(total, {"model_calls": pending}),
        pending,
        complete and not pending,
    )


def tool_ledger(db, run_id):
    """Authoritative tool and search counts for one Run."""
    calls, searches = db.execute(
        text(
            "SELECT count(*), count(*) FILTER (WHERE search) "
            "FROM por_domain.tool_calls WHERE run_id=:r"
        ),
        {"r": run_id},
    ).one()
    return {"tool_calls": int(calls), "search_calls": int(searches)}


def compose_run_usage(initial, model_charges, tool_counts, notes=None):
    """Compose public Run usage from the ledger that owns each number.

    Model calls and tokens come from ``por_domain.model_attempts``; tool and
    search counts come from ``por_domain.tool_calls``. Stage events contribute
    only business notes (warnings and degradation reasons), never counters, so
    one real call can never be counted twice.
    """
    usage = merge_budget_usage(initial, model_charges)
    usage = merge_budget_usage(usage, tool_counts)
    notes = notes or {}
    return merge_budget_usage(
        usage, {field: notes.get(field, []) for field in BUDGET_LIST_FIELDS}
    )


def start_model_attempt(
    scope, workflow_id, executor_id, call_id, *, wait_seconds=None
):
    """Admit one physical model attempt after any in-flight token bill settles.

    ``require_live_effect`` locks the request row, so the pending check and the
    receipt insert are atomic across Executors. A blocked caller exits that
    transaction before sleeping; waiting never holds a database transaction or
    connection. Cancellation, replacement Executors and approval expiry are
    re-read on every poll. A bill that never settles becomes a controlled
    ``ModelBudgetExceeded`` timeout instead of being treated as zero cost.
    """
    from open_deep_research.web.run_data import db_time
    from open_deep_research.workflow.dbos_config import domain_engine
    from open_deep_research.workflow.execution import require_live_effect, unconfirmed

    if wait_seconds is None:
        wait_seconds = MODEL_BILL_WAIT_SECONDS
    deadline = time.monotonic() + max(0.0, float(wait_seconds))

    while True:
        with domain_engine().begin() as db:
            require_live_effect(db, scope.run_id, workflow_id, executor_id)
            keys = {"r": scope.run_id, "id": call_id}
            if db.scalar(
                text(
                    "SELECT 1 FROM por_domain.model_attempts WHERE run_id=:r AND id=:id"
                ),
                keys,
            ):
                raise unconfirmed(
                    "Model attempt already exists without its native result"
                )
            if scope.expires_at is not None and db_time(db) >= scope.expires_at:
                raise ModelInvocationError("BriefApprovalExpired")
            if scope.config.budget_enabled:
                actual, pending, _complete = model_bill(db, scope.run_id)
                # With token ceilings, an in-flight bill has no provider token
                # count yet. Wait for its settlement instead of rejecting a
                # normal parallel Agent call as a budget failure. A finalized
                # attempt whose provider reported no usage can never be priced:
                # it stays counted as one call and marks the Run incomplete
                # instead of blocking every later Agent for the rest of the Run.
                token_limit = (
                    scope.config.max_input_tokens is not None
                    or scope.config.max_output_tokens is not None
                )
                if not (token_limit and pending):
                    require_physical_model_budget(
                        scope.config,
                        scope.initial_usage,
                        actual,
                        reserve_report=scope.reserve_report,
                    )
                    db.execute(
                        text(
                            "INSERT INTO por_domain.model_attempts(run_id,id,stage_id) "
                            "VALUES (:r,:id,:stage)"
                        ),
                        dict(keys, stage=scope.stage_id),
                    )
                    return
            else:
                db.execute(
                    text(
                        "INSERT INTO por_domain.model_attempts(run_id,id,stage_id) "
                        "VALUES (:r,:id,:stage)"
                    ),
                    dict(keys, stage=scope.stage_id),
                )
                return

        # The transaction above is closed before sleeping. A blocked caller
        # holds neither a pool connection nor the request row lock. Check the
        # deadline first, so an explicit zero-wait admission fails immediately.
        if time.monotonic() >= deadline:
            raise ModelBudgetExceeded(
                "Model admission timed out waiting for an unsettled token bill"
            )
        time.sleep(MODEL_BILL_POLL_SECONDS)


def finish_model_attempt(run_id, call_id, usage):
    from open_deep_research.web.run_data import dumps
    from open_deep_research.workflow.dbos_config import domain_engine
    from open_deep_research.workflow.execution import unconfirmed

    with domain_engine().begin() as db:
        if (
            db.execute(
                text("""
            UPDATE por_domain.model_attempts SET usage=:usage
            WHERE run_id=:r AND id=:id AND (usage IS NULL OR usage=:usage)
            """),
                {"r": run_id, "id": call_id, "usage": dumps(usage)},
            ).rowcount
            != 1
        ):
            raise unconfirmed("Conflicting model usage receipt")


async def begin_model_call():
    from open_deep_research.workflow.execution import (
        ExecutionControlError,
        owned_cleanup,
        owned_io,
        stage_context,
    )

    scope = stage_context.get()
    if scope is None:
        return None
    if owned_cleanup.get():
        raise ExecutionControlError("No new inference during cancellation cleanup")
    call_id = scope.call_id("model")
    _, cancellation = await owned_io(
        start_model_attempt, scope, DBOS.workflow_id, DBOS.executor_id, call_id
    )
    if cancellation is not None:
        await end_model_call(call_id, {})
        raise cancellation
    return call_id


async def end_model_call(call_id, usage):
    from open_deep_research.workflow.execution import commit_receipt, stage_context

    scope = stage_context.get()
    if call_id is not None and scope is not None:
        await commit_receipt(finish_model_attempt, scope.run_id, call_id, usage)


def physical_model_usage(usage, *, failed=False):
    """A response without a provider count leaves the token bill incomplete."""
    value = dict(usage)
    value["model_calls"] = max(value.get("model_calls", 0), 1)
    if not value.get("total_tokens") and (
        failed or not value.get("input_tokens") and not value.get("output_tokens")
    ):
        value["usage_complete"] = False
    return value


def admit_model_call(prior_attempts: dict[str, Any]) -> None:
    """Apply the current local budget before an independent SDK request."""
    admission = _MODEL_ADMISSION.get()
    if admission is not None:
        admission(prior_attempts)


@contextmanager
def model_budget_admission(
    configurable: Configuration, usage: Callable[[], dict[str, Any]]
) -> Iterator[None]:
    """Guard nested generation with this role's own projection.

    This is a local fast check over one Role's snapshot: it reserves the final
    report call so analysis and compression cannot consume it. It never claims
    the Run-wide ceiling, which only :func:`start_model_attempt` enforces across
    parallel Agents.
    """

    baseline_capture = _BUDGET_CAPTURE.get()

    def admit(prior_attempts: dict[str, Any]) -> None:
        projected = usage()
        active_capture = _BUDGET_CAPTURE.get()
        if active_capture is not None and active_capture is not baseline_capture:
            # Tool/report subcaptures are merged into the role only on exit;
            # account their already-completed calls during admission as well.
            projected = merge_budget_usage(projected, active_capture)
        projected = merge_budget_usage(projected, prior_attempts)
        if is_over_model_budget(configurable, projected) or not can_spend_model_call(
            configurable, projected, reserve_final_report_call=True
        ):
            raise ModelBudgetExceeded(
                "Nested model budget exhausted; final report reserve retained."
            )

    token = _MODEL_ADMISSION.set(admit)
    try:
        yield
    finally:
        _MODEL_ADMISSION.reset(token)


# 创建各计数器为零的预算记录，作为累加和状态初始化的起点。
def empty_budget_usage() -> dict[str, Any]:
    """Return an empty budget usage payload."""
    usage: dict[str, Any] = {
        field: 0 for field in (*BUDGET_COUNTER_FIELDS, *TOKEN_FIELDS)
    }
    usage["budget_warnings"] = []
    usage["degradation_reasons"] = []
    return usage


# 将缺失或不完整的消耗数据补齐为统一结构，兼容旧版预算字段。
def normalize_budget_usage(value: Any) -> dict[str, Any]:
    """Normalize partial or absent budget usage into the canonical dict shape."""
    usage = empty_budget_usage()
    if value is None:
        return usage
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, Mapping):
        return usage

    for field in (*BUDGET_COUNTER_FIELDS, *TOKEN_FIELDS):
        try:
            usage[field] = int(value.get(field, 0) or 0)
        except (TypeError, ValueError):
            usage[field] = 0

    for field in TOKEN_DETAIL_FIELDS:
        details = value.get(field) or {}
        if isinstance(details, Mapping) and details:
            usage[field] = {
                str(key): int(item)
                for key, item in details.items()
                if isinstance(item, int) and not isinstance(item, bool)
            }

    for field in BUDGET_LIST_FIELDS:
        items = value.get(field, []) or []
        if isinstance(items, str):
            items = [items]
        usage[field] = [str(item) for item in items if str(item)]

    return usage


# 从预算字典提取 LangChain 用量字段，供官方令牌聚合逻辑处理。
def _usage_metadata_from(usage: Mapping[str, Any]) -> UsageMetadata:
    """Project a normalized budget payload onto LangChain ``UsageMetadata``."""
    metadata: dict[str, Any] = {
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "total_tokens": usage.get("total_tokens", 0) or 0,
    }
    for field in TOKEN_DETAIL_FIELDS:
        details = usage.get(field) or {}
        if isinstance(details, Mapping) and details:
            metadata[field] = dict(details)
    return cast(UsageMetadata, metadata)


# 把聚合后的令牌元数据写回传入的预算字典，包含缓存和推理明细。
def _apply_usage_metadata(
    usage: dict[str, Any], metadata: UsageMetadata | None
) -> None:
    """Write a ``UsageMetadata`` payload back into the flat budget fields."""
    if not metadata:
        return
    for field in TOKEN_FIELDS:
        value = metadata.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            usage[field] = value
    for field in TOKEN_DETAIL_FIELDS:
        details = metadata.get(field) or {}
        if isinstance(details, Mapping) and details:
            usage[field] = dict(details)


# 合并两份消耗：调用次数累加，令牌使用官方聚合函数处理。
def merge_budget_usage(current_value: Any, new_value: Any = None) -> dict[str, Any]:
    """Merge two budget usage payloads: counters sum, tokens go through ``add_usage``."""
    merged = normalize_budget_usage(current_value)
    incoming = normalize_budget_usage(new_value)

    for field in BUDGET_COUNTER_FIELDS:
        merged[field] += incoming[field]

    combined = add_usage(_usage_metadata_from(merged), _usage_metadata_from(incoming))
    _apply_usage_metadata(merged, cast(UsageMetadata, combined))

    for field in BUDGET_LIST_FIELDS:
        seen = set(merged[field])
        for item in incoming[field]:
            if item not in seen:
                merged[field].append(item)
                seen.add(item)

    return merged


# 计算累计消耗相对基线的非负增量，避免重复记账。
def diff_budget_usage(total_value: Any, baseline_value: Any) -> dict[str, Any]:
    """Return the non-negative usage delta between a total payload and baseline."""
    total = normalize_budget_usage(total_value)
    baseline = normalize_budget_usage(baseline_value)
    diff = empty_budget_usage()

    for field in (*BUDGET_COUNTER_FIELDS, *TOKEN_FIELDS):
        diff[field] = max(0, total[field] - baseline[field])

    for field in TOKEN_DETAIL_FIELDS:
        base_details = baseline.get(field) or {}
        total_details = total.get(field) or {}
        diff[field] = {
            str(key): max(0, value - int(base_details.get(key, 0) or 0))
            for key, value in total_details.items()
        }

    for field in BUDGET_LIST_FIELDS:
        baseline_items = set(baseline[field])
        diff[field] = [item for item in total[field] if item not in baseline_items]

    return diff


# 在当前异步上下文开启嵌套用量捕获，返回用于恢复上下文的令牌。
def start_budget_capture() -> Token:
    """Start capturing nested budget usage in the current async context."""
    return _BUDGET_CAPTURE.set(empty_budget_usage())


# 取出本次捕获的消耗，并用令牌恢复之前的上下文。
def stop_budget_capture(token: Token) -> dict[str, Any]:
    """Stop nested budget capture and return the captured usage."""
    captured_usage = normalize_budget_usage(_BUDGET_CAPTURE.get())
    _BUDGET_CAPTURE.reset(token)
    return captured_usage


# 以上下文管理器捕获内部调用消耗；退出时始终恢复原来的上下文变量。
@contextmanager
def budget_capture() -> Iterator[dict[str, Any]]:
    """Capture nested usage and always restore the previous ContextVar value.

    The yielded dictionary is updated in place by :func:`capture_budget_usage`,
    so callers can consume the final delta after the ``with`` block.  The low-level
    start/stop API remains available for compatibility.
    """
    token = start_budget_capture()
    captured = _BUDGET_CAPTURE.get()
    assert captured is not None
    try:
        yield captured
    finally:
        stop_budget_capture(token)


# 向当前捕获上下文累计用量；未开启捕获时直接忽略。
def capture_budget_usage(usage: Any) -> None:
    """Record nested budget usage when a capture context is active."""
    captured_usage = _BUDGET_CAPTURE.get()
    if captured_usage is None:
        return
    merged_usage = merge_budget_usage(captured_usage, usage)
    captured_usage.clear()
    captured_usage.update(merged_usage)


# 创建仅包含降级原因的消耗记录，供后续汇总到报告。
def budget_usage_with_reason(reason: str) -> dict[str, Any]:
    """Create a budget payload that only records a degradation reason."""
    usage = empty_budget_usage()
    usage["degradation_reasons"] = [reason]
    return usage


# 创建仅包含预算警告的记录，不增加实际调用和令牌计数。
def budget_usage_with_warning(warning: str) -> dict[str, Any]:
    """Create a budget payload that only records a warning."""
    usage = empty_budget_usage()
    usage["budget_warnings"] = [warning]
    return usage


def budget_from_native_search() -> dict[str, Any]:
    """Create a budget payload for provider-side native web search."""
    usage = empty_budget_usage()
    usage["tool_calls"] = 1
    usage["search_calls"] = 1
    return usage


def budget_from_sdk_usage(sdk_usage: Any) -> dict[str, Any]:
    """Project SDK token counters for one response or provider error.

    The caller supplies the attempt count. Physical receipts mark missing
    provider usage incomplete with `physical_model_usage`.
    """
    usage = empty_budget_usage()
    for field in TOKEN_FIELDS:
        usage[field] = int(getattr(sdk_usage, field, 0) or 0)
    for field, names in (
        ("input", {"cache_read": "cached_tokens", "cache_creation": "cache_write_tokens"}),
        ("output", {"reasoning": "reasoning_tokens"}),
    ):
        details = getattr(sdk_usage, f"{field}_tokens_details", None)
        reported = {
            label: int(count)
            for label, name in names.items()
            if (count := getattr(details, name, 0))
        }
        if reported:
            usage[f"{field}_token_details"] = reported
    return usage


# 根据实际执行的工具列表统计工具和搜索调用增量。
def budget_from_tool_calls(
    tool_calls: list[dict[str, Any]],
    tools_by_name: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a budget payload for executed tool calls."""
    usage = empty_budget_usage()
    usage["tool_calls"] = len(tool_calls)
    usage["search_calls"] = sum(
        1 for tool_call in tool_calls if is_search_tool_call(tool_call, tools_by_name)
    )
    return usage


# 结合工具名、定义和元数据判断调用是否属于搜索或检索。
def is_search_tool_call(
    tool_call: dict[str, Any],
    tools_by_name: dict[str, Any] | None = None,
) -> bool:
    """Return whether a tool call is a retrieval/search call."""
    name = tool_call.get("name")
    if name in SEARCH_TOOL_NAMES:
        return True

    if not tools_by_name or name not in tools_by_name:
        return False

    tool = tools_by_name[name]
    if isinstance(tool, dict):
        return (
            tool.get("type") in {"search", "web_search_20250305", "web_search_preview"}
            or tool.get("name") in SEARCH_TOOL_NAMES
        )

    metadata = getattr(tool, "metadata", None) or {}
    return metadata.get("type") == "search" or metadata.get("name") in SEARCH_TOOL_NAMES


# 返回剩余模型调用次数，按需为最终报告预留一次；None 表示不限次数。
def remaining_model_calls(
    configurable: Configuration,
    usage: Any,
    *,
    reserve_final_report_call: bool = False,
) -> int | None:
    """Return the remaining model-call allowance, or None when unlimited."""
    limit = configurable.max_model_calls
    if not configurable.budget_enabled or limit is None:
        return None

    reserve = (
        1 if reserve_final_report_call and configurable.reserve_final_report_call else 0
    )
    return max(0, limit - reserve - normalize_budget_usage(usage)["model_calls"])


# 判断剩余预算能否再调用模型，可额外为最终报告预留一次调用。
def can_spend_model_call(
    configurable: Configuration,
    usage: Any,
    *,
    reserve_final_report_call: bool = False,
) -> bool:
    """Return whether another model call can be made."""
    remaining = remaining_model_calls(
        configurable, usage, reserve_final_report_call=reserve_final_report_call
    )
    return remaining is None or remaining > 0


# 检查新增工具及搜索消耗是否会突破配置上限。
def can_spend_tool_call(
    configurable: Configuration,
    usage: Any,
    increment: dict[str, Any],
) -> bool:
    """Return whether the tool/search budget can absorb the increment."""
    if not configurable.budget_enabled:
        return True

    current = normalize_budget_usage(usage)
    incoming = normalize_budget_usage(increment)
    if (
        configurable.max_tool_calls is not None
        and current["tool_calls"] + incoming["tool_calls"] > configurable.max_tool_calls
    ):
        return False
    if (
        configurable.max_search_calls is not None
        and current["search_calls"] + incoming["search_calls"]
        > configurable.max_search_calls
    ):
        return False
    return True


# 按当前预算拆分可执行与需跳过的工具调用，逐项预占后续消耗。
# 返回 (allowed, skipped)；调用方仍需为 skipped 生成匹配原 tool_call_id 的工具消息。
def filter_tool_calls_for_budget(
    configurable: Configuration,
    usage: Any,
    tool_calls: list[dict[str, Any]],
    tools_by_name: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split tool calls into executable and skipped calls under the configured budget."""
    if not configurable.budget_enabled:
        return tool_calls, []

    allowed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    projected_usage = normalize_budget_usage(usage)

    for tool_call in tool_calls:
        increment = budget_from_tool_calls([tool_call], tools_by_name)
        if can_spend_tool_call(configurable, projected_usage, increment):
            allowed.append(tool_call)
            projected_usage = merge_budget_usage(projected_usage, increment)
        else:
            skipped.append(tool_call)

    return allowed, skipped


# 返回还可启动的研究单元数量；未限制时返回 None。
def available_research_unit_slots(
    configurable: Configuration, usage: Any
) -> int | None:
    """Return how many new researcher subgraphs can start, or None when unlimited."""
    if not configurable.budget_enabled or configurable.max_model_calls is None:
        return None

    current = normalize_budget_usage(usage)["model_calls"]
    reserve = 1 if configurable.reserve_final_report_call else 0
    remaining = max(0, configurable.max_model_calls - reserve - current)
    return remaining // MIN_MODEL_CALLS_PER_RESEARCH_UNIT


# 模型准入只受模型调用与令牌上限约束，工具次数耗尽不得阻断模型请求。
def is_over_model_budget(configurable: Configuration, usage: Any) -> bool:
    """Return whether a model limit is exhausted; tool/search quotas are separate."""
    if not configurable.budget_enabled:
        return False
    normalized = normalize_budget_usage(usage)
    limits = {
        "model_calls": configurable.max_model_calls,
        "input_tokens": configurable.max_input_tokens,
        "output_tokens": configurable.max_output_tokens,
    }
    return any(
        limit is not None and normalized[field] >= limit
        for field, limit in limits.items()
    )


# 检查任一受限预算是否已超额，用于终止或降级研究。
def is_over_budget(configurable: Configuration, usage: Any) -> bool:
    """Return whether any configured budget has already been exceeded."""
    if not configurable.budget_enabled:
        return False
    normalized = normalize_budget_usage(usage)
    limits = {
        "model_calls": configurable.max_model_calls,
        "tool_calls": configurable.max_tool_calls,
        "search_calls": configurable.max_search_calls,
        "input_tokens": configurable.max_input_tokens,
        "output_tokens": configurable.max_output_tokens,
    }
    return any(
        limit is not None and normalized[field] >= limit
        for field, limit in limits.items()
    )


# 列出达到预警比例的预算项，提示接近上限的资源消耗。
def get_budget_warnings(configurable: Configuration, usage: Any) -> list[str]:
    """Return warning strings for budgets at or above the configured warning ratio."""
    if not configurable.budget_enabled:
        return []

    normalized = normalize_budget_usage(usage)
    warning_ratio = configurable.budget_warning_ratio
    limits = {
        "model_calls": configurable.max_model_calls,
        "tool_calls": configurable.max_tool_calls,
        "search_calls": configurable.max_search_calls,
        "input_tokens": configurable.max_input_tokens,
        "output_tokens": configurable.max_output_tokens,
    }
    warnings = []
    for field, limit in limits.items():
        if limit is None or limit <= 0:
            continue
        if normalized[field] / limit >= warning_ratio:
            warnings.append(f"{field} is at {normalized[field]}/{limit}.")
    return warnings


# 估算真实消息和绑定工具结构的输入令牌量，用于请求前的上下文检查。
def estimate_context_tokens(
    messages: Any,
    *,
    tools: list[Any] | None = None,
) -> int:
    """Estimate the tokens the next model request will carry.

    Delegates to LangChain's ``count_tokens_approximately`` over the real message
    objects (never ``str(messages)``) and includes bound tool schemas when the
    model call sends them.  Tools that cannot be converted to a provider schema
    (for example a custom non-BaseTool wrapper) are ignored rather than failing
    the run.
    """
    if not tools:
        return count_tokens_approximately(messages)
    try:
        return count_tokens_approximately(messages, tools=tools)
    except Exception:  # noqa: BLE001 - a tool schema must never break the estimate
        LOGGER.debug(
            "Tool schema is not convertible for token estimation.", exc_info=True
        )
        return count_tokens_approximately(messages)


# 通过 LangChain 近似计数器估算单段纯文本的令牌数。
def estimate_text_tokens(text: str) -> int:
    """Estimate tokens for one plain text block via LangChain's approximate counter."""
    if not text:
        return 0
    return count_tokens_approximately([HumanMessage(content=text)])


# 计算预估输入相对模型窗口的占用比例。
def context_pressure_ratio(estimated_tokens: int, context_window: int | None) -> float:
    """Return how full the next request is relative to the model context window."""
    if not context_window or context_window <= 0 or estimated_tokens <= 0:
        return 0.0
    return estimated_tokens / context_window


# 按近似令牌计数裁剪文本，返回裁剪后的内容及是否发生截断。
def truncate_text_to_token_budget(text: str, token_budget: int) -> tuple[str, bool]:
    """Approximate-truncate text to fit within a token budget.

    The token count comes from LangChain's ``count_tokens_approximately``; the
    character cut is derived from that count instead of a fixed char-per-token
    ratio.
    """
    if token_budget <= 0:
        return "", bool(text)
    total_tokens = estimate_text_tokens(text)
    if total_tokens <= token_budget:
        return text, False
    keep_chars = max(1, int(len(text) * token_budget / total_tokens))
    candidate = text[:keep_chars]
    while keep_chars > 1 and estimate_text_tokens(candidate) > token_budget:
        keep_chars = max(1, keep_chars - max(1, keep_chars // 10))
        candidate = text[:keep_chars]
    return candidate.rstrip(), True


# 读取可用输入令牌余额；未设置上限时返回 None。
def remaining_input_tokens(configurable: Configuration, usage: Any) -> int | None:
    """Return remaining input tokens, if the user configured that budget."""
    if not configurable.budget_enabled or configurable.max_input_tokens is None:
        return None
    return configurable.max_input_tokens - normalize_budget_usage(usage)["input_tokens"]


# 读取可用输出令牌余额；未设置上限时返回 None。
def remaining_output_tokens(configurable: Configuration, usage: Any) -> int | None:
    """Return remaining output tokens, if the user configured that budget."""
    if not configurable.budget_enabled or configurable.max_output_tokens is None:
        return None
    return (
        configurable.max_output_tokens - normalize_budget_usage(usage)["output_tokens"]
    )


# 将各项用量、上限、预警和降级原因整理为报告中的预算摘要。
def format_budget_summary(configurable: Configuration, usage: Any) -> str:
    """Format a concise budget summary for final reports."""
    if not configurable.budget_enabled:
        return ""

    normalized = normalize_budget_usage(usage)
    lines = [
        "### Budget usage",
        (
            f"- Model calls: {normalized['model_calls']}"
            + _format_limit(configurable.max_model_calls)
        ),
        (
            f"- Tool calls: {normalized['tool_calls']}"
            + _format_limit(configurable.max_tool_calls)
        ),
        (
            f"- Search calls: {normalized['search_calls']}"
            + _format_limit(configurable.max_search_calls)
        ),
        (
            f"- Input tokens: {normalized['input_tokens']}"
            + _format_limit(configurable.max_input_tokens)
        ),
        (
            f"- Output tokens: {normalized['output_tokens']}"
            + _format_limit(configurable.max_output_tokens)
        ),
    ]

    lines.extend(_format_token_details(normalized))

    warnings = normalized["budget_warnings"] + get_budget_warnings(
        configurable, normalized
    )
    reasons = normalized["degradation_reasons"]
    if warnings:
        lines.append("- Warnings: " + "; ".join(dict.fromkeys(warnings)))
    if reasons:
        lines.append("- Degradation: " + "; ".join(dict.fromkeys(reasons)))

    return "\n".join(lines)


# 仅在启用预算管理时，为报告追加资源消耗说明。
def append_budget_summary(report: str, configurable: Configuration, usage: Any) -> str:
    """Append budget usage details to a final report when budgeting is enabled."""
    summary = format_budget_summary(configurable, usage)
    if not summary:
        return report
    return f"{report.rstrip()}\n\n---\n\n{summary}"


# 仅展示供应商实际提供的缓存与推理令牌明细。
def _format_token_details(normalized: Mapping[str, Any]) -> list[str]:
    """Render cache/reasoning details only when the provider actually reported them."""
    input_details = normalized.get("input_token_details") or {}
    output_details = normalized.get("output_token_details") or {}
    lines = []

    cache_read = input_details.get("cache_read")
    cache_creation = input_details.get("cache_creation")
    if cache_read or cache_creation:
        lines.append(
            "- Cache tokens (read/creation): "
            f"{int(cache_read or 0)} / {int(cache_creation or 0)}"
        )

    reasoning = output_details.get("reasoning")
    if reasoning:
        lines.append(f"- Reasoning tokens: {int(reasoning)}")

    return lines


# 把预算上限转换为可读文本，区分有限值和未限制状态。
def _format_limit(limit: int | None) -> str:
    if limit is None:
        return ""
    return f" / {limit}"
