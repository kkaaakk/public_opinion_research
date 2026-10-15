# 覆盖真实尝试、结构化重试、嵌套调用、上下文压缩及并发令牌统计的边界。
"""Token accounting paths: structured raw usage, retries, context pressure, nesting.

Test doubles are real LangChain ``BaseChatModel`` subclasses so the official
lifecycle (``bind_tools``, ``include_raw`` envelopes, ``on_chat_model_start``,
``with_retry``) is exercised instead of faked metadata.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

import open_deep_research.workflow.shared as workflow_shared
from open_deep_research.budget import (
    ainvoke_model_with_budget,
    budget_from_model_accounting,
    estimate_context_tokens,
    model_accounting_config,
    start_budget_capture,
    stop_budget_capture,
    structured_output_chain,
)
from open_deep_research.configuration import Configuration
from open_deep_research.research_graph.context_manager import ContextManager
from open_deep_research.research_graph.extractor import GraphExtractor
from open_deep_research.research_graph.metrics import ResearchGraphMetrics
from open_deep_research.research_graph.models import (
    ResearchGraphScope,
    WorkingContext,
)
from open_deep_research.runtime.agent_runtime import AgentRuntime
from open_deep_research.search.tavily import summarize_webpage

LARGE_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
SMALL_USAGE = {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}

_SCRIPT: list = []


# 按输入步骤建立确定性的模型响应脚本。
def _script(*steps: Any) -> None:
    _SCRIPT[:] = list(steps)


# 按脚本产生正常、工具或异常响应的聊天模型替身。
class ScriptedChat(BaseChatModel):
    """Real LangChain ChatModel returning queued scripted results."""

    # 返回脚本模型的测试类型标识。
    @property
    def _llm_type(self) -> str:
        return "scripted"

    # 兼容工具绑定，使脚本模型可用于真实运行时接口。
    def bind_tools(self, tools, **kwargs):
        return self

    # 执行下一条脚本步骤，生成响应或抛出预设异常。
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if not _SCRIPT:
            raise AssertionError("scripted model has no queued response")
        return _SCRIPT.pop(0)()


# 构造带指定用量的标准聊天模型结果。
def _chat_result(message: AIMessage) -> ChatResult:
    return ChatResult(generations=[ChatGeneration(message=message)])


# 构造一次返回工具调用的脚本步骤。
def _tool_step(name: str, args: dict, usage: dict | None = None):
    message = AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": "call-1"}],
        response_metadata={"model_name": "scripted"},
        usage_metadata=usage or LARGE_USAGE,
    )
    return lambda: _chat_result(message)


# 构造一次返回普通文本的脚本步骤。
def _text_step(content: str = "done", usage: dict | None = None):
    message = AIMessage(
        content=content,
        response_metadata={"model_name": "scripted"},
        usage_metadata=usage or LARGE_USAGE,
    )
    return lambda: _chat_result(message)


# 构造一次抛出指定异常的脚本步骤。
def _error_step(error: Exception):
    # 在模型生成阶段抛出预设异常。
    def step():
        raise error

    return step


# 创建并填充官方用量回调处理器。
def _usage_handler(usage: dict | None = None) -> SimpleNamespace:
    """Stand-in for the public ``.usage_metadata`` of the official usage handler."""
    return SimpleNamespace(usage_metadata=({"scripted": usage} if usage else {}))


# ---------------------------------------------------------------------------
# Structured output keeps raw usage
# ---------------------------------------------------------------------------


# 验证结构化解析成功时原始响应令牌仍被保留。
def test_structured_raw_usage_is_preserved_and_parsed_still_works():
    from open_deep_research.state import ClarifyWithUser

    _script(
        _tool_step(
            "ClarifyWithUser",
            {"need_clarification": False, "question": "", "verification": "ok"},
        )
    )
    chain = structured_output_chain(ScriptedChat(), ClarifyWithUser, max_attempts=2)
    handler = UsageMetadataCallbackHandler()

    envelope = asyncio.run(chain.ainvoke([HumanMessage(content="hi")], config={"callbacks": [handler]}))

    assert envelope["raw"].usage_metadata == LARGE_USAGE
    assert envelope["parsed"].verification == "ok"
    usage = budget_from_model_accounting(1, handler)
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5
    assert usage["total_tokens"] == 15


# 验证解析失败会重新调用模型，而非重复解析同一响应。
def test_parsing_error_retries_the_real_model_call():
    from open_deep_research.state import ClarifyWithUser

    _script(
        _tool_step("ClarifyWithUser", {"wrong_field": True}, usage=SMALL_USAGE),
        _tool_step(
            "ClarifyWithUser",
            {"need_clarification": False, "question": "", "verification": "second"},
            usage=LARGE_USAGE,
        ),
    )
    chain = structured_output_chain(ScriptedChat(), ClarifyWithUser, max_attempts=3)

    envelope = asyncio.run(chain.ainvoke([HumanMessage(content="hi")]))

    assert envelope["parsed"].verification == "second"
    # The retry re-ran the real model call; the successful envelope keeps its usage.
    assert envelope["raw"].usage_metadata == LARGE_USAGE


# ---------------------------------------------------------------------------
# model_calls equals real chat-model attempts
# ---------------------------------------------------------------------------


# 验证供应商请求错误前后的真实尝试均记账。
def test_model_calls_counts_real_attempts_after_provider_error():
    _script(_error_step(ValueError("transient")), _text_step())
    model = ScriptedChat().with_retry(stop_after_attempt=3)

    # 在异步测试入口执行预设调用，并收集响应或异常后的用量。
    async def exercise():
        return await ainvoke_model_with_budget(model, [HumanMessage(content="hi")])

    response, delta = asyncio.run(exercise())

    assert response.content == "done"
    assert delta["model_calls"] == 2
    assert delta["input_tokens"] == 10
    assert delta["output_tokens"] == 5


# 验证结构化解析重试增加真实调用计数。
def test_model_calls_counts_real_attempts_after_parsing_error():
    from open_deep_research.state import ClarifyWithUser

    _script(
        _tool_step("ClarifyWithUser", {"wrong_field": 1}),
        _tool_step(
            "ClarifyWithUser",
            {"need_clarification": False, "question": "", "verification": "ok"},
        ),
    )
    chain = structured_output_chain(ScriptedChat(), ClarifyWithUser, max_attempts=3)

    # 在异步测试入口执行预设调用，并收集响应或异常后的用量。
    async def exercise():
        return await ainvoke_model_with_budget(chain, [HumanMessage(content="hi")])

    _response, delta = asyncio.run(exercise())

    # Both real attempts reported usage: the failed parse is no longer discarded.
    assert delta["model_calls"] == 2
    assert delta["input_tokens"] == 20
    assert delta["output_tokens"] == 10
    assert delta["total_tokens"] == 30


# 验证重试耗尽后捕获尝试次数且不猜测缺失令牌。
def test_retry_exhaustion_records_attempts_in_capture_without_fabricating_tokens():
    """Three failing attempts: the active Budget Capture keeps 3 attempts, no tokens."""
    _script(
        _error_step(ValueError("fail 1")),
        _error_step(ValueError("fail 2")),
        _error_step(ValueError("fail 3")),
    )
    config, counter, _usage = model_accounting_config()
    model = ScriptedChat().with_retry(stop_after_attempt=3)

    # 在异步测试入口执行预设调用，并收集响应或异常后的用量。
    async def exercise() -> dict:
        token = start_budget_capture()
        try:
            with pytest.raises(ValueError):
                await ainvoke_model_with_budget(
                    model, [HumanMessage(content="hi")], config=config
                )
        finally:
            return stop_budget_capture(token)

    captured = asyncio.run(exercise())

    assert counter.starts == 3
    assert captured["model_calls"] == 3
    assert captured["input_tokens"] == 0
    assert captured["output_tokens"] == 0
    assert captured["total_tokens"] == 0


# 验证解析重试耗尽后保留全部尝试和已上报用量。
def test_parsing_retry_exhaustion_records_attempts_and_reported_usage():
    """Three parsing failures: attempts and every provider-reported usage are captured."""
    from open_deep_research.state import ClarifyWithUser

    _script(
        _tool_step("ClarifyWithUser", {"wrong_field": 1}, usage=SMALL_USAGE),
        _tool_step("ClarifyWithUser", {"wrong_field": 2}, usage=SMALL_USAGE),
        _tool_step("ClarifyWithUser", {"wrong_field": 3}, usage=SMALL_USAGE),
    )
    chain = structured_output_chain(ScriptedChat(), ClarifyWithUser, max_attempts=3)

    # 在异步测试入口执行预设调用，并收集响应或异常后的用量。
    async def exercise() -> dict:
        token = start_budget_capture()
        try:
            with pytest.raises(Exception):
                await ainvoke_model_with_budget(chain, [HumanMessage(content="hi")])
        finally:
            return stop_budget_capture(token)

    captured = asyncio.run(exercise())

    assert captured["model_calls"] == 3
    assert captured["input_tokens"] == 12
    assert captured["output_tokens"] == 6
    assert captured["total_tokens"] == 18


# 验证没有用量的失败尝试只计调用次数。
def test_failed_attempts_without_usage_do_not_fabricate_tokens():
    """A successful retry after a failure only carries provider-reported usage."""
    _script(_error_step(ValueError("transient")), _text_step(usage=SMALL_USAGE))
    model = ScriptedChat().with_retry(stop_after_attempt=3)

    # 在异步测试入口执行预设调用，并收集响应或异常后的用量。
    async def exercise():
        return await ainvoke_model_with_budget(model, [HumanMessage(content="hi")])

    _response, delta = asyncio.run(exercise())

    assert delta["model_calls"] == 2
    assert delta["input_tokens"] == 4
    assert delta["total_tokens"] == 6


# 验证供应商错误与解析错误交错时用量仍完整累计。
def test_provider_error_then_parse_error_keeps_all_reported_usage():
    """Provider failure + parsing failure + success: 3 attempts, summed reported usage."""
    from open_deep_research.state import ClarifyWithUser

    _script(
        _error_step(ValueError("transient")),
        _tool_step("ClarifyWithUser", {"wrong_field": 1}, usage=SMALL_USAGE),
        _tool_step(
            "ClarifyWithUser",
            {"need_clarification": False, "question": "", "verification": "ok"},
            usage=LARGE_USAGE,
        ),
    )
    chain = structured_output_chain(ScriptedChat(), ClarifyWithUser, max_attempts=4)

    # 在异步测试入口执行预设调用，并收集响应或异常后的用量。
    async def exercise():
        return await ainvoke_model_with_budget(chain, [HumanMessage(content="hi")])

    _response, delta = asyncio.run(exercise())

    assert delta["model_calls"] == 3
    assert delta["input_tokens"] == 14
    assert delta["output_tokens"] == 7
    assert delta["total_tokens"] == 21


# 验证 BaseException 原样传播且预算捕获上下文恢复。
def test_boundary_preserves_base_exception_and_leaves_capture_balanced():
    """A BaseException is re-raised after accounting; no capture context leaks."""

    # 模拟必须原样传播的 BaseException。
    class FatalError(BaseException):
        pass

    # 在异步调用时抛出致命测试异常。
    class FatalRunnable:
        # 在模型调用阶段抛出预设异常，验证异常传播与降级规则。
        async def ainvoke(self, payload, config=None):
            raise FatalError()

    # 在异步测试入口执行预设调用，并收集响应或异常后的用量。
    async def exercise() -> tuple[dict, dict]:
        token = start_budget_capture()
        try:
            with pytest.raises(FatalError):
                await ainvoke_model_with_budget(FatalRunnable(), [HumanMessage(content="hi")])
        finally:
            captured = stop_budget_capture(token)
        return captured, stop_budget_capture(start_budget_capture())

    captured, fresh = asyncio.run(exercise())

    assert captured["model_calls"] == 0
    assert captured["total_tokens"] == 0
    assert fresh == captured


# 验证官方用量回调与项目预算通道保持独立且一致。
def test_official_usage_callback_stays_a_separate_observability_channel():
    handler = UsageMetadataCallbackHandler()
    _script(_text_step(usage=LARGE_USAGE))

    # 在异步测试入口执行预设调用，并收集响应或异常后的用量。
    async def exercise():
        return await ainvoke_model_with_budget(
            ScriptedChat(),
            [HumanMessage(content="hi")],
            config={"callbacks": [handler]},
        )

    _response, delta = asyncio.run(exercise())

    assert delta["model_calls"] == 1
    assert delta["input_tokens"] == 10
    # The official run-level handler observes the same single call.
    assert handler.usage_metadata["scripted"]["input_tokens"] == 10


# ---------------------------------------------------------------------------
# Nested tool model calls
# ---------------------------------------------------------------------------


# 验证嵌套结构化重试记录所有已上报令牌。
def test_nested_structured_model_with_retry_counts_all_reported_usage():
    from open_deep_research.state import Summary

    _script(
        _tool_step("Summary", {"wrong_field": "x"}, usage=SMALL_USAGE),
        _tool_step(
            "Summary",
            {"summary": "short summary", "key_excerpts": "evidence"},
            usage={"input_tokens": 9, "output_tokens": 3, "total_tokens": 12},
        ),
    )
    model = structured_output_chain(ScriptedChat(), Summary, max_attempts=3)

    # 在异步测试入口执行预设调用，并收集响应或异常后的用量。
    async def exercise() -> dict:
        token = start_budget_capture()
        try:
            await summarize_webpage(model, "page text")
        finally:
            return stop_budget_capture(token)

    captured = asyncio.run(exercise())

    # 2 real attempts; both provider-reported usages are summed exactly once.
    assert captured["model_calls"] == 2
    assert captured["input_tokens"] == 13
    assert captured["output_tokens"] == 5
    assert captured["total_tokens"] == 18


# 验证嵌套模型重试耗尽后尝试次数仍进入工具捕获。
def test_nested_retry_exhaustion_keeps_attempts_in_tool_capture():
    """A nested model that exhausts retries and degrades still reports its attempts."""
    from open_deep_research.state import Summary

    _script(
        _error_step(ValueError("fail 1")),
        _error_step(ValueError("fail 2")),
    )
    model = structured_output_chain(ScriptedChat(), Summary, max_attempts=2)

    # 在异步测试入口执行预设调用，并收集响应或异常后的用量。
    async def exercise() -> tuple[str, dict]:
        token = start_budget_capture()
        try:
            # summarize_webpage catches the failure and returns the raw page text.
            result = await summarize_webpage(model, "page text")
        finally:
            return result, stop_budget_capture(token)

    result, captured = asyncio.run(exercise())

    assert result == "page text"
    assert captured["model_calls"] == 2
    assert captured["input_tokens"] == 0
    assert captured["output_tokens"] == 0
    assert captured["total_tokens"] == 0


# ---------------------------------------------------------------------------
# Research Graph structured calls keep exact usage
# ---------------------------------------------------------------------------


# 构造图用量测试的固定运行范围。
def _scope() -> ResearchGraphScope:
    return ResearchGraphScope(run_id="run-a", role="public_signal", research_round=1, task_id="t1")


# 验证图抽取器返回模型实际上报的用量。
def test_graph_extractor_reports_exact_usage():
    _script(
        _tool_step(
            "GraphExtractionOutput",
            {},
            usage={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
        )
    )
    extractor = GraphExtractor(
        model=ScriptedChat(),
        model_name="scripted",
        max_tokens=512,
        max_retries=2,
        batch_token_limit=10_000,
    )
    from open_deep_research.research_graph.models import RawResearchDocument

    documents = [
        RawResearchDocument(source_id="SRC1", content="Official notice confirms a brake investigation.")
    ]

    result = asyncio.run(extractor.extract(documents, scope=_scope()))
    batch = result.batches[0]

    assert batch.input_tokens == 120
    assert batch.output_tokens == 30
    assert batch.input_token_quality == "exact"
    assert batch.output_token_quality == "exact"
    assert batch.budget_usage["model_calls"] == 1


# 验证上下文管理器返回精确响应令牌。
def test_context_manager_reports_exact_usage():
    from open_deep_research.research_graph.models import RelevantSubgraph

    _script(
        _tool_step(
            "WorkingContextDelta",
            {"recent_progress": "updated"},
            usage={"input_tokens": 40, "output_tokens": 12, "total_tokens": 52},
        )
    )
    manager = ContextManager(model=ScriptedChat(), model_name="scripted", max_retries=2)

    result = asyncio.run(
        manager.update(
            task=SimpleNamespace(objective="assess safety"),
            current=WorkingContext(current_objective="assess safety"),
            relevant_subgraph=RelevantSubgraph(run_id="run-a"),
            research_delta=[],
        )
    )

    assert result.budget_usage["model_calls"] == 1
    assert result.budget_usage["input_tokens"] == 40
    assert result.budget_usage["output_tokens"] == 12


# 验证滚动压缩返回实际模型用量。
def test_rolling_compact_reports_exact_usage():
    from open_deep_research.research_graph.compaction import rolling_compact

    _script(
        _tool_step(
            "RollingCompactOutput",
            {"rolling_summary": "compacted history"},
            usage={"input_tokens": 70, "output_tokens": 20, "total_tokens": 90},
        )
    )
    messages = [
        HumanMessage(content="assignment"),
        AIMessage(
            content="step 0",
            tool_calls=[{"name": "web_search", "args": {}, "id": "call-0"}],
        ),
        ToolMessage(content="raw 0", name="web_search", tool_call_id="call-0"),
        AIMessage(
            content="step 1",
            tool_calls=[{"name": "web_search", "args": {}, "id": "call-1"}],
        ),
        ToolMessage(content="raw 1", name="web_search", tool_call_id="call-1"),
    ]

    result = asyncio.run(
        rolling_compact(
            messages,
            previous_summary="",
            protected_context="task",
            model=ScriptedChat(),
            model_name="scripted",
            max_retries=2,
            recent_raw_steps=1,
        )
    )

    assert result.budget_usage["model_calls"] == 1
    assert result.budget_usage["input_tokens"] == 70
    assert result.budget_usage["output_tokens"] == 20


# 验证研究复核节点保留结构化输出的原始用量。
def test_research_review_node_reports_structured_usage(monkeypatch):
    import open_deep_research.deep_researcher as module

    monkeypatch.setenv("RESEARCH_GRAPH_ENABLED", "false")
    _script(
        _tool_step(
            "ResearchReview",
            {"research_complete": True},
            usage={"input_tokens": 55, "output_tokens": 15, "total_tokens": 70},
        )
    )
    monkeypatch.setattr(workflow_shared, "configurable_model", ScriptedChat())
    state = {
        "messages": [HumanMessage(content="brand risk")],
        "workflow": {"brief": "brand risk", "round": 1},
        "agents": {
            "public_signal": {"report": "signal"},
            "internal_knowledge": {"report": "internal"},
        },
        "research": {},
        "report": {},
        "runtime": {"budget": {}, "metrics": {}},
    }
    config = {
        "configurable": {
            "research_graph_enabled": False,
            "research_model": "scripted:model",
            "max_structured_output_retries": 2,
        }
    }

    result = asyncio.run(module.research_review(state, config))

    assert result["workflow"]["review"].research_complete is True
    budget = result["runtime"]["budget"]
    assert budget["model_calls"] == 1
    assert budget["input_tokens"] == 55
    assert budget["output_tokens"] == 15


# ---------------------------------------------------------------------------
# Context pressure uses the actual request structure with approximate counting
# ---------------------------------------------------------------------------


# 提供可控制上下文与压缩结果的工作区替身。
class _CompactionWorkspace:
    """Minimal workspace whose model_factory returns a scripted compactor."""

    # 初始化工作区任务、受保护内容和调用状态。
    def __init__(self, protected: str = "") -> None:
        self.rolling_summary = ""
        self.metrics = ResearchGraphMetrics()
        self.transcript = None
        self.scope = SimpleNamespace(research_round=1)
        self.protected = protected

    # 返回压缩时必须保留的测试上下文。
    def protected_context(self) -> str:
        return self.protected

    # 返回供压缩和研究调用使用的预设模型。
    def model_factory(self, _model_name, _max_tokens):
        return ScriptedChat()

    # 构造下一次模型调用的测试上下文。
    async def before_model(self, messages):
        return list(messages)

    # 提供工具批次接收接口，返回预设工作区结果。
    async def ingest(self, messages, _batch):
        return SimpleNamespace(messages=list(messages), budget_usage={})

    # 提供角色收尾接口，返回测试最终结果。
    async def finalize(self, _messages, _expected_output):
        return None


# 装配运行时及模型、工作区替身，供上下文压力测试。
def _runtime(workspace, configurable: Configuration, *, model=None, system_prompt="system", tools=None):
    # 返回固定压缩输出，隔离报告模型调用。
    async def compress_report(_messages, _task, _contract, _budget):
        return "compressed report", [], {}

    return AgentRuntime(
        role="public_signal",
        spec=SimpleNamespace(expected_output="evidence", display_name="Public Signal"),
        agent_state={},
        workspace=workspace,
        config=configurable,
        runtime_config={},
        model=model or ScriptedChat(),
        system_prompt=system_prompt,
        tools=tools or [],
        initial_budget={},
        execute_tool=None,
        compress_report=compress_report,
    )


# 创建包含 AI 与配对工具消息的测试历史。
def _history(steps: int) -> list:
    messages: list = [HumanMessage(content="assignment")]
    for index in range(steps):
        messages.append(
            AIMessage(
                content=f"step {index}",
                tool_calls=[{"name": "web_search", "args": {}, "id": f"call-{index}"}],
            )
        )
        messages.append(
            ToolMessage(
                content="raw result " * 100,
                name="web_search",
                tool_call_id=f"call-{index}",
            )
        )
    return messages


# 构造一次滚动压缩的脚本响应。
def _compaction_step(summary: str = "compacted history"):
    return _tool_step("RollingCompactOutput", {"rolling_summary": summary})


# 验证高上下文占用会触发压缩。
def test_high_context_pressure_triggers_compaction():
    _script(_compaction_step())
    runtime = _runtime(
        _CompactionWorkspace(),
        Configuration(
            research_graph_context_capacity_tokens=300,
            context_compaction_threshold_ratio=0.75,
            context_warning_ratio=0.6,
            recent_raw_steps=1,
        ),
    )
    messages = _history(steps=4)
    model_messages = [SystemMessage(content="system"), *messages]

    compacted, budget = asyncio.run(runtime._compact_history(messages, model_messages))

    assert budget["model_calls"] == 1
    assert len(compacted) == 4
    assert compacted[0].content == "assignment"


# 验证低压力时保留原历史且不额外调用压缩模型。
def test_low_context_pressure_skips_compaction():
    _script()
    runtime = _runtime(
        _CompactionWorkspace(),
        Configuration(
            research_graph_context_capacity_tokens=1_000_000,
            context_compaction_threshold_ratio=0.75,
        ),
    )
    messages = _history(steps=1)

    compacted, budget = asyncio.run(runtime._compact_history(messages, messages))

    assert compacted == messages
    assert budget == {}


# 验证系统提示词计入实际请求压力。
def test_system_prompt_is_part_of_context_pressure():
    """Small history + huge system prompt must trigger compaction."""
    _script(_compaction_step())
    runtime = _runtime(
        _CompactionWorkspace(),
        Configuration(
            research_graph_context_capacity_tokens=500,
            context_compaction_threshold_ratio=0.75,
            context_warning_ratio=0.6,
            recent_raw_steps=1,
        ),
        system_prompt="system policy " * 2000,
    )
    messages = _history(steps=2)
    model_messages = [SystemMessage(content=runtime.system_prompt), *messages]

    compacted, budget = asyncio.run(runtime._compact_history(messages, model_messages))

    assert budget["model_calls"] == 1
    assert len(compacted) < len(messages)


# 验证工具结构占用也影响压缩决策。
def test_tools_are_part_of_context_pressure():
    # 提供占用较大参数结构的工具，检查工具定义计入上下文压力。
    @tool(description="huge schema " * 3000)
    def huge_tool(query: str) -> str:
        """A tool with a large schema."""
        return ""

    messages = _history(steps=2)
    model_messages = [SystemMessage(content="system"), *messages]
    without_tools = estimate_context_tokens(model_messages)
    with_tools = estimate_context_tokens(model_messages, tools=[huge_tool])
    assert with_tools > without_tools

    _script(_compaction_step())
    # Capacity chosen between the two estimates: only the tool schema triggers compaction.
    capacity = max(1, int(with_tools / 0.75) - 10)
    runtime = _runtime(
        _CompactionWorkspace(),
        Configuration(
            research_graph_context_capacity_tokens=capacity,
            context_compaction_threshold_ratio=0.75,
            context_warning_ratio=0.6,
            recent_raw_steps=1,
        ),
        tools=[huge_tool],
    )

    compacted, budget = asyncio.run(runtime._compact_history(messages, model_messages))

    assert budget["model_calls"] == 1
    assert len(compacted) < len(messages)


# 验证估算与真实请求使用同一消息结构。
def test_pressure_estimation_uses_the_same_message_structure_as_the_call():
    _script(_compaction_step())
    capacity = 500
    runtime = _runtime(
        _CompactionWorkspace(),
        Configuration(
            research_graph_context_capacity_tokens=capacity,
            context_compaction_threshold_ratio=0.75,
            context_warning_ratio=0.6,
            recent_raw_steps=1,
        ),
        system_prompt="system policy " * 2000,
    )
    messages = _history(steps=2)
    model_messages = [SystemMessage(content=runtime.system_prompt), *messages]

    asyncio.run(runtime._compact_history(messages, model_messages))

    # The estimate the runtime used is exactly the official counter over model_messages.
    assert estimate_context_tokens(model_messages) >= capacity * 0.75


# 验证运行时在发送模型请求前完成上下文检查。
def test_agent_runtime_checks_context_before_invoking_the_model():
    events: list[str] = []
    _script(_text_step("done"))

    # 记录模型真实执行顺序，检查压力评估先于请求。
    class _RecordingModel(ScriptedChat):
        # 记录模型执行事件后委托原脚本生成器。
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            events.append("model")
            return super()._generate(messages, stop, run_manager, **kwargs)

    workspace = _CompactionWorkspace()

    # 记录请求构建步骤并保留原消息列表。
    async def _before_model(messages):
        events.append("build_request")
        return list(messages)

    workspace.before_model = _before_model
    runtime = _runtime(
        workspace,
        Configuration(research_graph_context_capacity_tokens=10_000),
        model=_RecordingModel(),
    )

    original_compact = runtime._compact_history

    # 记录上下文压力检查顺序后委托原压缩实现。
    async def _record_compact(messages, model_messages):
        events.append("pressure_check")
        return await original_compact(messages, model_messages)

    runtime._compact_history = _record_compact

    asyncio.run(runtime.run("collect evidence"))

    # Pressure is estimated on the actual request structure, before the model is invoked.
    assert events.index("build_request") < events.index("pressure_check")
    assert events.index("pressure_check") < events.index("model")


# ---------------------------------------------------------------------------
# Error routing: token-limit recovery vs ordinary exception propagation
# ---------------------------------------------------------------------------


# 创建供应商特定上下文超限异常。
def _token_limit_error() -> Exception:
    error_type = type("BadRequestError", (Exception,), {})
    error_type.__module__ = "openai.error"
    return error_type("This model's maximum context length is 8192 tokens.")


# 构造最终报告写作的测试状态。
def _writer_state() -> dict:
    from open_deep_research.state import Section

    return {
        "report": {
            "sections": [
                Section(
                    name="Risk",
                    description="Risk evidence",
                    research=True,
                    agent_role="risk_assessment",
                )
            ],
            "completed_sections": [],
        },
        "agents": {"risk_assessment": {"report": "risk evidence"}},
        "runtime": {"budget": {}},
    }


# 构造报告模型及预算测试配置。
def _writer_config() -> dict:
    return {
        "configurable": {
            "section_writer_model": "fixture:model",
            "research_graph_enabled": False,
        }
    }


# 验证普通异常不会进入上下文超限恢复路径。
def test_ordinary_exception_is_not_routed_to_token_limit_recovery(monkeypatch):
    """Regression guard for historical ``is_token_limit_exceeded(...) or True`` logic."""
    import open_deep_research.deep_researcher as module

    monkeypatch.setenv("RESEARCH_GRAPH_ENABLED", "false")

    # 模拟普通供应商故障，检查不会误入令牌超限恢复。
    class _FailingModel:
        # 接收运行配置并返回测试模型，兼容真实调用链。
        def with_config(self, _config):
            return self

        # 在模型调用阶段抛出预设异常，验证异常传播与降级规则。
        async def ainvoke(self, _messages, config=None):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(workflow_shared, "configurable_model", _FailingModel())

    with pytest.raises(RuntimeError, match="provider unavailable"):
        asyncio.run(module.section_writer(_writer_state(), _writer_config()))


# 验证已识别超限错误仍触发上下文恢复。
def test_token_limit_exception_still_enters_context_recovery(monkeypatch):
    import open_deep_research.deep_researcher as module

    monkeypatch.setenv("RESEARCH_GRAPH_ENABLED", "false")
    _script(_error_step(_token_limit_error()))
    monkeypatch.setattr(workflow_shared, "configurable_model", ScriptedChat())

    result = asyncio.run(module.section_writer(_writer_state(), _writer_config()))

    budget = result["runtime"]["budget"]
    # The failed real attempt is still budgeted, with no fabricated tokens.
    assert budget["model_calls"] == 1
    assert budget["total_tokens"] == 0
    assert (
        "Section 'Risk' was not written because the model context limit was reached."
        in budget["degradation_reasons"]
    )


# 验证超限识别依赖供应商错误特征，避免误判。
def test_token_limit_detection_is_specific_to_provider_errors():
    from open_deep_research.llm.errors import is_token_limit_exceeded

    assert is_token_limit_exceeded(RuntimeError("provider unavailable"), "deepseek:deepseek-chat") is False
    assert is_token_limit_exceeded(_token_limit_error(), "openai:gpt-4.1") is True


# ---------------------------------------------------------------------------
# Final report input shaping
# ---------------------------------------------------------------------------


# 验证兜底报告按令牌预算裁剪证据。
def test_fallback_final_report_truncates_findings_by_token_budget(monkeypatch):
    import open_deep_research.deep_researcher as module

    captured: dict = {}

    # 记录最终报告提示，返回固定报告内容。
    class _FinalReportModel:
        # 接收运行配置并返回测试模型，兼容真实调用链。
        def with_config(self, _config):
            return self

        # 记录模型实际输入，并返回预设章节或报告消息。
        async def ainvoke(self, messages, config=None):
            captured["prompt"] = messages[0].content
            return AIMessage(content="final report body")

    monkeypatch.setattr(workflow_shared, "configurable_model", _FinalReportModel())
    monkeypatch.setenv("RESEARCH_GRAPH_ENABLED", "false")
    huge_findings = "evidence line with facts, sources and citations. " * 400
    state = {
        "messages": [HumanMessage(content="brand risk question")],
        "agents": {"public_signal": {"report": huge_findings}},
        "workflow": {"brief": "brand risk brief"},
        "runtime": {"budget": {}},
    }
    config = {
        "configurable": {
            "budget_enabled": True,
            "max_input_tokens": 400,
            "final_report_model_max_tokens": 100,
            "research_graph_enabled": False,
        }
    }

    result = asyncio.run(module._fallback_report_generation(state, config))

    assert "final report body" in result["report"]["final"]
    assert "Truncated research findings to fit the remaining input token budget." in (
        result["report"]["final"]
    )
    assert len(captured["prompt"]) < len(huge_findings)


# 验证最终报告超限恢复保留失败与成功请求的尝试次数。
def test_final_report_context_recovery_counts_failed_and_successful_requests(monkeypatch):
    """A context-limit request plus the successful retry are both budgeted."""
    import open_deep_research.deep_researcher as module

    monkeypatch.setenv("RESEARCH_GRAPH_ENABLED", "false")
    _script(
        _error_step(_token_limit_error()),
        _text_step("recovered report", usage=SMALL_USAGE),
    )
    monkeypatch.setattr(workflow_shared, "configurable_model", ScriptedChat())
    state = {
        "messages": [HumanMessage(content="brand risk question")],
        "agents": {"public_signal": {"report": "signal evidence"}},
        "workflow": {"brief": "brand risk brief"},
        "runtime": {"budget": {}},
    }
    config = {
        "configurable": {
            "final_report_model": "deepseek:deepseek-chat",
            "final_report_model_max_tokens": 1000,
            "research_graph_enabled": False,
        }
    }

    result = asyncio.run(module._fallback_report_generation(state, config))

    assert "recovered report" in result["report"]["final"]
    budget = result["runtime"]["budget"]
    # First request hit the context limit, second succeeded: both are real requests.
    assert budget["model_calls"] == 2
    assert budget["input_tokens"] == 4
    assert budget["total_tokens"] == 6


# ---------------------------------------------------------------------------
# Reducer stays delta-only
# ---------------------------------------------------------------------------


# 验证并行 reducer 只累加新消耗增量。
def test_parallel_reducer_remains_delta_only():
    from open_deep_research.state import runtime_reducer

    # 按给定次数和令牌构造单份预算增量。
    def delta(calls: int, tokens: int) -> dict:
        payload = budget_from_model_accounting(
            calls,
            _usage_handler(
                {
                    "input_tokens": tokens,
                    "output_tokens": 0,
                    "total_tokens": tokens,
                }
            ),
        )
        return payload

    state: dict = {}
    for value in (delta(2, 10), delta(1, 5), delta(1, 7)):
        state = runtime_reducer(state, {"budget": value})

    assert state["budget"]["model_calls"] == 4
    assert state["budget"]["input_tokens"] == 22


# 验证并发调用的捕获独立，汇总时每份增量只处理一次。
def test_parallel_invocations_keep_usage_isolated_and_reduce_once():
    """Each invocation gets a fresh official handler; the reducer sums deltas once."""
    from open_deep_research.state import runtime_reducer

    # 返回指定用量的固定模型响应，检查并发捕获隔离。
    class FixedChat(BaseChatModel):
        usage: dict

        # 返回测试模型类型，兼容 LangChain 模型接口。
        @property
        def _llm_type(self) -> str:
            return "fixed"

        # 生成携带实例指定官方令牌用量的固定响应。
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            message = AIMessage(
                content="done",
                response_metadata={"model_name": "fixed"},
                usage_metadata=self.usage,
            )
            return _chat_result(message)

    # 在独立捕获上下文执行模型，退出时返回该调用的实际消耗。
    async def invoke(usage: dict) -> dict:
        token = start_budget_capture()
        try:
            _response, _delta = await ainvoke_model_with_budget(
                FixedChat(usage=usage), [HumanMessage(content="hi")]
            )
        finally:
            return stop_budget_capture(token)

    # 并发执行两份不同用量的模型调用，返回各自隔离的捕获结果。
    async def exercise() -> list[dict]:
        return await asyncio.gather(
            invoke({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
            invoke({"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}),
        )

    first, second = asyncio.run(exercise())

    assert first["input_tokens"] == 10
    assert first["total_tokens"] == 15
    assert second["input_tokens"] == 20
    assert second["total_tokens"] == 30

    state: dict = {}
    for delta in (first, second):
        state = runtime_reducer(state, {"budget": delta})

    assert state["budget"]["model_calls"] == 2
    assert state["budget"]["input_tokens"] == 30
    assert state["budget"]["total_tokens"] == 45
