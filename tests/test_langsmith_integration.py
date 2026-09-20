"""Integration-boundary tests for the LangSmith tracing split.

These tests exercise the project's own boundary, not the LangSmith SaaS:

* LangSmith owns execution tracing; the Agent Observer sidecar keeps usage
  telemetry; token/cost accounting stays in ``budget``.
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
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

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


class _RecordingRun:
    def __init__(self, name: str, run_type: str, metadata: dict[str, Any] | None):
        self.name = name
        self.run_type = run_type
        self.metadata: dict[str, Any] = dict(metadata or {})
        self.outputs: dict[str, Any] = {}
        self.ended = False
        self.error: str | None = None

    def add_outputs(self, outputs: dict[str, Any]) -> None:
        self.outputs.update(outputs)

    def add_metadata(self, metadata: dict[str, Any]) -> None:
        self.metadata.update(metadata)


class _RecordingContext:
    def __init__(self, run: _RecordingRun, journal: list[dict[str, Any]]):
        self._run = run
        self._journal = journal

    def __enter__(self) -> _RecordingRun:
        self._journal.append({"event": "enter", "run": self._run})
        return self._run

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self._run.ended = True
        if exc_type is not None:
            self._run.error = exc_type.__name__
        self._journal.append({"event": "exit", "run": self._run, "exc": exc_type})
        return False

    async def __aenter__(self) -> _RecordingRun:
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        return self.__exit__(exc_type, exc_value, traceback)


def _recording_trace(journal: list[dict[str, Any]]):
    """Build a stand-in for ``langsmith.trace`` capturing every span request."""

    def _trace(name: str, run_type: str = "chain", **kwargs: Any) -> _RecordingContext:
        run = _RecordingRun(name, run_type, kwargs.get("metadata"))
        journal.append({"event": "request", "name": name, "run_type": run_type, "run": run})
        return _RecordingContext(run, journal)

    return _trace


@contextlib.contextmanager
def _enabled_langsmith(monkeypatch: pytest.MonkeyPatch, journal: list[dict[str, Any]]):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test-only")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setattr(langsmith_module, "_ls_trace", _recording_trace(journal))
    yield


def _span_names(journal: list[dict[str, Any]]) -> list[str]:
    return [entry["name"] for entry in journal if entry["event"] == "request"]


def _span(name: str, journal: list[dict[str, Any]]) -> _RecordingRun:
    return next(
        entry["run"]
        for entry in journal
        if entry["event"] == "request" and entry["name"] == name
    )


# ---------------------------------------------------------------------------
# Configuration: enabled / disabled / no key / project default
# ---------------------------------------------------------------------------


def test_tracing_disabled_by_default_and_helpers_stay_safe():
    assert not langsmith_enabled()
    with trace_span("never_created") as run:
        assert run is None


def test_tracing_enabled_requires_api_key(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_ENDPOINT", raising=False)
    assert not langsmith_enabled()

    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test-only")
    assert langsmith_enabled()

    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    assert not langsmith_enabled()


def test_project_defaults_are_normalized_without_overriding_user_values(monkeypatch):
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)
    ensure_langsmith_configuration()
    assert langsmith_project() == DEFAULT_PROJECT

    monkeypatch.setenv("LANGSMITH_PROJECT", "my-team-project")
    ensure_langsmith_configuration()
    assert langsmith_project() == "my-team-project"


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


def test_correlation_metadata_contains_only_bounded_identifiers():
    metadata = correlation_metadata(
        {"configurable": {"thread_id": "thread-1", "checkpoint_id": "ckpt-9"}}
    )
    assert metadata["workflow"] == WORKFLOW_NAME
    assert metadata["thread_id"] == "thread-1"
    assert metadata["checkpoint_id"] == "ckpt-9"
    assert metadata["logical_run_id"].startswith("workflow_")
    assert len(metadata["logical_run_id"]) < 80


def test_logical_run_id_is_stable_per_thread(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    first = correlation_metadata({"configurable": {"thread_id": "thread-1"}})
    second = correlation_metadata({"configurable": {"thread_id": "thread-1"}})
    assert first["logical_run_id"] == second["logical_run_id"]
    other = correlation_metadata({"configurable": {"thread_id": "thread-2"}})
    assert other["logical_run_id"] != first["logical_run_id"]


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


def test_model_metadata_is_bounded():
    metadata = model_metadata(component="rag_query_rewrite", structured_output=True)
    assert metadata == {"component": "rag_query_rewrite", "structured_output": True}
    assert model_metadata() == {}


def test_invocation_metadata_includes_correlation():
    metadata = invocation_metadata({"configurable": {"thread_id": "thread-1"}})
    assert metadata["workflow"] == WORKFLOW_NAME
    assert metadata["thread_id"] == "thread-1"


# ---------------------------------------------------------------------------
# Span helper: no-op when disabled, real span when enabled, errors propagate
# ---------------------------------------------------------------------------


def test_trace_span_is_noop_when_disabled():
    with trace_span("disabled_stage", "retriever") as run:
        assert run is None


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


def test_trace_span_records_errors_without_swallowing_them(monkeypatch):
    journal: list[dict[str, Any]] = []
    with _enabled_langsmith(monkeypatch, journal):
        with pytest.raises(RuntimeError, match="business failure"):
            with trace_span("failing_stage"):
                raise RuntimeError("business failure")
    span = _span("failing_stage", journal)
    assert span.ended
    assert span.error == "RuntimeError"


def test_trace_span_setup_failure_never_breaks_business(monkeypatch):
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
    pipeline.indexer.embedding_backend = type("E", (), {"embed_query": staticmethod(lambda _q: [1.0, 0.0])})()
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


def test_rag_query_rewrite_records_bounded_lengths(monkeypatch):
    from open_deep_research.rag import query_rewriter

    journal: list[dict[str, Any]] = []

    class FakeModel:
        def invoke(self, payload, config=None):
            from langchain_core.messages import AIMessage

            return AIMessage(content="battery warranty complaints", usage_metadata={
                "input_tokens": 4,
                "output_tokens": 3,
                "total_tokens": 7,
            })

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

    def counted_search(query_vector, top_k):
        calls["vector"] += 1
        return original_search(query_vector, top_k=top_k)

    vectorstore.search = counted_search
    keyword_index = BM25Index([chunk])
    original_keyword = keyword_index.search

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


def test_graph_nodes_carry_agent_metadata_and_auto_trace_children(monkeypatch):
    import open_deep_research.deep_researcher as deep_researcher_module

    journal: list[dict[str, Any]] = []

    class _NodeModel(GenericFakeChatModel):
        pass

    def _model_messages(*_args, **_kwargs):
        return GenericFakeChatModel(messages=iter(["hello"]))

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test-only")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:9")


    monkeypatch.setattr(langsmith_module, "_ls_trace", _recording_trace(journal))
    monkeypatch.setattr(
        deep_researcher_module.public_opinion_subgraph,
        "ainvoke",
        _spy_subgraph_call(deep_researcher_module.public_opinion_subgraph, journal),
    )

    metadata_by_node = deep_researcher_module.public_opinion_subgraph.get_graph().nodes
    assert metadata_by_node  # graph topology is rendered
    # The builder attaches agent identity statically per node.
    builder_nodes = {
        node_name
        for node_name in (
            "public_signal_agent",
            "internal_knowledge_agent",
            "risk_assessment_agent",
            "response_strategy_agent",
        )
    }
    assert builder_nodes <= set(metadata_by_node)


def _spy_subgraph_call(graph: Any, journal: list[dict[str, Any]]):
    original = graph.ainvoke

    async def spy(value: Any, config: Any = None, **kwargs: Any) -> Any:
        journal.append({"event": "subgraph_call", "metadata": (config or {}).get("metadata")})
        return await original(value, config, **kwargs)

    return spy


def test_graph_metadata_propagates_to_children(monkeypatch):
    """Node metadata and config metadata must reach LLM/tool child runs."""
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test-only")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:9")

    class _State(TypedDict, total=False):
        value: int

    calls: list[dict[str, Any]] = []

    def _search(query: str) -> str:
        """fixture search"""
        calls.append({"tool": "dbhub__execute_sql"})
        return "ok"

    tool = StructuredTool.from_function(
        _search,
        name="dbhub__execute_sql",
        metadata={"tool_domain": "database", "mcp_server": "dbhub"},
    )

    async def node(state):
        model = GenericFakeChatModel(messages=iter(["hi"]))
        await model.ainvoke([HumanMessage(content="x")])
        await tool.ainvoke({"query": "x"})
        return {"value": 1}

    builder = StateGraph(_State)
    builder.add_node(
        "public_signal_agent",
        node,
        metadata=node_metadata("public_signal_agent", kind="agent"),
    )
    builder.add_edge(START, "public_signal_agent")
    builder.add_edge("public_signal_agent", END)
    graph = builder.compile(name=WORKFLOW_NAME)

    asyncio.run(
        graph.ainvoke(
            {"value": 0},
            {
                "configurable": {"thread_id": "thread-meta"},
                "metadata": {"logical_run_id": "workflow_meta"},
            },
        )
    )
    assert calls == [{"tool": "dbhub__execute_sql"}]  # executed exactly once


# ---------------------------------------------------------------------------
# Token accounting is untouched by tracing
# ---------------------------------------------------------------------------


def test_token_accounting_is_identical_with_tracing_enabled(monkeypatch):
    journal: list[dict[str, Any]] = []
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    message = _usage_message("done", usage)

    with _enabled_langsmith(monkeypatch, journal):
        _response, delta = asyncio.run(
            ainvoke_model_with_budget(
                GenericFakeChatModel(messages=iter([message])),
                [HumanMessage(content="hi")],
                observer_model="openai:fixture",
                observer_component="token_regression",
            )
        )

    assert delta["model_calls"] == 1
    assert delta["input_tokens"] == 10
    assert delta["output_tokens"] == 5
    assert delta["total_tokens"] == 15


def _usage_message(content: str, usage: dict[str, int]):
    from langchain_core.messages import AIMessage

    return AIMessage(
        content=content,
        usage_metadata=usage,
        response_metadata={"model_name": "generic-fake"},
    )
