# 统一统计模型、工具、检索和令牌消耗，并在预算不足时控制降级与上下文裁剪。
"""Budget policy and enforcement for bounded deep research runs.

LangChain services use ``AIMessage.usage_metadata`` and ``on_chat_model_start``;
SDK business roles use official response Usage and RunHooks model starts. Token
aggregation goes through LangChain's ``add_usage``; this module only adds the
project-owned policy layer: model/tool/search limits, token budgets, warnings,
degradation reasons, and per-node budget deltas for the LangGraph reducers.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Callable, Iterator, Mapping, cast

from langchain_core.callbacks import BaseCallbackHandler, UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.messages.ai import UsageMetadata, add_usage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.runnables import RunnableLambda
from langchain_core.runnables.config import ensure_config, merge_configs

from open_deep_research.configuration import Configuration

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
# delta for the tool.  LangChain's UsageMetadataCallbackHandler cannot replace this:
# it aggregates a whole run per model name and cannot attribute a delta to one tool
# result, which is exactly what the Budget Guard needs mid-run.
_BUDGET_CAPTURE: ContextVar[dict[str, Any] | None] = ContextVar(
    "open_deep_research_budget_capture",
    default=None,
)
_MODEL_ADMISSION: ContextVar[Callable[[int], None] | None] = ContextVar(
    "research_model_admission", default=None
)


class ModelBudgetExceeded(RuntimeError):
    """Reject a nested model attempt before its provider request starts."""


@contextmanager
def model_budget_admission(
    configurable: Configuration, usage: Callable[[], dict[str, Any]]
) -> Iterator[None]:
    """Apply existing policy to nested graph, summary and report model attempts.

    Pending attempts belong to one accounting invocation (including its retry
    chain). This is a role-local guard, not a distributed quota reservation.
    """

    baseline_capture = _BUDGET_CAPTURE.get()

    def admit(pending_attempts: int) -> None:
        projected = usage()
        active_capture = _BUDGET_CAPTURE.get()
        if active_capture is not None and active_capture is not baseline_capture:
            # Tool/report subcaptures are merged into the role only on exit;
            # account their already-completed calls during admission as well.
            projected = merge_budget_usage(projected, active_capture)
        projected = merge_budget_usage(projected, {"model_calls": pending_attempts})
        if is_over_budget(configurable, projected) or not can_spend_model_call(
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


# 通过 LangChain 模型启动回调统计真实尝试次数，自动涵盖重试。
class ModelAttemptCounter(BaseCallbackHandler):
    """Count real model attempts through LangChain's official start lifecycle.

    One ``on_chat_model_start`` (or ``on_llm_start`` for non-chat LLMs) equals one
    underlying model attempt, so ``Runnable.with_retry`` attempts are counted
    naturally without re-implementing any retry logic.  A chat model emits only
    ``on_chat_model_start``, so a single attempt is never counted twice.
    """

    raise_error = True
    # Admission must run before inline model observers. This changes callback
    # dispatch only; the admission predicate and usage accounting are unchanged.
    run_inline = True

    # 初始化调用计数及回调处理所需的实例状态。
    def __init__(self) -> None:
        """Initialize the attempt counter."""
        super().__init__()
        self.starts = 0
        self.admission = _MODEL_ADMISSION.get()

    # 每次聊天模型真正开始请求时增加一次尝试计数。
    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Count one chat-model attempt."""
        if self.admission is not None:
            self.admission(self.starts)
        self.starts += 1

    # 记录非聊天 LLM 的启动次数，与聊天模型回调分别适配。
    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Count one non-chat LLM attempt (chat models use ``on_chat_model_start``)."""
        if self.admission is not None:
            self.admission(self.starts)
        self.starts += 1


class _ObservedUsageMetadata(UsageMetadataCallbackHandler):
    """Feed observed successes and partial failures to the official aggregator."""

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        first = response.generations[0] if response.generations else []
        generation = first[0] if first else None
        if isinstance(generation, ChatGeneration):
            message = generation.message
            if (
                isinstance(message, AIMessage)
                and message.usage_metadata
                and not message.response_metadata.get("model_name")
            ):
                # A missing label does not make reported usage unknown. Project
                # only for this collector; callers/other callbacks keep the raw response.
                message = message.model_copy(
                    update={
                        "response_metadata": {
                            **message.response_metadata,
                            "model_name": "_unidentified",
                        }
                    }
                )
                groups = [list(group) for group in response.generations]
                groups[0][0] = generation.model_copy(update={"message": message})
                response = response.model_copy(update={"generations": groups})
        super().on_llm_end(response, **kwargs)

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        response = kwargs.pop("response", None)
        if isinstance(response, LLMResult):
            self.on_llm_end(response, **kwargs)


# 复制调用配置并挂载尝试计数、令牌用量回调，返回配置与两个处理器。
def model_accounting_config(
    config: Any = None,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[Any, ModelAttemptCounter, UsageMetadataCallbackHandler]:
    """Return ``(config, attempt_counter, usage_handler)`` for one model invocation.

    Both handlers use LangChain's lifecycle: ``ModelAttemptCounter`` consumes the
    ``on_chat_model_start`` lifecycle and ``UsageMetadataCallbackHandler`` consumes
    ``AIMessage.usage_metadata`` from every call, including partial failures and attempts whose
    structured output fails to parse.  A fresh usage handler per invocation keeps
    parallel agents, section writers, and tools isolated from each other.

    ``metadata`` is optional LangSmith correlation metadata (component, structured
    output).  It does not affect token accounting or the provider call.
    """
    counter = ModelAttemptCounter()
    usage_handler = _ObservedUsageMetadata()
    overrides: dict[str, Any] = {"callbacks": [counter, usage_handler]}
    if metadata:
        overrides["metadata"] = dict(metadata)
    merged = merge_configs(ensure_config(config), overrides)
    return merged, counter, usage_handler


# 构造本次模型调用的有限追踪字段，标记模型、组件和结构化输出模式。
def _model_langsmith_metadata(
    model_name: str | None = None,
    *,
    component: str | None = None,
    structured_output: bool = False,
) -> dict[str, Any]:
    """Build LangSmith-only metadata for one model invocation (bounded)."""
    from open_deep_research.observability.langsmith import model_metadata

    return model_metadata(
        component=str(component) if component else None,
        structured_output=structured_output,
    )


# 将真实尝试次数与供应商上报的令牌用量转换为预算增量；缺失用量不估算。
def budget_from_model_accounting(
    attempts: int,
    usage_handler: UsageMetadataCallbackHandler,
) -> dict[str, Any]:
    """Bridge the official attempt count and usage metadata into one Budget delta.

    Token aggregation is LangChain's ``add_usage`` over the handler's reported
    ``UsageMetadata`` values; missing usage stays zero and is never estimated.
    """
    usage = empty_budget_usage()
    usage["model_calls"] = max(0, int(attempts or 0))
    combined: UsageMetadata | None = None
    for metadata in usage_handler.usage_metadata.values():
        combined = add_usage(combined, metadata)
    _apply_usage_metadata(usage, combined)
    return usage


# 异步调用模型并返回响应和消耗增量；异常退出时也保留已发生的用量。
# runnable/payload 是实际模型链和输入；config 中原有回调会与记账回调合并。
# 成功返回 (response, delta)，失败时先写入当前捕获上下文，再原样抛出异常。
async def ainvoke_model_with_budget(
    runnable: Any,
    payload: Any,
    *,
    config: Any = None,
    model_name: str | None = None,
    component: str | None = None,
    structured_output: bool = False,
) -> tuple[Any, dict[str, Any]]:
    """Invoke one model runnable and account its budget.

    Returns ``(response, budget_delta)`` where real attempts come from the official
    chat-model start lifecycle and tokens come from the official
    ``UsageMetadataCallbackHandler`` (every provider-reported ``AIMessage``, not only
    the final response).  Nested calls record the delta into the active Budget
    Capture; if the call raises after real attempts, that fact is captured before
    the original exception is re-raised.

    ``model_name``/``component``/``structured_output`` are optional LangSmith
    correlation hints; they do not affect the provider call or token accounting.
    """
    merged, counter, usage_handler = model_accounting_config(
        config,
        metadata=_model_langsmith_metadata(
            model_name,
            component=component,
            structured_output=structured_output,
        ),
    )
    try:
        response = await runnable.ainvoke(payload, config=merged)
    except BaseException:
        capture_budget_usage(
            budget_from_model_accounting(counter.starts, usage_handler)
        )
        raise
    delta = budget_from_model_accounting(counter.starts, usage_handler)
    capture_budget_usage(delta)
    return response, delta


# 同步执行模型调用，采用与异步入口一致的尝试次数和令牌记账规则。
def invoke_model_with_budget(
    runnable: Any,
    payload: Any,
    *,
    config: Any = None,
    model_name: str | None = None,
    component: str | None = None,
    structured_output: bool = False,
) -> tuple[Any, dict[str, Any]]:
    """Invoke one model synchronously and account its budget.

    Mirrors :func:`ainvoke_model_with_budget`, including attempt and usage
    accounting when the call raises.
    """
    merged, counter, usage_handler = model_accounting_config(
        config,
        metadata=_model_langsmith_metadata(
            model_name,
            component=component,
            structured_output=structured_output,
        ),
    )
    try:
        response = runnable.invoke(payload, config=merged)
    except BaseException:
        capture_budget_usage(
            budget_from_model_accounting(counter.starts, usage_handler)
        )
        raise
    delta = budget_from_model_accounting(counter.starts, usage_handler)
    capture_budget_usage(delta)
    return response, delta


# 校验包含原始响应的结构化输出；解析失败时抛错，让外层重试重新请求模型。
def require_valid_structured_output(envelope: Any) -> Any:
    """Validate a LangChain ``include_raw=True`` structured envelope.

    Raises the parsing error (or a ``ValueError`` when nothing was parsed) so that
    a surrounding ``with_retry`` re-invokes the model instead of re-checking the
    same invalid envelope.
    """
    if not isinstance(envelope, Mapping):
        raise ValueError("Structured output did not return a raw/parsed envelope.")
    parsing_error = envelope.get("parsing_error")
    if parsing_error is not None:
        raise parsing_error
    if envelope.get("parsed") is None:
        raise ValueError("Structured output did not contain a parsed result.")
    return envelope


# 组合原始响应保留、结构化校验和重试，确保重试覆盖真实模型调用。
def structured_output_chain(model: Any, schema: Any, *, max_attempts: int = 0) -> Any:
    """Build a structured-output chain that keeps raw usage and retries real calls.

    Composition: ``with_structured_output(include_raw=True)`` (official raw/parsed/
    parsing_error envelope) ``|`` validation, wrapped in ``with_retry`` so the retry
    covers both the model call and parse validation.
    """
    chain = model.with_structured_output(schema, include_raw=True) | RunnableLambda(
        require_valid_structured_output
    )
    if max_attempts and max_attempts > 0:
        chain = chain.with_retry(stop_after_attempt=max_attempts)
    return chain


# 为模型供应商内置搜索生成一次检索消耗记录。
def budget_from_native_search() -> dict[str, Any]:
    """Create a budget payload for provider-side native web search."""
    usage = empty_budget_usage()
    usage["tool_calls"] = 1
    usage["search_calls"] = 1
    return usage


def budget_from_sdk_usage(sdk_usage: Any) -> dict[str, Any]:
    """Project official SDK response tokens without recounting model starts.

    RunHooks count attempts, including failed/cancelled requests. Only completed
    response usage supplies tokens; RunResult's cumulative usage is never added
    a second time. Missing provider usage remains zero, never an estimate.
    """
    usage = empty_budget_usage()
    for field in TOKEN_FIELDS:
        usage[field] = int(getattr(sdk_usage, field, 0) or 0)
    input_details = getattr(sdk_usage, "input_tokens_details", None)
    output_details = getattr(sdk_usage, "output_tokens_details", None)
    usage["input_token_details"] = {
        "cache_read": int(getattr(input_details, "cached_tokens", 0) or 0),
        "cache_creation": int(getattr(input_details, "cache_write_tokens", 0) or 0),
    }
    usage["output_token_details"] = {
        "reasoning": int(getattr(output_details, "reasoning_tokens", 0) or 0),
    }
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
