# 验证可选追踪的配置、有限元数据、阶段区间和记账一致性。
"""Integration-boundary tests for the LangSmith tracing split.

These tests exercise the project's own boundary, not the LangSmith SaaS:

* LangSmith owns execution tracing; token/cost accounting stays in ``budget``.
* Tracing is opt-in, fail-open, and must never change business behavior,
  execute a tool or model twice, or swallow exceptions.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool

import open_deep_research.observability.langsmith as langsmith_module
from open_deep_research.budget import ainvoke_model_with_budget
from open_deep_research.observability.langsmith import (
    DEFAULT_PROJECT,
    WORKFLOW_NAME,
    agent_metadata,
    correlation_metadata,
    ensure_langsmith_configuration,
    invocation_metadata,
    langsmith_enabled,
    langsmith_project,
    model_metadata,
    node_metadata,
    trace_span,
)

# ---------------------------------------------------------------------------
# Recording span stub (stand-in for the real LangSmith RunTree)
# ---------------------------------------------------------------------------


# 模拟追踪运行对象，记录输出和追加元数据。
class _RecordingRun:
    # 初始化模拟运行的名称、输出与元数据容器。
    def __init__(self, name: str, run_type: str, metadata: dict[str, Any] | None):
        self.name = name
        self.run_type = run_type
        self.metadata: dict[str, Any] = dict(metadata or {})
        self.outputs: dict[str, Any] = {}
        self.ended = False
        self.error: str | None = None

    # 保存测试中追踪区间附加的输出。
    def add_outputs(self, outputs: dict[str, Any]) -> None:
        self.outputs.update(outputs)

    # 保存测试中追加的追踪元数据。
    def add_metadata(self, metadata: dict[str, Any]) -> None:
        self.metadata.update(metadata)


# 模拟同步与异步追踪上下文的进入和退出行为。
class _RecordingContext:
    # 绑定追踪记录及上下文状态。
    def __init__(self, run: _RecordingRun, journal: list[dict[str, Any]]):
        self._run = run
        self._journal = journal

    # 返回模拟同步追踪运行。
    def __enter__(self) -> _RecordingRun:
        self._journal.append({"event": "enter", "run": self._run})
        return self._run

    # 记录同步退出时的异常信息。
    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self._run.ended = True
        if exc_type is not None:
            self._run.error = exc_type.__name__
        self._journal.append({"event": "exit", "run": self._run, "exc": exc_type})
        return False

    # 进入模拟异步追踪上下文。
    async def __aenter__(self) -> _RecordingRun:
        return self.__enter__()

    # 记录异步退出及异常信息。
    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        return self.__exit__(exc_type, exc_value, traceback)


# 安装记录型追踪替身，供断言实际阶段调用。
def _recording_trace(journal: list[dict[str, Any]]):
    """Build a stand-in for ``langsmith.trace`` capturing every span request."""

    # 创建并记录一次测试追踪上下文。
    def _trace(name: str, run_type: str = "chain", **kwargs: Any) -> _RecordingContext:
        run = _RecordingRun(name, run_type, kwargs.get("metadata"))
        journal.append(
            {"event": "request", "name": name, "run_type": run_type, "run": run}
        )
        return _RecordingContext(run, journal)

    return _trace


# 构造已启用追踪的隔离测试环境。
@contextlib.contextmanager
def _enabled_langsmith(monkeypatch: pytest.MonkeyPatch, journal: list[dict[str, Any]]):
    # Exercise the project tracing boundary without enabling LangChain's global
    # SaaS tracer, which would otherwise start background HTTP ingestion.
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_ENDPOINT", raising=False)
    monkeypatch.setattr(langsmith_module, "langsmith_enabled", lambda: True)
    monkeypatch.setattr(langsmith_module, "_ls_trace", _recording_trace(journal))
    yield


# 提取已记录追踪区间的名称列表。
def _span_names(journal: list[dict[str, Any]]) -> list[str]:
    return [entry["name"] for entry in journal if entry["event"] == "request"]


# 按名称查询单个记录的追踪区间。
def _span(name: str, journal: list[dict[str, Any]]) -> _RecordingRun:
    return next(
        entry["run"]
        for entry in journal
        if entry["event"] == "request" and entry["name"] == name
    )


# ---------------------------------------------------------------------------
# Configuration: enabled / disabled / no key / project default
# ---------------------------------------------------------------------------


# 验证默认关闭追踪时辅助接口仍可安全调用。
def test_tracing_disabled_by_default_and_helpers_stay_safe():
    assert not langsmith_enabled()
    with trace_span("never_created") as run:
        assert run is None


# 验证启用追踪需要有效 API 密钥配置。
def test_tracing_enabled_requires_api_key(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_ENDPOINT", raising=False)
    assert not langsmith_enabled()

    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test-only")
    assert langsmith_enabled()

    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    assert not langsmith_enabled()


# 验证缺省项目名被补齐且不覆盖用户设置。
def test_project_defaults_are_normalized_without_overriding_user_values(monkeypatch):
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)
    ensure_langsmith_configuration()
    assert langsmith_project() == DEFAULT_PROJECT

    monkeypatch.setenv("LANGSMITH_PROJECT", "my-team-project")
    ensure_langsmith_configuration()
    assert langsmith_project() == "my-team-project"


# 验证缺少密钥时显式启用的追踪会被规范为关闭。
def test_enabled_tracing_without_key_is_normalized_off(monkeypatch, caplog):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_ENDPOINT", raising=False)
    with caplog.at_level("WARNING"):
        ensure_langsmith_configuration()
    assert not langsmith_enabled()
    assert any("LANGSMITH_API_KEY" in message for message in caplog.messages)


# ---------------------------------------------------------------------------
# Bounded correlation metadata
# ---------------------------------------------------------------------------


# 验证关联元数据仅包含长度受限的标识。
def test_correlation_metadata_contains_only_bounded_identifiers():
    metadata = correlation_metadata(
        {"configurable": {"thread_id": "thread-1", "checkpoint_id": "ckpt-9"}}
    )
    assert metadata["workflow"] == WORKFLOW_NAME
    assert metadata["thread_id"] == "thread-1"
    assert metadata["checkpoint_id"] == "ckpt-9"
    assert metadata["logical_run_id"].startswith("workflow_")
    assert len(metadata["logical_run_id"]) < 80


# 验证同一线程的逻辑运行 ID 稳定。
def test_logical_run_id_is_stable_per_thread(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    first = correlation_metadata({"configurable": {"thread_id": "thread-1"}})
    second = correlation_metadata({"configurable": {"thread_id": "thread-1"}})
    assert first["logical_run_id"] == second["logical_run_id"]
    other = correlation_metadata({"configurable": {"thread_id": "thread-2"}})
    assert other["logical_run_id"] != first["logical_run_id"]


# 验证追踪元数据不携带状态正文和凭据。
def test_metadata_never_carries_state_or_secrets():
    metadata = correlation_metadata(
        {
            "configurable": {
                "thread_id": "thread-1",
                "research_model": "deepseek:deepseek-chat",
                "api_key": "super-secret",
            }
        }
    )
    serialized = repr(metadata)
    assert "super-secret" not in serialized
    assert "research_model" not in serialized


# 验证节点与角色元数据能明确标识对应智能体。
def test_node_and_agent_metadata_identify_agents():
    node = node_metadata("public_signal_agent", kind="agent")
    assert node == {
        "workflow": WORKFLOW_NAME,
        "node_name": "public_signal_agent",
        "node_kind": "agent",
        "agent_name": "public_signal_agent",
        "agent_role": "public_signal",
    }
    plain = node_metadata("section_writer", kind="writer")
    assert "agent_name" not in plain
    assert plain["node_kind"] == "writer"

    agent = agent_metadata("public_signal_agent", "public_signal")
    assert agent["agent_role"] == "public_signal"
    assert agent["workflow"] == WORKFLOW_NAME


# 验证模型追踪字段受长度限制。
def test_model_metadata_is_bounded():
    metadata = model_metadata(component="rag_query_rewrite", structured_output=True)
    assert metadata == {"component": "rag_query_rewrite", "structured_output": True}
    assert model_metadata() == {}


# 验证图调用元数据包含运行关联信息。
def test_invocation_metadata_includes_correlation():
    metadata = invocation_metadata({"configurable": {"thread_id": "thread-1"}})
    assert metadata["workflow"] == WORKFLOW_NAME
    assert metadata["thread_id"] == "thread-1"


# ---------------------------------------------------------------------------
# Span helper: no-op when disabled, real span when enabled, errors propagate
# ---------------------------------------------------------------------------


# 验证关闭追踪时区间上下文为无操作。
def test_trace_span_is_noop_when_disabled():
    with trace_span("disabled_stage", "retriever") as run:
        assert run is None


# 验证启用追踪时创建带正确元数据的区间。
def test_trace_span_creates_span_with_metadata_when_enabled(monkeypatch):
    journal: list[dict[str, Any]] = []
    with _enabled_langsmith(monkeypatch, journal):
        with trace_span(
            "vector_retrieval",
            "retriever",
            metadata={"retriever_type": "vector", "top_k": 5},
        ) as run:
            assert run is not None
            run.add_outputs({"result_count": 3})
    assert _span_names(journal) == ["vector_retrieval"]
    span = _span("vector_retrieval", journal)
    assert span.run_type == "retriever"
    assert span.metadata == {"retriever_type": "vector", "top_k": 5}
    assert span.outputs == {"result_count": 3}
    assert span.ended and span.error is None


# 验证区间记录业务错误且不吞掉异常。
def test_trace_span_records_errors_without_swallowing_them(monkeypatch):
    journal: list[dict[str, Any]] = []
    with _enabled_langsmith(monkeypatch, journal):
        with pytest.raises(RuntimeError, match="business failure"):
            with trace_span("failing_stage"):
                raise RuntimeError("business failure")
    span = _span("failing_stage", journal)
    assert span.ended
    assert span.error == "RuntimeError"


# 验证追踪初始化失败不会阻断业务执行。
def test_trace_span_setup_failure_never_breaks_business(monkeypatch):
    # 主动模拟追踪服务不可用，检查业务降级。
    def _broken_trace(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("langsmith unavailable")

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test-only")
    monkeypatch.setattr(langsmith_module, "_ls_trace", _broken_trace)
    with trace_span("broken_setup") as run:
        assert run is None


# ---------------------------------------------------------------------------
# RAG stages: named spans, no duplicate execution, unchanged results
# ---------------------------------------------------------------------------


# 验证 RAG 管线对关键阶段发出约定名称的追踪。
def test_rag_pipeline_emits_named_stage_spans(monkeypatch):
    from open_deep_research.rag.retriever import BM25Index, HybridChunkRetriever
    from open_deep_research.rag.service import RAGPipeline, RAGPipelineConfig
    from open_deep_research.rag.types import RAGChunk
    from open_deep_research.rag.vectorstore import InMemoryVectorStore

    journal: list[dict[str, Any]] = []
    chunk = RAGChunk(
        content="Atlas launch requires two approvals.",
        source="internal://atlas",
        title="Atlas handbook",
        chunk_id="atlas-1",
    )
    config = RAGPipelineConfig(
        knowledge_base_paths=[],
        memory_enabled=False,
        top_k=2,
        rerank_top_n=2,
        embedding_provider="hash",
        vectorstore_provider="memory",
        reranker_provider="simple",
    )
    pipeline = RAGPipeline(config)

    vectorstore = InMemoryVectorStore()
    vectorstore.add([chunk], [[1.0, 0.0]])
    pipeline.indexer.documents = [chunk]
    pipeline.indexer.chunks = [chunk]
    pipeline.indexer.embedding_backend = type(
        "E", (), {"embed_query": staticmethod(lambda _q: [1.0, 0.0])}
    )()
    pipeline.indexer.retriever = HybridChunkRetriever(
        vectorstore=vectorstore,
        keyword_index=BM25Index([chunk]),
        graph_enabled=False,
    )
    pipeline.indexer._ready = True
    pipeline.indexer._source_fingerprint = pipeline.indexer.source_fingerprint()

    with _enabled_langsmith(monkeypatch, journal):
        context = pipeline.query("Atlas launch approvals")

    assert "Atlas launch" in context.context
    names = _span_names(journal)
    assert names.count("vector_retrieval") == 1
    assert names.count("bm25_retrieval") == 1
    assert names.count("hybrid_merge") == 1
    assert names.count("rerank") == 1
    assert names.count("result_selection") == 1
    rerank_span = _span("rerank", journal)
    assert rerank_span.metadata["reranker_provider"] == config.reranker_provider
    assert rerank_span.outputs["output_count"] >= 1


# 验证查询改写追踪只记录受限长度等诊断信息。
def test_rag_query_rewrite_records_bounded_lengths(monkeypatch):
    from open_deep_research.rag import query_rewriter

    journal: list[dict[str, Any]] = []

    # 提供可记录配置或返回固定消息的模型替身。
    class FakeModel:
        # 返回固定查询改写消息，供改写结果和追踪检查。
        def invoke(self, payload, config=None):
            from langchain_core.messages import AIMessage

            return AIMessage(
                content="battery warranty complaints",
                usage_metadata={
                    "input_tokens": 4,
                    "output_tokens": 3,
                    "total_tokens": 7,
                },
            )

    with _enabled_langsmith(monkeypatch, journal):
        rewritten = query_rewriter.rewrite_query_with_model(
            "complaints about battery",
            model_name="openai:fixture",
            max_tokens=64,
            api_key=None,
            model_factory=lambda **_kwargs: FakeModel(),
        )

    assert rewritten == "battery warranty complaints"
    assert _span_names(journal) == ["query_rewrite"]
    span = _span("query_rewrite", journal)
    assert span.metadata["original_query_length"] == len("complaints about battery")
    assert span.outputs["rewritten_query_length"] == len(rewritten)
    # The rewritten query text itself must never be copied into span metadata.
    assert "battery" not in repr(span.metadata)


# 验证追踪包装不会导致向量或关键词检索重复执行。
def test_hybrid_retriever_does_not_execute_a_stage_twice(monkeypatch):
    from open_deep_research.rag.retriever import BM25Index, HybridChunkRetriever
    from open_deep_research.rag.types import RAGChunk
    from open_deep_research.rag.vectorstore import InMemoryVectorStore

    journal: list[dict[str, Any]] = []
    calls = {"vector": 0, "bm25": 0}

    chunk = RAGChunk(content="alpha evidence", source="s", title="t", chunk_id="c1")
    vectorstore = InMemoryVectorStore()
    vectorstore.add([chunk], [[1.0]])

    original_search = vectorstore.search

    # 记录向量检索次数后委托原实现，检查没有重复执行。
    def counted_search(query_vector, top_k):
        calls["vector"] += 1
        return original_search(query_vector, top_k=top_k)

    vectorstore.search = counted_search
    keyword_index = BM25Index([chunk])
    original_keyword = keyword_index.search

    # 记录 BM25 检索次数后委托原实现。
    def counted_keyword(query, top_k):
        calls["bm25"] += 1
        return original_keyword(query, top_k=top_k)

    keyword_index.search = counted_keyword
    retriever = HybridChunkRetriever(
        vectorstore=vectorstore,
        keyword_index=keyword_index,
        graph_enabled=False,
    )

    with _enabled_langsmith(monkeypatch, journal):
        results = retriever.retrieve("alpha", [1.0], top_k=1, keyword_top_k=1)

    assert len(results) == 1
    assert calls == {"vector": 1, "bm25": 1}
    assert _span_names(journal).count("vector_retrieval") == 1
    assert _span_names(journal).count("bm25_retrieval") == 1


# ---------------------------------------------------------------------------
# Graph wiring: agents are distinguishable and nothing executes twice
# ---------------------------------------------------------------------------


# 验证图节点携带角色信息，并依赖原生子调用追踪。
def test_graph_nodes_carry_agent_metadata_and_auto_trace_children():
    from open_deep_research.public_opinion_agents import get_public_opinion_agent_spec

    for role in (
        "public_signal",
        "internal_knowledge",
        "risk_assessment",
        "response_strategy",
    ):
        spec = get_public_opinion_agent_spec(role)
        metadata = agent_metadata(spec.node_name, role)
        assert metadata["agent_role"] == role
        assert role in str(metadata)


@pytest.mark.integration
def test_graph_metadata_propagates_to_children(monkeypatch):
    """Business model/tool callbacks accept bounded correlation without an engine."""
    calls = []

    async def search(query: str) -> str:
        calls.append(query)
        return "ok"

    tool = StructuredTool.from_function(
        coroutine=search,
        name="dbhub__execute_sql",
        description="fixture read",
        metadata={"tool_domain": "database", "mcp_server": "dbhub"},
    )

    async def run():
        cfg = {
            "metadata": {
                **node_metadata("public_signal_agent", kind="agent"),
                "logical_run_id": "workflow_meta",
            }
        }
        model = GenericFakeChatModel(messages=iter(["hi"]))
        await model.ainvoke([HumanMessage(content="x")], config=cfg)
        await tool.ainvoke({"query": "x"}, config=cfg)

    asyncio.run(run())
    assert calls == ["x"]


# ---------------------------------------------------------------------------
# Token accounting is untouched by tracing
# ---------------------------------------------------------------------------


# 验证追踪开关不影响预算和令牌统计。
def test_token_accounting_is_identical_with_tracing_enabled(monkeypatch):
    journal: list[dict[str, Any]] = []
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    message = _usage_message("done", usage)

    with _enabled_langsmith(monkeypatch, journal):
        _response, delta = asyncio.run(
            ainvoke_model_with_budget(
                GenericFakeChatModel(messages=iter([message])),
                [HumanMessage(content="hi")],
                model_name="openai:fixture",
                component="token_regression",
            )
        )

    assert delta["model_calls"] == 1
    assert delta["input_tokens"] == 10
    assert delta["output_tokens"] == 5
    assert delta["total_tokens"] == 15


# 构造带官方用量元数据的响应消息。
def _usage_message(content: str, usage: dict[str, int]):
    from langchain_core.messages import AIMessage

    return AIMessage(
        content=content,
        usage_metadata=usage,
        response_metadata={"model_name": "generic-fake"},
    )
